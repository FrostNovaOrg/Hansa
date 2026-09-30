"""hansa tells a coordinator's Claude Code session when something needs it, through the session's inbox.

Every interactive Claude Code session listens on a local Unix socket, its inbox: one line of JSON posted
there arrives in the session as a message from another session. A follower is a small background process
per session and desk. It reads the desk's event log from where it left off, picks what the coordinator
waits for (a task ended, a verification or design session ended, a queued merge check ended, the principal
answered a request, a provider an open request names became ready, the principal answered a release, a release
ended or became void, the server stopped), and posts it to that session's inbox. It ends when the session's
process does.

    `hansa coordinator`   starts one for the session it launches (--messaging-socket-path, a path it chose)
    `hansa notify on`     starts one for the session it is run from (CLAUDE_PID, CLAUDE_CODE_MESSAGING_SOCKET)
    `hansa notify off`    stops that one; `hansa notify` lists the desk's followers
    --clear-daily HH-HH   (with either of the first two) the follower also clears the session once a day (below)

When hansa's code on disk changes (an upgrade), a follower runs itself again on the new code, in the same process
and from where it was: otherwise a module it imports later would come from the new files, and the rest from the old.

Where things are kept:
    .hansa/followers/<key>.json   the desk's register: one record per follower (session, socket, read
                                  position, when it was last seen, its last post); written by the desk's owner
    <private>/<key>.state.json    the follower's own: its pid, and the nonce it was started with
    <private>/<key>.lock          held while it runs: one follower per session and desk
    <private>/<key>.log           what it printed
<private> is .hansa/followers/ too, except for root on an account's desk (see account.py): root neither reads
nor writes in the desk, so its follower asks the account's hansa for the log (`hansa notify-feed`) and keeps
its own files under root's home.

A task or request goes to the session that made it (`hansa add`, `retry`, `verify`, `merge`, `design` and
`ask` record it: its pid and start time), if that session has a live follower; otherwise to every follower
of the desk. Desk-wide events go to every follower.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import account
from .desk import Desk, now, read_json, write_json
from .run import alive, claude_bin, proc_start, server_state, srt_needed, srt_record

LIVE_SECONDS = 30  # a follower not seen for this long is not live (it polls every second or two)
MOST_LINES = 10  # past this many events in one message, the rest are counted, and hansa status has them
POST_WAIT = 60  # seconds a post is tried again while the inbox is not there yet
KEEP_DAYS = 7  # ended followers' records are kept this long
KEY = re.compile(r"^[0-9]+-[0-9a-f]{8}$")


# -- the session a command runs in ---------------------------------------------------------------------
def session_here():
    """The Claude Code session this command was run from: {pid, start}, or None outside one (a shell, the
    page, a worker). The start time tells the session from a later process given the same pid."""
    pid = os.environ.get("CLAUDE_PID", "")
    if not pid.isdigit() or os.environ.get("HANSA_TASK"):
        return None
    return {"pid": int(pid), "start": proc_start(int(pid))}


def recorded(sess) -> dict:
    """The fields that record the session in a task or request; none outside a session."""
    return {"session": sess} if sess else {}


def key_of(sess: dict) -> str:
    return f"{int(sess['pid'])}-{hashlib.sha1(str(sess.get('start') or '').encode()).hexdigest()[:8]}"


def same(a, b) -> bool:
    """Whether two recorded sessions are one: the same pid, started at the same time (when both are known)."""
    if not isinstance(a, dict) or not isinstance(b, dict) or a.get("pid") != b.get("pid"):
        return False
    return not a.get("start") or not b.get("start") or a["start"] == b["start"]


def gone(sess: dict) -> bool:
    return not alive(sess.get("pid"), sess.get("start") or "")


# -- the desk's side: the register and the log ----------------------------------------------------------
def register_dir(desk: Desk) -> Path:
    return desk.state_dir / "followers"


def records(desk: Desk) -> list:
    d = register_dir(desk)
    out = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        r = read_json(f)
        if isinstance(r, dict) and KEY.match(str(r.get("key", ""))):
            out.append(r)
    return out


def is_live(r: dict) -> bool:
    return not r.get("ended") and time.time() - (r.get("seen") or 0) < LIVE_SECONDS


def live_records(desk: Desk) -> list:
    return [r for r in records(desk) if is_live(r)]


def hears(desk: Desk, sess="here") -> bool:
    """Whether a session (by default the one this command runs in) has a live follower, so hansa tells it when
    its tasks end."""
    sess = session_here() if sess == "here" else sess
    return bool(sess) and any(same(r.get("session"), sess) for r in live_records(desk))


def hears_of(desk: Desk, tid: str) -> bool:
    """Whether the session this command runs in is told when this task ends: it has a live follower, and the task
    is its own or no other live session's (as items_for sends it)."""
    me = session_here()
    if not me:
        return False
    lives = [r.get("session") for r in live_records(desk)]
    if not any(same(me, s) for s in lives):
        return False
    try:
        theirs = desk.meta(tid).get("session")
    except SystemExit:
        return False
    return not theirs or same(theirs, me) or not any(same(theirs, s) for s in lives)


def wait_step(desk: Desk, tid: str, heard=None) -> str:
    """The next step for a task that runs on by itself: hansa tells a session that hears from it when the task
    ends; any other session waits for it. heard: whether the session hears of it (default: ask)."""
    if hears_of(desk, tid) if heard is None else heard:
        return f"hansa tells this session when {tid} ends"
    return f"hansa wait {tid}"


def log_end(desk: Desk) -> int:
    """Where the log's last whole line ends."""
    try:
        with open(desk.events_path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - 65536))
            tail = f.read()
    except FileNotFoundError:
        return 0
    return size - (len(tail) - tail.rfind(b"\n") - 1) if b"\n" in tail else max(0, size - len(tail))


def read_log(desk: Desk, pos: int, most: int = 8 << 20):
    """The events after pos, whole lines only, and where they end."""
    try:
        with open(desk.events_path, "rb") as f:
            f.seek(pos)
            data = f.read(most)
    except FileNotFoundError:
        return [], pos
    cut = data.rfind(b"\n") + 1
    events = []
    for line in data[:cut].splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            events.append(e)
    return events, pos + cut


def _next_step(desk: Desk, tid: str) -> str:
    """What hansa show would suggest next for this task, with hansa show first."""
    from .views import show_text
    try:
        nxt = next((x[6:] for x in show_text(desk, tid, heard=True).splitlines() if x.startswith("next: ")), "")
    except (SystemExit, OSError, ValueError, KeyError):
        nxt = ""
    nxt = nxt.replace("read the report above", f"read hansa show {tid}")
    return nxt if "hansa show" in nxt else f"hansa show {tid}" + (f" · {nxt}" if nxt else "")


