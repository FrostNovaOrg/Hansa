"""hansa tells a coordinator's Claude Code session when something needs it, through the session's inbox.

Every interactive Claude Code session listens on a local Unix socket, its inbox: one line of JSON posted
there arrives in the session as a message from another session. A follower is a small background process
per session and desk. It reads the desk's event log from where it left off, picks what the coordinator
waits for (a task ended, a verification ended, a queued merge check ended,
a provider an open request names became ready, the principal answered a release, a release
ended or became void, the server stopped, free space went under the floor or came back over it), and posts it to
that session's inbox. It ends when the session's process does.

    `hansa coordinator`   starts one for the session it launches (--messaging-socket-path, a path it chose)
    `hansa notify on`     starts one for the session it is run from (CLAUDE_PID, CLAUDE_CODE_MESSAGING_SOCKET)
    `hansa notify off`    stops that one; `hansa notify` lists the desk's followers

When hansa's code on disk changes (an upgrade), a follower runs itself again on the new code, in the same process
and from where it was: otherwise a module it imports later would come from the new files, and the rest from the old.

Where things are kept:
    .hansa/followers/<key>.json   the desk's register: one record per follower (session, socket, read
                                  position, when it was last seen, its last post); written by the desk's owner
    <private>/<key>.state.json    the follower's own: its pid, the nonce it was started with, and, once the
                                  desk gave it, the log position it reads from (`notify on` waits for it)
    <private>/<key>.lock          held while it runs: one follower per session and desk
    <private>/<key>.log           what it printed
<private> is .hansa/followers/ too, except for root on an account's desk (see account.py): root neither reads
nor writes in the desk, so its follower asks the account's hansa for the log (`hansa notify-feed`) and keeps
its own files under root's home.

A task or request goes to the session that made it (`hansa add`, `retry`, `verify`, `merge` and
`ask` record it: its pid and start time), if that session has a live follower; otherwise to every follower
of the desk. Desk-wide events go to every follower.
"""
from __future__ import annotations

import contextlib
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

from . import account, disk, session_value, without_session
from .desk import Desk, event_record, lane_targets, mkdir_private, now, read_json, write_json
from .run import alive, claude_bin, proc_start, rethink_text, server_state, srt_needed, srt_record

LIVE_SECONDS = 30  # a follower not seen for this long is not live (it polls every second or two)
SEEN_EVERY = 10  # a feed that changes nothing else in the follower's record writes its seen this often
MOST_LINES = 10  # past this many events in one message, the rest are counted, and hansa status has them
POST_WAIT = 60  # seconds to try a post before gathering new lines and trying again
KEEP_DAYS = 7  # ended followers' records are kept this long
KEY = re.compile(r"^[0-9]+-[0-9a-f]{8}$")
TASK_ID = re.compile(r"^t[0-9]+$")
LOOK_EVERY = 60.0  # seconds: a follower looks its session's status up at most this often, for the coordinator alert


# -- the session a command runs in ---------------------------------------------------------------------
def session_here():
    """The Claude Code session this command was run from: {pid, start}, or None outside one (a shell, the
    page, a worker). The start time tells the session from a later process given the same pid."""
    pid = session_value("CLAUDE_PID")
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
        e = event_record(line)
        if e is not None:
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
    else:
        head = f"{tid} {st}"
    head += f": {m.get('title', '')}"
    if m.get("verification_notes"):
        count = len(m["verification_notes"])
        head += f" · kept {count} {'note' if count == 1 else 'notes'} (hansa gaps)"
    chk = m.get("check") or {}
    if st == "done" and m.get("kind") != "verify":
        head += (" (no changes)" if not m.get("commits") else
                 " (its check could not run: the machine's problem, not the task's)" if chk.get("could_not_run") else
                 " (check passed)" if chk.get("ok") else
                 f" (check FAILED{': ' + chk['note'] if chk.get('note') else ''})" if chk else "")
        if m.get("verify_after_error"):
            head += f" · verification could not be queued: {m['verify_after_error']}"
    elif st in ("failed", "cancelled") and m.get("error") and m.get("error") != st:
        head += f" ({str(m['error'])[:200]})"
    elif st == "merging":
        from .views import merge_check_waits_for
        ahead = merge_check_waits_for(desk, m)
        what = "merge" if (m.get("merge_check") or {}).get("plain") else "merge check"
        head += f" (its {what} is queued and waits for {ahead})" if ahead else f" (its {what} is queued or running)"
    from .views import background_note
    from .run import oom_lines
    rethink = rethink_text(desk, m) if m.get("kind") == "verify" else ""
    note = background_note(m)
    memory = [*oom_lines(m, "run"), *oom_lines(chk, "check")]
    memory += [line for i, a in enumerate(m.get("attempts") or [], 1) for line in oom_lines(a, f"earlier run {i}")]
    return (f"{head}{' · ' + rethink if rethink else ''} · next: {_next_step(desk, tid)}"
            + (" · " + " · ".join(memory) if memory else "")
            + (f" · {note}" if note else ""))


