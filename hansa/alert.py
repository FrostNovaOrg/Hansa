"""Alerts: the principal's own command, which hansa runs when something waits for the principal.

The principal sets one command line on the page (settings `alert`, which only the page saves: desk.PRINCIPAL_ONLY);
the page records that it saved it, and when. hansa runs it when a release starts waiting for the principal's yes,
when a release ends (done, failed, void or forbidden), when a request to the principal is filed (hansa ask), and
when hansa serve has finished no round for STUCK_ALERT seconds; not for ordinary task events, which go to the
coordinator. The coordinator sends none: no hansa command does.

Alerts are queued in .hansa/alerts.json (any hansa process may queue one) and sent by hansa serve's alert thread:
as the desk's account, outside the sandbox, in the desk's folder, with hansa's own environment (not a lane's env),
the kinds in HANSA_ALERT_KIND (comma-separated when several go out together) and the text on standard input (UTF-8,
keys removed, cut at MOST_TEXT characters), stopped after TIMEOUT seconds. Alerts that come within GATHER seconds
of each other go out in one run (none waits more than MOST_WAIT); one run at a time (.hansa/alert.lock). A run
that fails or times out is recorded (event alert_failed, and .hansa/alert-last.json, which hansa status reads);
there is no retry: the next alert tries again. The text is in English: the page has no language setting (it
follows the browser's language, which hansa does not know when it sends).
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from .desk import Desk, now, read_json, write_json

KINDS = ("release_waiting", "release_ended", "ask", "server_stuck", "test")
GATHER = 20  # seconds: alerts that come within this of each other go out in one run
MOST_WAIT = 120  # seconds an alert waits at most for others to gather with it
TIMEOUT = 30  # seconds a run may take
MOST_TEXT = 1000  # characters on the command's standard input
TAIL = 5  # lines of a failed run's output kept
STUCK_ALERT = 15 * 60  # seconds hansa serve finishes no round before the principal is alerted
EVERY = 2.0  # seconds between the alert thread's looks


def queue_file(desk: Desk) -> Path:
    return desk.state_dir / "alerts.json"


def last_file(desk: Desk) -> Path:
    return desk.state_dir / "alert-last.json"


def command(s: dict) -> str:
    """The principal's alert command line; '' when none is set."""
    a = s.get("alert")
    return a["command"].strip() if isinstance(a, dict) and isinstance(a.get("command"), str) else ""


def queued(desk: Desk) -> list:
    q = read_json(queue_file(desk), [])
    return [x for x in q if isinstance(x, dict)] if isinstance(q, list) else []


def add(desk: Desk, kind: str, text: str) -> bool:
    """Queue an alert for the next run; False (nothing queued) when no alert command is set."""
    if not command(desk.settings()):
        return False
    with desk.lock():
        q = queued(desk)
        q.append({"kind": kind, "text": desk.redact(text), "at": time.time()})
        desk.state_dir.mkdir(exist_ok=True)
        write_json(queue_file(desk), q)
    return True


# -- what is alerted -------------------------------------------------------------------------------------------------
def _release_text(desk: Desk, rid, waiting: bool) -> str:
    from . import release
    r = release.get(desk, rid)
    if not r:
        return ""
    head = f"release #{r['id']} of lane {r.get('lane')} ({r.get('base')} at {str(r.get('commit') or '')[:10]})"
    if waiting:
        return (f"{head} waits for your yes on the page: {r.get('count', 0)} commit(s), command: {r.get('command')}"
                + (f"; note: {r['note']}" if r.get("note") else ""))
    return f"{head}: {release.state_text(desk, r)}"


def from_event(desk: Desk, kind: str, fields: dict) -> None:
    """Queue the alert an event calls for, if any (Desk.event calls this)."""
    if kind == "release_asked" and fields.get("state") == "waiting" or kind == "release_waits":
        add(desk, "release_waiting", _release_text(desk, fields.get("id"), True))
    elif kind in ("release_ended", "release_void", "release_forbidden"):
        add(desk, "release_ended", _release_text(desk, fields.get("id"), False))
    elif kind == "ask":
        a = next((x for x in desk.asks() if x.get("id") == fields.get("id")), {})
        add(desk, "ask", f"request #{fields.get('id')} waits for you on the page: {a.get('text', '')}")


# -- sending -----------------------------------------------------------------------------------------------------------
def text_of(desk: Desk, items: list) -> str:
    """What the command reads on its standard input: one line per alert, keys removed, cut at MOST_TEXT characters."""
    lines = [" ".join(str(x.get("text") or "").split()) for x in items]
    head = f"[hansa] desk {desk.root.name}"
    text = (f"{head}: {lines[0]}" if len(lines) == 1 else
            f"{head}: {len(lines)} alerts\n" + "\n".join(f"- {x}" for x in lines))
    text = desk.redact(text)
    return text if len(text) <= MOST_TEXT else text[:MOST_TEXT - 1] + "…"