def task_line(desk: Desk, m: dict) -> str:
    """A task that ended, as it is now, in one line with the next command."""
    tid, st = m["id"], m["state"]
    if m.get("kind") == "verify":
        head = (f"{tid}, the verification of {m.get('parent')}, ended: "
                + (f"verdict: {m['verdict']}" if m.get("verdict") else f"{st}, without a verdict"))
    elif m.get("kind") == "design":
        head = f"design session {tid} ended ({st})"
    else:
        head = f"{tid} {st}"
    head += f": {m.get('title', '')}"
    chk = m.get("check") or {}
    if st == "done" and m.get("kind") != "verify":
        head += (" (no changes)" if not m.get("commits") else
                 " (its check could not run: the machine's problem, not the task's)" if chk.get("could_not_run") else
                 " (check passed)" if chk.get("ok") else
                 f" (check FAILED{': ' + chk['note'] if chk.get('note') else ''})" if chk else "")
    elif st in ("failed", "cancelled") and m.get("error") and m.get("error") != st:
        head += f" ({str(m['error'])[:200]})"
    elif st == "merging":
        from .views import merge_check_waits_for
        ahead = merge_check_waits_for(desk, m)
        what = "merge" if (m.get("merge_check") or {}).get("plain") else "merge check"
        head += f" (its {what} is queued and waits for {ahead})" if ahead else f" (its {what} is queued or running)"
    from .views import background_note
    note = background_note(m)
    return f"{head} · next: {_next_step(desk, tid)}" + (f" · {note}" if note else "")


def _item(desk: Desk, e: dict):
    """What one event tells the coordinator: (key, line, recorded session, desk-wide, the session not to tell),
    or None. The session that cancelled a task with hansa cancel does not hear that it was cancelled."""
    kind, tid = e.get("event"), e.get("task")
    if kind in ("task_finished", "task_checked", "task_not_started", "merge_check_ended", "task_merged"):
        if kind == "task_finished" and e.get("state") in ("checking", "queued"):  # not ended yet / moved on
            return None
        if kind == "merge_check_ended" and e.get("note") == "cancelled":  # the coordinator cancelled it
            return None
        if kind == "task_merged" and not e.get("merge_checked") and not e.get("waited"):  # the coordinator saw it
            return None
        try:
            m = desk.meta(tid)
        except SystemExit:
            return None
        if kind == "merge_check_ended":
            key = ("merge", tid)
            from .views import waited_text
            said = (f"{tid}'s merge: {waited_text(e)}" if e.get("waited") else
                    f"{tid}'s merge check ended; nothing was merged: {e.get('note') or 'the check failed'}")
            line = f"{said}: {m.get('title', '')} · next: {_next_step(desk, tid)}"
        elif kind == "task_merged":
            how = ("in its turn behind a merge check, without a check of its own" if e.get("waited") else
                   "after its merge check passed")
            key, line = ("merge", tid), (f"{tid} merged into {e.get('into')} {how}: {m.get('title', '')}"
                                         + (f" · note: {m['unverified']}" if m.get("unverified") else ""))
        else:
            key, line = ("task", tid), task_line(desk, m)
        return key, line, m.get("session"), False, m.get("cancel_by") if m.get("state") == "cancelled" else None
    # hansa closes a design session's request itself, and the coordinator knows of one it closed (hansa ask --close)
    if kind == "ask_closed" and e.get("by") != "hansa" and not e.get("coordinator"):
        x = next((a for a in desk.asks() if a.get("id") == e.get("id")), None)
        if not x:
            return None
        return (("ask", x["id"]), f"request #{x['id']} answered: {x.get('answer') or '(closed without a note)'}"
                                  f" (you asked: {str(x.get('text', ''))[:120]})", x.get("session"), False, None)
    if kind == "ask_ready":  # a provider open requests name became ready since they were made (views.ready_news)
        named = [a for a in desk.asks() if a.get("id") in (e.get("ids") or []) and not a.get("closed")]
        if not named:
            return None
        sessions = [a.get("session") for a in named]
        return (("ready", e.get("provider")),
                f"{e.get('provider')} is ready now: done? Still open: "
                + "; ".join(f"request #{a['id']} ({str(a.get('text', ''))[:120]})" for a in named)
                + " (the principal closes a request on the page; if it is done, close it: hansa ask --close N \"why\")",
                sessions[0] if all(same(x, sessions[0]) for x in sessions) else None, False, None)
    if kind in ("release_answered", "release_ended", "release_void", "release_forbidden") and not e.get("quiet"):
        return _release_item(desk, e)
    if kind == "server_stopped":
        return ("server",), ("hansa serve stopped: no task starts or ends until it runs again · next: "
                             "hansa explain server"), None, True, None
    if kind == "srt_tried" and not e.get("ran") and srt_needed(desk.settings()):
        said = ("found, but it could not run: " + str(e.get("said") or "")[:200]) if e.get("found") \
            else "not found on its PATH"
        return ("srt",), (f"hansa serve could not run srt ({said}), so the lanes' setups and checks cannot run "
                          "· next: hansa explain server"), None, True, None
    return None


def _release_item(desk: Desk, e: dict):
    """A release the principal answered, that ended, or that became void or was refused for its words."""
    from . import release
    r = release.get(desk, e.get("id"))
    if not r:
        return None
    rid, lane = r["id"], r.get("lane")
    head = f"release #{rid} of lane {lane} ({r.get('base')} at {str(r.get('commit', ''))[:10]})"
    kind = e.get("event")
    if kind == "release_answered":
        key = ("release-answer", rid)
        line = (f"{head}: the principal said yes; hansa serve runs `{r.get('command')}` now · you are told when it ends"
                if e.get("yes") else
                f"{head}: the principal refused it" + (f": {e.get('reason')}" if e.get("reason") else "")
                + " · nothing was run; do not push or deploy it yourself")
    elif kind == "release_ended":
        key = ("release", rid)
        tail = " | ".join((r.get("tail") or [])[-5:])
        line = (f"{head} done: `{r.get('command')}` exited 0" if e.get("ok") else
                f"{head} FAILED" + (f" (exit {e.get('exit')})" if e.get("exit") is not None else "")
                + (f": {e.get('problem')}" if e.get("problem") else "") + (f" · last lines: {tail}" if tail else "")
                + " · next: hansa release; fix what failed, then ask again: hansa release " + str(lane))
    elif kind == "release_void":
        key = ("release", rid)
        line = f"{head} is void: {e.get('why')}; nothing was run · next: ask again, hansa release {lane}"
    else:
        key = ("release", rid)
        line = (f"{head} refused: what goes out holds words the principal keeps from going out · next: "
                f"hansa release lists where")
    return key, line, r.get("session"), False, None


def items_for(desk: Desk, events: list, me: dict) -> list:
    """What these events tell this follower's session, one line per task, request or desk-wide event."""
    others = [r.get("session") for r in live_records(desk) if r.get("key") != me.get("key")]
    out = {}
    for e in events:
        it = _item(desk, e)
        if not it:
            continue
        key, line, sess, desk_wide, quiet = it
        if quiet and same(quiet, me.get("session")):
            continue  # it cancelled the task itself
        if not (desk_wide or not sess or same(sess, me.get("session")) or not any(same(sess, o) for o in others)):
            continue  # its own session hears it
        out.pop(key, None)  # the latest line of a task comes last
        out[key] = {"key": list(key), "line": desk.redact(line)}
    return list(out.values())