def _item(desk: Desk, e: dict):
    """What one event tells the coordinator: (key, line, recorded session, desk-wide, the session not to tell),
    or None. The session that cancelled a task with hansa cancel does not hear that it was cancelled."""
    kind, tid = e.get("event"), e.get("task")
    if kind == "task_after_held":
        from . import after
        try:
            m = desk.meta(tid)
        except SystemExit:
            return None
        held = [d for d in after.pending(m, desk.tasks()) if d["held"]
                and (d["task"], d["latest"]) == (e.get("after"), e.get("latest"))]
        if m["state"] != "queued" or not held:
            return None
        return (("after", tid, e["after"], e["latest"]),
                f"{tid}: {m['title']} · {after.waiting(m, held)}", m.get("session"), False, None)
    if kind in ("task_finished", "task_checked", "task_not_started", "merge_check_ended", "task_merged"):
        if kind == "merge_check_ended" and e.get("note") == "cancelled":  # the coordinator cancelled it
            return None
        if kind == "task_merged" and not e.get("merge_checked") and not e.get("waited"):  # the coordinator saw it
            return None
        try:
            m = desk.meta(tid)
        except SystemExit:
            return None
        if kind in ("task_finished", "task_checked"):
            from .run import verification_takes_over
            if m.get("verify_after") and (m.get("state") in ("queued", "running") or verification_takes_over(m)):
                return None
            # A refusal has its own completion event: an older event read during the error write
            # must not post the same line before that event arrives.
            if m.get("verify_after_error") and not e.get("verification_error"):
                return None
        if kind == "task_finished" and e.get("state") in ("checking", "queued"):
            return None
        if kind == "merge_check_ended":
            key = ("merge", tid)
            from .views import waited_text
            said = (f"{tid}'s merge: {waited_text(e)}" if e.get("waited") else
                    f"{tid}'s merge check ended; nothing was merged: {e.get('note') or 'the check failed'}"
                    + (f" (it ran: {desk.redact(e['command'])})" if e.get("command") else ""))
            line = f"{said}: {m.get('title', '')} · next: {_next_step(desk, tid)}"
        elif kind == "task_merged":
            how = ("in its turn behind a merge check, without a check of its own" if e.get("waited") else
                   "after its merge check passed")
            key, line = ("merge", tid), (f"{tid} merged into {e.get('into')} {how}: {m.get('title', '')}"
                                         + (f" · note: {m['unverified']}" if m.get("unverified") else "")
                                         + (f" · its merge check ran: {desk.redact(e['command'])}"
                                            if e.get("command") and not e.get("waited") else ""))
        else:
            key, line = ("task", tid), task_line(desk, m)
        if kind in ("merge_check_ended", "task_merged"):
            from .run import oom_lines
            memory = oom_lines(m.get("merge_check_result") or {}, "merge check")
            if memory:
                line += " · " + " · ".join(memory)
        return key, line, m.get("session"), False, m.get("cancel_by") if m.get("state") == "cancelled" else None
    if kind == "ask_ready":  # a provider open requests name became ready since they were made (views.ready_news)
        named = [a for a in desk.asks() if a.get("id") in (e.get("ids") or []) and not a.get("closed")]
        if not named:
            return None
        sessions = [a.get("session") for a in named]
        return (("ready", e.get("provider")),
                f"{e.get('provider')} is ready now: done? Still open: "
                + "; ".join(f"request #{a['id']} ({str(a.get('text', ''))[:120]})" for a in named)
                + " (the principal answers in the conversation; record the outcome: hansa ask --close N \"<outcome>\")",
                sessions[0] if all(same(x, sessions[0]) for x in sessions) else None, False, None)
    if kind in ("release_answered", "release_ended", "release_watch_ended", "release_void", "release_forbidden",
                "release_stopped") and not e.get("quiet"):
        return _release_item(desk, e)
    if kind == "release_after_merge_waits":
        return (("after-merge", e.get("lane"), e.get("target")),
                f"lane {e.get('lane')} target {e.get('target')} after merges {e.get('merges')}: {e.get('why')}"
                " · next: hansa explain lane " + str(e.get("lane")), None, False, None)
    if kind == "provider_credit":
        return ("credit", e.get("provider"), e.get("since")), e["text"], None, True, None
    if kind == "disk_low":
        return ("disk",), (f"free space is under the floor ({e.get('floor') or 0:g} GB): "
                           + "; ".join(e.get("volumes") or []) + f": hansa serve starts {disk.STARTS} until "
                           + (e.get("again") or f"every volume has {(e.get('floor') or 0) + disk.MARGIN:g} GB free")
                           + "; what runs goes on · next: free "
                           "space, or hansa explain server"), None, True, None
    if kind == "memory_low":
        return ("memory",), (f"available memory is under the floor ({e.get('floor') or 0:g} GB): "
                             f"{disk.gb(e.get('available') or 0)} available: hansa serve starts {disk.STARTS} "
                             f"until {disk.gb(e.get('again') or 0)} is available; what runs goes on "
                             "· next: hansa explain server"), None, True, None
    if kind == "memory_ok":
        return ("memory",), ("available memory is back over the floor"
                             + (f" ({e.get('floor'):g} GB)" if e.get("floor") else " (the floor is off)")
                             + ": hansa serve starts things again"), None, True, None
    if kind == "memory_look_failed":
        return ("memory", "look"), (f"hansa serve could not look at available memory ({e.get('error')}), "
                                     "so it holds nothing back until it can · next: hansa explain server"), None, True, None
    if kind == "memory_look_ok":
        return ("memory", "look"), "hansa serve can look at available memory again", None, True, None
    if kind == "disk_ok":
        return ("disk",), ("free space is back over the floor"
                           + (f" ({e.get('floor') or 0:g} GB)" if e.get("floor") else " (the floor is off)")
                           + ": hansa serve starts things again"), None, True, None
    if kind == "disk_look_failed":
        return ("disk", "look"), (("hansa serve cannot look at the free space of " + "; ".join(e.get("places") or [])
                                   + ": the free space of its volume is not known, and holds nothing back")
                                  if e.get("places") else
                                  f"hansa serve could not look at free space ({e.get('error')}), so it holds nothing "
                                  "back until it can") + " · next: hansa explain server", None, True, None
    if kind == "disk_look_ok":
        return ("disk", "look"), ("hansa serve can look at the free space of " + "; ".join(e.get("places"))
                                  if e.get("places") else "hansa serve can look at free space again"), None, True, None
    if kind == "server_stopped":
        return ("server",), ("hansa serve stopped: no task starts or ends until it runs again · next: "
                             "hansa up"), None, True, None
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
    rid = r["id"]
    head = f"release #{rid} of {release.what(r)} ({r.get('base')} at {str(r.get('commit', ''))[:10]})"
    kind = e.get("event")
    if kind == "release_answered":
        key = ("release-answer", rid)
        line = (f"{head}: the principal said yes; hansa serve runs `{r.get('command')}` now · you are told when it ends"
                if e.get("yes") else
                f"{head}: the principal refused it" + (f": {e.get('reason')}" if e.get("reason") else "")
                + " · nothing was run; do not push or deploy it yourself")
    elif kind == "release_watch_ended":
        key = ("release-watch", rid)
        line = f"{head}: {release.watch_text(r)}"
        w = r.get("watch_run") or {}
        if w.get("state") == "failed":
            tail = " | ".join(w.get("tail", [])[-5:])
            if tail:
                line += f" · last lines: {tail}"
    elif kind == "release_ended":
        key = ("release", rid)
        tail = " | ".join((r.get("tail") or [])[-5:])
        line = (f"{head} done: `{r.get('command')}` exited 0" if e.get("ok") else
                f"{head} FAILED" + (f" (exit {e.get('exit')})" if e.get("exit") is not None else "")
                + (f": {e.get('problem')}" if e.get("problem") else "") + (f" · last lines: {tail}" if tail else "")
                + " · next: hansa release; fix what failed, then ask again: " + release.ask_again(r))
        if e.get("ok"):
            ln = desk.settings()["lanes"].get(r.get("lane")) or {}
            rs = [x for x in release.releases(desk) if x.get("lane") == r.get("lane")]
            for target, t in lane_targets(ln).items():
                if t.get("after") != r.get("target"):
                    continue
                if any(x.get("target") == target and (x.get("state") in release.OPEN or
                       (x.get("state") == "done" and x.get("commit") == r.get("commit")))
                       for x in rs):
                    continue
                line += f" · next: hansa release {r['lane']} {target}"
        if r.get("after_merge") and not e.get("ok"):
            line = (f"{head} check FAILED" + (f" (exit {e.get('exit')})" if e.get("exit") is not None else "")
                    + (f": {e.get('problem')}" if e.get("problem") else "")
                    + (f" · last lines: {tail}" if tail else "")
                    + " · merges since its last passing run: " + release.merges_text(
                        r["after_merge"]["since_pass"], r["after_merge"].get("since_pass_count"))
                    + " · next: hansa release; make a task to fix what failed, then this target runs again after "
                    "that task's merge")
        if e.get("stop_late"):
            line += " · the stop came after its command had ended, so it ended as its exit says"
    elif kind == "release_stopped":
        key = ("release", rid)
        who = "the principal" if e.get("by") == "page" else e.get("by") or "?"
        line = (f"{head} was stopped by {who}" + (f": {e.get('reason')}" if e.get("reason") else "")
                + (f" · its command had run and got TERM{' then KILL' if e.get('killed') else ''}; the server may be "
                   "half-switched: hansa release shows its last lines" if e.get("ran") else " · nothing was run")
                + " · next: ask again when it should go out, " + release.ask_again(r))
    elif kind == "release_void":
        key = ("release", rid)
        line = f"{head} is void: {e.get('why')}; nothing was run · next: ask again, {release.ask_again(r)}"
    else:
        key = ("release", rid)
        line = (f"{head} refused: what goes out holds words the principal keeps from going out · next: "
                f"hansa release lists where")
    from .run import oom_text
    if kind in ("release_ended", "release_stopped") and oom_text(r):
        line += " · " + oom_text(r)
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
        review = _review_task(desk, key)
        out.pop(key, None)  # the latest line of a task comes last
        out[key] = {"key": list(key), "line": desk.redact(line),  # review: the task waits for review (Watch)
                    **({"review": review} if review else {})}
    return list(out.values())


