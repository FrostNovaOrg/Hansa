"""Alerts: the principal's own command, which hansa runs when something waits for the principal.

The principal sets one command line on the page (settings `alert`, which only the page saves: desk.PRINCIPAL_ONLY);
the page records that it saved it, and when. hansa runs it when a release starts waiting for the principal's yes,
when a release ends (done, failed, void or forbidden), when the coordinator stops one (release_stopped; the principal's
own Stop on the page sends none), when a request to the principal is filed (hansa ask), when a lane's runs cannot
start because their sandbox cannot (once for each lane and reason: run.hold), and
when hansa serve has finished no round for STUCK_ALERT seconds; not for ordinary task events, which go to the
coordinator. The coordinator sends none: no hansa command does.

Alerts are queued in .hansa/alerts.json (any hansa process may queue one) and sent by hansa serve's alert thread:
as the desk's account, outside the sandbox, in the desk's folder, with hansa's own environment (not a lane's env),
the kinds in HANSA_ALERT_KIND (comma-separated when several go out together) and the text on standard input (UTF-8,
keys removed, cut at MOST_TEXT characters), stopped after TIMEOUT seconds. Alerts that come within GATHER seconds
of each other go out in one run (none waits more than MOST_WAIT); one run at a time (.hansa/alert.lock), and at
most MOST_RUNS in an hour: beyond that, what comes waits and goes out in one run when the hour frees a run, with a
line saying how many were held back. An entry of the queue that cannot be read is dropped, and recorded once
(event alert_dropped); the others go. The command's own exit ends its run, even when it leaves a child behind;
only at the time limit is its process group killed. A run
that fails or times out is recorded (event alert_failed, and .hansa/alert-last.json, which hansa status reads);
there is no retry: the next alert tries again. A
release_waiting alert carries the release's one-time code (release.new_code), which goes nowhere else: its queue
entry is gone once sent, and the code is taken out of what is kept of the run. The text is in English: the page has no language setting (it
follows the browser's language, which hansa does not know when it sends).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .desk import Desk, now, read_json, write_json

KINDS = ("release_waiting", "release_ended", "release_stopped", "ask", "server_stuck", "sandbox", "test")
GATHER = 20  # seconds: alerts that come within this of each other go out in one run
MOST_WAIT = 120  # seconds an alert waits at most for others to gather with it
TIMEOUT = 30  # seconds a run may take
MOST_TEXT = 1000  # characters on the command's standard input
TAIL = 5  # lines of a failed run's output kept
MOST_RUNS = 20  # runs an hour at most; beyond it, alerts are held and go out together
HOUR = 3600
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


def _good(x) -> bool:
    at = x.get("at") if isinstance(x, dict) else None
    return (isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at)
            and isinstance(x.get("kind"), str) and isinstance(x.get("text"), str))


def _read(desk: Desk) -> tuple:
    """The queue's entries that can be read, and how many cannot (a hand edit: hansa writes it whole)."""
    try:
        raw = queue_file(desk).read_bytes()
    except FileNotFoundError:
        return [], 0
    try:  # text that is not UTF-8 is read with replacement characters: one bad byte loses no alert
        q = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return [], 1 if raw.strip() else 0
    if not isinstance(q, list):
        return [], 1
    good = [x for x in q if _good(x)]
    return good, len(q) - len(good)


def queued(desk: Desk) -> list:
    return _read(desk)[0]


def _clean(desk: Desk) -> list:
    """Under the desk lock: the queue's good entries, with any that cannot be read dropped and recorded once."""
    good, bad = _read(desk)
    if bad:
        write_json(queue_file(desk), good)
        desk.event("alert_dropped", entries=bad, why="an entry of .hansa/alerts.json that cannot be read")
    return good


def add(desk: Desk, kind: str, text: str, code: str = "", release=None) -> bool:
    """Queue an alert for the next run; False (nothing queued) when no alert command is set. code: the one-time
    code of release `release` the text holds (it replaces one queued before for that release, now void); the entry
    goes once it is sent, and the code is taken out of what is kept of the run."""
    if not command(desk.settings()):
        return False
    with desk.lock():
        q = _clean(desk)
        if code:
            q = [x for x in q if not (x.get("code") and x.get("release") == release)]
        q.append({"kind": kind, "text": desk.redact(text), "at": time.time(),
                  **({"code": code, "release": release} if code else {})})
        desk.state_dir.mkdir(exist_ok=True)
        write_json(queue_file(desk), q)
    return True


