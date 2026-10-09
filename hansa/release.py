"""Releases: how a lane's base branch goes out (a push, a wait for CI, a deploy), run by hansa only after the
principal says yes on the page.

A lane has release targets: the ways its base branch goes out. Its release command (`hansa lane set L release
"..."`) is its one unnamed target; or it has named ones (`hansa lane target L NAME "..."`), each with its own
command, time limit, and optionally another target of the lane that must have sent out the same commit first
(after). The coordinator asks for a release of the base branch as it is now with `hansa release L [TARGET] [note]`
(TARGET is needed when the lane has more than one). hansa records the request in .hansa/releases.json, which no
agent may edit: the lane, the target, the commit, the command line, the note, the commits that go out (reachable
from the commit, on no remote-tracking branch of the repository and not in that target's last release that went
out) and the files they change. Before the request waits for the principal, the command line, the lines those commits add,
the names of the files they add or change, and their messages are checked against the principal's
release_forbidden words; a hit refuses it (the last net: hansa add refuses a title with one, and a merge a task
whose commits or merge commit hold one, run.words_in_merge). The principal answers on the page (Release, or Refuse with a
reason), or says in the conversation the one-time code their alert carried, which the coordinator passes on
(hansa release yes|no ID CODE: see answer_by_code); a target on the principal's releases_without_asking list needs no answer while it is what the list
recorded when the page saved it, and while the desk is not paused. hansa serve runs an approved
release in the lane's repository, as the desk's account, outside the sandbox, with hansa's own environment (not
the lane's env) and the target's time limit (else the lane's timeout_min), and only while the base branch and the
command line are still what the principal saw, and the target it must follow sent out that commit: otherwise the
request is void, says why, and the coordinator asks again. Its output is kept,
keys removed, in .hansa/releases/<id>.log. One release runs at a time per repository. A release runs in the
lane's repository and holds it for as long as it runs: none starts while a merge check runs there, nor a merge
while it runs. A target set --fixed (`hansa lane target L NAME --fixed`) runs instead in a detached checkout of
the release's commit made for it under .hansa/release-checkouts/ (removed when it ends, however it ends, and
swept after a restart), with HANSA_RELEASE_COMMIT, HANSA_RELEASE_TARGET and HANSA_RELEASE_REPO in its
environment; it holds nothing, so a long install leaves that repository's merges and checks going on.

The forbidden-word scan reads each outgoing commit's own change once (a merge's: what it adds beyond its
parents) and runs without the desk lock; the request is recorded under the lock only if what was scanned is still
what goes out (scan_key), and hansa serve scans again at the start only when something the scan depends on
changed since.

A release's state: waiting (for the principal) -> approved -> starting -> running -> done | failed; or refused
(the principal said no), void (what the principal saw changed, or the lane is gone), forbidden (a word that
must not go out), stopped (the coordinator, `hansa release --stop ID [reason]`, or the principal, Stop on the page,
called it off). One that has not started yet is closed as stopped at once; a running one stays running until its
command has ended: hansa serve sends TERM to the command's whole process group, and KILL STOP_GRACE seconds later
to what still runs there; its output so far is kept, and a fixed target's checkout is removed as at any other end.
A stopped release went out for no `--after`: like a failed one, it lets no other target through.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from . import without_session
from .desk import Desk, lane_targets, listed_key, listed_mismatch, now, read_json, target_name, write_json
from .run import (LOOK_LIMIT, SWEEP_GRACE, CannotLook, _state, alive, git, group_running, has_ended, leader,
                  merge_target, pids_with_arg, proc_start, record_descendants, stop, stop_recorded)

OPEN = ("waiting", "approved", "starting", "running")
MOST_COMMITS = 200  # of the outgoing commits kept on the request (all are checked for forbidden words)
MOST_FILES = 500
MOST_HITS = 20
SCAN_TRIES = 5  # scans of what goes out that may each find it changed by the time it is recorded
TAIL = 15
STOP_GRACE = 30  # seconds between the TERM a stopped release's command gets and the KILL to what still runs
# $0 is the launch word, which finds the run again after hansa serve restarts; the exit code goes in a file
SCRIPT = '/bin/sh -c "$1"; echo $? > "$2"'
# a hunk of a patch, or of a merge's combined diff (one more @ and one more -range per parent)
HUNK = re.compile(r"^(@@+) ((?:-\d+(?:,\d+)? )+\+\d+(?:,\d+)?) @@")
RANGE = re.compile(r"[-+]\d+(?:,(\d+))?")


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


def lane_listed(s: dict, lane: str, target: str = "") -> bool:
    """Whether the principal lets this release target of the lane go out without asking: it is on the list, and the
    lane has the repository and base branch, and the target the command, they had when the page saved the list. A
    tick for one target never covers another."""
    return any(isinstance(x, dict) and listed_key(x) == (lane, target) and not listed_mismatch(s, x)
               for x in s.get("releases_without_asking") or [])


def what(r: dict) -> str:
    """A release's lane and target, in words: 'lane a' or 'lane a target prod'."""
    t = target_name(r.get("target"))
    return f"lane {r.get('lane')}" + (f" {t}" if t else "")


def what_zh(r: dict) -> str:
    return f"工作线 {r.get('lane')}" + (f" 的目标 {r['target']}" if r.get("target") else "")


def ask_again(r: dict) -> str:
    """The command that asks for the same release again."""
    return f"hansa release {r.get('lane')}" + (f" {r['target']}" if r.get("target") else "")


def minutes(s: dict, r: dict) -> tuple:
    """A release's time limit in minutes, and whose it is ('the lane's' or 'target T's'): the target's own, else
    the lane's timeout_min."""
    ln = s["lanes"].get(r.get("lane")) or {}
    own = (lane_targets(ln).get(r.get("target") or "") or {}).get("timeout_min")
    if own:
        return own, f"target {r.get('target')}'s"
    return ln.get("timeout_min", 240), "the lane's"


def before_it(desk: Desk, s: dict, lane: str, target: str, repo, base: str, commit: str, zh: bool = False) -> str:
    """'' when the target may send this commit out now; else which release must go first, in one line: a target
    that must follow another goes out only with a commit the other one already sent out (done, same commit). zh: in
    Chinese, for the page."""
    after = (lane_targets((s["lanes"].get(lane) or {})).get(target) or {}).get("after")
    if not after:
        return ""
    if any(r.get("lane") == lane and (r.get("target") or "") == after and r.get("state") == "done"
           and r.get("repo") == repo_key(repo) and r.get("base") == base and r.get("commit") == commit
           for r in releases(desk)):
        return ""
    prev = last_out(desk, lane, repo, base, after)
    if zh:
        return (f"工作线 {lane} 的目标 {target} 要等目标 {after} 先发出同一个提交才能发出，而"
                + (f" {after} 上一次发出的发布（#{prev['id']}）是 {prev['commit'][:10]}，不是 {commit[:10]}"
                   if prev else f"目标 {after} 还没有从 {base} 发出过发布")
                + f"：先发布 {after}（hansa release {lane} {after}）")
    return (f"lane {lane} target {target} goes out only after target {after} sent out the same commit, and "
            + (f"{after}'s last release that went out (#{prev['id']}) was {prev['commit'][:10]}, not {commit[:10]}"
               if prev else f"no release of {after} went out from {base}")
            + f": release {after} first (hansa release {lane} {after})")


def holds_repo(r: dict) -> bool:
    """Whether a release holds its repository while it starts or runs: it runs there, unless its target runs from
    a fixed checkout of the commit (then merges and merge checks there go on meanwhile)."""
    return r.get("state") in ("starting", "running") and not r.get("fixed")


def running_on(desk: Desk, repo) -> dict | None:
    """The release starting or running in this repository and holding it, if any: no merge goes in there
    meanwhile."""
    key = repo_key(repo)
    return next((r for r in releases(desk) if holds_repo(r) and r.get("repo") == key), None)