def _review_task(desk: Desk, key: tuple):
    """A completion's review task: queue-time verifications post on behalf of their parent."""
    if key[0] not in ("task", "merge"):
        return None
    try:
        m = desk.meta(key[1])
        if m.get("kind") == "verify":
            m = desk.meta(m.get("parent") or "")
            if not m.get("verify_after"):
                return None
    except SystemExit:
        return None
    return m["id"] if waits_review(desk, m["id"]) else None


def waits_review(desk: Desk, tid) -> bool:
    """Whether finished work needs review now; queue-time requests wait for their verification."""
    if not isinstance(tid, str) or not TASK_ID.match(tid):
        return False
    try:
        m = desk.meta(tid)
    except SystemExit:
        return False
    from .run import verification_due
    from .views import verification
    try:
        v = verification(desk, m)
    except SystemExit:  # its verification folder is gone
        v = None
    return (m.get("state") == "done" and m.get("kind") != "verify" and not verification_due(m)
            and not (m.get("verify_after") and v and v["state"] in ("queued", "running")))


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
    mkdir_private(register_dir(desk))
    write_json(register_dir(desk) / f"{key}.json", rec)


def _server(desk: Desk) -> dict:
    """What a follower is told of hansa serve: server_up for a follower on older code, server for this one."""
    st = server_state(desk)
    return {"server_up": st["state"] != "gone", "server": st}


def handle(desk: Desk, req: dict) -> dict:
    """The desk's part of a follower's work, done as the desk's owner: start, feed, seen, end, list."""
    op = req.get("op")
    if op == "list":
        return {"followers": live_records(desk)}
    me = req.get("me") or {}
    key = str(me.get("key", ""))
    if not KEY.match(key):
        raise ValueError(f"not a follower's key: {key!r}")
    old = read_json(register_dir(desk) / f"{key}.json", {}) or {}
    old.pop("clear", None)  # an older follower's daily clear, which followers no longer do: dropped at the next write
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
        srv = _server(desk)  # what this follower is told of hansa serve
        summary, summary_items, review = [], [], set()
        if prev and int(prev.get("pos") or 0) < end:  # it ended before reading to the end: what was missed
            events, _ = read_log(desk, int(prev.get("pos") or 0), end - int(prev.get("pos") or 0))
            seen = set()
            for x in items_for(desk, events, me):  # one line per task or request, as it is now
                k = ("task", x["key"][1]) if x["key"][0] in ("task", "merge") else tuple(x["key"])
                if k in seen:
                    continue
                if k == ("server",) and srv["server"]["state"] != "gone":
                    continue  # it runs now: that it stopped while this session was away is not news
                seen.add(k)
                try:
                    summary.append(desk.redact(task_line(desk, desk.meta(k[1]))) if k[0] == "task" else x["line"])
                except SystemExit:  # a task folder gone meanwhile
                    continue
                summary_items.append({**x, "key": list(k), "line": summary[-1]})
                if x.get("review"):
                    review.add(x["review"])
        if prev and not summary:  # nothing to tell: whoever starts next need not tell it either
            _write_record(desk, prev["key"], {**prev, "caught_up": now()})
        done = int(prev.get("pos") or 0) if summary else end  # a refused summary still waits for the next follower
        _write_record(desk, key, {**me, "pos": done, "since": me.get("since") or now(), "seen": now(), "ended": None})
        missed = None
        if summary:
            ps = prev.get("session") or {}
            missed = {"key": prev["key"], "pid": ps.get("pid"), "name": prev.get("name") or "",
                      "at": prev.get("ended") or prev.get("seen"), "session_ended": _proc_gone(ps)}
        return {"pos": end, "done": done, "summary": summary, "summary_items": summary_items,
                "missed": missed, "summary_review": sorted(review), **srv}
    if op == "caught_up":  # the follower posted what an ended one missed: nobody is to post it again
        k = str(req.get("of", ""))
        r = read_json(register_dir(desk) / f"{k}.json", {}) if KEY.match(k) else {}
        if isinstance(r, dict) and r.get("ended"):
            _write_record(desk, k, {**r, "caught_up": now()})
        return {}
    if op in ("feed", "seen"):  # seen: a follower still trying a post (_deliver) stays live meanwhile
        events, pos = read_log(desk, int(req.get("pos") or 0)) if op == "feed" else ([], 0)
        items = items_for(desk, events, me) if events else []
        rec = {**old, **me, "pos": int(req.get("done") or 0), "ended": None}
        if {**rec, "seen": None} != {**old, "seen": None} or time.time() - (old.get("seen") or 0) >= SEEN_EVERY:
            _write_record(desk, key, {**rec, "seen": now()})  # else nothing changed but seen, and it is recent
        if op == "seen":
            return {}
        out = {"pos": pos, "items": items, **_server(desk), "srt_ran": _srt_ran(desk)}
        if req.get("minutes"):  # what the follower watches its session for (Watch, Turns); it asks once a minute
            from .alert import chinese, session_minutes
            out["coordinator_alert"] = session_minutes(desk)
            out["turns"], out["alert_zh"] = turns_of(desk, (me.get("session") or {}).get("pid")), chinese(desk)
        if isinstance(req.get("review"), list):  # of the tasks it posted waiting for review, those that still do
            out["review"] = [x for x in req["review"] if waits_review(desk, x)]
        return out
    if op == "alert":  # a case of the principal's coordinator alert the follower found (Watch): queued here
        from .alert import from_follower
        said = from_follower(desk, {**old, **me}, req)
        return {"queued": said == "queued", "told": said == "told"}
    if op == "end":
        _write_record(desk, key, {**old, **me, "pos": int(req.get("done") or old.get("pos") or 0), "seen": now(),
                                  "ended": now()})
        return {}
    raise ValueError(f"unknown request {op!r}")