def _srt_ran(desk: Desk):
    """Whether srt ran on hansa's last try of it: True, False, or None before any try."""
    return srt_record(desk).get("ran")


def _proc_gone(f) -> bool:
    """Whether a process a register records ({pid, start}: a follower, a session) is known to have ended. Another
    account's process (root's, seen by the account) counts as there when it cannot be signalled."""
    if not isinstance(f, dict) or not isinstance(f.get("pid"), int) or f["pid"] <= 0:
        return False
    try:
        os.kill(f["pid"], 0)
    except ProcessLookupError:
        return True
    except OSError:  # not ours to signal: it is there
        pass
    start = str(f.get("start") or "")
    if not start or any(c.isspace() for c in start):
        return False
    seen = proc_start(f["pid"])
    return bool(seen) and seen != start


def _write_record(desk: Desk, key: str, rec: dict) -> None:
    register_dir(desk).mkdir(parents=True, exist_ok=True)
    write_json(register_dir(desk) / f"{key}.json", rec)


def _server(desk: Desk) -> dict:
    """What a follower is told of hansa serve: server_up for a follower on older code, server for this one."""
    st = server_state(desk)
    return {"server_up": st["state"] != "gone", "server": st}


def handle(desk: Desk, req: dict) -> dict:
    """The desk's part of a follower's work, done as the desk's owner: start, feed, end, list."""
    op = req.get("op")
    if op == "list":
        return {"followers": live_records(desk)}
    me = req.get("me") or {}
    key = str(me.get("key", ""))
    if not KEY.match(key):
        raise ValueError(f"not a follower's key: {key!r}")
    old = read_json(register_dir(desk) / f"{key}.json", {}) or {}
    if op == "start":
        if isinstance(req.get("resume"), int):  # the same follower, restarted on new code: where it was
            _write_record(desk, key, {**old, **me, "pos": req["resume"], "seen": now(), "ended": None})
            return {"pos": req["resume"], "summary": [], **_server(desk)}
        end = log_end(desk)
        for r in records(desk):  # a follower killed outright has ended, however recently it was seen
            if not r.get("ended") and _proc_gone(r.get("follower")):
                _write_record(desk, r["key"], {**r, "ended": r.get("seen") or now()})
        mine = [r for r in records(desk) if r.get("uid") == me.get("uid") and not is_live(r)]
        for r in mine:  # long ended: forgotten
            if time.time() - (r.get("seen") or 0) > KEEP_DAYS * 86400:
                (register_dir(desk) / f"{r['key']}.json").unlink(missing_ok=True)
        prev = max(mine, key=lambda r: r.get("seen") or 0, default=None)
        if prev and prev.get("caught_up"):  # a follower after it already said what it missed
            prev = None
        summary = []
        if prev and int(prev.get("pos") or 0) < end:  # it ended before reading to the end: what was missed
            events, _ = read_log(desk, int(prev.get("pos") or 0), end - int(prev.get("pos") or 0))
            seen = set()
            for x in items_for(desk, events, me):  # one line per task or request, as it is now
                k = ("task", x["key"][1]) if x["key"][0] in ("task", "merge") else tuple(x["key"])
                if k in seen:
                    continue
                seen.add(k)
                try:
                    summary.append(desk.redact(task_line(desk, desk.meta(k[1]))) if k[0] == "task" else x["line"])
                except SystemExit:  # a task folder gone meanwhile
                    continue
        if prev and not summary:  # nothing to tell: whoever starts next need not tell it either
            _write_record(desk, prev["key"], {**prev, "caught_up": now()})
        _write_record(desk, key, {**me, "pos": end, "since": me.get("since") or now(), "seen": now(), "ended": None})
        missed = None
        if summary:
            ps = prev.get("session") or {}
            missed = {"key": prev["key"], "pid": ps.get("pid"), "name": prev.get("name") or "",
                      "at": prev.get("ended") or prev.get("seen"), "session_ended": _proc_gone(ps)}
        return {"pos": end, "summary": summary, "missed": missed, **_server(desk)}
    if op == "caught_up":  # the follower posted what an ended one missed: nobody is to post it again
        k = str(req.get("of", ""))
        r = read_json(register_dir(desk) / f"{k}.json", {}) if KEY.match(k) else {}
        if isinstance(r, dict) and r.get("ended"):
            _write_record(desk, k, {**r, "caught_up": now()})
        return {}
    if op == "feed":
        events, pos = read_log(desk, int(req.get("pos") or 0))
        items = items_for(desk, events, me)
        _write_record(desk, key, {**old, **me, "pos": int(req.get("done") or 0), "seen": now(), "ended": None})
        return {"pos": pos, "items": items, **_server(desk), "srt_ran": _srt_ran(desk)}
    if op == "end":
        _write_record(desk, key, {**old, **me, "pos": int(req.get("done") or old.get("pos") or 0), "seen": now(),
                                  "ended": now()})
        return {}
    raise ValueError(f"unknown request {op!r}")


def cmd_feed(desk: Desk) -> None:
    """`hansa notify-feed`: handle one request from standard input for a follower of root's."""
    print(json.dumps(handle(desk, json.loads(sys.stdin.read()))))


def source(desk: Desk):
    """How this process reaches the desk's part: directly, or as root through the account's hansa."""
    theirs = account.desk_account(desk.root)
    if not theirs:
        return lambda req: handle(desk, req)

    def via_account(req):
        r = account.run(theirs, ["notify-feed"], desk.root, input=json.dumps(req), capture_output=True, text=True,
                        timeout=120)
        if r.returncode:
            raise RuntimeError(f"the account's hansa notify-feed ended with {r.returncode}: {r.stderr.strip()[-300:]}")
        return json.loads(r.stdout)
    return via_account


def private_dir(desk: Desk) -> Path:
    """Where this account's followers of the desk keep their own files."""
    if account.desk_account(desk.root):
        d = Path.home() / ".hansa" / "followers" / hashlib.sha1(str(desk.root).encode()).hexdigest()[:12]
    else:
        d = register_dir(desk)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


# -- posting --------------------------------------------------------------------------------------------
def post(path: str, text: str) -> None:
    """Post one message to a session's inbox: one line of JSON, then close. HANSA_NOTIFY_CAPTURE names a
    file that gets what would be sent instead (the tests' sandbox cannot make a Unix socket)."""
    line = json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False) + "\n"
    capture = os.environ.get("HANSA_NOTIFY_CAPTURE")
    if capture:
        with open(capture, "a", encoding="utf-8") as f:
            f.write(json.dumps({"socket": path, "line": line}, ensure_ascii=False) + "\n")
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(30)
        s.connect(path)
        s.sendall(line.encode("utf-8"))