def running_text(r: dict) -> str:
    return f"release #{r['id']} of {what(r)}, which runs on that repository"


def merging_on(desk: Desk, s: dict, repo, tasks=None):
    """The task whose merge check starts or runs on this repository, if any: no release starts there meanwhile."""
    key = repo_key(repo)
    return next((m for m in (desk.tasks() if tasks is None else tasks)
                 if m["state"] == "merging" and (m.get("merge_check") or {}).get("phase") in ("starting", "running")
                 and (merge_target(s, m.get("lane")) or ("",))[0] == key), None)


# -- what goes out ---------------------------------------------------------------------------------------------
def _range(since: str, commit: str) -> list:
    """The commits that go out: reachable from the release commit, on no remote-tracking branch of the repository
    (the whole history when it has none) and not in the target's last release."""
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
    names = _git_bytes(repo, "log", "--cc", "--format=", "--name-only", "--no-renames", *rng)
    return commits, sorted({x for x in names.splitlines() if x})


def _added(lines):
    """(commit, file, line) for each line the patches of `git log -p --cc --format=%x00%H -U0` add: an ordinary
    commit's added lines, and a merge's lines in none of its parents (what the merge itself adds, such as a
    conflict's resolution; a line that came from a parent is that parent's commit's)."""
    sha = path = None
    old, new, n = [], 0, 1  # lines left in the hunk on each parent's side and on the result's; parents
    for line in lines:
        if new > 0 or any(x > 0 for x in old):  # inside a hunk: counted, so an added line that reads like a
            marks, text = line[:n], line[n:]   # header is still one
            if line.startswith("\\"):
                continue
            if "-" in marks:  # in some parent, not in the result
                old = [x - (m == "-") for x, m in zip(old, marks)]
                continue
            new -= 1
            old = [x - (m == " ") for x, m in zip(old, marks)]
            if marks == "+" * n:
                yield sha, path, text
            continue
        if line.startswith("\0"):
            sha, path = line[1:].strip(), None
        elif line.startswith("+++ "):
            p = line[4:].rstrip("\t")
            path = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
        else:
            m = HUNK.match(line)
            if m:
                n = len(m[1]) - 1
                sides = [int(c) if c else 1 for c in RANGE.findall(m[2])]
                if len(sides) == n + 1:
                    old, new = sides[:n], sides[n]


def forbidden_in(repo, since: str, commit: str, words: list, command: str = "") -> tuple:
    """Where what goes out holds one of the principal's forbidden words (any case, within one line, in text read as
    UTF-8): the release command line, and every outgoing commit's message, its author's and committer's names and
    emails (where: author or committer; line: Name <email>), the names of the files it adds or
    changes (a renamed one by its new name) and the lines it adds. Each commit's own change is read once: a merge's
    is what it adds beyond all its parents (git's dense combined diff, --cc), not its diff against each parent, so
    what a merge brings in from a branch is read in that branch's commits when they go out, and not at all when
    they are on a remote-tracking branch, already out. ([{word, commit, where, line}], each place once, at most
    MOST_HITS; how many commits were checked)."""
    return words_in(repo, _range(since, commit), words, [("release command", command)])


def words_in(repo, rng: list, words: list, texts=()) -> tuple:
    """forbidden_in for any range of commits (git log's arguments, ending in --), and for texts that are not in a
    commit yet ([(where, text)], each line looked at, commit ''): a merge's message before it is made, say."""
    words = [w for w in words if isinstance(w, str) and w.strip()]
    folded = [(w, w.casefold()) for w in words]
    hits, seen, checked = [], set(), 0

    def look(sha, where, line):
        low = line.casefold()
        for w, f in folded:
            if f in low and (where, line) not in seen:
                seen.add((where, line))
                hits.append({"word": w, "commit": sha or "", "where": where, "line": line.strip()[:300]})
                return

    for where, text in texts:
        for line in (text or "").splitlines():
            look("", where, line)
    sha = None
    for line in _git_lines(repo, "log", "--format=%x00%H%n%B", *rng):
        if line.startswith("\0"):
            sha, checked = line[1:].strip(), checked + 1
        elif folded:
            look(sha, "commit message", line)
    if not folded:
        return [], checked
    for line in _git_lines(repo, "log", "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce", *rng):
        sha, *who = line.split("\x1f")
        if len(who) == 4:
            look(sha, "author", f"{who[0]} <{who[1]}>")
            look(sha, "committer", f"{who[2]} <{who[3]}>")
    for line in _git_lines(repo, "log", "--cc", "--format=%x00%H", "--name-only", "--no-renames", "--diff-filter=d", *rng):
        if line.startswith("\0"):
            sha = line[1:].strip()
        elif line:
            look(sha, "file name", line)
    patch = _git_lines(repo, "log", "-p", "--cc", "--text", "--no-ext-diff", "--no-textconv", "--no-color",
                       "--unified=0", "--no-renames", "--format=%x00%H", *rng)
    for sha, path, line in _added(patch):
        look(sha, path or "?", line)
        if len(hits) >= MOST_HITS:
            patch.close()
            break
    return hits[:MOST_HITS], checked


def command_hits(ln: dict, words: list, only: str | None = None) -> list:
    """The forbidden words in a lane's release targets' command lines (any case, within one line), each
    (target, word, line) once; only: that one target's ("" the unnamed one)."""
    folded = [(w, w.casefold()) for w in words or [] if isinstance(w, str) and w.strip()]
    out = []
    for target, t in sorted(lane_targets(ln).items()):
        if only is not None and target != only:
            continue
        for line in str(t.get("command") or "").splitlines():
            w = next((w for w, f in folded if f in line.casefold()), None)
            if w:
                out.append((target, w, line.strip()[:300]))
    return out


def command_hits_text(desk: Desk, lane: str, hits: list) -> str:
    """What hansa says when a release command it has just set holds a forbidden word: the word and where."""
    return desk.redact("Warning: " + "; ".join(
        f"lane {lane}'s {'release target ' + t if t else 'release command'} holds \"{w}\", a word the principal "
        f"keeps from going out: {line}" for t, w, line in hits)
        + ". The setting is kept, but hansa refuses every release whose command line holds such a word: change the "
          "command, or ask the principal about the word")


def hits_text(hits: list) -> str:
    return "\n".join(f"  {h['commit'][:10]} {h['where']}: {h['line']}  (\"{h['word']}\")" for h in hits)


def title_hits(title: str, words: list) -> list:
    """The forbidden words in a task's title, which hansa puts in its commit message and its merge's: each
    word once, any case."""
    low = (title or "").casefold()
    return [w for w in dict.fromkeys(w for w in words or [] if isinstance(w, str) and w.strip()) if w.casefold() in low]


def named_files(command: str, files: list) -> list:
    """The changed files the command line names (./deploy.sh, scripts/release.sh, or by its name alone): what
    the release runs is in the change."""
    edge = r"""(?:^|[\s/'"=;&|()<>`])""", r"""(?=$|[\s'";&|()<>`])"""
    return [f for f in files if any(re.search(edge[0] + re.escape(x) + edge[1], command)
                                    for x in dict.fromkeys([f, os.path.basename(f)]) if x)]


def last_out(desk: Desk, lane: str, repo: str, base: str, target: str = "") -> dict | None:
    """The last release of the lane's target that went out, of this repository and base branch (a request recorded
    before targets had names is the lane's unnamed target's)."""
    done = [r for r in releases(desk) if r.get("lane") == lane and (r.get("target") or "") == target
            and r.get("state") == "done" and r.get("repo") == repo_key(repo) and r.get("base") == base]
    return done[-1] if done else None


def _has_commit(repo, sha: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=str(repo), capture_output=True,
                          stdin=subprocess.DEVNULL).returncode == 0