def run(desk: Desk, cmd: str, kinds: str, text: str, timeout: float = TIMEOUT) -> dict:
    """Run the alert command once: {ok, exit, timed_out, tail, at}."""
    env = {**os.environ, "HANSA_ALERT_KIND": kinds}
    for k in ("HANSA_TASK", "HANSA_MERGING"):
        env.pop(k, None)
    timed_out, code, out = False, None, b""
    try:
        proc = subprocess.Popen(["/bin/sh", "-c", cmd], cwd=str(desk.root), env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            out, _ = proc.communicate(text.encode("utf-8"), timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired, OSError, ValueError):
                out, _ = proc.communicate(timeout=5)
    except OSError as e:
        out = str(e).encode()
    said = desk.redact(out.decode("utf-8", errors="replace")).strip().splitlines()[-TAIL:]
    tail = [x[:200] for x in said]
    return {"ok": code == 0 and not timed_out, "exit": code, "timed_out": timed_out, "tail": tail, "at": now(),
            "kinds": kinds}


@contextlib.contextmanager
def _one_run(desk: Desk, wait: bool):
    """One alert run at a time, across processes; yields False when another holds it and wait is False."""
    desk.state_dir.mkdir(exist_ok=True)
    with open(desk.state_dir / "alert.lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            got = True
        except BlockingIOError:
            got = False
        try:
            yield got
        finally:
            if got:
                fcntl.flock(f, fcntl.LOCK_UN)


def _record(desk: Desk, result: dict, count: int) -> None:
    last = read_json(last_file(desk), {}) or {}
    rec = {"last": result, "failed": last.get("failed") if result["ok"] else result}
    write_json(last_file(desk), rec)
    if result["ok"]:
        desk.event("alert_sent", kinds=result["kinds"], alerts=count)
    else:
        desk.event("alert_failed", kinds=result["kinds"], alerts=count, exit=result["exit"],
                   timed_out=result["timed_out"], tail=result["tail"])


def send_due(desk: Desk, gather: float = GATHER, most_wait: float = MOST_WAIT, timeout: float = TIMEOUT,
             wait: bool = False) -> dict | None:
    """Send what is queued once it has gathered: when no alert came for `gather` seconds, or the first has waited
    `most_wait`. The result of the run; None when nothing went out."""
    with _one_run(desk, wait) as mine:
        if not mine:
            return None
        q = queued(desk)
        if not q:
            return None
        t = time.time()
        if t - max(x.get("at") or 0 for x in q) < gather and t - min(x.get("at") or t for x in q) < most_wait:
            return None
        with desk.lock():
            q = queued(desk)
            write_json(queue_file(desk), [])
        cmd = command(desk.settings())
        if not cmd or not q:  # cleared since they were queued: nothing to send them with
            return None
        kinds = ",".join(dict.fromkeys(str(x.get("kind")) for x in q))
        result = run(desk, cmd, kinds, text_of(desk, q), timeout)
        _record(desk, result, len(q))
        return result


def test(desk: Desk, timeout: float = TIMEOUT) -> dict:
    """The page's test button: a test alert sent now, with anything already queued."""
    if not command(desk.settings()):
        return {"ok": False, "problems": ["no alert command is set; save one first"]}
    add(desk, "test", "a test alert from the page: the alert command works")
    result = send_due(desk, gather=0, timeout=timeout, wait=True)
    return result or {"ok": False, "problems": ["the test alert was not sent"]}


def stuck_check(desk: Desk, told: float) -> float:
    """hansa serve's rounds quiet for STUCK_ALERT seconds: alert once, until a round comes again. told: when that
    was alerted, 0 if not; returns it as it is now."""
    from .run import last_round
    last = last_round(desk)
    quiet = time.time() - last if last else 0
    if quiet < STUCK_ALERT:
        return 0.0
    if not told:
        add(desk, "server_stuck", f"hansa serve has finished no round for {int(quiet // 60)} minutes: no task starts "
                                  "or ends, and no release runs; see hansa explain server")
        return time.time()
    return told


def start_thread(desk: Desk, every: float = EVERY) -> threading.Thread:
    """hansa serve's alert thread: it sends what gathered, and alerts when the server's own rounds stop coming."""
    def loop():
        told = 0.0
        while True:
            try:
                told = stuck_check(desk, told)
                send_due(desk)
            except Exception as e:  # noqa: BLE001 - keep sending; the error is on record
                with contextlib.suppress(Exception):
                    desk.event("alert_error", error=str(e)[:300])
            time.sleep(every)
    t = threading.Thread(target=loop, name="hansa-alert", daemon=True)
    t.start()
    return t


# -- words -------------------------------------------------------------------------------------------------------------
def _when(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "?"


def failure_line(desk: Desk) -> str:
    """For hansa status: the last failed alert run (when, exit code, a short tail); '' when none failed."""
    rec = read_json(last_file(desk), {}) or {}
    f = rec.get("failed") if isinstance(rec, dict) else None
    if not isinstance(f, dict):
        return ""
    how = f"timed out after {TIMEOUT} s" if f.get("timed_out") else f"exit {f.get('exit')}"
    tail = " | ".join(f.get("tail") or [])
    last = rec.get("last") or {}
    later = (f"; a later one went out {_when(last.get('at'))}" if last.get("ok") and (last.get("at") or 0) > (f.get("at") or 0)
             else "")
    return (f"alert command FAILED {_when(f.get('at'))} ({how})" + (f": {tail}" if tail else "") + later
            + " · the principal's, on the page")


def explain_line(s: dict) -> str:
    """For hansa explain: whether the principal set an alert command, and what it is."""
    cmd = command(s)
    a = s.get("alert") if isinstance(s.get("alert"), dict) else {}
    if not cmd:
        return ("Alerts: no alert command is set. The principal may set one on the page, which hansa runs when a "
                "release waits for the principal or ends, or a request is filed; you send no alerts and cannot set it.")
    return (f"Alerts: the principal's alert command is {cmd!r} (saved on the page {_when(a.get('at'))}); hansa runs it "
            "when a release waits for the principal or ends, a request is filed (hansa ask), or hansa serve finishes "
            "no round for a long time. You send no alerts and cannot set it.")
