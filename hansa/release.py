"""Releases: how a lane's base branch goes out (a push, a wait for CI, a deploy), run by hansa only after the
principal says yes on the page.

A lane has release targets: the ways its base branch goes out (`hansa lane target L NAME "..."`), each with its own
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
recorded when the page saved it, while the desk is not paused and its direct commits do not require the yes.
hansa serve runs an approved
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

A target's optional watch runs after exit 0, in the release's own checkout, with the same environment. Its
watch_run has a separate state and process group: it never holds the repository's queue, and a server restart
starts an unfinished watch again. hansa posts passed, failed or timed out to the release's coordinator session.

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

import codecs
import contextlib
import glob
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
import threading
import time
import uuid
from pathlib import Path

from . import outside, without_session
from .desk import (WRITE_FILE, Desk, _is_int, forbidden_exceptions_ok, forbidden_pattern, lane_targets, merge_targets_problem, listed_key,
                   listed_mismatch, now, read_json, target_name, write_json, write_text)
from .run import (LOOK_LIMIT, SWEEP_GRACE, CannotLook, _state, alive, git, group_running, has_ended, leader,
                  oom_begin, oom_result, oom_text,
                  run_running, save_stopped, serve_step, recording_start, retry_start_failure,
                  merge_check_lane, merge_target, pids_with_arg, proc_start, record_descendants, stop, stop_recorded)

OPEN = ("waiting", "approved", "starting", "running")
WATCH_OPEN = ("waiting", "starting", "running")
MOST_COMMITS = 200  # of the outgoing commits kept on the request (all are checked for forbidden words)
MOST_FILES = 500
MOST_HITS = 20
MOST_MERGES = 10  # newest landings named in an automatic request (the boundary and counts cover all)
SCAN_TRIES = 5  # scans of what goes out that may each find it changed by the time it is recorded
PATTERN_LINE_LIMIT = 2.0  # seconds on one line, across its patterns; not a setting
PATTERN_SCAN_LIMIT = 60.0  # seconds matching patterns in one scan; end a stall, not pace ordinary releases
PATTERN_TIMER_INTERVAL = 0.25
PATTERN_LINE_LENGTH = 10000  # bound re loops that do not check signals on Python 3.9
_UNRUN_PATTERN = object()
TAIL = 15
STOP_GRACE = 30  # seconds between the TERM a stopped release's command gets and the KILL to what still runs
# $0 is the launch word, which finds the run again after hansa serve restarts; the exit code goes in a file
SCRIPT = WRITE_FILE + '/bin/sh -c "$1"; write_file "$2" "$?"'
# a hunk of a patch, or of a merge's combined diff (one more @ and one more -range per parent)
HUNK = re.compile(r"^(@@+) ((?:-\d+(?:,\d+)? )+\+\d+(?:,\d+)?) @@")
RANGE = re.compile(r"[-+]\d+(?:,(\d+))?")
MAILBOX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_MAILBOX_DOMAIN_END = object()  # An empty label in legacy settings is still a label.


def mailbox_domains(domains) -> dict:
    """Index suffixes once per scan, by labels from the domain's end."""
    tree = {}
    for domain in domains:
        node = tree
        for label in reversed(domain.split(".")):
            node = node.setdefault(label, {})
        node[_MAILBOX_DOMAIN_END] = True
    return tree


def mailbox_hits(line: str, addresses=frozenset(), domains=frozenset()) -> list:
    """Mailbox-shaped strings outside reserved example domains and the principal's allowlist."""
    hits = []
    tree = domains if isinstance(domains, dict) else mailbox_domains(domains)
    for mailbox in MAILBOX.findall(line):
        domain = mailbox.rsplit("@", 1)[1].casefold()
        if (any(domain == d or domain.endswith("." + d) for d in ("example.com", "example.org", "example.net"))
                or domain.endswith((".invalid", ".test", ".example"))):
            continue
        if mailbox.casefold() in addresses:
            continue
        node = tree
        for label in reversed(domain.split(".")):
            node = node.get(label, {})
            if _MAILBOX_DOMAIN_END in node or not node:
                break
        if _MAILBOX_DOMAIN_END in node:
            continue
        hits.append(mailbox)
    return hits


def lane_exceptions(settings: dict, lane: str) -> list:
    """Entries excepted on this task or release's lane, spelled exactly as on the principal's list."""
    exceptions = settings.get("release_forbidden_except", [])
    if not forbidden_exceptions_ok(exceptions):
        return []
    return [x["entry"] for x in exceptions if lane in x["lanes"]]


def lane_words(settings: dict, lane: str) -> list:
    excepted = set(lane_exceptions(settings, lane))
    return [w for w in settings.get("release_forbidden") or [] if w not in excepted]


def word_matchers(words: list) -> list:
    """Compile patterns once per scan; literals keep their casefolded substring matching."""
    matchers = []
    for word in words or []:
        if isinstance(word, str) and word.strip():
            pattern = (forbidden_pattern(word)[0] if threading.current_thread() is threading.main_thread()
                       else _UNRUN_PATTERN) if word.startswith("re:") else None
            matchers.append((word, pattern if pattern is not None else word.casefold()))
    return matchers


def matches(matcher, line: str, folded: str) -> bool:
    if not isinstance(matcher, re.Pattern):
        return matcher in folded
    return bool(matcher.search(line))


class PatternLimit(Exception):
    def __init__(self, hit):
        self.hit = hit
        super().__init__(hit["reason"])


class PatternScan:
    """Only a running search may raise from SIGALRM; git reads and desk writes are never interrupted.

    Keep the timer for the whole scan and restore its caller's signal state on every exit.
    Off the main thread, refuse without running patterns.
    """
    def __init__(self, words):
        self.matchers = word_matchers(words)
        self.main_thread = threading.current_thread() is threading.main_thread()
        self.running = False
        self.matching_time = 0.0
        self.armed = False
        self.hits = []
        self.checked = 0

    def __enter__(self):
        if self.main_thread and any(isinstance(m, re.Pattern) for _, m in self.matchers):
            self.handler = signal.getsignal(signal.SIGALRM)
            self.timer = signal.getitimer(signal.ITIMER_REAL)
            signal.signal(signal.SIGALRM, self._alarm)
            signal.setitimer(signal.ITIMER_REAL, PATTERN_TIMER_INTERVAL, PATTERN_TIMER_INTERVAL)
            self.armed = True
        return self

    def __exit__(self, *_):
        if self.armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self.handler)
            signal.setitimer(signal.ITIMER_REAL, *self.timer)

    def line(self, line, sha="", where=""):
        self.line_started = time.monotonic()
        self.too_long = len(line) > PATTERN_LINE_LENGTH
        if self.too_long:
            word = next((w for w, m in self.matchers if isinstance(m, re.Pattern) or m is _UNRUN_PATTERN), None)
            if word:
                self.hits.append({"word": word, "commit": sha or "", "where": where,
                                  "line": line.strip()[:300], "kind": "pattern_limit", "code": "long",
                                  "reason": f"the line is longer than {PATTERN_LINE_LENGTH:,} characters, "
                                            "so patterns could not check it"})

    def _alarm(self, *_):
        if self.running:
            self._check()

    def _check(self):
        elapsed = time.monotonic()
        if not self.main_thread:
            code, seconds = "thread", None
            reason = ("patterns were not run: this scan is off the main thread, so the scan stopped there: "
                      "this line and the rest were not checked")
        elif self.matching_time + (elapsed - self.search_started) >= PATTERN_SCAN_LIMIT:
            code, seconds = "scan", PATTERN_SCAN_LIMIT
            reason = f"the patterns took over {PATTERN_SCAN_LIMIT:g} s in this scan, so the rest was not checked"
        elif elapsed - self.line_started >= PATTERN_LINE_LIMIT:
            code, seconds = "line", PATTERN_LINE_LIMIT
            reason = (f"the pattern took over {PATTERN_LINE_LIMIT:g} s on this line, so the scan stopped there: "
                      "this line and the rest were not checked")
        else:
            return
        raise PatternLimit({**self.place, "kind": "pattern_limit", "code": code,
                            "seconds": seconds, "reason": reason})

    def search(self, word, matcher, line, folded, sha="", where=""):
        if not isinstance(matcher, re.Pattern) and matcher is not _UNRUN_PATTERN:
            return matches(matcher, line, folded)
        if self.too_long:
            return False
        self.place = {"word": word, "commit": sha or "", "where": where, "line": line.strip()[:300]}
        self.search_started = time.monotonic()
        try:
            self.running = True
            self._check()
            found = matches(matcher, line, folded)
            self._check()
            return found
        finally:
            self.running = False
            self.matching_time += time.monotonic() - self.search_started


def releases_file(desk: Desk) -> Path:
    return desk.state_dir / "releases.json"


def log_dir(desk: Desk) -> Path:
    return desk.state_dir / "releases"


def releases(desk: Desk) -> list:
    rs = read_json(releases_file(desk), [])
    return [{**r, "target": r.get("target") or "default"} for r in rs if isinstance(r, dict)] if isinstance(rs, list) else []


def get(desk: Desk, rid) -> dict | None:
    return next((r for r in releases(desk) if r.get("id") == rid), None)


def _write(desk: Desk, rs: list) -> None:
    desk.state_dir.mkdir(mode=0o700, exist_ok=True)
    write_json(releases_file(desk), rs)
    desk.writes += 1


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


def lane_listed(s: dict, lane: str, target: str = "default") -> bool:
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
    return f"工作线 {r.get('lane')}" + (f" 的发布目标 {r['target']}" if r.get("target") else "")