def turns_of(desk: Desk, pid):
    """The record of how the session's turns end (up.record_turn), as written; None when there is none."""
    from .up import turns_dir
    try:
        path = turns_dir(desk) / f"{int(pid)}.json"
        return read_json(path, None) if path.is_file() and not path.is_symlink() else None
    except (TypeError, ValueError, OSError):
        return None


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
    mkdir_private(d)
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


class Lookups:
    """When `claude agents --json` last ran for a follower, by any of its lookups. One run gives both the session's
    name and its status, so the name lookup (Namer) and the Watch's share this clock: together they run it at most
    once every LOOK_EVERY seconds."""

    def __init__(self, at: float = 0.0):
        self.at = at

    def due(self, t: float) -> bool:
        return t - self.at >= LOOK_EVERY


class Namer:
    """The follower's lookups of its session's name (only the follower looks it up; status and notify show what
    it recorded). A lookup runs in a thread of its own, with a time limit, so posting never waits for it. While
    the name is missing it looks again after NAME_FIRST seconds, then waits twice as long each time, up to
    NAME_MOST; once named, every NAME_MOST (Claude Code may rename it). Each waits too until LOOK_EVERY seconds have
    passed since any lookup of the follower's began (clock: the Watch's, shared). look: the lookup, for tests."""

    def __init__(self, pid: int, name: str = "", first: float = 5.0, look=None, clock=None):
        self.name, self.look = name, look or (lambda: session_name(pid, NAME_LIMIT))
        self.clock = clock or Lookups()
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
        t = time.time()
        if not self.thread and t >= self.next and self.clock.due(t):
            self.clock.at = t
            self.found, self.thread = "", threading.Thread(target=self._run, name="hansa-look", daemon=True)
            self.thread.start()
        return self.name

    def heard(self, name: str) -> None:
        """The name a lookup of the Watch's found ('' for none): taken, and this one's own next lookup put off as
        after one of its own that found it."""
        if name:
            self.name, self.wait = name, NAME_FIRST
            self.next = max(self.next, time.time() + NAME_MOST)