# -- asking ------------------------------------------------------------------------------------------------------
def scan_key(repo, since: str, commit: str, words: list, command: str) -> str:
    """What a forbidden-word scan depends on, in one string: the commit, the starting point (the target's last
    release and the repository's remote-tracking branches), the principal's words and the command line. A scan
    made with the same key finds the same."""
    remotes = _git_bytes(repo, "for-each-ref", "--format=%(objectname) %(refname)", "refs/remotes")
    words = sorted(w for w in words if isinstance(w, str) and w.strip())
    # "identities": a scan from before authors and committers were read does not count for one that reads them
    return hashlib.sha256(json.dumps([repo_key(repo), commit, since, remotes, words, command, "identities"])
                          .encode()).hexdigest()


def _askable(desk: Desk, s: dict, lane: str, target: str | None) -> dict:
    """Under the desk lock: what a request for the lane's target would be now ({target, command, repo, base,
    commit, prev, since, fixed}); raises SystemExit, saying why, when it cannot be asked."""
    ln = s["lanes"].get(lane)
    if not ln:
        raise SystemExit(f"hansa: there is no lane {lane}; lanes: {', '.join(sorted(s['lanes'])) or 'none'}. "
                         "Nothing was asked")
    targets = lane_targets(ln)
    if not targets:
        raise SystemExit(f"hansa: lane {lane} has no release command; set it first: hansa lane set {lane} release "
                         "\"<the command that sends the base branch out>\", or name targets with hansa lane target "
                         f"{lane} NAME \"<command>\" (and tell the principal). Nothing was asked")
    if target is None:
        if len(targets) > 1:
            raise SystemExit(f"hansa: lane {lane} has {len(targets)} release targets ({', '.join(sorted(targets))}); "
                             f"say which: hansa release {lane} TARGET [note]. Nothing was asked")
        target = next(iter(targets))
    if target not in targets:
        raise SystemExit(f"hansa: lane {lane} has no release target {target}; its targets: "
                         f"{', '.join(sorted(x or '(unnamed)' for x in targets))}. Nothing was asked")
    command = targets[target].get("command") or ""
    repo, base = ln["repo"], ln.get("base") or "main"
    try:
        commit = git("rev-parse", "--verify", f"refs/heads/{base}^{{commit}}", cwd=repo)
    except (RuntimeError, OSError) as e:
        raise SystemExit(f"hansa: lane {lane}'s base branch {base} cannot be read in {repo} ({e}). Nothing was asked")
    first = before_it(desk, s, lane, target, repo, base, commit)
    if first:
        raise SystemExit(f"hansa: {first}. Nothing was asked")
    busy = [r for r in releases(desk) if r.get("lane") == lane and (r.get("target") or "") == target
            and r.get("state") in ("approved", "starting", "running")]
    if busy:
        r = busy[0]
        raise SystemExit(f"hansa: release #{r['id']} of {what({'lane': lane, 'target': target})} is already "
                         f"{r['state']}; one at a time. hansa release shows how it goes; hansa tells you when it "
                         "ends. Nothing was asked")
    prev = last_out(desk, lane, repo, base, target)
    since = prev["commit"] if prev and _has_commit(repo, prev["commit"]) else ""
    return {"target": target, "command": command, "repo": repo, "base": base, "commit": commit, "prev": prev,
            "since": since, "fixed": bool(targets[target].get("fixed"))}


def ask(desk: Desk, lane: str, note: str = "", by: str = "", session=None, target: str | None = None) -> dict:
    """Record a request for a release of the lane's base branch as it is now, by one of its release targets (None:
    its only one). Returns it; raises SystemExit, with nothing recorded, when it cannot be asked, and with the
    request recorded as forbidden on a forbidden word. What goes out is read and scanned without the desk lock
    (it can take long on a long history), then recorded under it only if it is still what goes out: otherwise it
    is read again."""
    scanned = None
    for _ in range(SCAN_TRIES):
        with desk.lock():
            s = desk.settings()
            a = _askable(desk, s, lane, target)
            words = s.get("release_forbidden") or []
            try:
                key = scan_key(a["repo"], a["since"], a["commit"], words, a["command"])
            except RuntimeError as e:
                raise SystemExit(f"hansa: the commits going out cannot be read ({desk.redact(str(e))}). "
                                 "Nothing was asked")
            if scanned and scanned[0] == (key, a["target"]):
                r = _record(desk, s, lane, a, key, scanned[1:], note, by, session)
                break
        try:  # without the lock: the server's round, the page and other commands go on meanwhile
            commits, files = outgoing(a["repo"], a["since"], a["commit"])
            hits, checked = forbidden_in(a["repo"], a["since"], a["commit"], words, a["command"])
        except RuntimeError as e:
            raise SystemExit(f"hansa: the commits going out cannot be read ({desk.redact(str(e))}). Nothing was asked")
        scanned = ((key, a["target"]), commits, files, hits, checked)
    else:
        raise SystemExit(f"hansa: what goes out from lane {lane} kept changing while it was read (its base branch "
                         "moving, say); ask again in a moment. Nothing was asked")
    target, commit, hits = r["target"], r["commit"], r.get("hits")
    desk.event("release_asked", id=r["id"], lane=lane, target=target, commit=commit, state=r["state"], by=by,
               **({"replaced": r["_replaced"]} if r.get("_replaced") else {}))
    for n in r.pop("_replaced", []):
        desk.event("release_void", id=n, lane=lane, target=target, why=f"replaced by release #{r['id']}", quiet=True)
    if hits:
        desk.event("release_forbidden", id=r["id"], lane=lane, target=target, commit=commit, found=len(hits),
                   quiet=True)
    return r


def _record(desk: Desk, s: dict, lane: str, a: dict, key: str, scanned: tuple, note: str, by: str,
            session) -> dict:
    """Under the desk lock: write the request, from what was read and scanned with this key."""
    commits, files, hits, checked = scanned
    target, command, prev, since = a["target"], a["command"], a["prev"], a["since"]
    rs = releases(desk)
    listed = lane_listed(s, lane, target)
    r = {"id": max((x.get("id", 0) for x in rs), default=0) + 1, "lane": lane, "target": target,
         "repo": repo_key(a["repo"]), "base": a["base"], "commit": a["commit"], "command": command,
         **({"fixed": True} if a["fixed"] else {}), "note": desk.redact(note.strip()), "asked": now(),
         "by": by, "since": since, "since_release": prev["id"] if prev and since else None,
         "count": len(commits), "checked": checked, "scanned": key, "commits": commits[:MOST_COMMITS],
         "files": files[:MOST_FILES], "files_count": len(files), "named": named_files(command, files[:MOST_FILES]),
         "state": "forbidden" if hits else "approved" if listed else "waiting",
         **({"hits": hits} if hits else {}), **({"without_asking": True, "answered": now()} if listed and not hits
                                               else {}), **({"session": session} if session else {})}
    replaced = []
    if not hits:  # a newer request for the same target replaces one still waiting for the principal
        for x in rs:
            if x.get("lane") == lane and (x.get("target") or "") == target and x.get("state") == "waiting":
                x.update(state="void", void=f"replaced by release #{r['id']}", void_zh=f"被发布 #{r['id']} 取代",
                         ended=now(), **spent(x, f"replaced by release #{r['id']}"))
                replaced.append(x["id"])
    rs.append(r)
    _write(desk, rs)
    return {**r, "_replaced": replaced}


