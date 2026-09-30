"""Releases: how a lane's base branch goes out (a push, a wait for CI, a deploy), run by hansa only after the
principal says yes on the page.

The coordinator sets a lane's release command (`hansa lane set L release "..."`) and asks for a release of the
base branch as it is now with `hansa release L [note]`. hansa records the request in .hansa/releases.json, which
no agent may edit: the lane, the commit, the command line, the note, the commits that go out (reachable from the
commit, on no remote-tracking branch of the repository and not in the lane's last release that went out) and the
files they change. Before the request waits for the principal, the command line, the lines those commits add,
the names of the files they add or change, and their messages are checked against the principal's
release_forbidden words; a hit refuses it. The principal answers on the page (Release, or Refuse with a
reason); a lane on the principal's releases_without_asking list needs no answer while it is what the list
recorded when the page saved it, and while the desk is not paused. hansa serve runs an approved
release in the lane's repository, as the desk's account, outside the sandbox, with hansa's own environment (not
the lane's env) and the lane's timeout_min, and only while the base branch and the command line are still what
the principal saw: otherwise the request is void, says why, and the coordinator asks again. Its output is kept,
keys removed, in .hansa/releases/<id>.log. One release runs at a time per repository, and none while a merge
check runs there (nor a merge while a release runs).

A release's state: waiting (for the principal) -> approved -> starting -> running -> done | failed; or refused
(the principal said no), void (what the principal saw changed, or the lane is gone), forbidden (a word that
must not go out).
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .desk import Desk, listed_mismatch, now, read_json, write_json
from .run import (LOOK_LIMIT, CannotLook, alive, git, has_ended, leader, merge_target, pids_with_arg, proc_start,
                  stop)

OPEN = ("waiting", "approved", "starting", "running")
MOST_COMMITS = 200  # of the outgoing commits kept on the request (all are checked for forbidden words)
MOST_FILES = 500
MOST_HITS = 20
TAIL = 15
# $0 is the launch word, which finds the run again after hansa serve restarts; the exit code goes in a file
SCRIPT = '/bin/sh -c "$1"; echo $? > "$2"'
HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def releases_file(desk: Desk) -> Path:
    return desk.state_dir / "releases.json"


def log_dir(desk: Desk) -> Path:
    return desk.state_dir / "releases"


def releases(desk: Desk) -> list:
    rs = read_json(releases_file(desk), [])
    return [r for r in rs if isinstance(r, dict)] if isinstance(rs, list) else []


def get(desk: Desk, rid) -> dict | None:
    return next((r for r in releases(desk) if r.get("id") == rid), None)


def _write(desk: Desk, rs: list) -> None:
    desk.state_dir.mkdir(exist_ok=True)
    write_json(releases_file(desk), rs)


def change(desk: Desk, rid, decide) -> dict | None:
    """Read a release, decide and write in one step under the desk lock; decide(r) gives the fields to set, or
    None to leave it as it is (then this returns None)."""
    with desk.lock():
        rs = releases(desk)
        r = next((x for x in rs if x.get("id") == rid), None)
        fields = decide(r) if r else None
        if fields is None:
            return None
        r.update(fields)
        _write(desk, rs)
    return r


def repo_key(repo) -> str:
    return str(Path(repo).expanduser().resolve())


def lane_listed(s: dict, lane: str) -> bool:
    """Whether the principal lets this lane's releases go out without asking: the lane is on the list, and has the
    repository, base branch and release command it had when the page saved the list."""
    return any(isinstance(x, dict) and x.get("lane") == lane and not listed_mismatch(s, x)
               for x in s.get("releases_without_asking") or [])


def running_on(desk: Desk, repo) -> dict | None:
    """The release starting or running on this repository, if any: no merge goes in there meanwhile."""
    key = repo_key(repo)
    return next((r for r in releases(desk) if r.get("state") in ("starting", "running") and r.get("repo") == key), None)


def running_text(r: dict) -> str:
    return f"release #{r['id']} of lane {r.get('lane')}, which runs on that repository"


def merging_on(desk: Desk, s: dict, repo, tasks=None):
    """The task whose merge check starts or runs on this repository, if any: no release starts there meanwhile."""
    key = repo_key(repo)
    return next((m for m in (desk.tasks() if tasks is None else tasks)
                 if m["state"] == "merging" and (m.get("merge_check") or {}).get("phase") in ("starting", "running")
                 and (merge_target(s, m.get("lane")) or ("",))[0] == key), None)


# -- what goes out ---------------------------------------------------------------------------------------------
def _range(since: str, commit: str) -> list:
    """The commits that go out: reachable from the release commit, on no remote-tracking branch of the repository
    (the whole history when it has none) and not in the lane's last release."""
    return [commit, *([f"^{since}"] if since else []), "--not", "--remotes", "--"]