class Watch:
    """The follower's part of the principal's `coordinator` alert: a refusing inbox, and its second and third cases.
    The inbox's wait and whether it was alerted are kept in last_post until a post succeeds, and count only while
    lines wait to be posted (refused_since). It looks its
    session's status up in `claude agents --json`, in a thread of its own so that posting never waits for it, at most
    once every LOOK_EVERY seconds counting the name lookup's (clock, shared with Namer), and only while one of them
    could hold: something waits to be posted, or was posted
    in the last `window` minutes (and the minute after, for the lookup that sees them go by), or the session was last
    seen waiting on its window with something posted to it; or a task posted to it has waited `review` minutes for
    review, not yet alerted. minutes: {no_session, window, review} as the desk gives them (alert.session_minutes;
    0: off). no_session also covers a running session's inbox refusing posts, once per stretch of failures.
        window  the session is `waiting` (a permission dialog or a question) at a lookup `window` minutes or more after
                the first post since a lookup last saw it otherwise: alerted once per stretch of waiting
        review  a task posted to it has waited `review` minutes for review since, and the session is `idle` at a
                lookup after that: alerted once for that task; hansa does not post it again
    Any other status (`busy`, `idle`, `shell`), one hansa does not know, or the session not listed ends a stretch and
    sends nothing; a lookup that fails changes nothing, sends nothing, and is written in the follower's log. state() is
    carried over a restart on new code, so what was told stays told. look: the lookup, for tests."""

    KEEP = ("first", "count", "oldest", "last", "last_count", "last_line", "told", "status", "seen", "looked",
            "reviews")

    def __init__(self, pid: int, was=None, look=None):
        w = was if isinstance(was, dict) else {}

        def num(k):
            return float(w[k]) if isinstance(w.get(k), (int, float)) else 0.0
        self.look = look or (lambda: session_row(pid, NAME_LIMIT))
        self.first, self.last, self.told, self.seen = (num(k) for k in ("first", "last", "told", "seen"))
        self.clock = Lookups(num("looked"))
        self.count, self.last_count = int(num("count")), int(num("last_count"))
        self.oldest, self.last_line, self.status = (str(w.get(k) or "") for k in ("oldest", "last_line", "status"))
        given = w.get("reviews") if isinstance(w.get("reviews"), dict) else {}
        self.reviews = {k: dict(v) for k, v in given.items() if isinstance(v, dict)
                        and isinstance(v.get("posted"), (int, float))}  # task: {posted, told}
        self.thread, self.got = None, None

    @property
    def looked(self) -> float:
        return self.clock.at

    def state(self) -> dict:
        return {k: getattr(self, k) for k in self.KEEP}

    def posted(self, t: float, lines: list, reviews=()) -> None:
        """A message went to the session at t: its lines, and the tasks in it that wait for review."""
        line = lines[0] if lines else ""
        if not self.first:
            self.first, self.count, self.oldest = t, 0, line
        self.count += len(lines)
        self.last, self.last_count, self.last_line = t, len(lines), line
        for tid in reviews:  # the wait counts from its latest post; told stays told
            self.reviews[tid] = {**self.reviews.get(tid, {}), "posted": t}

    def still(self, tids) -> None:
        """The desk's answer: which of the tasks asked about still wait for review (None: it was not asked)."""
        if isinstance(tids, list):
            self.reviews = {k: v for k, v in self.reviews.items() if k in tids}

    def wants(self, t: float, pending: bool, window: int, review: int) -> bool:
        """Whether a case could hold now, so that a lookup is worth its cost."""
        if window and (pending or (self.last and t - self.last <= window * 60 + LOOK_EVERY)
                       or (self.first and self.status == "waiting")):
            return True
        return bool(review) and any(not v.get("told") and t - v["posted"] >= review * 60 for v in self.reviews.values())

    def tick(self, t: float, pending: bool, minutes: dict, heard=None, last_post=None, want: bool = False) -> list:
        """Takes the answer of a lookup that ended, and starts one when due; never waits for one. Returns the cases
        that hold for the first time now, [(case, fields)], for the desk to queue (alert.from_follower). heard: given
        the session's name from each answer. want: the status is wanted besides (Turns.wants)."""
        window, review = (int(minutes.get(k) or 0) for k in ("window", "review"))
        out = []
        since = refused_since(last_post, pending)
        if minutes.get("no_session") and since is not None and not last_post.get("told") \
                and t - since >= minutes["no_session"] * 60:
            last_post["told"] = t
            out.append(("inbox", {"since": since}))
        if self.thread and not self.thread.is_alive():
            self.thread = None
            at, row, error = self.got
            if error:
                _say(f"could not look up the session's status for the principal's coordinator alert: {error}")
            else:
                if heard:
                    heard(str((row or {}).get("name") or "")[:80])
                out += self.saw(at, str((row or {}).get("status") or ""), window, review)
        if not self.thread and self.clock.due(t) and (want or self.wants(t, pending, window, review)):
            self.clock.at = t
            self.thread = threading.Thread(target=self._run, args=(t,), name="hansa-look", daemon=True)
            self.thread.start()
        return out

    def _run(self, at: float) -> None:
        try:
            self.got = (at, self.look(), "")
        except Exception as e:  # noqa: BLE001 - it cannot say; the follower's log has why
            self.got = (at, None, str(e) or type(e).__name__)

    def saw(self, at: float, status: str, window: int, review: int) -> list:
        """What a lookup begun at `at` found: the session's status, '' when it is not listed."""
        out = []
        self.status, self.seen = status, at
        if status == "waiting":
            if window and self.first and at - self.first >= window * 60 and not self.told:
                self.told = at
                out.append(("window", {"since": self.first, "count": self.count, "oldest": self.oldest}))
        else:  # seen doing something else: a stretch of waiting is over, and what was posted before was not missed
            self.told = 0.0
            if self.last >= at:  # posted after the lookup began: not seen yet
                self.first, self.count, self.oldest = self.last, self.last_count, self.last_line
            else:
                self.first, self.count, self.oldest = 0.0, 0, ""
        if status == "idle" and review:
            for tid, v in sorted(self.reviews.items()):
                if not v.get("told") and at - v["posted"] >= review * 60:
                    v["told"] = at
                    out.append(("review", {"task": tid, "posted": v["posted"]}))
        return out

    def holds(self, t: float, minutes: dict, last_post=None, pending: bool = False) -> dict:
        """What the follower's record says for hansa status: since when the session's inbox refuses posts and since
        when it waits on its window, when that holds; each task alerted of while the session was last seen idle, with
        when it was posted."""
        out = {}
        since = refused_since(last_post, pending)
        if minutes.get("no_session") and since is not None and t - since >= minutes["no_session"] * 60:
            out["inbox"] = since
        if minutes.get("window") and self.status == "waiting" and self.first \
                and t - self.first >= minutes["window"] * 60:
            out["window"] = self.first
        told = {k: v["posted"] for k, v in sorted(self.reviews.items()) if v.get("told")}
        if minutes.get("review") and self.status == "idle" and told:
            out["review"] = told
        return out


# How the coordinator's turns ended (up.record_turn's record), by what may cure a failure
TURN_WAIT = ("rate_limit", "overloaded", "server_error", "unknown")  # waiting may
TURN_PRINCIPAL = ("billing_error", "oauth_org_not_allowed", "account_on_hold", "verification_required",
                  "model_not_found", "cloud_credential_error", "invalid_request")  # only the principal can
TURN_LOGIN, TURN_LONG = "authentication_failed", "max_output_tokens"
TURN_REPOSTS = (2, 10, 30)  # minutes after a stretch of failures began: its reposts, while failures that wait may cure
TURN_LOGIN_WAIT = 2  # minutes after authentication_failed (perhaps a short login refresh) before acting on it
TURN_KEPT = 50  # subjects kept unhandled at most: without the hooks, nothing ever empties it


def turn_record(rec):
    """A turn record (up.record_turn's) as Turns uses it, or None when there is none or it is not one."""
    if not isinstance(rec, dict) or not isinstance(rec.get("ok"), bool):
        return None
    num = [rec.get(k) if isinstance(rec.get(k), (int, float)) and not isinstance(rec.get(k), bool) else None
           for k in ("at", "failed_since", "last_ok", "fails")]
    if num[0] is None:
        return None
    return {"at": float(num[0]), "ok": rec["ok"], "error": str(rec.get("error") or "unknown"),
            "failed_since": num[1], "last_ok": num[2], "fails": int(num[3] or 0)}