def message(desk_root: str, lines: list, head: str = "") -> str:
    shown = lines if len(lines) <= MOST_LINES else lines[:MOST_LINES]
    more = [f"- and {len(lines) - len(shown)} more: hansa status"] if len(lines) > len(shown) else []
    return "\n".join([f"[hansa] desk {desk_root}" + (f": {head}" if head else "")] + [f"- {x}" for x in shown] + more)


# -- the follower ---------------------------------------------------------------------------------------
def agents_rows(timeout: float = 15) -> list:
    """The live sessions as `claude agents --json` lists them. RuntimeError when it cannot say."""
    try:
        r = subprocess.run([claude_bin(), "agents", "--json"], capture_output=True, text=True, errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"claude agents --json could not run ({e})") from None
    if r.returncode:
        raise RuntimeError(f"claude agents --json ended with {r.returncode}: {(r.stderr or r.stdout).strip()[-200:]}")
    try:
        data = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError(f"claude agents --json printed what is not JSON: {r.stdout.strip()[:120]!r}") from None
    rows = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), None) \
        if isinstance(data, dict) else None
    if rows is None:
        raise RuntimeError(f"claude agents --json printed no list of sessions: {r.stdout.strip()[:120]!r}")
    return [x for x in rows if isinstance(x, dict)]


def session_row(pid: int, timeout: float = 15):
    """The session's entry in `claude agents --json`, or None when it has none. RuntimeError when it cannot say."""
    return next((x for x in agents_rows(timeout) if str(x.get("pid")) == str(pid)), None)


def session_name(pid: int, timeout: float = 15) -> str:
    """The session's name as `claude agents --json` lists it; '' when it cannot say."""
    try:
        return str((session_row(pid, timeout) or {}).get("name") or "")[:80]
    except RuntimeError:
        return ""


NAME_FIRST, NAME_MOST, NAME_LIMIT = 15.0, 300.0, 10.0  # seconds: the first wait, the longest, one lookup's limit


class Namer:
    """The follower's lookups of its session's name (only the follower looks it up; status and notify show what
    it recorded). A lookup runs in a thread of its own, with a time limit, so posting never waits for it. While
    the name is missing it looks again after NAME_FIRST seconds, then waits twice as long each time, up to
    NAME_MOST; once named, every NAME_MOST (Claude Code may rename it). look: the lookup, for tests."""

    def __init__(self, pid: int, name: str = "", first: float = 5.0, look=None):
        self.name, self.look = name, look or (lambda: session_name(pid, NAME_LIMIT))
        self.wait, self.next, self.thread, self.found = NAME_FIRST, time.time() + first, None, ""

    def _run(self) -> None:
        try:
            self.found = self.look() or ""
        except Exception:  # noqa: BLE001 - it cannot say; the next lookup may
            self.found = ""

    def tick(self) -> str:
        """The name as known now; starts a lookup when one is due, never waits for one."""
        if self.thread and not self.thread.is_alive():
            self.thread = None
            if self.found:
                self.name, self.wait = self.found, NAME_FIRST
            self.next = time.time() + (NAME_MOST if self.name else self.wait)
            if not self.found:
                self.wait = min(self.wait * 2, NAME_MOST)
        if not self.thread and time.time() >= self.next:
            self.found, self.thread = "", threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        return self.name


def _say(text: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + text, flush=True)


def missed_head(m) -> str:
    """The first line of what a follower posts about what an ended one missed: whose it was, and since when."""
    if not isinstance(m, dict):
        return "what changed while no session heard from hansa, as it is now:"
    whose = "session " + who(m.get("pid"), m.get("name") or "")
    at = time.strftime("%H:%M", time.localtime(m["at"])) if m.get("at") else "?"
    how = f"{whose} ended at {at}" if m.get("session_ended") else f"hansa stopped posting to {whose} at {at}"
    return f"{how}; since then, for its tasks and requests:"


CODE_SETTLE = 2.0  # seconds hansa's code stays as it is before a follower restarts on it (an upgrade writes files)


def code_mark() -> tuple:
    """hansa's code on disk as it stands: each module's path, change time and size."""
    out = []
    for f in sorted(Path(__file__).resolve().parent.rglob("*.py")):
        try:
            st = f.stat()
        except OSError:
            continue
        out.append((str(f), st.st_mtime_ns, st.st_size))
    return tuple(out)


def _restart(state: dict) -> None:
    """Run this follower again on hansa's code as it is now on disk, in this process (its pid stays), from where
    it was. Returns only when it could not."""
    if "notify-follow" not in sys.argv:
        return
    env = {**os.environ, "HANSA_NOTIFY_RESUME": json.dumps(state)}
    sys.stdout.flush()
    try:
        os.execve(sys.executable, [sys.executable, "-m", "hansa", *sys.argv[1:]], env)
    except OSError as e:
        _say(f"could not restart on the new code ({e}); this follower carries on with the old")


def _resumed():
    """What a follower restarted on new code was doing (_restart); None for one started afresh."""
    try:
        st = json.loads(os.environ.pop("HANSA_NOTIFY_RESUME", "") or "null")
    except ValueError:
        return None
    return st if isinstance(st, dict) and isinstance(st.get("pos"), int) else None