def ask_again(r: dict) -> str:
    """The command that asks for the same release again."""
    return f"hansa release {r.get('lane')}" + (f" {r['target']}" if r.get("target") else "")


def minutes(s: dict, r: dict) -> tuple:
    """A release's time limit in minutes, and whose it is ('the lane's' or 'target T's'): the target's own, else
    the lane's timeout_min."""
    ln = s["lanes"].get(r.get("lane")) or {}
    own = (lane_targets(ln).get(r.get("target") or "") or {}).get("timeout_min")
    if _is_int(own, 1, 1440):
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
        return (f"工作线 {lane} 的发布目标 {target} 要等发布目标 {after} 先发出同一个提交才能发出，而"
                + (f" {after} 上一次发出的发布（#{prev['id']}）是 {prev['commit'][:10]}，不是 {commit[:10]}"
                   if prev else f"发布目标 {after} 还没有从 {base} 发出过发布")
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
    names = _git_bytes(repo, "log", "--cc", "--format=", "--name-only", "--no-renames", "-z", *rng)
    return commits, sorted({x for x in names.split("\0") if x})


def hansa_merges(desk: Desk, s: dict, a: dict, tasks=None) -> set:
    """Landings in this repository and base, from every lane; status supplies its task snapshot."""
    found = set()
    for m in desk.tasks() if tasks is None else tasks:
        if not m.get("merge_commit"):
            continue
        ln = s["lanes"].get(m.get("lane"), {})
        repo = m.get("merge_repo") or m.get("repo") or ln.get("repo")
        base = m.get("merge_base") or ln.get("base")
        if repo and repo_key(repo) == repo_key(a["repo"]) and (base or "main") == a["base"]:
            found.add(m["merge_commit"])
    return found


def direct_commits(repo, since: str, commit: str, commits: list, merges: set) -> list:
    """Outgoing first-parent commits other than hansa landings, and what their merges brought in."""
    rows = _git_bytes(repo, "log", "--first-parent", "--format=%H %P", *_range(since, commit))
    line = [row.split() for row in rows.splitlines()]
    if not since and not _git_bytes(repo, "for-each-ref", "--format=%(refname)", "refs/remotes").strip():
        oldest = [i for i, row in enumerate(line) if row[0] in merges]
        if oldest:
            line = line[:oldest[-1] + 1]  # before hansa's first landing is the repository's earlier history
    direct = set()
    for row in line:
        sha, *parents = row
        if sha in merges:
            continue
        direct.add(sha)
        if len(parents) > 1:
            # Subtract only this merge's first parent, never all the ancestors of a later hansa merge:
            # those can include direct commits already on the base.
            direct.update(_git_bytes(repo, "rev-list", sha, f"^{parents[0]}", "--").splitlines())
    return [c for c in commits if c["sha"] in direct]


@contextlib.contextmanager
def _binary_blobs(repo):
    """One batch reader per scan; only blobs with a NUL in their first 8,000 bytes are binary, regardless of
    attributes. Other objects' bodies are consumed to keep the next answer aligned; missing objects have none."""
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(["git", "cat-file", "--batch"], cwd=str(repo), stdout=subprocess.PIPE,
                             stderr=err, stdin=subprocess.PIPE)

        def binary(blob):
            p.stdin.write((blob + "\n").encode("ascii"))
            p.stdin.flush()
            header = p.stdout.readline().split()
            if len(header) != 3:  # missing (or another answer without an object body)
                return False
            size = int(header[2])
            prefix = p.stdout.read(min(size, 8000))
            left = size - len(prefix)
            while left:
                chunk = p.stdout.read(min(left, 65536))
                if not chunk:
                    raise RuntimeError(f"git cat-file --batch: incomplete object {blob}")
                left -= len(chunk)
            if p.stdout.read(1) != b"\n":
                raise RuntimeError(f"git cat-file --batch: incomplete object {blob}")
            return header[1] == b"blob" and b"\0" in prefix

        try:
            yield binary
        finally:
            p.stdin.close()
            p.stdout.close()
            p.kill()
            p.wait()


def _git_path(path: str) -> str:
    """A path in git's line-oriented output: quotePath=false still quotes quotes, backslashes and controls.
    Git's C escapes describe UTF-8 bytes; use NUL-separated output instead where no patch headers are needed."""
    if path.startswith('"') and path.endswith('"'):
        return codecs.escape_decode(path[1:-1].encode("utf-8"))[0].decode("utf-8", "replace")
    return path


def _added(lines, binary_blob):
    """(commit, file, line) for each line the patches of `git log -p --cc --format=%x00%H -U0` add: an ordinary
    commit's added lines, and a merge's lines in none of its parents (what the merge itself adds, such as a
    conflict's resolution; a line that came from a parent is that parent's commit's). Only binary blob results
    are skipped; --full-index supplies new object IDs, including in a combined diff."""
    sha = path = None
    blob, binary = None, False
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
            if marks == "+" * n and not binary:
                yield sha, path, text
            continue
        if line.startswith("\0"):
            sha, path = line[1:].strip(), None
        elif line.startswith("diff "):
            blob, binary = None, False
        elif line.startswith("index "):
            blob = line.split()[1].split("..")[-1]
        elif line.startswith("+++ "):
            p = _git_path(line[4:].rstrip("\t"))
            path = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
            binary = binary_blob(blob) if path else False
        else:
            m = HUNK.match(line)
            if m:
                n = len(m[1]) - 1
                sides = [int(c) if c else 1 for c in RANGE.findall(m[2])]
                if len(sides) == n + 1:
                    old, new = sides[:n], sides[n]


def forbidden_in(repo, since: str, commit: str, words: list, command: str = "", mailboxes: bool = False,
                 mailbox_allow=()) -> tuple:
    """Where what goes out holds one of the principal's forbidden words (any case, within one line, in text read as
    UTF-8): the release command line, and every outgoing commit's message, its author's and committer's names and
    emails (where: author or committer; line: Name <email>), the names of the files it adds or
    changes (a renamed one by its new name) and the lines it adds. Each commit's own change is read once: a merge's
    is what it adds beyond all its parents (git's dense combined diff, --cc), not its diff against each parent, so
    what a merge brings in from a branch is read in that branch's commits when they go out, and not at all when
    they are on a remote-tracking branch, already out. ([{word, commit, where, line}], each place once, at most
    MOST_HITS; how many commits were checked)."""
    return words_in(repo, _range(since, commit), words, [("release command", command)], mailboxes, mailbox_allow)


def words_in(repo, rng: list, words: list, texts=(), mailboxes: bool = False, mailbox_allow=()) -> tuple:
    """forbidden_in for any range of commits (git log's arguments, ending in --), and for texts that are not in a
    commit yet ([(where, text)], each line looked at, commit ''): a merge's message before it is made, say.
    mailboxes: also check mailbox-shaped strings on every added line against example domains and the allowlist."""
    with PatternScan(words) as scan:
        try:
            return _words_in(repo, rng, texts, mailboxes, mailbox_allow, scan)
        except PatternLimit as e:
            return scan.hits[:MOST_HITS - 1] + [e.hit], scan.checked


def _words_in(repo, rng, texts, mailboxes, mailbox_allow, scan):
    allow = [entry.casefold() for entry in mailbox_allow] if mailboxes else []
    addresses = {entry for entry in allow if not entry.startswith("@")}
    domains = mailbox_domains({entry[1:] for entry in allow if entry.startswith("@")})
    matchers = scan.matchers
    hits, seen, checked = scan.hits, set(), 0
    mailboxes_seen = set()

    def look(sha, where, line):
        scan.line(line, sha, where)
        low = line.casefold()
        for w, matcher in matchers:
            if scan.search(w, matcher, line, low, sha, where) and (where, line) not in seen:
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
            scan.checked = checked
        elif matchers:
            look(sha, "commit message", line)
    if not matchers and not mailboxes:
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
            look(sha, "file name", _git_path(line))
    patch = _git_lines(repo, "log", "-p", "--cc", "--text", "--no-ext-diff", "--no-textconv", "--no-color",
                       "--unified=0", "--no-renames", "--full-index", "--format=%x00%H", *rng)
    try:
        with _binary_blobs(repo) as binary_blob:
            for sha, path, line in _added(patch, binary_blob):
                look(sha, path or "?", line)
                if mailboxes:
                    for mailbox in mailbox_hits(line, addresses, domains):
                        place = (sha, path, line, mailbox)
                        if place not in mailboxes_seen:
                            mailboxes_seen.add(place)
                            hits.append({"kind": "mailbox", "word": mailbox, "commit": sha or "", "where": path or "?",
                                         "line": line.strip()[:300]})
                if len(hits) >= MOST_HITS:
                    break
    finally:
        patch.close()
    return hits[:MOST_HITS], checked


def command_hits(ln: dict, words: list, only: str | None = None) -> list:
    """The forbidden words in a lane's release targets' command lines (any case, within one line), each
    (target, word, line) once, with a fourth item for a pattern-limit hit; only: that one target's ("" the unnamed one)."""
    out = []
    with PatternScan(words) as scan:
        try:
            for target, t in sorted(lane_targets(ln).items()):
                if only is not None and target != only:
                    continue
                for line in str(t.get("command") or "").splitlines():
                    before = len(scan.hits)
                    scan.line(line, where="release command")
                    low = line.casefold()
                    w = next((w for w, matcher in scan.matchers
                              if scan.search(w, matcher, line, low, where="release command")), None)
                    if w:
                        out.append((target, w, line.strip()[:300]))
                    for hit in scan.hits[before:]:
                        out.append((target, hit["word"], hit["line"] + "; " + pattern_limit_text(hit), hit))
        except PatternLimit as e:
            out.append((target, e.hit["word"], e.hit["line"] + "; " + pattern_limit_text(e.hit), e.hit))
    return out


def command_hits_text(desk: Desk, lane: str, hits: list) -> str:
    """What hansa says when a command it has just set holds a forbidden word or could not be checked."""
    out = []
    for hit in hits:
        t, w, line = hit[:3]
        target = f"lane {lane}'s {'release target ' + t if t else 'release command'}"
        if len(hit) > 3:
            out.append(f'{target}\'s command line could not be checked against pattern "{w}": {line}')
        else:
            out.append(f'{target} holds "{w}", a word the principal keeps from going out: {line}')
    return desk.redact("Warning: " + "; ".join(out)
        + ". The setting is kept, but hansa refuses every release whose command line holds such a word "
          "or could not be checked: change the command, or ask the principal about the word")


def hits_text(hits: list, zh: bool = False) -> str:
    out = []
    for limit, heading in ((False, "查到的禁发字眼" if zh else "Words found that must not go out"),
                           (True, "这一行无法检查" if zh else "The line could not be checked")):
        group = [h for h in hits if (h.get("kind") == "pattern_limit") == limit]
        if group:
            out.append(heading + ":")
            out.extend(f"  {h['commit'][:10]} {h['where']}: {h['line']}  (\"{h['word']}\")"
                       + ("; " + pattern_limit_text(h, zh) if limit else "") for h in group)
    return "\n".join(out)


def pattern_limit_text(hit, zh: bool = False):
    if zh:
        seconds = hit.get("seconds") or (PATTERN_SCAN_LIMIT if hit.get("code") == "scan" else PATTERN_LINE_LIMIT)
        reason = {"long": "这一行超过 10,000 字符，因此正则无法检查它",
                  "line": f"正则在这一行耗时超过 {seconds:g} 秒，因此检查到此停止，这一行及剩余内容未检查",
                  "scan": f"本次检查中正则的总耗时超过 {seconds:g} 秒，因此剩余内容未检查",
                  "thread": "本次检查不在主线程上，正则未运行，因此检查到此停止，这一行及剩余内容未检查"}.get(hit.get("code"), hit["reason"])
        return reason + "。请缩短或拆分这一行，或请负责人修改正则（hansa ask）"
    return (hit["reason"] + ". Shorten or split the line, or ask the principal to change the pattern (hansa ask)")


def mailbox_refusal(hits: list, tid: str = "") -> str:
    """Mailbox-specific reason and remedy; empty for ordinary forbidden-word hits."""
    if not any(h.get("kind") == "mailbox" for h in hits):
        return ""
    reason = "a mailbox does not use a reserved example domain and is not on the principal's allowlist"
    if any(h.get("kind") not in ("mailbox", "pattern_limit") for h in hits):
        reason += "; the change also holds words the principal keeps from going out"
    retry = (f'Replace it with an example address in hansa retry {tid} --fresh "..." '
             f"(a retry that continues from {tid} keeps its commits)" if tid else
             "Replace it with an example address in the outgoing history")
    return (f"{reason}\n{hits_text(hits)}\n{retry} (example.com, example.org, example.net, or a domain ending in "
            ".invalid, .test or .example), or, if the project needs the address, ask the principal to add it or "
            "its domain to the allowlist on the page (hansa ask). "
            "More: hansa help forbidden-words")


def title_hits(title: str, words: list, detailed: bool = False) -> list:
    """The forbidden words in a task's title, which hansa puts in its commit message and its merge's: each
    word once, any case."""
    seen = set()
    with PatternScan(words) as scan:
        hits = scan.hits
        try:
            for line in (title or "").splitlines():
                scan.line(line, where="title")
                low = line.casefold()
                for w, matcher in scan.matchers:
                    if w not in seen and scan.search(w, matcher, line, low, where="title"):
                        seen.add(w)
                        hits.append({"word": w, "commit": "", "where": "title", "line": line.strip()[:300]})
        except PatternLimit as e:
            hits.append(e.hit)
    return hits if detailed else [h["word"] for h in hits]


def named_files(command: str, files: list) -> list:
    """The changed files the command line names (./deploy.sh, scripts/release.sh, or by its name alone): what
    the release runs is in the change."""
    edge = r"""(?:^|[\s/'"=;&|()<>`])""", r"""(?=$|[\s'";&|()<>`])"""
    return [f for f in files if any(re.search(edge[0] + re.escape(x) + edge[1], command)
                                    for x in dict.fromkeys([f, os.path.basename(f)]) if x)]


def last_out(desk: Desk, lane: str, repo: str, base: str, target: str = "") -> dict | None:
    """The last release of the lane's target that went out, of this repository and base branch (a request recorded
    before targets had names is the lane's unnamed target's)."""
    done = [r for r in releases(desk) if r.get("lane") == lane and (r.get("target") or "") == (target or "default")
            and r.get("state") == "done" and r.get("repo") == repo_key(repo) and r.get("base") == base]
    return done[-1] if done else None


def _has_commit(repo, sha: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=str(repo), capture_output=True,
                          stdin=subprocess.DEVNULL).returncode == 0


# -- asking ------------------------------------------------------------------------------------------------------
def repository_input(command: str, repository, commit: str, fixed: bool = False, directory=None) -> tuple:
    """Check recognized repository inputs. Fixed relative inputs are checked in their actual checkout."""
    root = Path(repository).resolve()
    cwd = Path(directory).resolve() if directory else root
    entries = {}

    def load(repository, revision):
        result = subprocess.run(["git", "ls-tree", "-r", "-z", revision], cwd=repository, capture_output=True,
                                stdin=subprocess.DEVNULL)
        if result.returncode:
            raise RuntimeError("cannot read release input tree")
        for row in filter(None, os.fsdecode(result.stdout).split("\0")):
            info, name = row.split("\t", 1)
            _, kind, oid = info.split()
            path = repository / name
            if kind == "blob":
                entries[path] = repository, revision, name
            else:  # An initialized submodule's files are bound by its recorded commit.
                try:
                    if not git("rev-parse", "--show-prefix", cwd=path):
                        load(path, oid)
                except (OSError, RuntimeError):
                    pass
    load(root, commit)
    if fixed and directory and root in cwd.parents:
        load(cwd, commit)

    def dependencies(candidate):
        candidate = cwd / candidate
        try:
            resolved = candidate.resolve(strict=True)
            if not resolved.is_file() or root not in resolved.parents:
                return []
        except (OSError, RuntimeError, ValueError):
            return []
        cursor, parts, links = Path(candidate.anchor), list(candidate.parts[1:]), []
        while parts:
            part = parts.pop(0)
            if part == "..":
                cursor = cursor.parent
                continue
            path = cursor / part
            if path.is_symlink():
                links.append(path)
                target = Path(os.readlink(path))
                parts = list(target.parts) + parts
                if target.is_absolute():
                    cursor = Path(parts.pop(0))
            else:
                cursor = path
        return links + [resolved]

    def change_directory(path, physical):
        candidate = cwd / (path if physical else os.path.normpath(path))
        if candidate.is_dir() and os.access(candidate, os.X_OK):
            return candidate.resolve() if physical else Path(os.path.normpath(path))
        return None

    for word, pattern in outside._input_paths(command, directory_change=(
            (lambda path, physical: path) if fixed and directory is None else change_directory)):
        if fixed and directory is None and not word.is_absolute():
            continue
        matches = glob.glob(str(Path(glob.escape(str(cwd))) / pattern))
        for candidate in matches or [word]:
            for path in dependencies(candidate):
                if root not in path.parents:
                    continue
                relative = path.relative_to(root).as_posix()
                try:
                    if path in entries:
                        if fixed:
                            source, revision, name = entries[path]
                            git("diff", "--quiet", "--no-ext-diff", "--no-textconv", revision, "--",
                                f":(literal){name}", cwd=source)
                        continue
                except (RuntimeError, OSError):
                    pass
                return (f"repository file {relative} must match a file in release commit {commit[:10]}; "
                        "commit it or use a path from that commit before asking again",
                        f"仓库文件 {relative} 必须对应发布提交 {commit[:10]} 中的文件；"
                        "请先提交，或使用该提交中的路径，再重新请求")
    return "", ""


def scan_key(repo, since: str, commit: str, words: list, command: str, mailboxes: bool = False,
             mailbox_allow=(), exceptions=()) -> str:
    """What a forbidden-word scan depends on, in one string: the commit, the starting point (the target's last
    release and the repository's remote-tracking branches), the principal's entries, lane exceptions, mailbox
    rule, allowlist and command line. A scan
    made with the same key finds the same."""
    remotes = _git_bytes(repo, "for-each-ref", "--format=%(objectname) %(refname)", "refs/remotes")
    words = sorted(w if w.startswith("re:") else w.casefold() for w in words if isinstance(w, str) and w.strip())
    # "identities": a scan from before authors and committers were read does not count for one that reads them
    parts = [repo_key(repo), commit, since, remotes, words, command, "identities",
             mailboxes, sorted(entry.casefold() for entry in mailbox_allow), sorted(set(exceptions))]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _askable(desk: Desk, s: dict, lane: str, target: str | None) -> dict:
    """Under the desk lock: what a request for the lane's target would be now ({target, command, repo, base,
    commit, prev, since, fixed}); raises SystemExit, saying why, when it cannot be asked."""
    ln = s["lanes"].get(lane)
    if not ln:
        raise SystemExit(f"hansa: there is no lane {lane}; lanes: {', '.join(sorted(s['lanes'])) or 'none'}. "
                         "Nothing was asked")
    targets = lane_targets(ln)
    if ln.get("merge_lane"):
        why = merge_targets_problem(lane, ln) or f"lane {lane}: a merge lane releases nothing; use a lane that releases"
        raise SystemExit(f"hansa: {why}. Nothing was asked")
    if not targets:
        raise SystemExit(f"hansa: lane {lane} has no release target; set it first: hansa lane target "
                         f"{lane} NAME \"<the command that sends the base branch out>\" "
                         "(and tell the principal). Nothing was asked")
    if target is None:
        if len(targets) > 1:
            raise SystemExit(f"hansa: lane {lane} has {len(targets)} release targets ({', '.join(sorted(targets))}); "
                             f"say which: hansa release {lane} TARGET [note]. Nothing was asked")
        target = next(iter(targets))
    if target not in targets:
        note_hint = (f" For a one-word note, name the target: hansa release {lane} {next(iter(targets))} {target}."
                     if len(targets) == 1 else "")
        raise SystemExit(f"hansa: lane {lane} has no release target {target}; its targets: "
                         f"{', '.join(sorted(x or '(unnamed)' for x in targets))}.{note_hint} Nothing was asked")
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
    return {"target": target, "about": targets[target].get("about") or "", "command": command,
            "repo": repo, "base": base, "commit": commit, "prev": prev,
            "since": since, "fixed": bool(targets[target].get("fixed")),
            "watch": targets[target].get("watch") or "",
            "watch_timeout_min": targets[target].get("watch_timeout_min", 90)}


def ask(desk: Desk, lane: str, note: str = "", by: str = "", session=None, target: str | None = None,
        direct: str | None = None, after_merge: bool = False) -> dict:
    """Record a request for a release of the lane's base branch as it is now, by one of its release targets (None:
    its only one). Returns it; raises SystemExit, with nothing recorded, when it cannot be asked, and with the
    request recorded as forbidden on a forbidden word. What goes out is read and scanned without the desk lock
    (it can take long on a long history), then recorded under it only if it is still what goes out: otherwise it
    is read again."""
    if direct is not None and not direct.strip():
        raise SystemExit('hansa: --direct needs a reason: --direct "<why>". Nothing was asked')
    scanned = None
    for _ in range(SCAN_TRIES):
        with desk.lock():
            s = desk.settings()
            automatic = None
            tasks = None
            if after_merge:
                tasks = desk.tasks()
                automatic = _after_merge_ready(s, lane, target, tasks, releases(desk))
                if not automatic:
                    raise SystemExit("hansa: this target has no settled merges to run now. Nothing was asked")
                if not lane_listed(s, lane, target):
                    raise SystemExit("hansa: the principal's tick no longer counts. Nothing was asked")
            a = _askable(desk, s, lane, target)
            merges = hansa_merges(desk, s, a, tasks)
            words = lane_words(s, lane)
            mailboxes = s.get("release_mailboxes", False)
            mailbox_allow = s.get("release_mailbox_allow") or []
            try:
                key = scan_key(a["repo"], a["since"], a["commit"], words, a["command"], mailboxes, mailbox_allow,
                               lane_exceptions(s, lane))
            except RuntimeError as e:
                raise SystemExit(f"hansa: the commits going out cannot be read ({desk.redact(str(e))}). "
                                 "Nothing was asked")
            if scanned and scanned[0] == (key, a["target"], merges, automatic):
                if automatic:
                    a["after_merge"] = automatic
                    note = "Asked by hansa after merges: " + merges_text(automatic["merges"], automatic["merges_count"])
                    by = "hansa"
                r = _record(desk, s, lane, a, key, scanned[1:], note, by, session, direct)
                break
        try:  # without the lock: the server's round, the page and other commands go on meanwhile
            commits, files = outgoing(a["repo"], a["since"], a["commit"])
            directs = direct_commits(a["repo"], a["since"], a["commit"], commits, merges)
            hits, checked = forbidden_in(a["repo"], a["since"], a["commit"], words, a["command"], mailboxes, mailbox_allow)
        except RuntimeError as e:
            raise SystemExit(f"hansa: the commits going out cannot be read ({desk.redact(str(e))}). Nothing was asked")
        scanned = ((key, a["target"], merges, automatic), commits, files, hits, checked, directs)
    else:
        raise SystemExit(f"hansa: what goes out from lane {lane} kept changing while it was read (its base branch "
                         "moving, say); ask again in a moment. Nothing was asked")
    target, commit, hits = r["target"], r["commit"], r.get("hits")
    desk.event("release_asked", id=r["id"], lane=lane, target=target, commit=commit, state=r["state"], by=by,
               **({"replaced": r["_replaced"]} if r.get("_replaced") else {}))
    for n in r.get("_replaced", []):
        desk.event("release_void", id=n, lane=lane, target=target, why=f"replaced by release #{r['id']}", quiet=True)
    if hits:
        desk.event("release_forbidden", id=r["id"], lane=lane, target=target, commit=commit, found=len(hits),
                   quiet=not after_merge)
    return r


def _record(desk: Desk, s: dict, lane: str, a: dict, key: str, scanned: tuple, note: str, by: str,
            session, direct: str | None) -> dict:
    """Under the desk lock: write the request, from what was read and scanned with this key."""
    commits, files, hits, checked, directs = scanned
    if direct is not None and not directs:
        raise SystemExit("hansa: --direct was given, but this release has no direct commits. Nothing was asked")
    held = bool(directs) and s.get("release_direct", "record") != "record"
    if held and direct is None:
        names = "\n".join(f"  {c['sha'][:10]} {c['subject']}" for c in directs[:10])
        raise SystemExit(desk.redact(
            f"hansa: this release holds {len(directs)} direct commit(s) (the first 10 at most):\n{names}\n"
            f'The principal requires their yes for direct commits; ask with hansa release {lane} {a["target"]} '
            '--direct "<why>". Nothing was asked'))
    target, command, prev, since = a["target"], a["command"], a["prev"], a["since"]
    try:
        why, _ = repository_input(command, a["repo"], a["commit"], a["fixed"])
    except (RuntimeError, OSError) as e:
        raise SystemExit(f"hansa: the release inputs cannot be read ({desk.redact(str(e))}). Nothing was asked")
    if why:
        raise SystemExit(desk.redact(f"hansa: {why}. Nothing was asked"))
    rs = releases(desk)
    listed = lane_listed(s, lane, target) and not held
    r = {"id": max((x.get("id", 0) for x in rs), default=0) + 1, "lane": lane, "target": target,
         "about": desk.redact(a["about"]),
         "repo": repo_key(a["repo"]), "base": a["base"], "commit": a["commit"], "command": command,
         **({"watch": a["watch"], "watch_timeout_min": a["watch_timeout_min"]} if a["watch"] else {}),
         **outside.inspect(command, a["repo"]),
         **({"fixed": True} if a["fixed"] else {}), "note": desk.redact(note.strip()), "asked": now(),
         **({"after_merge": a["after_merge"]} if a.get("after_merge") else {}),
         "by": by, "since": since, "since_release": prev["id"] if prev and since else None,
         "count": len(commits), "checked": checked, "scanned": key, "commits": commits[:MOST_COMMITS],
         "direct": directs[:MOST_COMMITS], "direct_count": len(directs),
         **({"direct_reason": desk.redact(direct.strip())} if direct is not None else {}),
         **({"direct_hold": True} if held else {}),
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
    the lane's base branch changed, the base branch moved, an outside file changed); '' when it still is.
    zh: in Chinese, for the page."""
    return stale_both(desk, s, r)[zh]


def stale_both(desk: Desk, s: dict, r: dict) -> tuple:
    """stale in English and Chinese, from one look at the outside files and base branch: (English, Chinese)."""
    ln = s["lanes"].get(r.get("lane"))
    if not ln:
        return f"lane {r.get('lane')} is no longer on the desk", f"工作线 {r.get('lane')} 已不在工作台上"
    target = r.get("target") or ""
    t = lane_targets(ln).get(target)
    if t is None and target:
        return (f"lane {r['lane']} has no release target {target} any more",
                f"工作线 {r['lane']} 已没有发布目标 {target}")
    if ((t or {}).get("command") or "") != r.get("command"):
        now_as, now_zh, then = (t or {}).get("command") or "(none)", (t or {}).get("command") or "（无）", r.get("command")
        return (f"{what(r)}'s release command changed since it was asked: it is now {now_as!r}, not {then!r}",
                f"{what_zh(r)} 的发布命令在请求之后改了：现在是 {now_zh!r}，不是 {then!r}")
    if bool((t or {}).get("fixed")) != bool(r.get("fixed")):
        return (f"how {what(r)} runs (--fixed: from a fixed checkout, or in the repository) changed since it was asked",
                f"{what_zh(r)} 的运行方式（--fixed：在固定检出里运行，或在仓库里运行）在请求之后改了")
    if ((t or {}).get("watch") or "") != (r.get("watch") or ""):
        return (f"{what(r)}'s watch command changed since it was asked",
                f"{what_zh(r)} 的发布监测命令在请求之后改了")
    if repo_key(ln["repo"]) != r.get("repo") or (ln.get("base") or "main") != r.get("base"):
        return (f"lane {r['lane']}'s repository or base branch changed since it was asked",
                f"工作线 {r['lane']} 的仓库或基础分支在请求之后改了")
    files = outside.inspect(r.get("command") or "", ln["repo"])["outside"]
    if "outside" not in r and files:
        path = files[0]["path"]
        return (f"the request did not record the outside file {path}; ask again",
                f"请求没有记录仓库外的文件 {path}；请重新请求")
    path = outside.changed(r.get("outside", []), files)
    if path:
        return (f"outside file {path} changed since the release was asked",
                f"仓库外的文件 {path} 在请求发布之后变了")
    try:
        tip = git("rev-parse", "--verify", f"refs/heads/{r['base']}^{{commit}}", cwd=r["repo"])
        why = repository_input(r.get("command") or "", ln["repo"], r["commit"], bool(r.get("fixed")))
        if why[0]:
            return why
    except (RuntimeError, OSError) as e:
        return (desk.redact(f"{r['base']} cannot be read in {r['repo']} ({e})")[:400],
                desk.redact(f"{r['base']} 在 {r['repo']} 里读不出来（{e}）")[:400])
    if tip != r.get("commit"):
        return (f"{r['base']} moved since it was asked: it is at {tip[:10]} now, not {r['commit'][:10]}",
                f"{r['base']} 在请求之后动了：现在在 {tip[:10]}，不是 {r['commit'][:10]}")
    return "", ""


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
    why, why_zh = stale_both(desk, desk.settings(), r)
    if why:
        void(desk, rid, why, ("waiting",), why_zh)
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
# waiting alert for the release makes a new one when sent; CODE_TRIES wrong codes void it (then only the page answers).
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
        write_text(f, secrets.token_hex(32), mode=0o600, follow_symlinks=False)
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
        why, why_zh = stale_both(desk, s, r)
        if why:  # no longer what the principal was alerted of
            void(desk, rid, why, ("waiting",), why_zh)
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
    """Record what a running release's command has started, also what left its process group (as a command's
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


def _stopping(desk: Desk, r: dict, proc=None, pending=None) -> None:
    """hansa serve's round for a running release asked to stop: TERM to its command's process group (and to what it
    started that left the group) once, then KILL to what still runs STOP_GRACE seconds later. A starting one has no
    command yet: the next round. One whose command had already ended (its exit file is written) gets no signal:
    finish records how it ended, and that the stop came after."""
    pending = {} if pending is None else pending
    key = ("release", r["id"])
    with serve_step():
        save_stopped(pending, key, r, lambda fields: change(desk, r["id"], lambda x: fields))
    kept = pending.get(key)
    if kept and kept[0] == (r.get("launch"), r.get("started")):
        r = {**r, **kept[1]}
    else:
        r = get(desk, r["id"]) or r
    if not r.get("pid") or r.get("stop_late"):
        return
    if not r.get("termed"):
        if (log_dir(desk) / f"{r['id']}.exit").exists():
            change(desk, r["id"], lambda x: {"stop_late": now()} if x.get("state") in ("starting", "running") else None)
            return
        with serve_step():
            r = _record_started(desk, r)
        _signal_group(r, signal.SIGTERM, proc)
        _signal_each(_left(r), signal.SIGTERM)
        save_stopped(pending, key, r,
                     lambda fields: change(desk, r["id"], lambda x: fields if x.get("state") in ("starting", "running") else None),
                     {"termed": now()})
    elif not r.get("killed") and time.time() - r["termed"] >= STOP_GRACE and _running(r):
        _signal_group(r, signal.SIGKILL, proc)
        _signal_each(_left(r), signal.SIGKILL)
        save_stopped(pending, key, r,
                     lambda fields: change(desk, r["id"], lambda x: fields if x.get("state") in ("starting", "running") else None),
                     {"killed": now()})


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


def sweep_checkouts(desk: Desk, rs: list | None = None) -> None:
    """Remove the checkouts of releases that no longer run: left by a server that stopped, or a start that
    failed. rs: the releases as read already."""
    d = desk.state_dir / "release-checkouts"
    if not d.is_dir():
        return
    rs = {str(r.get("id")): r for r in (releases(desk) if rs is None else rs)}
    for x in d.iterdir():
        r = rs.get(x.name) or {}
        if r.get("state") not in ("starting", "running") and not watch_open(r):
            drop_checkout(desk, x.name, r.get("repo"))


def _not_ready(desk: Desk, s: dict, r: dict) -> tuple | None:
    """Under the desk lock: None when an approved release may start now as far as what the principal saw goes;
    else (why, why_zh) to void it, or () when it only waits (sent back to the principal, or the desk is paused)."""
    rid, target = r["id"], r.get("target") or ""
    if r.get("after_merge"):
        t = lane_targets(s["lanes"].get(r["lane"])).get(target) or {}
        if not _is_int(t.get("after_merge_min"), 1, 1440) or t.get("after"):
            return ("runs after merges were turned off for this target", "此发布目标已关闭合并后自动运行")
        if not lane_listed(s, r["lane"], target):
            return ("the principal's tick no longer counts for this target", "此发布目标的负责人勾选已不再有效")
    if r.get("without_asking") and not r.get("after_merge") and not lane_listed(s, r["lane"], target):
        # Automatic requests already checked their tick above; fingerprint outside files once.
        change(desk, rid, lambda x: {"state": "waiting", "without_asking": None, "answered": None})
        desk.event("release_waits", id=rid, lane=r["lane"], target=target,
                   why="the target is no longer on the principal's list of releases without asking, or no longer "
                       "what the list recorded")
        return ()
    if r.get("without_asking") and desk.paused():  # while paused, only a release the principal said yes to
        return ()
    why, why_zh = stale_both(desk, s, r)
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
            words = lane_words(s, r["lane"])
            mailboxes = s.get("release_mailboxes", False)
            mailbox_allow = s.get("release_mailbox_allow") or []
            try:
                key = scan_key(r["repo"], r.get("since") or "", r["commit"], words, r["command"], mailboxes,
                               mailbox_allow, lane_exceptions(s, r["lane"]))
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
            scanned = (key, *forbidden_in(r["repo"], r.get("since") or "", r["commit"], words, r["command"],
                                         mailboxes, mailbox_allow))
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
        why, why_zh = repository_input(r["command"], r["repo"], r["commit"], True, directory=path)
        if why:
            void(desk, rid, why, ("starting",), why_zh)
            drop_checkout(desk, rid, r["repo"])
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
    change(desk, rid, lambda x: oom_begin(x))
    with open(d / f"{rid}.log", "wb") as out:
        proc = subprocess.Popen(["/bin/sh", "-c", SCRIPT, token, r["command"], str(d / f"{rid}.exit")],
                                cwd=str(cwd), env=command_env(r), stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)
    with recording_start(desk, ("release", rid, token), proc.pid, proc):
        change(desk, rid, lambda x: {"state": "running", "pid": proc.pid, "pid_start": proc_start(proc.pid)})
    return proc


def command_env(r: dict) -> dict:
    """The desk account's environment for both commands of a release, without an agent's session."""
    env = {**without_session(os.environ), "HANSA_RELEASE": str(r["id"]), "HANSA_RELEASE_COMMIT": r["commit"],
           "HANSA_RELEASE_TARGET": r.get("target") or "", "HANSA_RELEASE_REPO": r["repo"]}
    for k in ("HANSA_TASK", "HANSA_MERGING"):
        env.pop(k, None)
    return env


def finish(desk: Desk, rid, ran: bool = True) -> dict | None:
    """After a release's command ended (ran False: a release stopped before its command ran): its exit code and its
    output, keys removed; done or failed, or stopped when it was stopped before its command ended. What its command
    started that still runs (also outside its process group) is stopped first."""
    r = get(desk, rid)
    if not r or r.get("state") not in ("starting", "running"):
        return None
    memory = oom_result(r.get("oom_start")) if ran else {}
    left = stop_recorded(_left(r), SWEEP_GRACE)
    if left:
        desk.event("release_left_running_stopped", id=rid, pids=left)
    d = log_dir(desk)
    try:
        code = int((d / f"{rid}.exit").read_text().strip())
    except (OSError, ValueError):
        code = None
    with desk.lock():  # a key saved meanwhile is either known here or scrubbed after
        log = d / f"{rid}.log"
        text = desk.redact(log.read_text(encoding="utf-8", errors="replace")) if log.exists() else ""
        if log.exists():
            write_text(log, text)
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
        return {"state": "stopped" if stopped else "done" if ok else "failed", "exit": code, "ran": ran, **memory,
                "problem": "" if stopped else problem, "ended": now(), "tail": text.strip().splitlines()[-TAIL:],
                "seconds": round(now() - (x.get("started") or now())), "pid": None, "pid_start": "",
                **({"stop_late": x.get("stop_late") or now()} if late else {}),
                **({"watch_run": {"state": "waiting"}} if ok and not stopped and x.get("watch") else {})}
    r = change(desk, rid, ending)
    if r and r.get("fixed") and not watch_open(r):
        drop_checkout(desk, rid, r.get("repo"))
    (d / f"{rid}.exit").unlink(missing_ok=True)
    if r and r["state"] == "stopped":
        desk.event("release_stopped", id=rid, lane=r.get("lane"), target=r.get("target") or "", commit=r.get("commit"),
                   by=r.get("stopped_by") or "", reason=r.get("stop_reason") or "", ran=ran, exit=code,
                   killed=bool(r.get("killed")))
    elif r:
        desk.event("release_ended", id=rid, lane=r.get("lane"), target=r.get("target") or "", commit=r.get("commit"),
                   ok=ok, exit=code, problem=problem,
                   **({"stop_late": True, "stopped_by": r.get("stopped_by") or ""} if r.get("stop_late") else {}))
    return r


def _not_started(desk: Desk, r: dict, e: Exception) -> None:
    why = desk.redact(f"it could not start: {e}")[:600]
    if change(desk, r["id"], lambda x: {"state": "failed", "problem": why, "ended": now()}
              if x.get("state") in ("approved", "starting") else None):
        desk.event("release_ended", id=r["id"], lane=r.get("lane"), target=r.get("target") or "",
                   commit=r.get("commit"), ok=False, exit=None, problem=why)
    desk.failed_starts.pop(("release", r["id"], (get(desk, r["id"]) or {}).get("launch")), None)


def _recover(desk: Desk, r: dict) -> None:
    """A release an earlier server left starting: take over its run, or finish it; never start it again."""
    key = ("release", r["id"], r.get("launch"))
    if retry_start_failure(desk, key, lambda e: _not_started(desk, r, e)):
        return
    try:
        found = leader(pids_with_arg(r.get("launch") or "", r.get("started")))
    except CannotLook:
        since = r.get("lookup_failing_since") or now()
        if time.time() - since < LOOK_LIMIT:
            change(desk, r["id"], lambda x: {"lookup_failing_since": since} if x.get("state") == "starting" else None)
            return
        found = None
    if found:
        try:
            with recording_start(desk, key, found):
                change(desk, r["id"], lambda x: {"state": "running", "pid": found, "pid_start": proc_start(found)}
                       if x.get("state") == "starting" else None)
        except Exception as e:
            _not_started(desk, r, e)
        return
    if not (log_dir(desk) / f"{r['id']}.exit").exists():
        change(desk, r["id"], lambda x: {"lost": "hansa serve stopped as it started the release, so whether its command "
                                                 "ran is not known: read its output (hansa release), and ask again if it "
                                                 "has to go out"})
    finish(desk, r["id"])


def watch_open(r: dict) -> bool:
    return (r.get("watch_run") or {}).get("state") in WATCH_OPEN


def _watch_change(desk: Desk, rid, fields: dict) -> dict | None:
    return change(desk, rid, lambda r: {"watch_run": {**r["watch_run"], **fields}} if watch_open(r) else None)


def _start_watch(desk: Desk, r: dict) -> subprocess.Popen:
    """Each watch owns the release's checkout, so a moving base and later releases cannot change its files."""
    rid = r["id"]
    token = f"hansa-release-watch-{rid}-{uuid.uuid4().hex[:8]}"
    change(desk, rid, lambda x: {"watch_run": {"state": "starting", "launch": token, "started": now()}})
    path = checkout_dir(desk, rid)
    if not (path / ".git").is_file():
        drop_checkout(desk, rid, r.get("repo"))  # a crash may have left worktree creation unfinished
        path.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "--detach", str(path), r["commit"], cwd=r["repo"])
    d = log_dir(desk)
    (d / f"{rid}.watch.exit").unlink(missing_ok=True)
    with open(d / f"{rid}.watch.log", "wb") as out:
        proc = subprocess.Popen(["/bin/sh", "-c", SCRIPT, token, r["watch"], str(d / f"{rid}.watch.exit")],
                                cwd=str(path), env=command_env(r), stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)
    with recording_start(desk, ("release-watch", rid, token), proc.pid, proc):
        _watch_change(desk, rid, {"state": "running", "pid": proc.pid, "pid_start": proc_start(proc.pid)})
    return proc


def _finish_watch(desk: Desk, rid, problem: str = "") -> None:
    r = get(desk, rid)
    if not r or not watch_open(r):
        return
    w, d = r["watch_run"], log_dir(desk)
    stop_recorded(_left(w), SWEEP_GRACE)
    try:
        code = int((d / f"{rid}.watch.exit").read_text().strip())
    except (OSError, ValueError):
        code = None
    with desk.lock():
        log = d / f"{rid}.watch.log"
        text = desk.redact(log.read_text(encoding="utf-8", errors="replace")) if log.exists() else ""
        if log.exists():
            write_text(log, text)
        result = ("timed_out" if w.get("timed_out") else "passed" if code == 0 and not problem else "failed")
        ended = _watch_change(desk, rid, {"state": result, "exit": code, "ended": now(),
                                         "tail": text.strip().splitlines()[-TAIL:],
                                         "problem": desk.redact(problem)[:600], "pid": None, "pid_start": ""})
    (d / f"{rid}.watch.exit").unlink(missing_ok=True)
    drop_checkout(desk, rid, r.get("repo"))
    if ended:
        desk.failed_starts.pop(("release-watch", rid, w.get("launch")), None)
        desk.event("release_watch_ended", id=rid, lane=r.get("lane"), target=r.get("target"),
                   commit=r.get("commit"), state=result, exit=code)


def _watch_tick(desk: Desk, rs, procs: dict, starting: bool, pending: dict) -> None:
    """Watches are separate from the repository queue. An earlier server's unfinished watch starts over;
    stop its old process group before launching it again, including a launch whose pid was not recorded."""
    for r in rs():
        if not watch_open(r):
            continue
        with serve_step():
            rid, w = r["id"], r["watch_run"]
            proc = procs.get(rid)
            if proc is None:
                if w.get("state") in ("starting", "running"):
                    pid, start = w.get("pid"), w.get("pid_start", "")
                    if not pid:
                        pid = leader(pids_with_arg(w.get("launch") or "", w.get("started")))
                        start = proc_start(pid) if pid else ""
                    stop(pid, start)
                    stop_recorded(_left(w), SWEEP_GRACE)
                    change(desk, rid, lambda x: {"watch_run": {"state": "waiting"}})
                if starting:
                    try:
                        procs[rid] = _start_watch(desk, r)
                    except Exception as e:  # noqa: BLE001 - a watch failure must not hold later releases
                        _finish_watch(desk, rid, f"watch could not start: {e}")
                continue
            seen = _seen(w)
            if record_descendants(proc.pid, seen):
                updated = _watch_change(desk, rid, {"started_procs": {str(p): st for p, st in seen.items()}})
                w = updated["watch_run"] if updated else w
            key = ("release-watch", rid)
            save_stopped(pending, key, w, lambda fields: _watch_change(desk, rid, fields))
            w = (get(desk, rid) or r)["watch_run"]
            limit = r.get("watch_timeout_min", 90)
            if not _is_int(limit, 1, 1440):
                limit = 90
            if not has_ended(proc) and (w.get("timed_out") or time.time() - w["started"] > 60 * limit):
                try:
                    save_stopped(pending, key, w, lambda fields: _watch_change(desk, rid, fields), {"timed_out": True})
                finally:
                    stop(proc.pid, w.get("pid_start", ""), proc)
            if has_ended(proc):
                stop(proc.pid, "", proc)
                _finish_watch(desk, rid)
                del procs[rid]


def merges_text(merges: list, count: int | None = None) -> str:
    """The tasks a run covers, named even if their lane later moves or disappears."""
    names = "; ".join(f"{m['id']} ({m.get('title', '')})" for m in merges)
    return names + (f"; and {count - len(merges)} more" if count is not None and count > len(merges) else "")


def _after_merge_ready(s: dict, lane: str, target: str, tasks: list, rs: list, *, settled: bool = True) -> dict | None:
    """Use the round's records first: only settled, unconsumed landings merit looking at a tick or at git.
    Automatic requests consume their batch however they end; completed manual runs cover earlier landings.
    The boundary is at the ask, not the end: a merge during a fixed run remains for the next run.
    settled=False also reads batches still waiting out their quiet time, for status."""
    ln = s["lanes"].get(lane) or {}
    t = lane_targets(ln).get(target) or {}
    delay = t.get("after_merge_min")
    if not _is_int(delay, 1, 1440) or t.get("after"):
        return None
    repo, base = repo_key(ln["repo"]), ln.get("base") or "main"
    target_runs = [r for r in rs if r.get("lane") == lane and r.get("target") == target]
    if any(r.get("state") in OPEN for r in target_runs):
        return None
    own = [r for r in target_runs if r.get("repo") == repo and r.get("base") == base]
    through = max(((r.get("after_merge") or {}).get("through", 0)
                   or ((r.get("asked") or 0) if r.get("state") in ("done", "failed") else 0)
                   for r in own), default=0)
    passed = [r for r in own if r.get("state") == "done"]
    last_pass = passed[-1] if passed else {}
    pass_through = ((last_pass.get("after_merge") or {}).get("through")
                    or last_pass.get("asked") or 0)
    merges = []
    for m in tasks:
        if m.get("state") == "merging" and (merge_target(s, m.get("lane")) or ("",))[0] == repo:
            return None  # every merge of this repository, including one queued behind another
        if not m.get("merged") or not m.get("merge_commit"):
            continue
        origin = s["lanes"].get(m.get("lane")) or {}
        landed_repo = m.get("merge_repo") or m.get("repo") or origin.get("repo")
        landed_base = m.get("merge_base") or origin.get("base") or "main"
        if landed_repo and repo_key(landed_repo) == repo and landed_base == base:
            merges.append({k: m.get(k) for k in ("id", "title", "merged", "merge_commit")})
    merges.sort(key=lambda m: (m["merged"], m["id"]))
    fresh = [m for m in merges if m["merged"] > through]
    if not fresh or (settled and now() - fresh[-1]["merged"] < 60 * delay):
        return None
    since_pass = [m for m in merges if m["merged"] > pass_through]
    return {"through": fresh[-1]["merged"], "merges": fresh[-MOST_MERGES:], "merges_count": len(fresh),
            "since_pass": since_pass[-MOST_MERGES:], "since_pass_count": len(since_pass)}


def after_merge_rows(desk: Desk, s: dict, tasks: list) -> list:
    """Unasked batches, read by the same rules as the scheduler, including their quiet time."""
    rs, out = releases(desk), []
    for lane, ln in s["lanes"].items():
        for target, t in lane_targets(ln).items():
            batch = _after_merge_ready(s, lane, target, tasks, rs, settled=False)
            if not batch:
                continue
            listed = lane_listed(s, lane, target)
            held = False
            try:
                commit = git("rev-parse", "--verify", f"refs/heads/{ln.get('base') or 'main'}^{{commit}}", cwd=ln["repo"])
                if listed and s.get("release_direct", "record") != "record":
                    a = _askable(desk, s, lane, target)
                    commit = a["commit"]
                    commits, _ = outgoing(a["repo"], a["since"], commit)
                    held = bool(direct_commits(a["repo"], a["since"], commit, commits,
                                               hansa_merges(desk, s, a, tasks)))
            except (RuntimeError, OSError, SystemExit):
                continue  # no release can be asked from an unreadable base or outgoing range
            delay = t["after_merge_min"]
            at = batch["through"] + 60 * delay
            if not listed:
                en, zh = "waits for the principal's tick", "等待负责人勾选"
                if any(listed_key(e) == (lane, target) for e in s.get("releases_without_asking") or []):
                    en += "; the previous tick no longer counts"
                    zh += "；原来的勾选已失效"
            elif held:
                en, zh = "held: direct commits need the principal's yes", "已暂缓：直接提交需要负责人同意"
            elif now() >= at:
                en, zh = "starts when the server next looks", "服务器下次检查时开始"
            else:
                en = f"starts at {when(at)}, {delay} minute{'s' if delay != 1 else ''} after the last merge"
                zh = f"于 {when(at)} 开始，即上次合并后 {delay} 分钟"
            en = f"{target} on lane {lane} at {commit[:10]} {en}"
            zh = f"工作线 {lane} 的发布目标 {target} 在提交 {commit[:10]} 上{zh}"
            if desk.paused():
                en += "; " + waits_text(desk, s, {"without_asking": True})
                zh += "；" + waits_text(desk, s, {"without_asking": True}, zh=True)
            out.append({"lane": lane, "target": target, "commit": commit, "starts_at": at, "what": en, "what_zh": zh})
    return out


def _after_merges(desk: Desk, s: dict, tasks, rs) -> None:
    """Ask via the ordinary release path; failed gates tell the coordinator once per changed batch/settings."""
    configured = [(lane, target) for lane, ln in s["lanes"].items()
                  for target, t in lane_targets(ln).items() if t.get("after_merge_min")]
    if not configured:
        return
    tasks = (tasks or desk.tasks)()
    for lane, target in configured:
        with serve_step():
            batch = _after_merge_ready(s, lane, target, tasks, rs())
            if not batch:
                continue
            signature = json.dumps([batch, s], sort_keys=True)
            previous = desk.after_merge_waits.get((lane, target))
            if previous == (signature, True):
                continue
            held = False
            if not lane_listed(s, lane, target):
                held = True  # outside files are fingerprinted again only for a new batch or settings
                why = "the principal's tick no longer counts" if any(
                    listed_key(e) == (lane, target) for e in s.get("releases_without_asking") or []) else \
                    "this target needs the principal's tick"
            else:
                try:
                    ask(desk, lane, target=target, after_merge=True)
                except SystemExit as e:
                    why = str(e)
                    held = '--direct "<why>"' in why or "tick no longer counts" in why
                else:
                    desk.after_merge_waits.pop((lane, target), None)
                    continue
            desk.after_merge_waits[lane, target] = (signature, held)
            if previous != (signature, held):
                desk.event("release_after_merge_waits", lane=lane, target=target, why=why,
                           merges=merges_text(batch["merges"], batch["merges_count"]))


def tick(desk: Desk, s: dict, procs: dict, starting: bool = True, tasks=None, pending=None, watch_procs=None,
         seen=None) -> None:
    """hansa serve's round for releases: finish those that ended, stop those over their time limit, take
    over or finish those an earlier server left, and start approved ones, one per repository, none where a
    merge check runs (also while dispatch is paused: the principal said yes, and they use no provider). A release
    asked to stop gets TERM, then KILL after STOP_GRACE seconds, and is finished once nothing of it runs. What a
    running release's command starts is recorded each round, and what of it still runs is stopped at its end. None
    starts while starting is False (free space under the floor): an approved one waits, and goes once there is space.
    tasks: what gives the desk's tasks as this round read them (Dispatcher.tasks); else they are read here. seen: a
    list this fills with the releases as last read, and the Desk.writes they were read at, for the round's later
    steps (the status line's summary)."""
    read = [] if seen is None else seen
    pending = {} if pending is None else pending
    watch_procs = {} if watch_procs is None else watch_procs

    def rs() -> list:  # read once a round, and again only after this process changed a release (Desk.writes)
        if not read or read[0] != desk.writes:
            read[:] = [desk.writes, releases(desk)]
        return read[1]
    _after_merges(desk, s, tasks, rs)
    if not procs and not watch_procs and not any(r.get("state") in ("approved", "starting", "running")
                                               or watch_open(r) for r in rs()):
        sweep_checkouts(desk, rs())  # none open: only what a server that stopped left
        return
    for r in rs():
        with serve_step():
            if r.get("state") == "running":
                with serve_step():
                    r = _record_started(desk, r)
            if r.get("state") in ("starting", "running") and r.get("stop_asked"):
                with serve_step():
                    _stopping(desk, r, procs.get(r["id"]), pending)
    for rid, proc in list(procs.items()):
        with serve_step():
            if has_ended(proc):
                r = get(desk, rid) or {}
                if r.get("stop_asked") and r.get("termed") and _running(r, proc.pid):
                    continue  # what it started still ends its step: KILL after the grace
                stop(proc.pid, "", proc)  # what it left running in its group ends with it
                save_stopped(pending, ("release", rid), r, lambda fields: change(desk, rid, lambda x: fields))
                finish(desk, rid)
                del procs[rid]
    for r in rs():
        with serve_step():
            if r["id"] in procs:
                continue
            if r.get("state") == "running" and not alive(r.get("pid"), r.get("pid_start", "")):
                if r.get("stop_asked") and r.get("termed") and _running(r):
                    continue
                save_stopped(pending, ("release", r["id"]), r, lambda fields: change(desk, r["id"], lambda x: fields))
                finish(desk, r["id"])
            elif r.get("state") == "starting":
                _recover(desk, r)
    for r in rs():
        with serve_step():
            if r.get("state") != "running":
                continue
            limit, whose = minutes(s, r)
            if r.get("timed_out") or time.time() - (r.get("started") or time.time()) > 60 * limit:
                proc = procs.get(r["id"])
                if not run_running(r, proc):
                    continue
                try:
                    if not r.get("timed_out"):
                        save_stopped(pending, ("release", r["id"]), r,
                                     lambda fields: change(desk, r["id"], lambda x: fields),
                                     {"timed_out": True, "limit_min": limit, "limit_of": whose})
                finally:
                    stop(r.get("pid"), r.get("pid_start", ""), proc)
    _watch_tick(desk, rs, watch_procs, starting, pending)
    with serve_step():
        sweep_checkouts(desk, rs())
    _after_merges(desk, s, tasks, rs)  # a run just ended: merges during it make one more run
    approved = sorted((r for r in rs() if r.get("state") == "approved"), key=lambda r: r["id"])
    if not starting or not approved:
        return
    tasks = (tasks or desk.tasks)()
    # one at a time per repository, fixed or not: two releases of one repository may send to the same place
    held = {r.get("repo") for r in rs() if r.get("state") in ("starting", "running")}
    for r in approved:
        with serve_step():
            if r.get("repo") in held or (not r.get("fixed") and merging_on(desk, s, r.get("repo"), tasks)):
                continue
            try:
                proc = start(desk, r["id"])
            except Exception as e:  # noqa: BLE001 - say: nothing went out, and the release says why
                _not_started(desk, r, e)
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
        return (f"等工作台恢复（工作台已暂停：{paused}）：暂停时只运行你点过头的发布" if zh else
                f"waits until the desk is resumed (it is paused: {paused}): while it is paused, only a release the "
                "principal said yes to runs")
    other = next((x for x in releases(desk) if x.get("state") in ("starting", "running") and x.get("repo") == r.get("repo")
                  and x["id"] != r["id"]), None)
    if other:
        return (f"等同一仓库上{what_zh(other)}的发布 #{other['id']} 结束" if zh else
                f"waits for release #{other['id']} of {what(other)} on the same repository")
    m = not r.get("fixed") and merging_on(desk, s, r.get("repo"), tasks)
    if m:
        lane = merge_check_lane(s, m)
        return (f"等同一仓库上工作线 {lane} 的 {m['id']} 合并检查结束" if zh else
                f"waits for {m['id']}'s merge check on lane {lane}, on the same repository")
    from .run import not_looking
    from . import disk, memory
    low = disk.held(desk)
    if low:
        return (f"等磁盘空出来：有卷的剩余空间低于下限（{disk.floor_zh(disk.floor(s), low)}），hansa serve 暂不开始新的事"
                if zh else "waits: " + disk.short_text(desk, low))
    if memory.held(desk):
        return ("等可用内存恢复到下限加 1 GB，hansa serve 暂不开始新的事" if zh else "waits: " + memory.short_text(desk))
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
            return "等你在网页上点头" + (f"，或在对话里把提醒里的一次性码告诉协调员（{code}）" if code_live(r) else
                                       f"（{code}）" if code else "")
        return "waiting for the principal's yes on the page" + (
            f", or the one-time code of their alert said in the conversation ({code})" if code_live(r) else
            f" ({code})" if code else "")
    if st == "approved":
        if zh:
            how = ("不用问你就发布（这个发布目标在你的清单上）" if r.get("without_asking")
                   else f"你在 {when(r.get('answered'))} 点了头" + ("（在对话里用提醒里的一次性码，经协调员）" if coded else ""))
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
            return (f"你在 {when(r.get('answered'))} 拒绝了" + ("（在对话里用提醒里的一次性码，经协调员）" if coded else "")
                    + (f"：{r['reason']}" if r.get("reason") else ""))
        return (f"refused by the principal {when(r.get('answered'))}"
                + (" (through the coordinator, with the one-time code of their alert)" if coded else "")
                + (f": {r['reason']}" if r.get("reason") else ""))
    if st == "void":
        return f"作废：{r.get('void_zh') or r.get('void')}；什么都没运行" if zh else f"void: {r.get('void')}; nothing was run"
    if st == "forbidden":
        # This summary also goes out in alerts: keep the hits themselves on the machine.
        hits = r.get("hits", [])
        limits = sum(h.get("kind") == "pattern_limit" for h in hits)
        ordinary = [h for h in hits if h.get("kind") != "pattern_limit"]
        parts = []
        if ordinary or not limits:
            mailbox = mailbox_refusal(ordinary)
            parts.append((f"邮箱未使用保留的示例域名，也不在允许名单中（{len(ordinary)} 处）" if zh else
                          mailbox.splitlines()[0]) if mailbox else
                         (f"要发出去的内容里有你不许发出去的字眼（{len(ordinary)} 处）" if zh else
                          f"what goes out holds words the principal keeps from going out ({len(ordinary)} place(s))"))
        if limits:
            parts.append(f"{limits} 行无法用正则检查" if zh else
                         f"{limits} line(s) could not be checked against the patterns")
        return ("被拒绝：" if zh else "refused: ") + ("；" if zh else "; ").join(parts)
    return st or "?"


def _who(r: dict, zh: bool = False) -> str:
    by = r.get("stopped_by") or ""
    if zh:
        return "你" if by == "page" else "协调员" if by == "coordinator" else by or "?"
    return "the principal" if by == "page" else "the coordinator" if by == "coordinator" else by or "?"


def checked_text(r: dict) -> str:
    """How many outgoing commits were checked for forbidden words, and how many of them the request shows."""
    n = r.get("checked", r.get("count", 0))
    shown = len(r.get("commits") or [])
    return (f"{n} outgoing commit(s) checked for forbidden words" + (f" (the first {shown} shown)" if shown < n else ""))


def direct_lines(r: dict) -> list:
    """Direct commits and the recorded reason; older requests have neither."""
    n = r.get("direct_count", 0)
    if not n:
        return []
    commits = r.get("direct") or []
    out = [f"{n} direct commit(s)" + (f" (the first {len(commits)} shown)" if len(commits) < n else "")]
    out += [f"  {c['sha'][:10]} {c['subject']}" for c in commits]
    if r.get("direct_reason"):
        out.append(f"reason for direct commits: {r['direct_reason']}")
    if r.get("direct_hold") and r.get("state") == "waiting":
        out.append("direct commits require the principal's yes even when the target is ticked")
    return out


def runs_text(t: dict, zh: bool = False) -> str:
    """Which way a target (or a release of it) runs, in words."""
    if t.get("fixed"):
        return ("在为它做的提交固定检出里运行，不占仓库" if zh else
                "runs from a fixed checkout of the commit, holding nothing")
    return "在仓库里运行，运行时占着仓库" if zh else "runs in the repository, holding it while it runs"


def line(desk: Desk, r: dict, s: dict | None = None) -> str:
    return (f"#{r['id']} {what(r)} at {r.get('commit', '')[:10]} ({r.get('base')}): "
            f"{state_text(desk, r, s)}" + (f"; {oom_text(r)}" if oom_text(r) else "")
            + ("; " + watch_text(r) if r.get("watch_run") else ""))


def watch_text(r: dict, zh: bool = False) -> str:
    w = r.get("watch_run") or {}
    st = w.get("state")
    if not st:
        return "发布成功后监测" if zh else "watch after successful release"
    if st == "timed_out":
        return "发布监测超时" if zh else "watch timed out"
    if st == "failed":
        return (("发布监测失败" if zh else "watch failed")
                + (f" (exit {w['exit']})" if w.get("exit") is not None else "")
                + (f": {w['problem']}" if w.get("problem") else ""))
    return ({"waiting": "发布监测等待运行", "starting": "发布监测正在启动", "running": "发布监测正在运行",
             "passed": "发布监测通过"}.get(st, st) if zh else "watch " + st)


def list_text(desk: Desk) -> str:
    rs = releases(desk)
    if not rs:
        return ("no releases yet. A lane's release command is set with hansa lane target L default \"<command>\", or its "
                "named targets with hansa lane target L NAME \"<command>\"; hansa release L [TARGET] [note] asks for one")
    s = desk.settings()
    live = [r for r in rs if r.get("state") in OPEN or watch_open(r)]
    recent = [r for r in rs if r.get("state") not in OPEN and not watch_open(r)][-10:]
    out = []
    for title, group in (("open", live), ("recent", list(reversed(recent)))):
        if not group:
            continue
        out.append(f"{title}:")
        for r in group:
            out.append("  " + line(desk, r, s))
            out.append(f"      command: {r.get('command')} ({runs_text(r)})" + (f" · note: {r['note']}" if r.get("note") else ""))
            if r.get("watch"):
                out.append(f"      watch: {r['watch']} [{r.get('watch_timeout_min', 90)} min]; {watch_text(r)}")
                out += [f"      | {x}" for x in (r.get("watch_run") or {}).get("tail", [])[-5:]]
            out.append(f"      {checked_text(r)}")
            out += ["      " + x for x in outside.lines(r)]
            out += ["      " + x for x in direct_lines(r)]
            if r.get("state") == "forbidden":
                out += [f"      {x}" for x in hits_text(r.get("hits", [])).splitlines()]
            if r.get("state") in ("done", "failed", "stopped") and r.get("tail"):
                out += [f"      | {x}" for x in r["tail"][-5:]]
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
        return [f"  release: none (set one with hansa lane target {name} default \"<command>\", or name targets with "
                f"hansa lane target {name} NAME \"<command>\")", "  last release: none", *listed]
    out = []
    for target, t in sorted(targets.items()):
        rs = [r for r in releases(desk) if r.get("lane") == name and (r.get("target") or "") == target]
        limit = t.get("timeout_min") or ln.get("timeout_min", 240)
        head = f"  release target {target}" if target else "  release"
        out += [f"{head}: {t.get('command')} (hansa release {name}{' ' + target if target else ''} asks for it; "
                f"{how[bool(t.get('fixed'))]}; {limit} min as its limit"
                + (f"; only for a commit target {t['after']} already sent out" if t.get("after") else "")
                + ("; " + after_merge_text(s, name, target) if t.get("after_merge_min") else "")
                + (": the principal lets it go out without asking" if lane_listed(s, name, target)
                   else unlisted(s, name, target)) + ")",
                f"  last release{' of ' + target if target else ''}: " + (line(desk, rs[-1], s) if rs else "none")]
        out += ["    " + x for x in outside.lines(outside.inspect(t.get("command") or "", ln["repo"],
                                                                 fingerprint=False))]
        if t.get("watch"):
            out.append(f"    watch: {t['watch']} [{t.get('watch_timeout_min', 90)} min]; after exit 0, holding nothing")
    return out + listed


def after_merge_text(s: dict, lane: str, target: str) -> str:
    delay = lane_targets(s["lanes"][lane])[target]["after_merge_min"]
    gate = ("ticked by the principal" if lane_listed(s, lane, target) else
            "needs the principal's tick; the setting is kept but nothing runs until they tick this target")
    return f"after merges: {delay} min; {gate}"


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
    shown = [r for r in rs if r.get("state") in OPEN or watch_open(r)] + [r for r in rs if r.get("state") not in OPEN
                                                           and not watch_open(r)
                                                           and (r.get("ended") or r.get("asked") or 0) >= week][-5:]
    out = []
    for r in shown:
        row = {k: r.get(k) for k in ("id", "lane", "target", "base", "commit", "command", "note", "state", "asked",
                                     "answered",
                                     "reason", "void", "exit", "problem", "count", "commits", "files", "files_count",
                                     "named", "hits", "ended", "without_asking", "since_release", "fixed",
                                     "stop_asked", "stopped_by", "stop_reason", "answered_by", "channel",
                                     "outside", "outside_folders")}
        if r.get("watch"):
            row.update(watch=r["watch"], watch_timeout_min=r.get("watch_timeout_min", 90),
                       watch_run={k: v for k, v in (r.get("watch_run") or {}).items()
                                  if k in ("state", "exit", "started", "ended", "tail", "problem")},
                       watch_what=watch_text(r), watch_what_zh=watch_text(r, zh=True))
        row.update({k: r[k] for k in ("direct", "direct_count", "direct_reason", "direct_hold") if k in r})
        c = r.get("code") or {}  # whether a code went out, and when; never the code, nor its hash
        row["code_sent"] = c.get("sent") if code_live(r) else None
        waits = r.get("state") == "waiting"
        row["code"], row["code_zh"] = (code_text(r), code_text(r, zh=True)) if waits else ("", "")
        row["runs"], row["runs_zh"] = runs_text(r), runs_text(r, zh=True)
        row["checked"] = r.get("checked", r.get("count"))
        row["what"], row["what_zh"] = state_text(desk, r, s), state_text(desk, r, s, zh=True)
        row["tail"] = (r.get("tail") or [])[-TAIL:]
        if r.get("state") == "waiting":
            row["moved"], row["moved_zh"] = stale_both(desk, s, r)
        out.append(row)
    return out