# -- what is alerted -------------------------------------------------------------------------------------------------
def _release_text(desk: Desk, rid, waiting: bool, code: str = "") -> str:
    from . import release
    r = release.get(desk, rid)
    if not r:
        return ""
    target = r.get("target") or ""  # "": the lane's one unnamed target, its release command
    head = (f"release #{r['id']} of lane {r.get('lane')}, target {target or '(unnamed)'} "
            f"({r.get('base')} at {str(r.get('commit') or '')[:10]})")
    if waiting:  # the code first, so that no cut of a long text takes it
        return (f"{head} waits for your yes on the page"
                + (f", or say code {code} to the coordinator in the conversation (it answers this release only, once, "
                   f"for {release.CODE_LIFE // 3600} hours; send it nowhere else)" if code else "")
                + f": {r.get('count', 0)} commit(s), command: {r.get('command')}"
                + (f"; note: {r['note']}" if r.get("note") else ""))
    return f"{head}: {release.state_text(desk, r)}"


def from_event(desk: Desk, kind: str, fields: dict) -> None:
    """Queue the alert an event calls for, if any (Desk.event calls this)."""
    if kind == "release_asked" and fields.get("state") == "waiting" or kind == "release_waits":
        from . import release
        rid = fields.get("id")
        if not command(desk.settings()):  # no alert command: no code; the page answers
            return
        code = release.new_code(desk, rid)  # a new one each time it waits: one made before is void
        add(desk, "release_waiting", _release_text(desk, rid, True, code), code=code, release=rid)
    elif kind in ("release_ended", "release_void", "release_forbidden"):
        add(desk, "release_ended", _release_text(desk, fields.get("id"), False))
    elif kind == "release_stopped" and fields.get("by") != "page":  # the principal knows of their own Stop
        add(desk, "release_stopped", _release_text(desk, fields.get("id"), False))
    elif kind == "ask":
        a = next((x for x in desk.asks() if x.get("id") == fields.get("id")), {})
        add(desk, "ask", f"request #{fields.get('id')} waits for you on the page: {a.get('text', '')}")
    elif kind == "sandbox_held" and fields.get("alert"):  # once for each lane and reason (run.hold)
        from .run import sandbox_held_text
        why = sandbox_held_text({**fields, "kind": fields.get("run")})
        add(desk, "sandbox", f"lane {fields.get('lane')} cannot run: {why}; "
                             + (f"task {fields['task']} is held" if fields.get("task") else "its tasks are held")
                             + ", not failed")