def follow(desk: Desk, key: str, sess: dict, sock: str, nonce: str, name: str = "", clear: str = "") -> int:
    """`hansa notify-follow`: run the follower of one session until that session's process ends."""
    priv = private_dir(desk)
    held = open(priv / f"{key}.lock", "a")
    for _ in range(10):
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            time.sleep(0.2)
    else:
        _say("another follower already runs for this session; this one ends")
        return 0
    resume = _resumed()
    was = resume or {}
    me_proc = {"pid": os.getpid(), "start": proc_start(os.getpid())}
    write_json(priv / f"{key}.state.json", {**me_proc, "nonce": nonce, "session": sess, "socket": sock,
                                             "since": was.get("since") or now()})
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    ask = source(desk)
    via_account = bool(account.desk_account(desk.root))
    poll = float(os.environ.get("HANSA_NOTIFY_POLL") or (2.0 if via_account else 1.0))
    gather = float(os.environ.get("HANSA_NOTIFY_GATHER") or 3.0)
    me = {"key": key, "session": sess, "socket": sock, "name": was.get("name") or name, "follower": me_proc,
          "uid": os.getuid(), "user": account.name_of(os.getuid()), "since": was.get("since") or now(),
          "last_post": was.get("last_post")}
    daily = DailyClear(clear, sess["pid"], was.get("clear")) if clear else None
    if daily:
        me["clear"] = daily.rec
    done = 0
    try:
        r = ask({"op": "start", "me": me, **({"resume": resume["pos"]} if resume else {})})
        pos = done = r["pos"]
        if resume:
            was_up, told_down, told_srt = was.get("was_up"), float(was.get("told_down") or 0), bool(was.get("told_srt"))
            told_stuck = float(was.get("told_stuck") or 0)
        else:
            was_up, told_down, told_srt, told_stuck = r.get("server_up"), 0.0, False, 0.0
        _say(("restarted on hansa's new code; " if resume else "") +
             f"following desk {desk.root} for session {sess['pid']} at {sock}, from byte {pos}")
        if r.get("summary"):
            if not _deliver(sock, message(str(desk.root), r["summary"], missed_head(r.get("missed"))), sess, me):
                return 0
            try:
                ask({"op": "caught_up", "me": me, "of": (r.get("missed") or {}).get("key")})
            except Exception as e:  # noqa: BLE001 - the next follower may then say it again
                _say(f"could not record that what was missed is told: {e}")
        pending, first, last, errors = [], 0.0, 0.0, 0
        namer = Namer(sess["pid"], me["name"])
        mark, changed = code_mark(), 0.0
        while True:
            if gone(sess):
                _say("the session's process ended; so does this follower")
                return 0
            me["name"] = namer.tick()  # recorded with the next feed, for hansa status and hansa notify
            new_mark = code_mark()
            if new_mark != mark:  # an upgrade: a module imported from now on would come from the new files
                mark, changed = new_mark, time.time()
            elif changed and time.time() - changed >= CODE_SETTLE:
                _say(f"hansa's code changed on disk; restarting on it from byte {done}")
                _restart({"pos": done, "since": me["since"], "name": me["name"], "last_post": me["last_post"],
                          "was_up": was_up, "told_down": told_down, "told_srt": told_srt, "told_stuck": told_stuck,
                          "clear": daily.rec if daily else None})
                changed = 0.0
            try:
                r = ask({"op": "feed", "me": me, "pos": pos, "done": done})
                errors = 0
            except Exception as e:  # noqa: BLE001 - the desk may be busy or moving; try again, then give up
                errors += 1
                _say(f"could not read the desk: {e}")
                if errors >= 60:
                    return 1
                time.sleep(poll)
                continue
            pos, items = r["pos"], r["items"]
            if any(x["key"] == ["server"] for x in items):
                told_down = True
            items, told_down, told_stuck = server_news(r, items, was_up, told_down, told_stuck)
            was_up = r.get("server_up")
            srt = any(x["key"] == ["srt"] for x in items)
            if srt and (told_srt or r.get("srt_ran")):  # said once until srt runs again; or it runs by now
                items = [x for x in items if x["key"] != ["srt"]]
            told_srt = False if r.get("srt_ran") else told_srt or srt
            if items:
                pending += [x["line"] for x in items]
                last = time.time()
                first = first or last
            if pending and (time.time() - last >= gather or time.time() - first >= 20):
                if not _deliver(sock, message(str(desk.root), pending), sess, me):
                    return 0
                pending, first, done = [], 0.0, pos
            if daily:
                daily.tick((me["last_post"] or {}).get("at") or 0.0, bool(pending))
            time.sleep(poll)
    finally:
        try:
            ask({"op": "end", "me": me, "done": done})
        except Exception as e:  # noqa: BLE001 - the register then shows it as gone quiet, which it is
            _say(f"could not record the end: {e}")
        write_json(priv / f"{key}.state.json", {**read_json(priv / f"{key}.state.json", {}), "ended": now()})
        held.close()


def server_news(r: dict, items: list, was_up, told_down: float, told_stuck: float) -> tuple:
    """What the follower says of hansa serve after a feed (run.server_state): not running only once its process
    is gone; busy or stuck once its rounds stay quiet for long; and, after either, that it runs again once it
    finishes a round after that was said. told_down, told_stuck: when that was said, 0 if not. Returns (items,
    told_down, told_stuck)."""
    st = r.get("server") or {"state": "up" if r.get("server_up") else "gone", "quiet": 0}
    if any(x["key"] == ["server"] for x in items):  # hansa serve stopped, and said so
        told_down = told_down or time.time()
    if st["state"] == "gone":
        if was_up and not told_down:
            items.append({"key": ["server"], "line": "hansa serve is not running (it stopped without a word): "
                                                     "no task starts or ends until it runs again · next: "
                                                     "hansa explain server"})
            told_down = time.time()
    elif st["state"] == "stuck":
        if not told_stuck and not told_down:
            mins = (st.get("quiet") or 0) // 60
            items.append({"key": ["server"], "line": f"hansa serve (pid {st.get('pid')}) has not finished a round "
                                                     f"for {mins} minutes: busy or stuck · next: hansa explain server"})
            told_stuck = time.time()
    elif st["state"] == "up" and (told_down or told_stuck) \
            and time.time() - (st.get("quiet") or 0) >= max(told_down, told_stuck):  # not the one stopping now
        items.append({"key": ["server"], "line": "hansa serve runs again"})
        told_down = told_stuck = 0.0
    return items, told_down, told_stuck


def _deliver(sock: str, text: str, sess: dict, me: dict) -> bool:
    """Post, trying again for a minute while the inbox is not there yet. False when the session is gone."""
    end = time.time() + POST_WAIT
    while True:
        try:
            post(sock, text)
            me["last_post"] = {"at": now(), "text": text.splitlines()[1][2:][:160] if "\n" in text else text[:160]}
            _say("posted: " + text.replace("\n", " | ")[:500])
            return True
        except OSError as e:
            if gone(sess):
                _say(f"the session's process ended before the post ({e}); so does this follower")
                return False
            if time.time() >= end:
                me["last_post"] = {"at": now(), "text": f"could not post: {e}", "failed": True}
                _say(f"gave up posting after {POST_WAIT} s ({e}): " + text.replace("\n", " | ")[:500])
                return True
            time.sleep(2)


# -- the daily clear ------------------------------------------------------------------------------------
# hansa coordinator --clear-daily 04-09 (or hansa notify on --clear-daily 04-09): once a day, in that window of
# hours, the follower types /clear into the session's tmux pane, only when the session is idle, nobody is typing
# and the input box is empty. An Enter that lands on a dialog picks an option, and /clear after someone's
# half-written line would send it: so anything it cannot read counts as busy. A session started inside today's
# window has just been told what to do by its principal: it is not cleared again that day.
CLEAR_EVERY = 60  # seconds between checks in the window (HANSA_CLEAR_EVERY)
CLEAR_QUIET = 300  # no post to the session for this long before a clear (HANSA_CLEAR_QUIET)
KEYS_QUIET = 90  # no key pressed in a terminal on the pane's tmux session for this long
CLEARED_WAIT = 15  # after Enter, the session's sessionId changes within this long
TYPED_WAIT = 0.5  # after typing /clear, before the box is read again
TYPED_READS = 4  # when the box cannot be read then, it is read again this many times, TYPED_WAIT apart
RULE = re.compile(r"─{3,}(?: .+ ─+)?")  # a rule line; the one above the box may carry the session's name
WINDOW = re.compile(r"^([0-9]{1,2})-([0-9]{1,2})$")
ESC = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]")
DIM = re.compile(r"\x1b\[2m.*?(?:\x1b\[0m|\x1b\[22m|$)")