# -- the principal's answer ----------------------------------------------------------------------------------------
def stale(desk: Desk, s: dict, r: dict, zh: bool = False) -> str:
    """Why a release is no longer what the principal saw (the lane or its target gone, the target's command or
    the lane's base branch changed, the base branch moved); '' when it still is. zh: in Chinese, for the page."""
    ln = s["lanes"].get(r.get("lane"))
    if not ln:
        return f"工作线 {r.get('lane')} 已不在桌面上" if zh else f"lane {r.get('lane')} is no longer on the desk"
    target = r.get("target") or ""
    t = lane_targets(ln).get(target)
    if t is None and target:
        return (f"工作线 {r['lane']} 已没有发布目标 {target}" if zh else
                f"lane {r['lane']} has no release target {target} any more")
    if ((t or {}).get("command") or "") != r.get("command"):
        now_as, then = (t or {}).get("command") or ("（无）" if zh else "(none)"), r.get("command")
        return (f"{what_zh(r)} 的发布命令在请求之后改了：现在是 {now_as!r}，不是 {then!r}" if zh else
                f"{what(r)}'s release command changed since it was asked: it is now {now_as!r}, not {then!r}")
    if bool((t or {}).get("fixed")) != bool(r.get("fixed")):
        return (f"{what_zh(r)} 的运行方式（--fixed，固定检出）在请求之后改了" if zh else
                f"how {what(r)} runs (--fixed: from a fixed checkout, or in the repository) changed since it was asked")
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
                                     "ended": now(), **spent(x, "the release became void")}
               if x.get("state") in states else None)
    if r:
        desk.event("release_void", id=rid, lane=r.get("lane"), target=r.get("target") or "", why=why)
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
        return _answered(desk, r, yes, reason, by)


def _answered(desk: Desk, r: dict, yes: bool, reason: str, by: str, channel: str = "") -> dict:
    """Under the desk lock: record the principal's answer to a waiting release (channel "code": given through the
    coordinator with the one-time code of the principal's alert); its code lapses either way."""
    rid = r["id"]
    reason = desk.redact(str(reason or "").strip())[:2000]
    said = {"answered": now(), "answered_by": by, **({"channel": channel} if channel else {}),
            **spent(r, "the release was answered")}
    how = {"by": by, **({"channel": channel} if channel else {})}
    if not yes:
        change(desk, rid, lambda x: {"state": "refused", "reason": reason, **said, "ended": now()})
        desk.event("release_answered", id=rid, lane=r.get("lane"), target=r.get("target") or "", yes=False,
                   reason=reason, **how)
        return {"ok": True}
    why = stale(desk, desk.settings(), r)
    if why:
        void(desk, rid, why, ("waiting",), stale(desk, desk.settings(), r, zh=True))
        return {"ok": False, "void": why, "problems": [f"release #{rid} is void: {why}. Nothing was run; the "
                                                       "coordinator is told, and asks again"]}
    change(desk, rid, lambda x: {"state": "approved", **said, **({"reason": reason} if reason else {})})
    desk.event("release_answered", id=rid, lane=r.get("lane"), target=r.get("target") or "", yes=True, **how)
    return {"ok": True}


# -- the one-time code of the principal's alert --------------------------------------------------------------------
# A release that waits for the principal gets a code, which travels only in the principal's alert (alert.py): the
# principal may say it in the conversation, and the coordinator passes it on (hansa release yes|no ID CODE). hansa
# keeps only an HMAC of it (the release's number and the code), keyed with a key in the desk's secrets/, which no
# agent may read. It answers that release only, once, while it waits as it was asked, for CODE_LIFE seconds; a new
# waiting alert for the release makes a new one; CODE_TRIES wrong codes void it (then only the page answers).
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no look-alikes: no 0/O, 1/I/l
CODE_LENGTH = 6
CODE_LIFE = 24 * 3600
CODE_TRIES = 3


def code_key_file(desk: Desk) -> Path:
    return desk.secrets_dir / "release-code.hmac"  # not *.key: no provider's key, so never looked for in text


def _code_key(desk: Desk) -> bytes:
    f = code_key_file(desk)
    if not f.is_file():
        desk.secrets_dir.mkdir(mode=0o700, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(32))
        os.replace(tmp, f)
    return f.read_text().strip().encode()


def _code_hash(desk: Desk, rid, code: str) -> str:
    return hmac.new(_code_key(desk), f"release {rid}: {code}".encode(), hashlib.sha256).hexdigest()


def plain_code(code: str) -> str:
    """A code as the principal may say it: any case, with spaces or dashes."""
    return re.sub(r"[\s-]", "", str(code or "")).upper()


def hide_codes(text: str, codes) -> str:
    """text with each of codes replaced by [code], in any case and with the spaces or dashes answers accept; called
    before any cut, so that no part of a code is left."""
    for c in codes:
        c = plain_code(c)
        if c:
            text = re.sub(r"[\s-]*".join(re.escape(x) for x in c), "[code]", text, flags=re.IGNORECASE)
    return text


def new_code(desk: Desk, rid) -> str:
    """A new one-time code for a release waiting for the principal (one made before is void); '' when it does not
    wait. Only its HMAC is kept."""
    while True:  # a code of digits alone could be read in a time or a count
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if not code.isdigit():
            break
    digest = _code_hash(desk, rid, code)
    r = change(desk, rid, lambda x: {"code": {"hash": digest, "made": now(), "wrong": 0}}
               if x.get("state") == "waiting" else None)
    return code if r else ""


def code_sent(desk: Desk, rid, code: str, ok: bool) -> None:
    """Record that the alert holding a release's code went out (ok: the command exited 0), if it is still its code;
    a code that did not go out at all (cut from the text) is void."""
    digest = _code_hash(desk, rid, code)

    def sent(x):
        c = x.get("code") or {}
        if not c.get("hash") or not hmac.compare_digest(c["hash"], digest):
            return None
        return {"code": {**c, "sent": now(), "sent_ok": bool(ok)}}
    change(desk, rid, sent)


def code_unsent(desk: Desk, rid, code: str) -> None:
    """A release's code that never went out in an alert is void, if it is still its code."""
    digest = _code_hash(desk, rid, code)
    change(desk, rid, lambda x: spent(x, "it did not go out in the alert")
           if hmac.compare_digest((x.get("code") or {}).get("hash") or "", digest) else None)


def spent(r: dict, why: str) -> dict:
    """The fields that void a release's live code (its hash dropped), with why; {} when it has none."""
    c = r.get("code") or {}
    if not c.get("hash"):
        return {}
    return {"code": {**{k: v for k, v in c.items() if k != "hash"}, "void": why, "void_at": now()}}


def code_live(r: dict) -> bool:
    c = r.get("code") or {}
    return (r.get("state") == "waiting" and bool(c.get("hash")) and not c.get("void")
            and time.time() - (c.get("made") or 0) < CODE_LIFE and (c.get("wrong") or 0) < CODE_TRIES)