class Turns:
    """The follower's part of how its session's turns end (.hansa/coordinator-turns/<pid>.json, which the session's
    Stop and StopFailure hooks write; the desk gives it with the feed, once a minute): when an API error ended a turn
    hansa started, it posts again what the session has not handled, or alerts the principal (kind coordinator, case
    `turn`; `turn_ok` once the session works again).
        unhandled  subject: {line, at}, the latest line posted for it; filled at each post, emptied of what was
                   posted before an ok turn ended
        a stretch  failures in a row (the record's failed_since); a turn the principal started (nothing posted
                   between the turn before's end and its failure) is never posted again, and alerts only after
                   turn_min. Failures that waiting may cure (TURN_WAIT) post again at TURN_REPOSTS minutes, and alert
                   after turn_min; those only the principal can cure (TURN_PRINCIPAL) alert at once and stop reposts
                   until an ok turn; authentication_failed is posted again once after TURN_LOGIN_WAIT minutes and alerts
                   if that turn fails too (alerts then when nothing waits); max_output_tokens is posted again once,
                   ending "go on", and alerts if the next failure is that too. At most len(TURN_REPOSTS) reposts in a
                   stretch, each only while the session was seen idle after the failure, one per failed turn; none
                   once alerted, and no second alert in a stretch
    turn_min: the minutes as the desk gives them (alert.session_minutes; 0: no turn alerts, reposts still go). No
    record (a session without the hooks, or no turn ended yet) does nothing. state() is carried over a restart on
    new code, so what was told stays told and what was posted again is not posted again."""

    KEEP = ("unhandled", "posted", "seen_at", "since", "error", "failed_at", "principal", "reposts", "reposted",
            "told", "login", "long", "blocked", "urgent")

    def __init__(self, was=None):
        w = was if isinstance(was, dict) else {}

        def num(k):
            return float(w[k]) if isinstance(w.get(k), (int, float)) and not isinstance(w.get(k), bool) else 0.0
        given = w.get("unhandled") if isinstance(w.get("unhandled"), dict) else {}
        self.unhandled = {str(k): {"line": str(v.get("line") or ""), "at": float(v["at"])} for k, v in given.items()
                          if isinstance(v, dict) and isinstance(v.get("at"), (int, float))}
        self.posted, self.seen_at, self.since, self.failed_at, self.reposted, self.told, self.login, self.long = (
            num(k) for k in ("posted", "seen_at", "since", "failed_at", "reposted", "told", "login", "long"))
        self.reposts = int(num("reposts"))
        self.error = str(w.get("error") or "")
        self.principal, self.blocked, self.urgent = (bool(w.get(k)) for k in ("principal", "blocked", "urgent"))

    def state(self) -> dict:
        return {k: getattr(self, k) for k in self.KEEP}

    def posted_lines(self, t: float, items) -> None:
        """A message went to the session at t: [(subject, line)]."""
        for k, line in items:
            self.unhandled.pop(k, None)
            self.unhandled[k] = {"line": line, "at": t}
        while len(self.unhandled) > TURN_KEPT:
            self.unhandled.pop(next(iter(self.unhandled)))
        self.posted = t

    def holds(self) -> dict:
        """For hansa status: the stretch of failed turns that holds now, {error, since, reposts, told (when the
        principal was alerted, 0 if not)}; {} when there is none (no failure seen, or a turn ended ok since)."""
        if not self.since:
            return {}
        return {"error": self.error or "unknown", "since": self.since, "reposts": self.reposts, "told": self.told}

    def wants(self) -> bool:
        """Whether the session's status is worth looking up for this: a stretch of failures goes on, not yet alerted."""
        return bool(self.since) and not self.told

    def _new_stretch(self, since: float) -> None:
        self.since, self.reposts, self.reposted, self.told, self.login, self.long = since, 0, 0.0, 0.0, 0.0, 0.0
        self.blocked = self.urgent = self.principal = False
        self.error, self.failed_at = "", 0.0

    def _over(self, ok_at, turn_min: int) -> list:
        """An ok turn ended at ok_at: the stretch is over, and what was posted before it handled."""
        out = [("turn_ok", {"since": self.since, "at": ok_at})] if self.told and turn_min else []
        self.unhandled = {k: v for k, v in self.unhandled.items() if v["at"] > ok_at}
        self._new_stretch(0.0)
        return out

    def _ended(self, rec: dict, turn_min: int) -> list:
        """A turn ended that this has not seen yet."""
        prev, self.seen_at = self.seen_at, rec["at"]
        if rec["ok"]:
            return self._over(rec["at"], turn_min)
        since = rec["failed_since"] or rec["at"]
        out = []
        if self.since and since != self.since:  # an ok turn came between, unseen: that stretch is over
            out = self._over(rec["last_ok"] or since, turn_min)
        if not self.since:
            self._new_stretch(since)
        # the end of the turn before this one: exact for a stretch's first failure, else no earlier than seen
        prev = (rec["last_ok"] or 0.0) if rec["fails"] <= 1 else max(prev, since)
        self.principal = self.posted <= prev
        self.error, self.failed_at = rec["error"], rec["at"]
        if self.error in TURN_PRINCIPAL:
            self.blocked = True
            self.urgent = self.urgent or not self.principal
        if self.login and rec["at"] > self.login or self.error == TURN_LONG and self.long and rec["at"] > self.long:
            self.urgent = True  # what was posted again after authentication_failed failed too; or max_output_tokens
        return out

    def tick(self, t: float, rec, status: str, seen: float, turn_min: int) -> tuple:
        """What to do now, with the record as last read and the status a lookup begun at `seen` found: (a repost
        {why, error, at, lines, go_on} or None, the alert cases [(case, fields)] that hold for the first time now).
        The follower posts the repost, and calls went() once it did."""
        rec, turn_min = turn_record(rec), int(turn_min or 0)
        if rec is None:
            return None, []
        out = self._ended(rec, turn_min) if rec["at"] != self.seen_at else []
        if rec["ok"] or not self.since:
            return None, out
        repost = None
        if (not self.told and not self.blocked and not self.principal and status == "idle" and seen >= self.failed_at
                and self.reposted < self.failed_at and self.reposts < len(TURN_REPOSTS)):
            lines = [v["line"] for v in self.unhandled.values()]
            why = ("wait" if self.error in TURN_WAIT and lines and t >= self.since + TURN_REPOSTS[self.reposts] * 60
                   else "login" if self.error == TURN_LOGIN and not self.login and lines
                   and t >= self.failed_at + TURN_LOGIN_WAIT * 60
                   else "long" if self.error == TURN_LONG and not self.long else "")
            if why:
                repost = {"why": why, "error": self.error, "at": self.failed_at, "lines": lines,
                          "go_on": why == "long"}
        if not self.told and turn_min and (
                self.urgent or t - self.since >= turn_min * 60
                or self.error == TURN_LOGIN and not self.login and not self.unhandled
                and t >= self.failed_at + TURN_LOGIN_WAIT * 60):
            self.told = t
            out.append(("turn", {"error": self.error, "since": self.since, "count": len(self.unhandled),
                                 "fails": rec["fails"]}))
        return repost, out

    def went(self, t: float, why: str) -> None:
        """The repost tick() gave went to the session at t."""
        self.reposts += 1
        self.reposted = self.posted = t
        if why == "login":
            self.login = t
        elif why == "long":
            self.long = t