def _git_bytes(repo, *args) -> str:
    """git's output as text, for reading a change: no program the repository names runs (an external diff, a
    text converter), and paths are not quoted."""
    r = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=str(repo), capture_output=True,
                       stdin=subprocess.DEVNULL)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {r.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return r.stdout.decode("utf-8", "replace")


def _git_lines(repo, *args):
    """git's output as _git_bytes reads it, line by line, without holding it all: the whole history can go out."""
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(["git", "-c", "core.quotePath=false", *args], cwd=str(repo), stdout=subprocess.PIPE,
                             stderr=err, stdin=subprocess.DEVNULL)
        try:
            for raw in p.stdout:
                yield raw[:-1].decode("utf-8", "replace") if raw.endswith(b"\n") else raw.decode("utf-8", "replace")
        except GeneratorExit:  # enough was read
            p.kill()
            raise
        finally:
            p.stdout.close()
            p.wait()
        if p.returncode:
            err.seek(0)
            raise RuntimeError(f"git {' '.join(args[:2])} failed: "
                               f"{err.read().decode('utf-8', 'replace').strip()[:300]}")


def outgoing(repo, since: str, commit: str) -> tuple:
    """The commits that go out ({sha, subject}, newest first) and the files they change."""
    rng = _range(since, commit)
    rows = _git_bytes(repo, "log", "--format=%H%x1f%s", *rng)
    commits = [dict(zip(("sha", "subject"), x.split("\x1f", 1))) for x in rows.splitlines() if "\x1f" in x]
    names = _git_bytes(repo, "log", "-m", "--format=", "--name-only", "--no-renames", *rng)
    return commits, sorted({x for x in names.splitlines() if x})


def _added(lines):
    """(commit, file, line) for each line the patches of `git log -p --format=%x00%H -U0` add."""
    sha = path = None
    old = new = 0
    for line in lines:
        if old > 0 or new > 0:  # inside a hunk: counted, so an added line that reads like a header is still one
            if line.startswith("+"):
                new -= 1
                yield sha, path, line[1:]
            elif line.startswith("-"):
                old -= 1
            elif not line.startswith("\\"):
                old, new = old - 1, new - 1
            continue
        if line.startswith("\0"):
            sha, path = line[1:].strip(), None
        elif line.startswith("+++ "):
            p = line[4:].rstrip("\t")
            path = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
        else:
            m = HUNK.match(line)
            if m:
                old = int(m[1]) if m[1] is not None else 1
                new = int(m[2]) if m[2] is not None else 1


def forbidden_in(repo, since: str, commit: str, words: list, command: str = "") -> tuple:
    """Where what goes out holds one of the principal's forbidden words (any case, within one line, in text read as
    UTF-8): the release command line, and every outgoing commit's message, the names of the files it adds or
    changes (a renamed one by its new name) and the lines it adds. ([{word, commit, where, line}], each place
    once, at most MOST_HITS; how many commits were checked)."""
    words = [w for w in words if isinstance(w, str) and w.strip()]
    folded = [(w, w.casefold()) for w in words]
    rng = _range(since, commit)
    hits, seen, checked = [], set(), 0

    def look(sha, where, line):
        low = line.casefold()
        for w, f in folded:
            if f in low and (where, line) not in seen:
                seen.add((where, line))
                hits.append({"word": w, "commit": sha or "", "where": where, "line": line.strip()[:300]})
                return

    for line in command.splitlines():
        look("", "release command", line)
    sha = None
    for line in _git_lines(repo, "log", "--format=%x00%H%n%B", *rng):
        if line.startswith("\0"):
            sha, checked = line[1:].strip(), checked + 1
        elif folded:
            look(sha, "commit message", line)
    if not folded:
        return [], checked
    for line in _git_lines(repo, "log", "-m", "--format=%x00%H", "--name-only", "--no-renames", "--diff-filter=d", *rng):
        if line.startswith("\0"):
            sha = line[1:].strip()
        elif line:
            look(sha, "file name", line)
    patch = _git_lines(repo, "log", "-p", "-m", "--text", "--no-ext-diff", "--no-textconv", "--no-color",
                       "--unified=0", "--no-renames", "--format=%x00%H", *rng)
    for sha, path, line in _added(patch):
        look(sha, path or "?", line)
        if len(hits) >= MOST_HITS:
            patch.close()
            break
    return hits[:MOST_HITS], checked


def hits_text(hits: list) -> str:
    return "\n".join(f"  {h['commit'][:10]} {h['where']}: {h['line']}  (\"{h['word']}\")" for h in hits)


def named_files(command: str, files: list) -> list:
    """The changed files the command line names (./deploy.sh, scripts/release.sh, or by its name alone): what
    the release runs is in the change."""
    edge = r"""(?:^|[\s/'"=;&|()<>`])""", r"""(?=$|[\s'";&|()<>`])"""
    return [f for f in files if any(re.search(edge[0] + re.escape(x) + edge[1], command)
                                    for x in dict.fromkeys([f, os.path.basename(f)]) if x)]


def last_out(desk: Desk, lane: str, repo: str, base: str) -> dict | None:
    """The lane's last release that went out, of this repository and base branch."""
    done = [r for r in releases(desk) if r.get("lane") == lane and r.get("state") == "done"
            and r.get("repo") == repo_key(repo) and r.get("base") == base]
    return done[-1] if done else None


def _has_commit(repo, sha: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=str(repo), capture_output=True,
                          stdin=subprocess.DEVNULL).returncode == 0


# -- asking ------------------------------------------------------------------------------------------------------
def ask(desk: Desk, lane: str, note: str = "", by: str = "", session=None) -> dict:
    """Record a request for a release of the lane's base branch as it is now. Returns it; raises SystemExit, with
    nothing recorded, when it cannot be asked, and with the request recorded as forbidden on a forbidden word."""
    with desk.lock():
        s = desk.settings()
        ln = s["lanes"].get(lane)
        if not ln:
            raise SystemExit(f"hansa: there is no lane {lane}; lanes: {', '.join(sorted(s['lanes'])) or 'none'}. "
                             "Nothing was asked")
        command = ln.get("release") or ""
        if not command.strip():
            raise SystemExit(f"hansa: lane {lane} has no release command; set it first: hansa lane set {lane} release "
                             "\"<the command that sends the base branch out>\" (and tell the principal). Nothing was asked")
        repo, base = ln["repo"], ln.get("base") or "main"
        try:
            commit = git("rev-parse", "--verify", f"refs/heads/{base}^{{commit}}", cwd=repo)
        except (RuntimeError, OSError) as e:
            raise SystemExit(f"hansa: lane {lane}'s base branch {base} cannot be read in {repo} ({e}). Nothing was asked")
        rs = releases(desk)
        busy = [r for r in rs if r.get("lane") == lane and r.get("state") in ("approved", "starting", "running")]
        if busy:
            r = busy[0]
            raise SystemExit(f"hansa: release #{r['id']} of lane {lane} is already {r['state']}; one at a time. "
                             f"hansa release shows how it goes; hansa tells you when it ends. Nothing was asked")
        prev = last_out(desk, lane, repo, base)
        since = prev["commit"] if prev and _has_commit(repo, prev["commit"]) else ""
        try:
            commits, files = outgoing(repo, since, commit)
            hits, checked = forbidden_in(repo, since, commit, s.get("release_forbidden") or [], command)
        except RuntimeError as e:
            raise SystemExit(f"hansa: the commits going out cannot be read ({desk.redact(str(e))}). Nothing was asked")
        listed = lane_listed(s, lane)
        r = {"id": max((x.get("id", 0) for x in rs), default=0) + 1, "lane": lane, "repo": repo_key(repo),
             "base": base, "commit": commit, "command": command, "note": desk.redact(note.strip()), "asked": now(),
             "by": by, "since": since, "since_release": prev["id"] if prev and since else None,
             "count": len(commits), "checked": checked, "commits": commits[:MOST_COMMITS], "files": files[:MOST_FILES],
             "files_count": len(files), "named": named_files(command, files[:MOST_FILES]),
             "state": "forbidden" if hits else "approved" if listed else "waiting",
             **({"hits": hits} if hits else {}), **({"without_asking": True, "answered": now()} if listed and not hits
                                                   else {}), **({"session": session} if session else {})}
        replaced = []
        if not hits:  # a newer request replaces one still waiting for the principal
            for x in rs:
                if x.get("lane") == lane and x.get("state") == "waiting":
                    x.update(state="void", void=f"replaced by release #{r['id']}", void_zh=f"被发布 #{r['id']} 取代",
                             ended=now())
                    replaced.append(x["id"])
        rs.append(r)
        _write(desk, rs)
    desk.event("release_asked", id=r["id"], lane=lane, commit=commit, state=r["state"], by=by,
               **({"replaced": replaced} if replaced else {}))
    for n in replaced:
        desk.event("release_void", id=n, lane=lane, why=f"replaced by release #{r['id']}", quiet=True)
    if hits:
        desk.event("release_forbidden", id=r["id"], lane=lane, commit=commit, found=len(hits), quiet=True)
    return r


# -- the principal's answer ----------------------------------------------------------------------------------------
def stale(desk: Desk, s: dict, r: dict, zh: bool = False) -> str:
    """Why a release is no longer what the principal saw (the lane gone, its release command or base branch
    changed, the base branch moved); '' when it still is. zh: in Chinese, for the page."""
    ln = s["lanes"].get(r.get("lane"))
    if not ln:
        return f"工作线 {r.get('lane')} 已不在桌面上" if zh else f"lane {r.get('lane')} is no longer on the desk"
    if (ln.get("release") or "") != r.get("command"):
        now_as, then = ln.get("release") or ("（无）" if zh else "(none)"), r.get("command")
        return (f"工作线 {r['lane']} 的发布命令在请求之后改了：现在是 {now_as!r}，不是 {then!r}" if zh else
                f"lane {r['lane']}'s release command changed since it was asked: it is now {now_as!r}, not {then!r}")
    if repo_key(ln["repo"]) != r.get("repo") or (ln.get("base") or "main") != r.get("base"):
        return (f"工作线 {r['lane']} 的仓库或基础分支在请求之后改了" if zh else
                f"lane {r['lane']}'s repository or base branch changed since it was asked")
    try:
        tip = git("rev-parse", "--verify", f"refs/heads/{r['base']}^{{commit}}", cwd=r["repo"])
    except (RuntimeError, OSError) as e:
        return desk.redact(f"{r['base']} 在 {r['repo']} 里读不出来（{e}）" if zh else
                           f"{r['base']} cannot be read in {r['repo']} ({e})")[:400]
    if tip != r.get("commit"):
        return (f"{r['base']} 在请求之后动了：现在在 {tip[:10]}，不是 {r['commit'][:10]}" if zh else
                f"{r['base']} moved since it was asked: it is at {tip[:10]} now, not {r['commit'][:10]}")
    return ""


def void(desk: Desk, rid, why: str, states=("waiting", "approved"), why_zh: str = "") -> dict | None:
    r = change(desk, rid, lambda x: {"state": "void", "void": why, **({"void_zh": why_zh} if why_zh else {}),
                                     "ended": now()} if x.get("state") in states else None)
    if r:
        desk.event("release_void", id=rid, lane=r.get("lane"), why=why)
    return r


def answer(desk: Desk, rid, yes: bool, reason: str = "", commit: str = "", command=None, by: str = "page") -> dict:
    """The principal's answer on the page, to the release as the page showed it (commit, command). A yes stands
    only while the base branch and the command line are still what the page showed: otherwise the release is
    void, and says why."""
    with desk.lock():
        r = get(desk, rid)
        if not r:
            return {"ok": False, "problems": [f"there is no release #{rid}"]}
        if r.get("state") != "waiting":
            return {"ok": False, "problems": [f"release #{rid} is not waiting for an answer: it is {r.get('state')}"]}
        if commit != r.get("commit") or (command is not None and command != r.get("command")):
            return {"ok": False, "problems": [f"release #{rid} is not what this page shows; reload the page"]}
        reason = desk.redact(str(reason or "").strip())[:2000]
        if not yes:
            change(desk, rid, lambda x: {"state": "refused", "reason": reason, "answered": now(), "answered_by": by,
                                         "ended": now()})
            desk.event("release_answered", id=rid, lane=r.get("lane"), yes=False, reason=reason, by=by)
            return {"ok": True}
        why = stale(desk, desk.settings(), r)
        if why:
            void(desk, rid, why, ("waiting",), stale(desk, desk.settings(), r, zh=True))
            return {"ok": False, "void": why, "problems": [f"release #{rid} is void: {why}. Nothing was run; the "
                                                           "coordinator is told, and asks again"]}
        change(desk, rid, lambda x: {"state": "approved", "answered": now(), "answered_by": by,
                                     **({"reason": reason} if reason else {})})
        desk.event("release_answered", id=rid, lane=r.get("lane"), yes=True, by=by)
    return {"ok": True}


# -- running -------------------------------------------------------------------------------------------------------
def start(desk: Desk, rid) -> subprocess.Popen | None:
    """Start an approved release, when everything the principal saw still holds; None when it did not start
    (void, forbidden, back to waiting)."""
    with desk.lock():  # no merge lands, and no answer or request comes, between this look and the start
        r = get(desk, rid)
        if not r or r.get("state") != "approved":
            return None
        s = desk.settings()
        if r.get("without_asking") and not lane_listed(s, r["lane"]):  # off the list, or no longer what it recorded
            change(desk, rid, lambda x: {"state": "waiting", "without_asking": None, "answered": None})
            desk.event("release_waits", id=rid, lane=r["lane"], why="the lane is no longer on the principal's list "
                                                                       "of lanes released without asking, or no longer "
                                                                       "what the list recorded")
            return None
        if r.get("without_asking") and desk.paused():  # while paused, only a release the principal said yes to
            return None
        why, why_zh = stale(desk, s, r), stale(desk, s, r, zh=True)
        if not why:
            try:
                current = git("rev-parse", "--abbrev-ref", "HEAD", cwd=r["repo"])
                dirty = git("status", "--porcelain", "--untracked-files=no", cwd=r["repo"])
            except (RuntimeError, OSError) as e:
                current, dirty = r["base"], ""
                why = desk.redact(f"the checkout at {r['repo']} cannot be read ({e})")
                why_zh = desk.redact(f"{r['repo']} 的检出读不出来（{e}）")
            if not why and current != r["base"]:
                why = (f"the checkout at {r['repo']} is on {current}, not {r['base']}, so the command would not run at "
                       f"the commit the principal saw; the principal needs to switch it back")
                why_zh = (f"{r['repo']} 的检出在 {current} 上，不在 {r['base']} 上，命令不会在你看到的那个提交上运行；"
                          f"需要你把它切回去")
            elif not why and dirty:
                why = (f"the checkout at {r['repo']} has uncommitted changes, so the command would not run on exactly "
                       f"the commit the principal saw")
                why_zh = f"{r['repo']} 的检出有没提交的改动，命令不会正好在你看到的那个提交上运行"
        if why:
            void(desk, rid, why, ("approved",), why_zh)
            return None
        try:
            hits, checked = forbidden_in(r["repo"], r.get("since") or "", r["commit"], s.get("release_forbidden") or [],
                                         r["command"])
        except RuntimeError as e:
            void(desk, rid, desk.redact(f"the commits going out cannot be read ({e})")[:400], ("approved",),
                 desk.redact(f"要发出去的提交读不出来（{e}）")[:400])
            return None
        if hits:  # a word the principal added since it was asked
            change(desk, rid, lambda x: {"state": "forbidden", "hits": hits, "checked": checked, "ended": now()})
            desk.event("release_forbidden", id=rid, lane=r["lane"], commit=r["commit"], found=len(hits))
            return None
        d = log_dir(desk)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{rid}.exit").unlink(missing_ok=True)
        token = f"hansa-release-{rid}-{uuid.uuid4().hex[:8]}"
        env = {**os.environ, "HANSA_RELEASE": str(rid), "HANSA_RELEASE_COMMIT": r["commit"]}
        for k in ("HANSA_TASK", "HANSA_MERGING"):
            env.pop(k, None)
        change(desk, rid, lambda x: {"state": "starting", "launch": token, "started": now(), "checked": checked})
        with open(d / f"{rid}.log", "wb") as out:
            proc = subprocess.Popen(["/bin/sh", "-c", SCRIPT, token, r["command"], str(d / f"{rid}.exit")],
                                    cwd=r["repo"], env=env, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        change(desk, rid, lambda x: {"state": "running", "pid": proc.pid, "pid_start": proc_start(proc.pid)})
    desk.event("release_started", id=rid, lane=r["lane"], commit=r["commit"])
    return proc


def finish(desk: Desk, rid) -> dict | None:
    """After a release's command ended: its exit code and its output, keys removed; done or failed."""
    r = get(desk, rid)
    if not r or r.get("state") not in ("starting", "running"):
        return None
    d = log_dir(desk)
    try:
        code = int((d / f"{rid}.exit").read_text().strip())
    except (OSError, ValueError):
        code = None
    (d / f"{rid}.exit").unlink(missing_ok=True)
    with desk.lock():  # a key saved meanwhile is either known here or scrubbed after
        log = d / f"{rid}.log"
        text = desk.redact(log.read_text(encoding="utf-8", errors="replace")) if log.exists() else ""
        if log.exists():
            log.write_text(text, encoding="utf-8")
    limit = r.get("limit_min")
    problem = (f"stopped at the lane's time limit ({limit} min)" if r.get("timed_out") else
               r.get("lost") or ("" if code is not None else "it was interrupted before it could end (no exit code)"))
    ok = code == 0 and not problem
    r = change(desk, rid, lambda x: None if x.get("state") not in ("starting", "running") else {
        "state": "done" if ok else "failed", "exit": code, "problem": problem, "ended": now(),
        "tail": text.strip().splitlines()[-TAIL:], "seconds": round(now() - (x.get("started") or now())), "pid": None,
        "pid_start": ""})
    if r:
        desk.event("release_ended", id=rid, lane=r.get("lane"), commit=r.get("commit"), ok=ok, exit=code, problem=problem)
    return r


def _recover(desk: Desk, r: dict) -> None:
    """A release an earlier server left starting: take over its run, or finish it; never start it again."""
    try:
        found = leader(pids_with_arg(r.get("launch") or "", r.get("started")))
    except CannotLook:
        since = r.get("lookup_failing_since") or now()
        if time.time() - since < LOOK_LIMIT:
            change(desk, r["id"], lambda x: {"lookup_failing_since": since} if x.get("state") == "starting" else None)
            return
        found = None
    if found:
        change(desk, r["id"], lambda x: {"state": "running", "pid": found, "pid_start": proc_start(found)}
               if x.get("state") == "starting" else None)
        return
    if not (log_dir(desk) / f"{r['id']}.exit").exists():
        change(desk, r["id"], lambda x: {"lost": "hansa serve stopped as it started the release, so whether its command "
                                                 "ran is not known: read its output (hansa release), and ask again if it "
                                                 "has to go out"})
    finish(desk, r["id"])


def tick(desk: Desk, s: dict, procs: dict) -> None:
    """hansa serve's round for releases: finish those that ended, stop those over their lane's time limit, take
    over or finish those an earlier server left, and start approved ones, one per repository, none where a
    merge check runs (also while dispatch is paused: the principal said yes, and they use no provider)."""
    for rid, proc in list(procs.items()):
        if has_ended(proc):
            del procs[rid]
            stop(proc.pid, "", proc)  # what it left running in its group ends with it
            finish(desk, rid)
    for r in releases(desk):
        if r["id"] in procs:
            continue
        if r.get("state") == "running" and not alive(r.get("pid"), r.get("pid_start", "")):
            finish(desk, r["id"])
        elif r.get("state") == "starting":
            _recover(desk, r)
    for r in releases(desk):
        if r.get("state") != "running" or r.get("timed_out"):
            continue
        minutes = (s["lanes"].get(r.get("lane")) or {}).get("timeout_min", 240)
        if time.time() - (r.get("started") or time.time()) > 60 * minutes:
            change(desk, r["id"], lambda x: {"timed_out": True, "limit_min": minutes})
            stop(r.get("pid"), r.get("pid_start", ""), procs.get(r["id"]))
    tasks = desk.tasks()
    held = {r.get("repo") for r in releases(desk) if r.get("state") in ("starting", "running")}
    for r in sorted((r for r in releases(desk) if r.get("state") == "approved"), key=lambda r: r["id"]):
        if r.get("repo") in held or merging_on(desk, s, r.get("repo"), tasks):
            continue
        try:
            proc = start(desk, r["id"])
        except Exception as e:  # noqa: BLE001 - say: nothing went out, and the release says why
            why = desk.redact(f"it could not start: {e}")[:600]
            if change(desk, r["id"], lambda x: {"state": "failed", "problem": why, "ended": now()}
                      if x.get("state") in ("approved", "starting") else None):
                desk.event("release_ended", id=r["id"], lane=r.get("lane"), commit=r.get("commit"), ok=False, exit=None,
                           problem=why)
            continue
        if proc:
            procs[r["id"]] = proc
            held.add(r.get("repo"))


# -- words ---------------------------------------------------------------------------------------------------------
def when(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else ""


def waits_text(desk: Desk, s: dict, r: dict, tasks=None, zh: bool = False) -> str:
    """What an approved release waits for, in words (zh: in Chinese, for the page)."""
    paused = desk.paused()
    if r.get("without_asking") and paused:
        return (f"等桌面恢复（桌面已暂停：{paused}）：暂停时只运行你点过头的发布" if zh else
                f"waits until the desk is resumed (it is paused: {paused}): while it is paused, only a release the "
                "principal said yes to runs")
    other = next((x for x in releases(desk) if x.get("state") in ("starting", "running") and x.get("repo") == r.get("repo")
                  and x["id"] != r["id"]), None)
    if other:
        return (f"等同一仓库上工作线 {other.get('lane')} 的发布 #{other['id']} 结束" if zh else
                f"waits for release #{other['id']} of lane {other.get('lane')} on the same repository")
    m = merging_on(desk, s, r.get("repo"), tasks)
    if m:
        return (f"等同一仓库上工作线 {m.get('lane')} 的 {m['id']} 合并验收结束" if zh else
                f"waits for {m['id']}'s merge check on lane {m.get('lane')}, on the same repository")
    from .run import not_looking
    gone = not_looking(desk)
    if zh:
        return "后台（hansa serve）没在运行或卡住了，开始不了" if gone else "hansa serve 下次查看时开始（几秒内）"
    return gone or "starts when hansa serve next looks (within seconds)"


def state_text(desk: Desk, r: dict, s: dict | None = None, zh: bool = False) -> str:
    """A release's state in words (zh: in Chinese, for the page)."""
    st = r.get("state")
    if st == "waiting":
        return "等你在网页上点头" if zh else "waiting for the principal's yes on the page"
    if st == "approved":
        if zh:
            how = ("不用问你就发布（这条工作线在你的清单上）" if r.get("without_asking")
                   else f"你在 {when(r.get('answered'))} 点了头")
            return f"{how}；{waits_text(desk, s or desk.settings(), r, zh=True)}"
        how = "released without asking (the lane is on the principal's list)" if r.get("without_asking") \
            else f"the principal said yes {when(r.get('answered'))}"
        return f"{how}; {waits_text(desk, s or desk.settings(), r)}"
    if st in ("starting", "running"):
        return f"从 {when(r.get('started'))} 起在运行" if zh else f"running since {when(r.get('started'))}"
    if st == "done":
        return (f"{when(r.get('ended'))} 完成（退出码 0，{r.get('seconds', 0)} 秒）" if zh else
                f"done {when(r.get('ended'))} (exit 0, {r.get('seconds', 0)} s)")
    if st == "failed":
        if zh:
            return (f"{when(r.get('ended'))} 失败" + (f"（退出码 {r['exit']}）" if r.get("exit") is not None else "")
                    + (f"：{r['problem']}" if r.get("problem") else ""))
        return (f"FAILED {when(r.get('ended'))}" + (f" (exit {r['exit']})" if r.get("exit") is not None else "")
                + (f": {r['problem']}" if r.get("problem") else ""))
    if st == "refused":
        if zh:
            return f"你在 {when(r.get('answered'))} 拒绝了" + (f"：{r['reason']}" if r.get("reason") else "")
        return f"refused by the principal {when(r.get('answered'))}" + (f": {r['reason']}" if r.get("reason") else "")
    if st == "void":
        return f"作废：{r.get('void_zh') or r.get('void')}；什么都没运行" if zh else f"void: {r.get('void')}; nothing was run"
    if st == "forbidden":
        n = len(r.get("hits", []))
        return (f"被拒绝：要发出去的内容里有你不许发出去的字眼（{n} 处）" if zh else
                f"refused: what goes out holds words the principal keeps from going out ({n} place(s))")
    return st or "?"


def checked_text(r: dict) -> str:
    """How many outgoing commits were checked for forbidden words, and how many of them the request shows."""
    n = r.get("checked", r.get("count", 0))
    shown = len(r.get("commits") or [])
    return (f"{n} outgoing commit(s) checked for forbidden words" + (f" (the first {shown} shown)" if shown < n else ""))


def line(desk: Desk, r: dict, s: dict | None = None) -> str:
    return (f"#{r['id']} lane {r.get('lane')} at {r.get('commit', '')[:10]} ({r.get('base')}): "
            f"{state_text(desk, r, s)}")


def list_text(desk: Desk) -> str:
    rs = releases(desk)
    if not rs:
        return ("no releases yet. A lane's release command is set with hansa lane set L release \"<command>\"; "
                "hansa release L [note] asks for one")
    s = desk.settings()
    live = [r for r in rs if r.get("state") in OPEN]
    recent = [r for r in rs if r.get("state") not in OPEN][-10:]
    out = []
    for title, group in (("open", live), ("recent", list(reversed(recent)))):
        if not group:
            continue
        out.append(f"{title}:")
        for r in group:
            out.append("  " + line(desk, r, s))
            out.append(f"      command: {r.get('command')}" + (f" · note: {r['note']}" if r.get("note") else ""))
            out.append(f"      {checked_text(r)}")
            if r.get("state") in ("done", "failed") and r.get("tail"):
                out += [f"      | {x}" for x in r["tail"][-5:]]
            if r.get("state") == "forbidden":
                out += ["    " + x for x in hits_text(r.get("hits", [])).splitlines()]
    out.append("the whole output of a release: .hansa/releases/<id>.log (the principal's; hansa release shows its last lines)")
    return "\n".join(out)


def lane_lines(desk: Desk, name: str, ln: dict) -> list:
    """For hansa explain lane: the release command and the lane's last release."""
    s = desk.settings()
    rs = [r for r in releases(desk) if r.get("lane") == name]
    last = rs[-1] if rs else None
    return [
        "  release: " + (f"{ln['release']} (hansa release {name} asks for it; it runs in {ln['repo']} at the base "
                         "branch's commit, as the desk's account, outside the sandbox, with hansa's environment (not "
                         "the lane's env) and the lane's minutes as its limit, only after the principal says yes on "
                         "the page" + (": this lane is on the principal's list of lanes released without asking"
                                       if lane_listed(s, name) else unlisted(s, name)) + ")"
                         if ln.get("release") else f"none (set one with hansa lane set {name} release \"<command>\")"),
        "  last release: " + (line(desk, last, s) if last else "none")]


def unlisted(s: dict, name: str) -> str:
    """For hansa explain lane: a lane on the principal's list that no longer goes out without asking, and why."""
    why = next((listed_mismatch(s, x) for x in s.get("releases_without_asking") or []
                if (x if isinstance(x, str) else x.get("lane") if isinstance(x, dict) else None) == name), "")
    return (f": this lane is on the principal's list of lanes released without asking, but {why}, so its releases "
            "wait for the yes until the principal saves the list again") if why else ""


def page_rows(desk: Desk, s: dict, tasks=None) -> list:
    """For the page and hansa status: the open releases and the latest ended ones of the past week."""
    rs = releases(desk)
    week = time.time() - 7 * 86400
    shown = [r for r in rs if r.get("state") in OPEN] + [r for r in rs if r.get("state") not in OPEN
                                                           and (r.get("ended") or r.get("asked") or 0) >= week][-5:]
    out = []
    for r in shown:
        row = {k: r.get(k) for k in ("id", "lane", "base", "commit", "command", "note", "state", "asked", "answered",
                                     "reason", "void", "exit", "problem", "count", "commits", "files", "files_count",
                                     "named", "hits", "ended", "without_asking", "since_release")}
        row["checked"] = r.get("checked", r.get("count"))
        row["what"], row["what_zh"] = state_text(desk, r, s), state_text(desk, r, s, zh=True)
        row["tail"] = (r.get("tail") or [])[-TAIL:]
        if r.get("state") == "waiting":
            row["moved"], row["moved_zh"] = stale(desk, s, r), stale(desk, s, r, zh=True)
        out.append(row)
    return out