def answer_by_code(desk: Desk, rid, yes: bool, code: str, reason: str = "", by: str = "coordinator") -> dict:
    """The principal's answer given in the conversation with the one-time code of their alert, passed on by the
    coordinator: checked against this release's code only. A right code answers it (recorded as given through the
    coordinator, channel "code") and lapses; a wrong one is counted, and CODE_TRIES of them void the code."""
    from .alert import command as alert_command
    with desk.lock():
        s = desk.settings()
        r = get(desk, rid)
        if not r:
            return {"ok": False, "problems": [f"there is no release #{rid}"]}
        if r.get("state") != "waiting":
            return {"ok": False, "problems": [f"release #{rid} is not waiting for an answer: it is {r.get('state')}; "
                                              "its code, if it had one, has lapsed"]}
        c = r.get("code") or {}
        if not alert_command(s) and not c.get("hash"):
            return {"ok": False, "problems": ["the principal has set no alert command, so no code is sent: approval "
                                              f"of release #{rid} is on the page only"]}
        if not c.get("hash"):
            why = c.get("void") or "no code was sent for it"
            tries = " (only the page answers it now)" if (c.get("wrong") or 0) >= CODE_TRIES else ""
            return {"ok": False, "problems": [f"release #{rid} has no live code ({why}){tries}: approval is on the "
                                              "page"]}
        if time.time() - (c.get("made") or 0) >= CODE_LIFE:
            change(desk, rid, lambda x: spent(x, f"older than {CODE_LIFE // 3600} hours"))
            return {"ok": False, "problems": [f"the code of release #{rid} has lapsed (it is older than "
                                              f"{CODE_LIFE // 3600} hours): approval is on the page"]}
        why = stale(desk, s, r)
        if why:  # no longer what the principal was alerted of
            void(desk, rid, why, ("waiting",), stale(desk, s, r, zh=True))
            return {"ok": False, "void": why, "problems": [f"release #{rid} is void: {why}; its code has lapsed. "
                                                           "Nothing was run; ask again"]}
        if not hmac.compare_digest(c["hash"], _code_hash(desk, rid, plain_code(code))):
            wrong = (c.get("wrong") or 0) + 1
            gone = wrong >= CODE_TRIES
            change(desk, rid, lambda x: {"code": {**(x.get("code") or {}), "wrong": wrong}}
                   if not gone else {"code": {**spent(x, f"{CODE_TRIES} wrong codes")["code"], "wrong": wrong}})
            desk.event("release_code_wrong", id=rid, lane=r.get("lane"), target=r.get("target") or "", wrong=wrong,
                       void=gone)
            return {"ok": False, "problems": [
                f"that is not the code of release #{rid}; after {CODE_TRIES} wrong codes it is void, so only the page "
                "answers it now" if gone else
                f"that is not the code of release #{rid} ({wrong} of {CODE_TRIES} wrong; at {CODE_TRIES} it is void "
                "and only the page answers it)"]}
        return _answered(desk, r, yes, hide_codes(reason, [code]), by, channel="code")


def code_text(r: dict, zh: bool = False) -> str:
    """For the page and hansa status: whether a waiting release's code went out in the principal's alert, and when;
    never the code itself."""
    c = r.get("code") or {}
    if not c:
        return ""
    if not code_live(r):
        why = c.get("void") or (f"超过 {CODE_LIFE // 3600} 小时" if zh else f"older than {CODE_LIFE // 3600} hours")
        return f"一次性码已作废（{why}）：只能在网页上回答" if zh else f"its one-time code is void ({why}): answer it on the page"
    if c.get("sent"):
        ok = c.get("sent_ok")
        return (f"一次性码在 {when(c['sent'])} 随提醒发出" + ("" if ok else "（提醒命令报了失败）") if zh else
                f"a one-time code went out in the principal's alert {when(c['sent'])}"
                + ("" if ok else " (the alert command reported a failure)"))
    return "一次性码在等提醒发出" if zh else "a one-time code waits to go out in the principal's alert"


def stop_release(desk: Desk, rid, reason: str = "", by: str = "cli") -> dict:
    """Call a release off (the coordinator's hansa release --stop, the principal's Stop on the page): one that has not
    started yet (waiting, approved) is closed as stopped now; a starting or running one is marked, and hansa serve
    stops its command (TERM to its process group, KILL STOP_GRACE seconds later) and records it as stopped when it
    has ended. {ok, state} or {ok: False, problems}."""
    reason = desk.redact(str(reason or "").strip())[:2000]
    with desk.lock():
        r = get(desk, rid)
        if not r:
            return {"ok": False, "problems": [f"there is no release #{rid}"]}
        st = r.get("state")
        if st not in OPEN:
            return {"ok": False, "problems": [f"release #{rid} has already ended: it is {st}"]}
        if r.get("stop_asked"):
            return {"ok": False, "problems": [f"release #{rid} is already being stopped (asked by "
                                              f"{r.get('stopped_by') or '?'}); hansa serve ends it within "
                                              f"{STOP_GRACE} seconds of its TERM"]}
        asked = {"stop_asked": now(), "stopped_by": by, "stop_reason": reason}
        ended = st in ("waiting", "approved")
        change(desk, rid, lambda x: {**asked, "state": "stopped", "ended": now(), **spent(x, "the release was stopped")}
               if ended else asked)
    if ended:
        desk.event("release_stopped", id=rid, lane=r.get("lane"), target=r.get("target") or "", by=by, reason=reason,
                   ran=False)
    else:
        desk.event("release_stopping", id=rid, lane=r.get("lane"), target=r.get("target") or "", by=by, reason=reason)
    return {"ok": True, "state": "stopped" if ended else "stopping"}


def _signal_group(r: dict, sig, proc=None) -> None:
    """Send a signal to a release's command's process group: while its leader is this server's unreaped child, or
    still the process that was started, or anything of its group still runs."""
    pid = r.get("pid")
    if not pid or not (proc is not None or alive(pid, r.get("pid_start", "")) or group_running(pid)):
        return
    try:
        os.killpg(pid, sig)
    except OSError:
        pass


def _seen(r: dict) -> dict:
    """What a release's command started, as recorded while it ran (_record_started): {pid: start}."""
    return {int(p): st for p, st in (r.get("started_procs") or {}).items() if str(p).isdigit()}


def _record_started(desk: Desk, r: dict) -> dict:
    """Record what a running release's command has started, also what left its process group (as a preview's
    is: run.record_descendants), so that it is stopped at the release's end; the record as it is now."""
    seen = _seen(r)
    if r.get("pid") and record_descendants(r["pid"], seen):
        r = change(desk, r["id"], lambda x: {"started_procs": {**(x.get("started_procs") or {}),
                                                               **{str(p): st for p, st in seen.items()}}}
                   if x.get("state") in ("starting", "running") else None) or r
    return r


def _left(r: dict) -> dict:
    """What the release's command started that still runs outside its process group (the group is
    group_running's): {pid: start}."""
    return {p: st for p, st in _seen(r).items() if p != r.get("pid") and alive(p, st) and _state(p) != "Z"}


def _running(r: dict, pid=None) -> bool:
    """Whether anything of a release's command still runs: its process group, or what it started that left it."""
    return group_running(pid or r.get("pid")) or bool(_left(r))


def _stopping(desk: Desk, r: dict, proc=None) -> None:
    """hansa serve's round for a running release asked to stop: TERM to its command's process group (and to what it
    started that left the group) once, then KILL to what still runs STOP_GRACE seconds later. A starting one has no
    command yet: the next round. One whose command had already ended (its exit file is written) gets no signal:
    finish records how it ended, and that the stop came after."""
    if not r.get("pid") or r.get("stop_late"):
        return
    if not r.get("termed"):
        if (log_dir(desk) / f"{r['id']}.exit").exists():
            change(desk, r["id"], lambda x: {"stop_late": now()} if x.get("state") in ("starting", "running") else None)
            return
        r = _record_started(desk, r)
        _signal_group(r, signal.SIGTERM, proc)
        _signal_each(_left(r), signal.SIGTERM)
        change(desk, r["id"], lambda x: {"termed": now()} if x.get("state") in ("starting", "running") else None)
    elif not r.get("killed") and time.time() - r["termed"] >= STOP_GRACE and _running(r):
        _signal_group(r, signal.SIGKILL, proc)
        _signal_each(_left(r), signal.SIGKILL)
        change(desk, r["id"], lambda x: {"killed": now()} if x.get("state") in ("starting", "running") else None)


def _signal_each(procs: dict, sig) -> None:
    for p in procs:
        try:
            os.kill(p, sig)
        except OSError:
            pass


# -- running -------------------------------------------------------------------------------------------------------
def checkout_dir(desk: Desk, rid) -> Path:
    """Where a release of a fixed target runs: a detached worktree of the release's commit, made for it."""
    return desk.state_dir / "release-checkouts" / str(rid)


def drop_checkout(desk: Desk, rid, repo) -> None:
    """Remove a release's own checkout (and git's record of it), whatever is in it."""
    path = checkout_dir(desk, rid)
    if repo:
        try:
            git("worktree", "remove", "--force", "--force", str(path), cwd=repo)
        except (RuntimeError, OSError):
            pass
    if os.path.lexists(path):
        shutil.rmtree(path, ignore_errors=True)
    if repo:
        try:
            git("worktree", "prune", cwd=repo)
        except (RuntimeError, OSError):
            pass