def repost_text(desk_root: str, r: dict, zh: bool = False) -> str:
    """What the follower posts again after a turn ended with an API error (Turns.tick's repost), in the alert
    language."""
    at = time.strftime("%H:%M", time.localtime(r["at"]))
    lines = r["lines"]
    if zh:
        head = f"你的上一轮在 {at} 因 API 错误（{r['error']}）结束。" + ("此后发来、尚未处理的：" if lines else "")
    else:
        head = (f"Your last turn ended with an API error ({r['error']}) at {at}."
                + (" Posted since then and not yet handled:" if lines else ""))
    text = message(desk_root, lines, head)
    return text + ("\n" + ("请继续" if zh else "go on") if r.get("go_on") else "")


def refused_since(last_post, pending: bool):
    """Since when the session's inbox refuses posts, while lines wait to be posted (pending); None when it does not.
    Only a failure _deliver records counts, with its since: an older follower's failed post (no since) dropped its
    lines, and nothing waits for it."""
    p = last_post if isinstance(last_post, dict) else {}
    since = p.get("since")
    return float(since) if pending and p.get("failed") and isinstance(since, (int, float)) else None


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
    env = {**without_session(os.environ), "HANSA_NOTIFY_RESUME": json.dumps(state)}
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


def follow(desk: Desk, key: str, sess: dict, sock: str, nonce: str, name: str = "") -> int:
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
          "last_post": was.get("last_post"), "coordinator": {}}  # coordinator: what hansa status shows of Watch
    watch = Watch(sess["pid"], was.get("watch"))
    turns, turn_rec, zh = Turns(was.get("turns")), None, False  # the record of how its turns end, as last read
    minutes, minutes_at = {}, 0.0  # what it watches for, as the desk last said (alert.session_minutes)
    done = 0
    try:
        r = ask({"op": "start", "me": me, **({"resume": resume["pos"]} if resume else {})})
        pos, done = r["pos"], r.get("done", r["pos"])
        # only now is it started (start): an event logged after `notify on` said so is past this position
        write_json(priv / f"{key}.state.json", {**read_json(priv / f"{key}.state.json", {}), "from": pos})
        if resume:
            was_up, told_down, told_srt = was.get("was_up"), float(was.get("told_down") or 0), bool(was.get("told_srt"))
            told_stuck = float(was.get("told_stuck") or 0)
        else:
            was_up, told_down, told_srt, told_stuck = r.get("server_up"), 0.0, False, 0.0
        srt_done = told_srt  # told_srt as of done: a restart reads again from there, so it carries this one
        _say(("restarted on hansa's new code; " if resume else "") +
             f"following desk {desk.root} for session {sess['pid']} at {sock}, from byte {pos}")
        summary, missed = bool(r.get("summary")), r.get("missed")
        pending = {tuple(x["key"]): x for x in r.get("summary_items") or []}
        first, last, errors = 0.0, 0.0, 0
        namer = Namer(sess["pid"], me["name"], clock=watch.clock)
        mark, changed = code_mark(), 0.0

        def beat():  # at each try of a refused post: the record stays live, for hansa status and hears()
            try:
                ask({"op": "seen", "me": me, "done": done})
            except Exception as e:  # noqa: BLE001 - the next feed writes it
                _say(f"could not keep this follower's record live: {e}")
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
                          "was_up": was_up, "told_down": told_down, "told_srt": srt_done, "told_stuck": told_stuck,
                          "watch": watch.state(), "turns": turns.state()})
                changed = 0.0
            try:
                asked = time.time() - minutes_at >= LOOK_EVERY
                r = ask({"op": "feed", "me": me, "pos": pos, "done": done, **({"minutes": True} if asked else {}),
                         **({"review": sorted(watch.reviews)} if watch.reviews else {})})
                errors = 0
                if asked:
                    minutes, minutes_at = r.get("coordinator_alert") or {}, time.time()
                    turn_rec, zh = r.get("turns"), bool(r.get("alert_zh"))
            except Exception as e:  # noqa: BLE001 - the desk may be busy or moving; try again, then give up
                errors += 1
                _say(f"could not read the desk: {e}")
                if errors >= 60:
                    return 1
                time.sleep(poll)
                continue
            pos, items = r["pos"], r["items"]
            watch.still(r.get("review"))
            if any(x["key"] == ["server"] for x in items):
                told_down = True
            items, told_down, told_stuck = server_news(r, items, was_up, told_down, told_stuck)
            was_up = r.get("server_up")
            srt = any(x["key"] == ["srt"] for x in items)
            if srt and (told_srt or r.get("srt_ran")):  # said once until srt runs again; or it runs by now
                items = [x for x in items if x["key"] != ["srt"]]
            told_srt = False if r.get("srt_ran") else told_srt or srt
            if items:
                for x in items:
                    k = tuple(x["key"])
                    pending.pop(k, None)  # the latest line per subject, also across feeds and refused posts
                    pending[k] = x
                last = time.time()
                first = first or last
            if pending and (summary or time.time() - last >= gather or time.time() - first >= 20):
                lines = [x["line"] for x in pending.values()]
                if _deliver(sock, message(str(desk.root), lines, missed_head(missed) if summary else ""), sess, me,
                            beat=beat):
                    watch.posted(time.time(), lines, [x["review"] for x in pending.values() if x.get("review")])
                    turns.posted_lines(time.time(), [(json.dumps(x["key"]), x["line"]) for x in pending.values()])
                    pending, first, done, srt_done = {}, 0.0, pos, told_srt
                    if summary:
                        summary = False
                        try:
                            ask({"op": "caught_up", "me": me, "of": (missed or {}).get("key")})
                        except Exception as e:  # noqa: BLE001 - the next follower may then say it again
                            _say(f"could not record that what was missed is told: {e}")
                elif gone(sess):
                    return 0
            cases = watch.tick(time.time(), bool(pending or summary), minutes, heard=lambda n: namer.heard(n),
                               last_post=me["last_post"], want=turns.wants())
            repost, said = turns.tick(time.time(), turn_rec, watch.status, watch.seen, minutes.get("turn") or 0)
            if repost:
                if _deliver(sock, repost_text(str(desk.root), repost, zh), sess, me, beat=beat):
                    turns.went(time.time(), repost["why"])
                elif gone(sess):
                    return 0
            for case, fields in cases + said:
                try:
                    r = ask({"op": "alert", "me": me, "case": case, **fields})
                    _say(f"the principal's coordinator alert ({case}) " + (
                        "is queued" if r.get("queued") else "went already: another follower alerted of this task"
                        if r.get("told") else "was not queued: no alert channel is set up"))
                except Exception as e:  # noqa: BLE001 - told is told: no retry, as for any alert
                    _say(f"could not queue the principal's coordinator alert ({case}): {e}")
            me["coordinator"] = watch.holds(time.time(), minutes, me["last_post"], bool(pending or summary))
            if stretch := turns.holds():  # failed turns go on: hansa status says so
                me["coordinator"]["turn"] = stretch
            time.sleep(poll)
    finally:
        try:
            ask({"op": "end", "me": me, "done": done})
        except Exception as e:  # noqa: BLE001 - the register then shows it as gone quiet, which it is
            _say(f"could not record the end: {e}")
        write_json(priv / f"{key}.state.json", {**read_json(priv / f"{key}.state.json", {}), "ended": now()})
        if gone(sess):  # not when hansa notify off ends it: the session still runs, its inbox with it
            remove_inbox(sock)
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
                                                     "hansa up"})
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