# -- sending -----------------------------------------------------------------------------------------------------------
def text_of(desk: Desk, items: list, held: int = 0) -> str:
    """What the command reads on its standard input: one line per alert, keys removed, cut at MOST_TEXT characters.
    held: how many of them the limit of MOST_RUNS runs an hour held back."""
    items = sorted(items, key=lambda x: not x.get("code"))  # those holding a code first: no cut takes it
    lines = [" ".join(str(x.get("text") or "").split()) for x in items]
    head = f"[hansa] desk {desk.root.name}"
    if held:
        head += f" ({held} held back by the limit of {MOST_RUNS} runs an hour)"
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
    # stdin and output are files, not pipes: the command's own exit ends the run, even when a child it left behind
    # still holds them; only at the time limit is its whole group killed
    with tempfile.TemporaryFile() as given, tempfile.TemporaryFile() as said:
        given.write(text.encode("utf-8"))
        given.seek(0)
        try:
            proc = subprocess.Popen(["/bin/sh", "-c", cmd], cwd=str(desk.root), env=env, stdin=given,
                                    stdout=said, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    proc.wait(timeout=5)
            said.seek(0)
            out = said.read()
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


def _last(desk: Desk) -> dict:
    try:
        rec = read_json(last_file(desk), {})
    except ValueError:  # not UTF-8: a hand edit; hansa writes it whole
        rec = {}
    return rec if isinstance(rec, dict) else {}


def _runs(rec: dict, t: float) -> list:
    """When the runs of the last hour began."""
    runs = rec.get("runs")
    return [x for x in runs if isinstance(x, (int, float)) and t - HOUR < x <= t] if isinstance(runs, list) else []


def _record(desk: Desk, result: dict, count: int, began: float) -> None:
    last = _last(desk)
    rec = {"last": result, "failed": last.get("failed") if result["ok"] else result,
           "runs": _runs(last, began) + [began]}
    write_json(last_file(desk), rec)
    if result["ok"]:
        desk.event("alert_sent", kinds=result["kinds"], alerts=count)
    else:
        desk.event("alert_failed", kinds=result["kinds"], alerts=count, exit=result["exit"],
                   timed_out=result["timed_out"], tail=result["tail"])


def send_due(desk: Desk, gather: float = GATHER, most_wait: float = MOST_WAIT, timeout: float = TIMEOUT,
             wait: bool = False, capped: bool = True) -> dict | None:
    """Send what is queued once it has gathered: when no alert came for `gather` seconds, or the first has waited
    `most_wait`; and, when capped, only if fewer than MOST_RUNS runs began in the last hour (else they wait, and go
    out together when the hour frees a run). The result of the run; None when nothing went out."""
    with _one_run(desk, wait) as mine:
        if not mine:
            return None
        good, bad = _read(desk)
        if bad:
            with desk.lock():
                good = _clean(desk)
        if not good:
            return None
        t = time.time()
        if t - max(x["at"] for x in good) < gather and t - min(x["at"] for x in good) < most_wait:
            return None
        rec = _last(desk)
        if capped and len(_runs(rec, t)) >= MOST_RUNS:
            if not rec.get("held_since"):  # recorded once per hold
                write_json(last_file(desk), {**rec, "held_since": t})
                desk.event("alert_held", alerts=len(good), most_runs=MOST_RUNS)
            return None
        with desk.lock():
            q = _clean(desk)
            write_json(queue_file(desk), [])
        cmd = command(desk.settings())
        codes = [x for x in q if isinstance(x.get("code"), str) and x["code"]]
        if not cmd or not q:  # cleared since they were queued: nothing to send them with
            _codes_went(desk, codes, "", False)
            return None
        held_since = rec.get("held_since") if isinstance(rec.get("held_since"), (int, float)) else None
        held = len(q) if held_since else 0
        if held_since:
            write_json(last_file(desk), {k: v for k, v in rec.items() if k != "held_since"})
        kinds = ",".join(dict.fromkeys(x["kind"] for x in q))
        text = text_of(desk, q, held)
        result = run(desk, cmd, kinds, text, timeout)
        result["tail"] = [_hide(x, codes) for x in result["tail"]]  # what the command wrote back may echo a code
        _codes_went(desk, codes, text, result["ok"])
        _record(desk, result, len(q), t)
        return result


def _hide(line: str, codes: list) -> str:
    for x in codes:
        line = line.replace(x["code"], "[code]").replace(x["code"].lower(), "[code]")
    return line


def _codes_went(desk: Desk, codes: list, text: str, ok: bool) -> None:
    """Record on each release whether its code went out; one that did not (no command to send it with, or cut from
    the text) is void: the page answers that release."""
    from . import release
    for x in codes:
        if x["code"] in text:
            release.code_sent(desk, x.get("release"), x["code"], ok)
        else:
            release.code_unsent(desk, x.get("release"), x["code"])


def test(desk: Desk, timeout: float = TIMEOUT) -> dict:
    """The page's test button: a test alert sent now, with anything already queued."""
    if not command(desk.settings()):
        return {"ok": False, "problems": ["no alert command is set; save one first"]}
    add(desk, "test", "a test alert from the page: the alert command works")
    result = send_due(desk, gather=0, timeout=timeout, wait=True, capped=False)  # the principal's own press
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
    rec = _last(desk)
    f = rec.get("failed")
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
                "release waits for the principal or ends, or a request is filed; you send no alerts and cannot set it. "
                "With none, no release gets a one-time code: the principal approves releases on the page only.")
    return (f"Alerts: the principal's alert command is {cmd!r} (saved on the page {_when(a.get('at'))}); hansa runs it "
            "when a release waits for the principal or ends, you stop a release (hansa release --stop), a request is "
            "filed (hansa ask), a lane's runs cannot start because their sandbox cannot, or hansa serve finishes no "
            "round for a long time. You send no alerts and cannot set it. A release waiting for the principal gets a "
            "one-time code that only that alert carries: the principal answers on the page, or says the code in the "
            "conversation, and you pass it on with hansa release yes ID CODE (or no ID CODE). Never guess one.")