def sweep_checkouts(desk: Desk) -> None:
    """Remove the checkouts of releases that no longer run: left by a server that stopped, or a start that
    failed."""
    d = desk.state_dir / "release-checkouts"
    if not d.is_dir():
        return
    rs = {str(r.get("id")): r for r in releases(desk)}
    for x in d.iterdir():
        r = rs.get(x.name) or {}
        if r.get("state") not in ("starting", "running"):
            drop_checkout(desk, x.name, r.get("repo"))


def _not_ready(desk: Desk, s: dict, r: dict) -> tuple | None:
    """Under the desk lock: None when an approved release may start now as far as what the principal saw goes;
    else (why, why_zh) to void it, or () when it only waits (sent back to the principal, or the desk is paused)."""
    rid, target = r["id"], r.get("target") or ""
    if r.get("without_asking") and not lane_listed(s, r["lane"], target):  # off the list, or changed
        change(desk, rid, lambda x: {"state": "waiting", "without_asking": None, "answered": None})
        desk.event("release_waits", id=rid, lane=r["lane"], target=target,
                   why="the target is no longer on the principal's list of releases without asking, or no longer "
                       "what the list recorded")
        return ()
    if r.get("without_asking") and desk.paused():  # while paused, only a release the principal said yes to
        return ()
    why, why_zh = stale(desk, s, r), stale(desk, s, r, zh=True)
    if not why:  # the order, again: the target it must follow sent out this commit
        why = before_it(desk, s, r["lane"], target, r["repo"], r["base"], r["commit"])
        why_zh = why and before_it(desk, s, r["lane"], target, r["repo"], r["base"], r["commit"], zh=True)
    if not why and not r.get("fixed"):  # it runs in the repository: on the base branch, as committed
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
    return (why, why_zh) if why else None


def start(desk: Desk, rid) -> subprocess.Popen | None:
    """Start an approved release, when everything the principal saw still holds; None when it did not start
    (void, forbidden, back to waiting). Its forbidden-word scan is the request's when nothing it depends on
    changed since (scan_key); else it is scanned again, without the desk lock, and started only if what was
    scanned is still what goes out."""
    scanned = None
    for _ in range(SCAN_TRIES):
        with desk.lock():  # no merge lands, and no answer or request comes, between this look and the start
            r = get(desk, rid)
            if not r or r.get("state") != "approved":
                return None
            s = desk.settings()
            target = r.get("target") or ""
            why = _not_ready(desk, s, r)
            if why is not None:
                if why:
                    void(desk, rid, why[0], ("approved",), why[1])
                return None
            words = s.get("release_forbidden") or []
            try:
                key = scan_key(r["repo"], r.get("since") or "", r["commit"], words, r["command"])
            except RuntimeError as e:
                void(desk, rid, desk.redact(f"the commits going out cannot be read ({e})")[:400], ("approved",),
                     desk.redact(f"要发出去的提交读不出来（{e}）")[:400])
                return None
            found = (([], r.get("checked")) if key == r.get("scanned") else
                     scanned[1:] if scanned and scanned[0] == key else None)
            if found is not None:
                hits, checked = found
                if hits:  # a word the principal added since it was asked
                    change(desk, rid, lambda x: {"state": "forbidden", "hits": hits, "checked": checked,
                                                 "ended": now()})
                    desk.event("release_forbidden", id=rid, lane=r["lane"], target=target, commit=r["commit"],
                               found=len(hits))
                    return None
                token = f"hansa-release-{rid}-{uuid.uuid4().hex[:8]}"
                change(desk, rid, lambda x: {"state": "starting", "launch": token, "started": now(),
                                             "checked": checked, "scanned": key})
                if not r.get("fixed"):  # it holds the repository: launched before any merge can land
                    proc = _launch(desk, r, token, r["repo"])
                break
        try:  # without the lock: the server's round goes on meanwhile
            scanned = (key, *forbidden_in(r["repo"], r.get("since") or "", r["commit"], words, r["command"]))
        except RuntimeError as e:
            void(desk, rid, desk.redact(f"the commits going out cannot be read ({e})")[:400], ("approved",),
                 desk.redact(f"要发出去的提交读不出来（{e}）")[:400])
            return None
    else:
        return None  # what goes out kept changing while it was scanned: the next round tries again
    if r.get("fixed"):  # its own checkout of the commit, made without the lock: nothing waits for it
        path = checkout_dir(desk, rid)
        drop_checkout(desk, rid, r["repo"])  # one a crash left half made
        path.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "--detach", str(path), r["commit"], cwd=r["repo"])
        if (get(desk, rid) or {}).get("stop_asked"):  # stopped meanwhile: its command never runs
            finish(desk, rid, ran=False)
            return None
        proc = _launch(desk, r, token, path)
    desk.event("release_started", id=rid, lane=r["lane"], target=target, commit=r["commit"])
    return proc


def _launch(desk: Desk, r: dict, token: str, cwd) -> subprocess.Popen:
    """Run a release's command in cwd, its output to its log, and record it as running."""
    rid = r["id"]
    d = log_dir(desk)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.exit").unlink(missing_ok=True)
    env = {**without_session(os.environ), "HANSA_RELEASE": str(rid), "HANSA_RELEASE_COMMIT": r["commit"],
           "HANSA_RELEASE_TARGET": r.get("target") or "", "HANSA_RELEASE_REPO": r["repo"]}
    for k in ("HANSA_TASK", "HANSA_MERGING"):
        env.pop(k, None)
    with open(d / f"{rid}.log", "wb") as out:
        proc = subprocess.Popen(["/bin/sh", "-c", SCRIPT, token, r["command"], str(d / f"{rid}.exit")],
                                cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)
    change(desk, rid, lambda x: {"state": "running", "pid": proc.pid, "pid_start": proc_start(proc.pid)})
    return proc


def finish(desk: Desk, rid, ran: bool = True) -> dict | None:
    """After a release's command ended (ran False: a release stopped before its command ran): its exit code and its
    output, keys removed; done or failed, or stopped when it was stopped before its command ended. What its command
    started that still runs (also outside its process group) is stopped first."""
    r = get(desk, rid)
    if not r or r.get("state") not in ("starting", "running"):
        return None
    left = stop_recorded(_left(r), SWEEP_GRACE)
    if left:
        desk.event("release_left_running_stopped", id=rid, pids=left)
    if r.get("fixed"):  # its command ended: its own checkout goes, whatever the ending
        drop_checkout(desk, rid, r.get("repo"))
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
    limit, whose = r.get("limit_min"), r.get("limit_of") or "the lane's"
    problem = (f"stopped at {whose} time limit ({limit} min)" if r.get("timed_out") else
               r.get("lost") or ("" if code is not None else "it was interrupted before it could end (no exit code)"))
    ok = code == 0 and not problem

    def ending(x):
        if x.get("state") not in ("starting", "running"):
            return None
        # called off before its command ended (it got TERM), or before it ran: stopped, however it then ended. A stop
        # that came after its command had ended ends it as its exit says (stop_late)
        stopped = bool(x.get("stop_asked")) and (bool(x.get("termed")) or not ran)
        late = bool(x.get("stop_asked")) and not stopped
        return {"state": "stopped" if stopped else "done" if ok else "failed", "exit": code, "ran": ran,
                "problem": "" if stopped else problem, "ended": now(), "tail": text.strip().splitlines()[-TAIL:],
                "seconds": round(now() - (x.get("started") or now())), "pid": None, "pid_start": "",
                **({"stop_late": x.get("stop_late") or now()} if late else {})}
    r = change(desk, rid, ending)
    if r and r["state"] == "stopped":
        desk.event("release_stopped", id=rid, lane=r.get("lane"), target=r.get("target") or "", commit=r.get("commit"),
                   by=r.get("stopped_by") or "", reason=r.get("stop_reason") or "", ran=ran, exit=code,
                   killed=bool(r.get("killed")))
    elif r:
        desk.event("release_ended", id=rid, lane=r.get("lane"), target=r.get("target") or "", commit=r.get("commit"),
                   ok=ok, exit=code, problem=problem,
                   **({"stop_late": True, "stopped_by": r.get("stopped_by") or ""} if r.get("stop_late") else {}))
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