def _deliver(sock: str, text: str, sess: dict, me: dict, beat=None) -> bool:
    """True only when posted. After a minute of refusals, gather new lines before trying again; stop if gone.
    beat: called at each refused try, to keep the follower's record live while this blocks."""
    end = time.time() + POST_WAIT
    failed = me.get("last_post") or {}
    failed = failed if refused_since(failed, True) is not None else {}  # an older follower's failure: a new stretch
    since = failed.get("since")
    while True:
        try:
            post(sock, text)
            me["last_post"] = {"at": now(), "text": text.splitlines()[1][2:][:160] if "\n" in text else text[:160]}
            _say("posted: " + text.replace("\n", " | ")[:500])
            return True
        except OSError as e:
            if since is None:
                since = time.time()
            if gone(sess):
                _say(f"the session's process ended before the post ({e}); so does this follower")
                return False
            if beat:
                beat()
            if time.time() >= end:
                me["last_post"] = {**failed, "at": now(), "text": f"could not post: {e}", "failed": True,
                                   "since": since}
                _say(f"inbox still refuses posts after {POST_WAIT} s; keeping the lines to try again ({e}): "
                     + text.replace("\n", " | ")[:500])
                time.sleep(2)  # the next batch's first try must not come faster than the tries within this one
                return False
            time.sleep(2)


# -- starting and stopping one ------------------------------------------------------------------------------
def spawn(desk: Desk, args: list, log: Path) -> None:
    """Start `hansa ARGS` detached: a session of its own, not the caller's child, keeping none of the caller's
    files open (so the caller's tool call ends at once), its output going to log."""
    env = without_session(os.environ)  # the follower is given its session and inbox on its command line
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


START_WAIT = 60  # seconds notify on waits for its follower to say where it reads from; a last guard


def start(desk: Desk, sess: dict, sock: str, name: str = "", wait: bool = True) -> str:
    """Start the follower of this session: 'started', 'already' (one runs), or 'unknown' (it has not said yet).
    'started' once the desk gave it the log position it reads from, not before: an event logged after this
    returns is one it reads (a follower that only runs could still be asking, and start after that event)."""
    key = key_of(sess)
    if running(desk, key):
        return "already"
    nonce = uuid.uuid4().hex
    spawn(desk, ["notify-follow", "--key", key, "--pid", str(sess["pid"]), "--start", sess.get("start") or "",
                 "--socket", sock, "--nonce", nonce, "--name", name],
          private_dir(desk) / f"{key}.log")
    end = time.time() + (START_WAIT if wait else 0)
    while time.time() < end:
        st = running(desk, key)
        if st and st.get("nonce") == nonce:
            if isinstance(st.get("from"), int):
                return "started"
            time.sleep(0.1)
            continue
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
        os.utime(d)  # in use again: run.sweep_tmp removes only an empty one left alone for an hour
        path = os.path.join(d, f"c{pid}.sock")
        try:
            if stat.S_ISSOCK(os.lstat(path).st_mode):  # left by an earlier process with this pid
                os.unlink(path)
        except FileNotFoundError:
            pass
        return path
    raise RuntimeError("there is no short folder only this account can open for the session's inbox")


def remove_inbox(sock: str) -> None:
    """Once its session has ended: the inbox socket_path gave it, and its folder when no other session's inbox is
    in it. An inbox elsewhere (the session's own --messaging-socket-path) is left as it is."""
    d = os.path.dirname(sock)
    if os.path.basename(d) != f"hansa-{os.getuid()}":
        return
    with contextlib.suppress(OSError):
        if stat.S_ISSOCK(os.lstat(sock).st_mode):
            os.unlink(sock)
    with contextlib.suppress(OSError):
        os.rmdir(d)  # only when empty


def turn_on(desk: Desk) -> str:
    """Start posting to the session this is run from."""
    sess, sock = session_here(), session_value("CLAUDE_CODE_MESSAGING_SOCKET")
    if os.environ.get("HANSA_TASK"):
        raise SystemExit("hansa: notify is for the coordinator's session, not a worker's; nothing was started")
    if not sess or not sock:
        raise SystemExit("hansa: notify on starts hansa posting to the Claude Code session it is run from, and this "
                         "is not run from one (CLAUDE_PID and CLAUDE_CODE_MESSAGING_SOCKET are not set); nothing was "
                         "started. Run it with the coordinator session's Bash tool")
    how = start(desk, sess, sock)  # its follower looks the name up; this command starts no claude
    name = next((r.get("name") or "" for r in records(desk) if same(r.get("session"), sess)), "")
    if how == "already":
        return (f"hansa already notifies this session, {who(sess['pid'], name)}; nothing was started. hansa notify lists "
                "the desk's followers")
    if how == "unknown":
        raise SystemExit(f"hansa: the follower did not say it started within {START_WAIT} s; see "
                         f"{private_dir(desk) / (key_of(sess) + '.log')}")
    said = (f"hansa now posts to this session, {who(sess['pid'], name)}, through its inbox: its own tasks and requests, and "
            "those no other session hears. Do not keep hansa wait running in the background meanwhile. "
            "Stop it with hansa notify off")
    return said


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
    return "\n".join(out)


def who(pid, name: str = "") -> str:
    """A session as hansa names it everywhere: its pid, and its name as its follower last recorded it."""
    return f"pid {pid} ({name or 'no name yet'})"


def names(desk: Desk) -> list:
    """The sessions hansa notifies, for hansa status, in the form hansa names a session everywhere."""
    return [who((r.get("session") or {}).get("pid"), r.get("name") or "") for r in live_records(desk)]