def parse_window(text) -> tuple:
    """--clear-daily's window, HH-HH in local time (22-03 runs past midnight): (first hour, hour it ends)."""
    m = WINDOW.match(str(text or "").strip())
    a, b = (int(m[1]), int(m[2]) or 24) if m else (-1, -1)
    if not (0 <= a <= 23 and 1 <= b <= 24 and a != b):
        raise ValueError(f"--clear-daily takes a window of hours in local time, such as 04-09; not {text!r}")
    return a, b


def in_window(window: str, t: float) -> tuple:
    """(inside the window now, the day its latest start fell on, whether that window has passed)."""
    a, b = parse_window(window)
    lt = time.localtime(t)
    h, today = lt.tm_hour, time.strftime("%Y-%m-%d", lt)
    if a < b:
        return a <= h < b, today, h >= b
    day = today if h >= a else time.strftime("%Y-%m-%d", time.localtime(t - 86400))
    return h >= a or h < b, day, b <= h < a


def window_opened(window: str, day: str) -> float:
    """When the window opened on this day (in_window's day), in seconds since the epoch."""
    return time.mktime(time.strptime(f"{day} {parse_window(window)[0]:02d}", "%Y-%m-%d %H"))


def tmux(*args) -> str:
    """What a tmux command printed. RuntimeError when it could not run or failed."""
    try:
        r = subprocess.run(["tmux", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"tmux could not run ({e})") from None
    if r.returncode:
        raise RuntimeError(f"tmux {args[0]} ended with {r.returncode}: {r.stderr.strip()[-200:]}")
    return r.stdout


def _parent(pid: int) -> int:
    try:
        return int(subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True,
                                  timeout=10).stdout.strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def started_at(pid: int):
    """When the session's process started, in seconds since the epoch; None when it cannot be read.
    HANSA_CLEAR_STARTED stands in for it (tests)."""
    given = os.environ.get("HANSA_CLEAR_STARTED")
    if given:
        try:
            return float(given)
        except ValueError:
            return None
    s = proc_start(pid)
    try:
        if ":" in s:  # Linux: the boot's id, then clock ticks from boot to the start
            btime = next(int(x.split()[1]) for x in Path("/proc/stat").read_text().splitlines()
                         if x.startswith("btime "))
            return btime + int(s.rsplit(":", 1)[1]) / os.sysconf("SC_CLK_TCK")
        return float(s) if s else None  # macOS: seconds since the epoch
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def clear_pane(pid: int) -> tuple:
    """The tmux pane the session runs in (TMUX_PANE, whose process is the session or runs it): (pane, ''), or
    ('', why the daily clear cannot be done)."""
    pane = os.environ.get("TMUX_PANE", "")
    if not pane:
        return "", "the session does not run in tmux (TMUX_PANE is not set), and hansa types the /clear through tmux"
    try:
        top = int(tmux("display-message", "-p", "-t", pane, "#{pane_pid}").strip())
    except (RuntimeError, ValueError) as e:
        return "", f"tmux could not say which process runs in pane {pane} ({e})"
    p, seen = int(pid), set()
    while p > 1 and p not in seen:
        if p == top:
            return pane, ""
        seen.add(p)
        p = _parent(p)
    return "", f"the session (pid {pid}) does not run in tmux pane {pane}, whose process is {top}"


def _plain(line: str) -> str:
    return ESC.sub("", DIM.sub("", line))


def _rule(line: str) -> bool:
    return bool(RULE.fullmatch(ESC.sub("", line).strip()))


def box_text(capture: str):
    """What is typed in the session's input box, from `tmux capture-pane -p -e`: the text after the prompt, with
    dimmed placeholders and suggestions left out. None when the box is not where it is when nothing else is up (a
    rule, the box line: ❯ and a no-break space, a rule, and below them only the footer): a dialog, a menu, a box
    of several lines."""
    lines = capture.rstrip("\n").split("\n")
    last = max((i for i, x in enumerate(lines) if _rule(x)), default=-1)
    if last < 2 or not _rule(lines[last - 2]):
        return None
    box = _plain(lines[last - 1])
    return box[2:] if box.startswith("❯\xa0") else None


def idle_reading(pid: int) -> tuple:
    """(True, its sessionId) when `claude agents --json` shows the session idle with nothing waiting for an
    answer; (False, why not) otherwise, and when it cannot say."""
    try:
        row = session_row(pid)
    except RuntimeError as e:
        return False, str(e)
    if row is None:
        return False, f"claude agents --json lists no session with pid {pid}"
    if row.get("status") != "idle":
        return False, f"the session is {row.get('status')!r}, not idle"
    if row.get("waitingFor"):
        return False, f"the session waits: {str(row['waitingFor'])[:80]}"
    return True, str(row.get("sessionId") or "")


class DailyClear:
    """A follower's daily /clear of its session. Its record (rec) goes into the register, for hansa notify:
    window, pane, off (why it cannot be done), began (when the session started; None when that cannot be read), day,
    state (cleared / unconfirmed / left), at, held (the last reason), left (what hansa met when it typed /clear and
    left it)."""

    def __init__(self, window: str, pid: int, rec=None):
        self.window, self.pid = window, pid
        self.every = float(os.environ.get("HANSA_CLEAR_EVERY") or CLEAR_EVERY)
        self.quiet = float(os.environ.get("HANSA_CLEAR_QUIET") or CLEAR_QUIET)
        if isinstance(rec, dict) and rec.get("window") == window:  # restarted on new code: as it was
            self.rec = rec
            if "began" not in rec:  # a record from before hansa kept the session's start
                rec["began"] = started_at(pid)
        else:
            pane, why = clear_pane(pid)
            self.rec = {"window": window, "pane": pane, "off": why or None, "began": None if why else started_at(pid)}
            _say(f"daily clear {window}: " + (f"off: {why}" if why else f"on, through tmux pane {pane}"))
            if not why:
                self.new_today(time.time())
        self.next, self.idle_since, self.inside, self.sid = 0.0, None, None, ""

    def new_today(self, t: float) -> None:
        """A session this follower has no record of the day for, started inside today's window after it opened:
        its principal has just told it what to do, so today counts as done. Unknown start: the clear goes on."""
        inside, day, _ = in_window(self.window, t)
        began = self.rec.get("began")
        if not inside or began is None:
            return
        if window_opened(self.window, day) <= began <= t:
            self.rec.update(day=day, state="new", at=began)
            _say(f"daily clear {self.window}: the session started at {time.strftime('%H:%M', time.localtime(began))}, "
                 "inside today's window; next clear tomorrow")

    def hold(self, why: str) -> None:
        self.rec.update(held=why, held_at=now())
        _say(f"daily clear held: {why}")

    def tick(self, last_post: float, pending: bool) -> None:
        """Called on each round of the follower; checks once every minute, in the window."""
        t = time.time()
        if self.rec.get("off") or t < self.next:
            return
        self.next = t + self.every
        inside, day, _ = in_window(self.window, t)
        if inside != self.inside:
            _say(f"daily clear: {'inside' if inside else 'outside'} the window {self.window}")
            self.inside = inside
        if not inside:
            self.idle_since = None
            return
        if self.rec.get("day") != day:
            self.rec.update(day=day, state=None, at=None, held=None, held_at=None, left=None, step=None)
        if self.rec.get("state"):  # done today
            return
        why = self.why_not(t, last_post, pending)
        if why:
            self.hold(why)
        else:
            self.clear()

    def why_not(self, t: float, last_post: float, pending: bool) -> str:
        """Why the session is not to be cleared now; '' when every condition holds."""
        ok, said = idle_reading(self.pid)
        if not ok:
            self.idle_since = None
            return said
        self.sid = said
        if self.idle_since is None:
            self.idle_since = t
            return "idle on one reading; a second one comes a minute later"
        if t - self.idle_since < self.every:
            return "idle on readings less than a minute apart"
        if pending or t - last_post < self.quiet:
            return "hansa has a message for the session" if pending else \
                f"hansa posted to the session {int(t - last_post)} s ago"
        pane = self.rec["pane"]
        try:
            where = tmux("display-message", "-p", "-t", pane, "#{session_id}").strip()
            keys = [int(x) for x in tmux("list-clients", "-t", where, "-F", "#{client_activity}").split()]
        except (RuntimeError, ValueError) as e:
            return f"could not read when a key was last pressed ({e})"
        if keys and t - max(keys) < KEYS_QUIET:
            return f"a key was pressed in a terminal on the session's tmux session {int(t - max(keys))} s ago"
        try:
            box = box_text(tmux("capture-pane", "-p", "-e", "-t", pane))
        except RuntimeError as e:
            return f"could not read the pane ({e})"
        if box is None:
            return "no input box alone at the bottom of the pane (a dialog, a menu, or a box of several lines)"
        if box.strip(" "):
            return "there are words in the input box"
        return ""

    def left(self, what: str, step: str = "") -> None:
        """/clear was typed, or may have been, and what became of it is not known: nothing more is typed today.
        step: 'typing' when typing /clear itself failed, 'back' when the backspaces taking it back failed."""
        self.rec.update(state="left", at=now(), held=None, left=what, step=step or None)
        head = "hansa tried to type /clear" if step == "typing" else "typed /clear"
        back = ("it is not known how many of the six backspaces taking it back went through" if step == "back"
                else "nothing taken back")
        _say(f"daily clear: {head}, but then {what}; no Enter, {back}, and no other try today: "
             "/clear may be left in the box")

    def clear(self) -> None:
        """Type /clear; press Enter only when the box then reads exactly that and the session is still idle."""
        pane = self.rec["pane"]
        self.idle_since = None  # whatever happens, two idle readings again before the next try
        step = "typing"  # what tmux was doing when it failed
        try:
            tmux("send-keys", "-t", pane, "-l", "/clear")
            step = ""
            for _ in range(1 + TYPED_READS):  # a box that cannot be read yet: again, for about two seconds
                time.sleep(TYPED_WAIT)
                box = box_text(tmux("capture-pane", "-p", "-e", "-t", pane))
                if box is not None:
                    break
            if box is None:  # what hansa typed may be there still; not typed again today
                return self.left("the input box could not be read")
            ok, said = idle_reading(self.pid)
            if box.rstrip(" ") != "/clear" or not ok:
                if not box.rstrip(" ").endswith("/clear"):  # where its /clear went is not known: left as it is
                    return self.left(f"the box read {box!r}")
                step = "back"
                tmux("send-keys", "-t", pane, *["BSpace"] * 6)  # take it back; whatever else is there stays
                self.hold(f"after /clear was typed the box read {box!r} and the session "
                          f"{'was idle' if ok else 'was not idle: ' + said}; no Enter; the /clear was taken back")
                return
            tmux("send-keys", "-t", pane, "Enter")
        except RuntimeError as e:
            return self.left(f"tmux failed ({e})", step)
        end, sid = time.time() + CLEARED_WAIT, ""
        while time.time() < end:
            time.sleep(0.5)
            try:
                sid = str((session_row(self.pid) or {}).get("sessionId") or "")
            except RuntimeError:
                continue
            if sid and sid != self.sid:
                break
        if sid and sid != self.sid:
            self.rec.update(state="cleared", at=now())
            _say(f"daily clear: cleared the session (sessionId {self.sid or '?'} -> {sid})")
        else:
            self.rec.update(state="unconfirmed", at=now())  # not typed again today: what it met is not known
            _say(f"daily clear: pressed Enter after /clear, but the sessionId did not change within {CLEARED_WAIT} s "
                 f"(still {sid or 'unreadable'}); no other try today")


def clear_text(c, t: float = 0) -> str:
    """A follower's daily clear for hansa notify: its window and today's state; '' when it has none."""
    if not isinstance(c, dict) or not c.get("window"):
        return ""
    if c.get("off"):
        return f"daily clear {c['window']}: off ({c['off']})"
    try:
        inside, day, passed = in_window(c["window"], t or time.time())
    except ValueError:
        return f"daily clear {c['window']}: not a window"
    mine = c if c.get("day") == day else {}
    hm = time.strftime("%H:%M", time.localtime(mine["at"])) if mine.get("at") else "?"
    held = f"held: {mine['held']}" if mine.get("held") else ""
    if mine.get("state") == "cleared":
        today = f"cleared at {hm}"
    elif mine.get("state") == "new":
        today = f"started {hm}, inside today's window; next clear tomorrow"
    elif mine.get("state") == "left":
        today = (("hansa tried to type /clear" if mine.get("step") == "typing" else "/clear typed")
                 + f" at {hm}, but then {mine.get('left') or 'the input box could not be read'}"
                 + ("; it is not known how many of the six backspaces taking it back went through"
                    if mine.get("step") == "back" else "") + ": /clear may be left in the box")
    elif mine.get("state"):
        today = f"Enter pressed at {hm}, but no new session was seen"
    elif inside:
        today = held or "in the window, not checked yet"
    elif passed and c.get("began") and c["began"] >= window_opened(c["window"], day):
        # started inside the window (with its follower started after it) or after it: nothing was due today
        a, b = parse_window(c["window"])
        after = c["began"] >= window_opened(c["window"], day) + ((b - a) % 24) * 3600
        today = (f"started {time.strftime('%H:%M', time.localtime(c['began']))}, "
                 f"{'after' if after else 'inside'} today's window; next clear tomorrow")
    elif passed:
        today = "window passed without a clear" + (f" (last {held})" if held else "")
    else:
        today = "window not reached yet today"
    return f"daily clear {c['window']}: {today}"


# -- starting and stopping one ------------------------------------------------------------------------------
def spawn(desk: Desk, args: list, log: Path) -> None:
    """Start `hansa ARGS` detached: a session of its own, not the caller's child, keeping none of the caller's
    files open (so the caller's tool call ends at once), its output going to log."""
    env = dict(os.environ)
    here = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = here + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    argv = [sys.executable, "-m", "hansa", "--desk", str(desk.root), *args]
    out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    child = os.fork()
    if child == 0:  # pragma: no cover - runs in the forked process
        try:
            os.setsid()
            if os.fork():
                os._exit(0)
            null = os.open(os.devnull, os.O_RDONLY)
            os.dup2(null, 0)
            os.dup2(out, 1)
            os.dup2(out, 2)
            os.closerange(3, min(os.sysconf("SC_OPEN_MAX"), 65536) if os.sysconf("SC_OPEN_MAX") > 0 else 65536)
            os.chdir("/")
            os.execve(argv[0], argv, env)
        finally:
            os._exit(127)
    os.close(out)
    os.waitpid(child, 0)


def running(desk: Desk, key: str):
    """The follower of this session, when one runs: its state, else None."""
    st = read_json(private_dir(desk) / f"{key}.state.json", {}) or {}
    return st if st.get("pid") and not st.get("ended") and alive(st["pid"], st.get("start", "")) else None


def start(desk: Desk, sess: dict, sock: str, name: str = "", wait: bool = True, clear: str = "") -> str:
    """Start the follower of this session: 'started', 'already' (one runs), or 'unknown' (it has not said yet).
    clear: the window of its daily clear, if any."""
    key = key_of(sess)
    if running(desk, key):
        return "already"
    nonce = uuid.uuid4().hex
    spawn(desk, ["notify-follow", "--key", key, "--pid", str(sess["pid"]), "--start", sess.get("start") or "",
                 "--socket", sock, "--nonce", nonce, "--name", name, *(["--clear-daily", clear] if clear else [])],
          private_dir(desk) / f"{key}.log")
    end = time.time() + (10 if wait else 0)
    while time.time() < end:
        st = running(desk, key)
        if st and st.get("nonce") == nonce:
            return "started"
        if st:
            return "already"
        time.sleep(0.1)
    return "unknown"


def socket_path(pid: int) -> str:
    """A short inbox path for a session hansa starts, in a folder only this account can open."""
    for base in (os.environ.get("XDG_RUNTIME_DIR"), os.environ.get("TMPDIR"), "/tmp"):
        if not base or not os.path.isabs(base):
            continue
        d = os.path.join(base.rstrip("/"), f"hansa-{os.getuid()}")
        if len(d) > 80:  # a socket's path stays under 104 bytes on macOS
            continue
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        except OSError:
            continue
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            continue
        if stat.S_IMODE(st.st_mode) & 0o077:
            os.chmod(d, 0o700)
        path = os.path.join(d, f"c{pid}.sock")
        try:
            if stat.S_ISSOCK(os.lstat(path).st_mode):  # left by an earlier process with this pid
                os.unlink(path)
        except FileNotFoundError:
            pass
        return path
    raise RuntimeError("there is no short folder only this account can open for the session's inbox")


def clear_said(pid: int, window: str, running: bool = False) -> str:
    """What starting a daily clear for this session says: when it clears, or why it cannot. running: the session
    already runs (hansa notify on), so it has no hook that shows it the desk after a clear."""
    pane, why = clear_pane(pid)
    a, b = parse_window(window)
    if why:
        return f"The daily clear is off: {why}. Notices still go out."
    return (f"Once a day between {a:02d}:00 and {b % 24:02d}:00, when this session is idle, nobody is typing and its "
            f"input box is empty, hansa types /clear into tmux pane {pane}."
            + ("\nhansa cannot add a hook to a session that is already running, so after a clear this one does not "
               "read hansa status by itself (a session started with hansa coordinator --clear-daily does)."
               if running else ""))


def turn_on(desk: Desk, clear: str = "") -> str:
    sess, sock = session_here(), os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET", "")
    if clear:
        try:
            parse_window(clear)
        except ValueError as e:
            raise SystemExit(f"hansa: {e}; nothing was started")
    if os.environ.get("HANSA_TASK"):
        raise SystemExit("hansa: notify is for the coordinator's session, not a worker's; nothing was started")
    if not sess or not sock:
        raise SystemExit("hansa: notify on starts hansa posting to the Claude Code session it is run from, and this "
                         "is not run from one (CLAUDE_PID and CLAUDE_CODE_MESSAGING_SOCKET are not set); nothing was "
                         "started. Run it with the coordinator session's Bash tool")
    how = start(desk, sess, sock, clear=clear)  # its follower looks the name up; this command starts no claude
    name = next((r.get("name") or "" for r in records(desk) if same(r.get("session"), sess)), "")
    if how == "already":
        return (f"hansa already notifies this session, {who(sess['pid'], name)}; nothing was started. hansa notify lists "
                "the desk's followers" + ("; for a daily clear, hansa notify off, then on again with --clear-daily"
                                          if clear else ""))
    if how == "unknown":
        raise SystemExit(f"hansa: the follower did not say it started within 10 s; see "
                         f"{private_dir(desk) / (key_of(sess) + '.log')}")
    said = (f"hansa now posts to this session, {who(sess['pid'], name)}, through its inbox: its own tasks and requests, and "
            "those no other session hears. Do not keep hansa wait running in the background meanwhile. "
            "Stop it with hansa notify off")
    return said + ("\n" + clear_said(sess["pid"], clear, running=True) if clear else "")


def turn_off(desk: Desk) -> str:
    sess = session_here()
    if not sess:
        raise SystemExit("hansa: notify off stops the follower of the Claude Code session it is run from, and this "
                         "is not run from one; nothing was stopped. hansa notify lists the desk's followers")
    st = running(desk, key_of(sess))
    if not st:
        return "hansa does not notify this session; nothing to stop"
    try:
        os.kill(st["pid"], signal.SIGTERM)
    except OSError:
        pass
    end = time.time() + 10
    while time.time() < end and alive(st["pid"], st.get("start", "")):
        time.sleep(0.1)
    return ("hansa no longer posts to this session. To hear when tasks end, run hansa wait in the background, "
            "or hansa notify on again")


def when(t) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "?"


def list_text(desk: Desk) -> str:
    rows = source(desk)({"op": "list"})["followers"]
    if not rows:
        return ("hansa notifies no session of this desk. From the coordinator's session: hansa notify on (a session "
                "started with hansa coordinator is notified by itself)")
    out = [f"hansa notifies {len(rows)} session(s) of desk {desk.root} through their inbox:"]
    for r in rows:
        lp = r.get("last_post") or {}
        out.append(f"  {who((r.get('session') or {}).get('pid'), r.get('name') or '')} · since "
                   f"{when(r.get('since'))} · last post: " + (f"{when(lp.get('at'))} {lp.get('text', '')}" if lp
                                                               else "none yet"))
        if clear_text(r.get("clear")):
            out.append(f"    {clear_text(r.get('clear'))}")
    return "\n".join(out)


def who(pid, name: str = "") -> str:
    """A session as hansa names it everywhere: its pid, and its name as its follower last recorded it."""
    return f"pid {pid} ({name or 'no name yet'})"


def names(desk: Desk) -> list:
    """The sessions hansa notifies, for hansa status, in the form hansa names a session everywhere."""
    return [who((r.get("session") or {}).get("pid"), r.get("name") or "") for r in live_records(desk)]