def tick(desk: Desk, s: dict, procs: dict, starting: bool = True) -> None:
    """hansa serve's round for releases: finish those that ended, stop those over their time limit, take
    over or finish those an earlier server left, and start approved ones, one per repository, none where a
    merge check runs (also while dispatch is paused: the principal said yes, and they use no provider). A release
    asked to stop gets TERM, then KILL after STOP_GRACE seconds, and is finished once nothing of it runs. What a
    running release's command starts is recorded each round, and what of it still runs is stopped at its end. None
    starts while starting is False (free space under the floor): an approved one waits, and goes once there is space."""
    for r in releases(desk):
        if r.get("state") == "running":
            r = _record_started(desk, r)
        if r.get("state") in ("starting", "running") and r.get("stop_asked"):
            _stopping(desk, r, procs.get(r["id"]))
    for rid, proc in list(procs.items()):
        if has_ended(proc):
            r = get(desk, rid) or {}
            if r.get("stop_asked") and r.get("termed") and _running(r, proc.pid):
                continue  # what it started still ends its step: KILL after the grace
            del procs[rid]
            stop(proc.pid, "", proc)  # what it left running in its group ends with it
            finish(desk, rid)
    for r in releases(desk):
        if r["id"] in procs:
            continue
        if r.get("state") == "running" and not alive(r.get("pid"), r.get("pid_start", "")):
            if r.get("stop_asked") and r.get("termed") and _running(r):
                continue
            finish(desk, r["id"])
        elif r.get("state") == "starting":
            _recover(desk, r)
    for r in releases(desk):
        if r.get("state") != "running" or r.get("timed_out"):
            continue
        limit, whose = minutes(s, r)
        if time.time() - (r.get("started") or time.time()) > 60 * limit:
            change(desk, r["id"], lambda x: {"timed_out": True, "limit_min": limit, "limit_of": whose})
            stop(r.get("pid"), r.get("pid_start", ""), procs.get(r["id"]))
    sweep_checkouts(desk)
    if not starting:
        return
    tasks = desk.tasks()
    # one at a time per repository, fixed or not: two releases of one repository may send to the same place
    held = {r.get("repo") for r in releases(desk) if r.get("state") in ("starting", "running")}
    for r in sorted((r for r in releases(desk) if r.get("state") == "approved"), key=lambda r: r["id"]):
        if r.get("repo") in held or (not r.get("fixed") and merging_on(desk, s, r.get("repo"), tasks)):
            continue
        try:
            proc = start(desk, r["id"])
        except Exception as e:  # noqa: BLE001 - say: nothing went out, and the release says why
            why = desk.redact(f"it could not start: {e}")[:600]
            if change(desk, r["id"], lambda x: {"state": "failed", "problem": why, "ended": now()}
                      if x.get("state") in ("approved", "starting") else None):
                desk.event("release_ended", id=r["id"], lane=r.get("lane"), target=r.get("target") or "",
                           commit=r.get("commit"), ok=False, exit=None, problem=why)
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
        return (f"等同一仓库上{what_zh(other)}的发布 #{other['id']} 结束" if zh else
                f"waits for release #{other['id']} of {what(other)} on the same repository")
    m = not r.get("fixed") and merging_on(desk, s, r.get("repo"), tasks)
    if m:
        return (f"等同一仓库上工作线 {m.get('lane')} 的 {m['id']} 合并验收结束" if zh else
                f"waits for {m['id']}'s merge check on lane {m.get('lane')}, on the same repository")
    from .run import not_looking
    from . import disk
    low = disk.held(desk)
    if low:
        return (f"等磁盘空出来：有卷的剩余空间低于下限（{disk.floor_text(disk.floor(s))}），hansa serve 暂不开始新的事"
                if zh else "waits: " + disk.short_text(desk, low))
    gone = not_looking(desk)
    if zh:
        return "后台（hansa serve）没在运行或卡住了，开始不了" if gone else "hansa serve 下次查看时开始（几秒内）"
    return gone or "starts when hansa serve next looks (within seconds)"


def state_text(desk: Desk, r: dict, s: dict | None = None, zh: bool = False) -> str:
    """A release's state in words (zh: in Chinese, for the page)."""
    st = r.get("state")
    coded = r.get("channel") == "code"
    if st == "waiting":
        code = code_text(r, zh)
        if zh:
            return "等你在网页上点头" + (f"，或在对话里把提醒里的一次性码告诉协调者（{code}）" if code_live(r) else
                                       f"（{code}）" if code else "")
        return "waiting for the principal's yes on the page" + (
            f", or the one-time code of their alert said in the conversation ({code})" if code_live(r) else
            f" ({code})" if code else "")
    if st == "approved":
        if zh:
            how = ("不用问你就发布（这个发布目标在你的清单上）" if r.get("without_asking")
                   else f"你在 {when(r.get('answered'))} 点了头" + ("（在对话里用提醒里的一次性码，经协调者）" if coded else ""))
            return f"{how}；{waits_text(desk, s or desk.settings(), r, zh=True)}"
        how = "released without asking (it is on the principal's list)" if r.get("without_asking") \
            else f"the principal said yes {when(r.get('answered'))}" + (
                " (through the coordinator, with the one-time code of their alert)" if coded else "")
        return f"{how}; {waits_text(desk, s or desk.settings(), r)}"
    if st in ("starting", "running"):
        if r.get("stop_asked"):
            return (f"从 {when(r.get('started'))} 起在运行；{_who(r, zh)}在 {when(r.get('stop_asked'))} 叫停了"
                    + (f"（{r['stop_reason']}）" if r.get("stop_reason") else "")
                    + f"，正在停（先发 TERM，{STOP_GRACE} 秒后对还在运行的发 KILL）" if zh else
                    f"running since {when(r.get('started'))}; being stopped by {_who(r)} since "
                    f"{when(r.get('stop_asked'))}" + (f" ({r['stop_reason']})" if r.get("stop_reason") else "")
                    + f": TERM to its command, KILL {STOP_GRACE} s later to what still runs")
        return f"从 {when(r.get('started'))} 起在运行" if zh else f"running since {when(r.get('started'))}"
    if st == "stopped":
        ran = r.get("ran", bool(r.get("started")))  # one stopped as its fixed checkout was made never ran
        if zh:
            return (f"{_who(r, zh)}在 {when(r.get('ended'))} 叫停了" + (f"：{r['stop_reason']}" if r.get("stop_reason") else "")
                    + ("（命令已运行，留下了到那时的输出；服务器可能停在半路）" if ran else "；什么都没运行"))
        return (f"stopped by {_who(r)} {when(r.get('ended'))}" + (f": {r['stop_reason']}" if r.get("stop_reason") else "")
                + ("; its command had run (its output so far is kept; the server may be half-switched)" if ran
                   else "; nothing was run"))
    late = ((f"；{_who(r, zh)}在 {when(r.get('stop_asked'))} 叫停时，它的命令已经结束了" if zh else
             f"; the stop {_who(r)} asked at {when(r.get('stop_asked'))} came after its command had ended")
            if r.get("stop_late") else "")
    if st == "done":
        return (f"{when(r.get('ended'))} 完成（退出码 0，{r.get('seconds', 0)} 秒）{late}" if zh else
                f"done {when(r.get('ended'))} (exit 0, {r.get('seconds', 0)} s){late}")
    if st == "failed":
        if zh:
            return (f"{when(r.get('ended'))} 失败" + (f"（退出码 {r['exit']}）" if r.get("exit") is not None else "")
                    + (f"：{r['problem']}" if r.get("problem") else "") + late)
        return (f"FAILED {when(r.get('ended'))}" + (f" (exit {r['exit']})" if r.get("exit") is not None else "")
                + (f": {r['problem']}" if r.get("problem") else "") + late)
    if st == "refused":
        if zh:
            return (f"你在 {when(r.get('answered'))} 拒绝了" + ("（在对话里用提醒里的一次性码，经协调者）" if coded else "")
                    + (f"：{r['reason']}" if r.get("reason") else ""))
        return (f"refused by the principal {when(r.get('answered'))}"
                + (" (through the coordinator, with the one-time code of their alert)" if coded else "")
                + (f": {r['reason']}" if r.get("reason") else ""))
    if st == "void":
        return f"作废：{r.get('void_zh') or r.get('void')}；什么都没运行" if zh else f"void: {r.get('void')}; nothing was run"
    if st == "forbidden":
        n = len(r.get("hits", []))
        return (f"被拒绝：要发出去的内容里有你不许发出去的字眼（{n} 处）" if zh else
                f"refused: what goes out holds words the principal keeps from going out ({n} place(s))")
    return st or "?"


def _who(r: dict, zh: bool = False) -> str:
    by = r.get("stopped_by") or ""
    if zh:
        return "你" if by == "page" else "协调者" if by == "coordinator" else by or "?"
    return "the principal" if by == "page" else "the coordinator" if by == "coordinator" else by or "?"


def checked_text(r: dict) -> str:
    """How many outgoing commits were checked for forbidden words, and how many of them the request shows."""
    n = r.get("checked", r.get("count", 0))
    shown = len(r.get("commits") or [])
    return (f"{n} outgoing commit(s) checked for forbidden words" + (f" (the first {shown} shown)" if shown < n else ""))


def runs_text(t: dict, zh: bool = False) -> str:
    """Which way a target (or a release of it) runs, in words."""
    if t.get("fixed"):
        return ("在为它做的提交固定检出里运行，不占仓库" if zh else
                "runs from a fixed checkout of the commit, holding nothing")
    return "在仓库里运行，运行时占着仓库" if zh else "runs in the repository, holding it while it runs"


def line(desk: Desk, r: dict, s: dict | None = None) -> str:
    return (f"#{r['id']} {what(r)} at {r.get('commit', '')[:10]} ({r.get('base')}): "
            f"{state_text(desk, r, s)}")


def list_text(desk: Desk) -> str:
    rs = releases(desk)
    if not rs:
        return ("no releases yet. A lane's release command is set with hansa lane set L release \"<command>\", or its "
                "named targets with hansa lane target L NAME \"<command>\"; hansa release L [TARGET] [note] asks for one")
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
            out.append(f"      command: {r.get('command')} ({runs_text(r)})" + (f" · note: {r['note']}" if r.get("note") else ""))
            out.append(f"      {checked_text(r)}")
            if r.get("state") in ("done", "failed", "stopped") and r.get("tail"):
                out += [f"      | {x}" for x in r["tail"][-5:]]
            if r.get("state") == "forbidden":
                out += ["    " + x for x in hits_text(r.get("hits", [])).splitlines()]
    out.append("the whole output of a release: .hansa/releases/<id>.log (the principal's; hansa release shows its last lines)")
    return "\n".join(out)


def lane_lines(desk: Desk, name: str, ln: dict) -> list:
    """For hansa explain lane: the release targets and the last release of each."""
    s = desk.settings()
    targets = lane_targets(ln)
    how = {False: f"it runs in {ln['repo']} at the base branch's commit, as the desk's account, outside the sandbox, "
                  "with hansa's environment (not the lane's env), holding the repository (merges there wait for it), "
                  "only after the principal says yes on the page",
           True: "it runs from a checkout of the base branch's commit made for it (removed when it ends), as the "
                 "desk's account, outside the sandbox, with hansa's environment (not the lane's env), holding nothing "
                 f"(merges in {ln['repo']} go on meanwhile), only after the principal says yes on the page"}
    files = ln.get("release_files") or []
    listed = (["  release files (built and run by its releases outside the sandbox; hansa merge refuses a change "
               "touching one until a verification of it holds): " + " ".join(files)] if files else
              [f"  release files: none; advised, since its releases build and run what is on the base outside the "
               f"sandbox: hansa lane set {name} release_files \"pom.xml **/pom.xml build.sh tools/release.sh\" (the "
               f"build files and release scripts), and a change touching one is merged only once a verification of "
               f"it holds"] if targets else [])
    if not targets:
        return [f"  release: none (set one with hansa lane set {name} release \"<command>\", or name targets with "
                f"hansa lane target {name} NAME \"<command>\")", "  last release: none", *listed]
    out = []
    for target, t in sorted(targets.items()):
        rs = [r for r in releases(desk) if r.get("lane") == name and (r.get("target") or "") == target]
        limit = t.get("timeout_min") or ln.get("timeout_min", 240)
        head = f"  release target {target}" if target else "  release"
        out += [f"{head}: {t.get('command')} (hansa release {name}{' ' + target if target else ''} asks for it; "
                f"{how[bool(t.get('fixed'))]}; {limit} min as its limit"
                + (f"; only for a commit target {t['after']} already sent out" if t.get("after") else "")
                + (": the principal lets it go out without asking" if lane_listed(s, name, target)
                   else unlisted(s, name, target)) + ")",
                f"  last release{' of ' + target if target else ''}: " + (line(desk, rs[-1], s) if rs else "none")]
    return out + listed


def unlisted(s: dict, name: str, target: str = "") -> str:
    """For hansa explain lane: a target on the principal's list that no longer goes out without asking, and why."""
    why = next((listed_mismatch(s, x) for x in s.get("releases_without_asking") or []
                if listed_key(x) == (name, target)), "")
    return (f": it is on the principal's list of releases without asking, but {why}, so its releases wait for the "
            "yes until the principal takes it back in on the page (its button for that, or unticking and ticking "
            "it again, then Save; a plain Save keeps the old entry)") if why else ""


def page_rows(desk: Desk, s: dict, tasks=None) -> list:
    """For the page and hansa status: the open releases and the latest ended ones of the past week."""
    rs = releases(desk)
    week = time.time() - 7 * 86400
    shown = [r for r in rs if r.get("state") in OPEN] + [r for r in rs if r.get("state") not in OPEN
                                                           and (r.get("ended") or r.get("asked") or 0) >= week][-5:]
    out = []
    for r in shown:
        row = {k: r.get(k) for k in ("id", "lane", "target", "base", "commit", "command", "note", "state", "asked",
                                     "answered",
                                     "reason", "void", "exit", "problem", "count", "commits", "files", "files_count",
                                     "named", "hits", "ended", "without_asking", "since_release", "fixed",
                                     "stop_asked", "stopped_by", "stop_reason", "answered_by", "channel")}
        c = r.get("code") or {}  # whether a code went out, and when; never the code, nor its hash
        row["code_sent"] = c.get("sent") if code_live(r) else None
        waits = r.get("state") == "waiting"
        row["code"], row["code_zh"] = (code_text(r), code_text(r, zh=True)) if waits else ("", "")
        row["runs"], row["runs_zh"] = runs_text(r), runs_text(r, zh=True)
        row["checked"] = r.get("checked", r.get("count"))
        row["what"], row["what_zh"] = state_text(desk, r, s), state_text(desk, r, s, zh=True)
        row["tail"] = (r.get("tail") or [])[-TAIL:]
        if r.get("state") == "waiting":
            row["moved"], row["moved_zh"] = stale(desk, s, r), stale(desk, s, r, zh=True)
        out.append(row)
    return out
