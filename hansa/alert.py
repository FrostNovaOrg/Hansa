"""Alerts: the principal's own command, which hansa runs when something waits for the principal.

The principal sets one command line on the page (settings `alert`, which only the page saves: desk.PRINCIPAL_ONLY);
the page records that it saved it, and when. hansa runs it when a release starts waiting for the principal's yes,
when a release ends (done, failed, void or forbidden), when the coordinator stops one (release_stopped; the principal's
own Stop on the page sends none), when a to-do for the principal is recorded (hansa ask), when a lane's runs cannot
start because a provider ran out of credit (once per rest: run.rest_provider), when a lane's runs cannot
start because their sandbox cannot (once for each lane and reason: run.hold), when free space goes under the floor
and hansa serve starts nothing new (once each time: disk.look),
when hansa serve has finished no round for STUCK_ALERT seconds, and when what hansa posts to the coordinator's session
cannot reach it (kind `coordinator`, below); not for ordinary task events, which go to the
coordinator. The coordinator sends none: no hansa command does.

The `coordinator` alert has three cases, each after the minutes the principal sets on the page (settings
`coordinator_alert`, desk.COORDINATOR_ALERT; 0 turns a case off):
    no session listens   something a follower would post (notify._item) has waited no_session_min in the event log
                         while the desk has no live follower: hansa serve sends it once (unheard_check), and again only
                         after a follower has run and the case begun anew
    waits on its window  `claude agents --json` lists a live follower's session as `waiting` (a permission dialog or a
                         question) window_min after something was posted to it: once per stretch of waiting
    work sits untouched  a task posted to the session has waited review_min for review, and the session is `idle` at
                         a lookup after that: once for that task; hansa does not post it again
The follower looks for the last two (notify.Watch) and asks the desk to queue them (from_follower).

Alerts are queued in .hansa/alerts.json (any hansa process may queue one) and sent by hansa serve's alert thread:
as the desk's account, outside the sandbox, in a folder of its own under the desk's secrets/alert-runs (run_dir),
with hansa's own environment (not a lane's env),
the kinds in HANSA_ALERT_KIND (comma-separated when several go out together) and the text on standard input (UTF-8,
keys removed, cut at MOST_TEXT characters), stopped after TIMEOUT seconds. Alerts that come within GATHER seconds
of each other go out in one run (none waits more than MOST_WAIT); to-dos closed before that run are left out.
One run at a time (.hansa/alert.lock), and at
most MOST_RUNS in an hour: beyond that, what comes waits and goes out in one run when the hour frees a run, with a
line saying how many were held back. An entry of the queue that cannot be read is dropped, and recorded once
(event alert_dropped); the others go. The command's own exit ends its run, even when it leaves a child behind;
only at the time limit is its process group killed. A run
that fails or times out is recorded (event alert_failed, and .hansa/alert-last.json, which hansa status reads);
there is no retry: the next alert tries again. A
release_waiting alert carries the release's one-time code (release.new_code), which goes nowhere else: its queue
entry is gone once sent, and none of what the command printed for such an alert is kept (only how it ended). A phone
shows the first hundred characters or so, so the code and what to do with it come right after the desk's name:
"[hansa] desk NAME: release #1 (lane a, target staging) · code K7P3QX: to release it, give this code to the coordinator
in the conversation (this release only, once, 24 hours; send it nowhere else), or answer on the page · main at
08e2cfc99f, 8 commit(s), command: ...; note: ..."; several in one run: "[hansa] desk NAME: 3 alerts · #1 code K7P3QX
· #2 code M4R8TW", then one line each, those holding a code first.
That folder is removed when the command ends, and what a stopped
or killed hansa serve left there is swept when it starts (sweep_runs): secrets/ is closed to the coordinator, workers,
verifiers, so a leftover that holds the alert's text is never theirs to read. The text is in English: the page has no language setting (it
follows the browser's language, which hansa does not know when it sends).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import without_session
from .desk import Desk, now, read_json, write_json

KINDS = ("release_waiting", "release_ended", "release_stopped", "ask", "server_stuck", "sandbox", "disk", "coordinator", "credit",
         "test")
GATHER = 20  # seconds: alerts that come within this of each other go out in one run
MOST_WAIT = 120  # seconds an alert waits at most for others to gather with it
TIMEOUT = 30  # seconds a run may take
MOST_TEXT = 1000  # characters on the command's standard input
TAIL = 5  # lines of a failed run's output kept
WITHHELD = "its output was not kept, as the alert held a one-time code"  # nor any part of it, in any form
MOST_RUNS = 20  # runs an hour at most; beyond it, alerts are held and go out together
HOUR = 3600
STUCK_ALERT = 15 * 60  # seconds hansa serve finishes no round before the principal is alerted
EVERY = 2.0  # seconds between the alert thread's looks
UNHEARD_EVERY = 10.0  # seconds between its looks at whether any session hears from hansa (unheard_check)


def queue_file(desk: Desk) -> Path:
    return desk.state_dir / "alerts.json"


def last_file(desk: Desk) -> Path:
    return desk.state_dir / "alert-last.json"


def runs_dir(desk: Desk) -> Path:
    """Where each run's own folder is made: in secrets/, which the coordinator, workers and verifiers
    may not read, as what the command writes there may hold the alert's text and its code."""
    return desk.secrets_dir / "alert-runs"


def sweep_runs(desk: Desk) -> None:
    """Remove the folders of runs that a stopped or killed hansa serve left (hansa serve calls this as it starts,
    before its alert thread runs)."""
    d = runs_dir(desk)
    if d.is_dir():
        for x in d.iterdir():
            if x.is_dir() and not x.is_symlink():
                shutil.rmtree(x, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    x.unlink()


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


def add(desk: Desk, kind: str, text: str, code: str = "", release=None, ask=None) -> bool:
    """Queue an alert for the next run; False (nothing queued) when no alert command is set. code: the one-time
    code of release `release` the text holds (it replaces one queued before for that release, now void); the entry
    goes once it is sent, and none of what the command prints for it is kept. ask: the to-do's ID, so a closed
    to-do can be left out when the gathered alerts go out."""
    if not command(desk.settings()):
        return False
    with desk.lock():
        q = _clean(desk)
        if code:
            q = [x for x in q if not (x.get("code") and x.get("release") == release)]
        q.append({"kind": kind, "text": desk.redact(text), "at": time.time(),
                  **({"code": code, "release": release} if code else {}), **({"ask": ask} if ask is not None else {})})
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
    at = f"{r.get('base')} at {str(r.get('commit') or '')[:10]}"
    rest = (f"{r.get('count', 0)} commit(s), command: {r.get('command')}"
            + (f"; note: {r['note']}" if r.get("note") else ""))
    if waiting and r.get("direct_count"):
        rest = f"{r['direct_count']} direct commit(s), " + rest
    if waiting and code:  # the code and what to do with it first: a phone shows the first hundred characters or so,
        # and no cut of a long text takes it
        return (f"release #{r['id']} (lane {r.get('lane')}, target {target or '(unnamed)'}) · code {code}: to release "
                f"it, give this code to the coordinator in the conversation (this release only, once, "
                f"{release.CODE_LIFE // 3600} hours; send it nowhere else), or answer on the page · {at}, {rest}")
    head = f"release #{r['id']} of lane {r.get('lane')}, target {target or '(unnamed)'} ({at})"
    if waiting:
        return f"{head} waits for your yes on the page: {rest}"
    return f"{head}: {release.state_text(desk, r)}"


def from_event(desk: Desk, kind: str, fields: dict) -> None:
    """Queue the alert an event calls for, if any (Desk.event calls this)."""
    if kind.startswith("release_"):
        from . import release
        r = release.get(desk, fields.get("id")) or {}
        if r.get("after_merge") and not (kind == "release_ended" and r.get("state") == "failed"):
            return  # these automatic checks alert the principal only on failure
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
        add(desk, "ask", f"to-do #{fields.get('id')} for you: {a.get('text', '')}", ask=fields.get("id"))
    elif kind == "provider_credit":
        add(desk, "credit", fields["text"])
    elif kind == "sandbox_held" and fields.get("alert"):  # once for each lane and reason (run.hold)
        from .run import sandbox_held_text
        why = sandbox_held_text({**fields, "kind": fields.get("run")})
        add(desk, "sandbox", f"lane {fields.get('lane')} cannot run: {why}; "
                             + (f"task {fields['task']} is held" if fields.get("task") else "its tasks are held")
                             + ", not failed")
    elif kind == "disk_low":  # once each time it goes under (disk.look)
        add(desk, "disk", f"free space is under the floor ({fields.get('floor') or 0:g} GB): "
                          + "; ".join(fields.get("volumes") or []) + ": hansa serve starts no new task or release "
                          "until there is space again; what runs goes on")


# -- sending -----------------------------------------------------------------------------------------------------------
def text_of(desk: Desk, items: list, held: int = 0) -> str:
    """What the command reads on its standard input: one line per alert, keys removed, cut at MOST_TEXT characters.
    held: how many of them the limit of MOST_RUNS runs an hour held back."""
    items = sorted(items, key=lambda x: not x.get("code"))  # those holding a code first: no cut takes it
    lines = [" ".join(str(x.get("text") or "").split()) for x in items]
    codes = [x for x in items if x.get("code")]
    name = "/".join(str(desk.root).split("/")[-2:]).lstrip("/") or "/"
    head = f"[hansa] desk {name}"
    held_text = f" ({held} held back by the limit of {MOST_RUNS} runs an hour)" if held else ""
    if len(lines) == 1:  # a code stays early: what was held back is said after it
        text = f"{head}: {lines[0]}{held_text}" if codes else f"{head}{held_text}: {lines[0]}"
    else:  # the first line gives each waiting release's number and code, before what was held back
        text = (f"{head}: {len(lines)} alerts" + "".join(f" · #{x.get('release')} code {x['code']}" for x in codes)
                + held_text + "\n" + "\n".join(f"- {x}" for x in lines))
    text = desk.redact(text)
    return text if len(text) <= MOST_TEXT else text[:MOST_TEXT - 1] + "…"


def run(desk: Desk, cmd: str, kinds: str, text: str, timeout: float = TIMEOUT, codes=()) -> dict:
    """Run the alert command once: {ok, exit, timed_out, limit, tail, at}. It runs in a folder of its own under
    runs_dir, removed after however the command ends (what a killed server left, sweep_runs removes), so that what it
    writes to a relative path lands nowhere hansa keeps or anyone but the principal reads. codes: the one-time codes the text holds;
    then none of what the command printed is kept (tail is empty, withheld says why), as it may echo a code in any
    form."""
    env = {**without_session(os.environ), "HANSA_ALERT_KIND": kinds}
    for k in ("HANSA_TASK", "HANSA_MERGING"):
        env.pop(k, None)
    timed_out, code, out = False, None, b""
    # stdin and output are files, not pipes: the command's own exit ends the run, even when a child it left behind
    # still holds them; only at the time limit is its whole group killed
    desk.secrets_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(desk.secrets_dir, 0o700)
    runs_dir(desk).mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryFile() as given, tempfile.TemporaryFile() as said, \
            tempfile.TemporaryDirectory(prefix="run-", dir=runs_dir(desk)) as here:
        given.write(text.encode("utf-8"))
        given.seek(0)
        try:
            proc = subprocess.Popen(["/bin/sh", "-c", cmd], cwd=here, env=env, stdin=given,
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
    result = {"ok": code == 0 and not timed_out, "exit": code, "timed_out": timed_out, "limit": timeout, "tail": [],
              "at": now(),
              "kinds": kinds}
    if codes:
        return {**result, "withheld": WITHHELD}
    said = desk.redact(out.decode("utf-8", errors="replace")).strip().splitlines()[-TAIL:]
    return {**result, "tail": [x[:200] for x in said]}


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
                   timed_out=result["timed_out"], limit=result.get("limit"), tail=result["tail"],
                   **({"withheld": result["withheld"]} if result.get("withheld") else {}))


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
            closed = {a["id"] for a in desk.asks() if a.get("closed")}
            q = [x for x in _clean(desk) if x["kind"] != "ask" or x.get("ask") not in closed]
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
        result = run(desk, cmd, kinds, text, timeout, codes=[x["code"] for x in codes])
        _codes_went(desk, codes, text, result["ok"])
        _record(desk, result, len(q), t)
        return result


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
    from .run import _last_round, _server_elapsed
    last, mono = _last_round(desk)
    quiet = _server_elapsed(last, mono) if last else 0
    if quiet < STUCK_ALERT:
        return 0.0
    if not told:
        add(desk, "server_stuck", f"hansa serve has finished no round for {int(quiet // 60)} minutes: no task starts "
                                  "or ends, and no release runs; see hansa explain server")
        return time.time()
    return told


# -- the coordinator's session cannot be reached (kind `coordinator`) -------------------------------------------------
def unheard_file(desk: Desk) -> Path:
    return desk.state_dir / "coordinator-alert.json"


def _unheard(desk: Desk) -> dict:
    try:
        rec = read_json(unheard_file(desk), {})
    except ValueError:  # not UTF-8: a hand edit; hansa writes it whole
        rec = {}
    return rec if isinstance(rec, dict) else {}


def _mins(seconds: float) -> int:
    return max(0, int(seconds // 60))


def _at(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _short(line: str) -> str:
    """What waits, as a follower's line says it, without its next command."""
    line = " ".join(str(line).split()).split(" · next: ")[0]
    return line if len(line) <= 160 else line[:159] + "…"


def _waiting(rec: dict) -> list:
    """What the record says waits unheard, the oldest first: [{at, line}]."""
    waits = rec.get("waits") if isinstance(rec.get("waits"), dict) else {}
    return sorted((x for x in waits.values() if isinstance(x, dict) and _at(x.get("at"))), key=lambda x: x["at"])


def unheard_check(desk: Desk, t: float | None = None) -> dict:
    """hansa serve's look at the first case of the `coordinator` alert: something a follower would post (notify._item)
    has waited no_session_min in the event log, and the desk has no live follower. What waits since a follower last
    posted is kept in unheard_file ({pos, waits: {what: {at, line}}, told}); told: when it was alerted. A live
    follower, or the case turned off, clears it: the case begins anew. So does a follower the register shows started
    after the alert, live or not (one may start and end between two looks): what waits is then read again from where
    it got to. Returns the record as it is now."""
    from . import notify
    from .desk import coordinator_alert_settings
    t = time.time() if t is None else t
    s = desk.settings()
    mins = coordinator_alert_settings(s)["no_session_min"]
    rec = stored = _unheard(desk)
    end, recs = notify.log_end(desk), notify.records(desk)
    if not mins or not isinstance(rec.get("pos"), int) or any(notify.is_live(r) for r in recs):
        new = {"pos": end}  # a session hears from hansa (or this is the first look): nothing waits unheard
    else:
        if _at(rec.get("told")) and any(_at(r.get("since")) and r["since"] > rec["told"] for r in recs):
            rec = {"pos": rec["pos"]}  # a follower ran since the alert: the case begins anew
        posted = [r["pos"] for r in recs if isinstance(r.get("pos"), int)]  # where a follower got to
        pos = min(max([rec["pos"], *posted]), end)
        waits = dict(rec["waits"]) if isinstance(rec.get("waits"), dict) else {}
        while pos < end:
            events, at = notify.read_log(desk, pos, end - pos)
            if at == pos:
                break
            pos = at
            for e in events:
                it = notify._item(desk, e)
                if it and not it[4]:  # not a cancel the session made itself: that one knows
                    key = f"task {it[0][1]}" if it[0][0] in ("task", "merge") else " ".join(map(str, it[0]))
                    waits.setdefault(key, {"at": e["ts"] if _at(e.get("ts")) else t,
                                           "line": _short(desk.redact(it[1]))})
        new = {"pos": pos, "waits": waits, **({"told": rec["told"]} if _at(rec.get("told")) else {})}
        items = _waiting(new)
        if items and "told" not in new and t - items[0]["at"] >= mins * 60 and command(s) \
                and add(desk, "coordinator", unheard_text(desk, items, t)):
            new["told"] = t
    if new != stored:
        desk.state_dir.mkdir(exist_ok=True)
        write_json(unheard_file(desk), new)
    return new


def unheard_text(desk: Desk, items: list, t: float) -> str:
    """The first case's alert: the case, the session that last heard from hansa, and what waits."""
    from . import notify
    last = max(notify.records(desk), key=lambda r: r.get("seen") or 0, default=None)
    if last:
        sess = last.get("session") or {}
        how = ("its session ended" if notify.gone(sess) else
               "its session runs, but notifications were turned off or its follower stopped")
        who = (f"the last was session {notify.who(sess.get('pid'), str(last.get('name') or ''))}, until "
               f"{_when(last.get('ended') or last.get('seen'))}: {how}")
    else:
        who = "none has on this desk yet"
    return (f"the coordinator's session cannot be reached: no session hears from hansa ({who}) · {len(items)} "
            f"thing(s) wait for the coordinator, the oldest for {_mins(t - items[0]['at'])} min: {items[0]['line']}")


def session_minutes(s: dict) -> dict:
    """What a follower watches its session for (notify.Watch): the minutes of the second and third cases, 0 for off;
    both 0 when no alert command is set, so that it looks nothing up."""
    from .desk import coordinator_alert_settings
    mins = coordinator_alert_settings(s)
    on = bool(command(s))
    return {"window": mins["window_min"] if on else 0, "review": mins["review_min"] if on else 0}


def reviews_file(desk: Desk) -> Path:
    return desk.state_dir / "coordinator-review.json"


def from_follower(desk: Desk, me: dict, req: dict) -> str:
    """A follower's case of the `coordinator` alert (notify.Watch), queued in words: the session (its pid, and its name
    when known), the case, and what waits. The third case goes once per task's round across the desk, however many
    followers watch it (a task that records no session is every follower's): reviews_file keeps {task: its round's
    finished} for those alerted that still wait for review. Returns "queued", "told" (another follower's alert went
    for it), or "" when nothing was queued."""
    from . import notify
    sess = me.get("session") if isinstance(me.get("session"), dict) else {}
    who = notify.who(sess.get("pid"), str(me.get("name") or "")[:80])
    t = time.time()
    if req.get("case") == "window":
        since = req["since"] if _at(req.get("since")) else t
        count = req["count"] if isinstance(req.get("count"), int) else 0
        text = (f"the coordinator's session {who} has waited on its window (a permission dialog or a question) for "
                f"{_mins(t - since)} min, so it cannot act on what hansa posted to it: {count} thing(s) since "
                f"{time.strftime('%H:%M', time.localtime(since))}, the oldest: {_short(req.get('oldest') or '')} · "
                "answer it in that session")
    elif req.get("case") == "review" and notify.TASK_ID.match(str(req.get("task") or "")):
        tid = req["task"]
        try:
            m = desk.meta(tid)
        except SystemExit:
            return ""
        title = m.get("title", "")
        posted = req["posted"] if _at(req.get("posted")) else t
        text = (f"the coordinator's session {who} is idle while {tid} has waited for review for {_mins(t - posted)} "
                f"min since hansa posted it there: {title} · hansa does not post it again")
        with desk.lock():
            try:
                told = read_json(reviews_file(desk), {})
            except ValueError:  # not UTF-8: a hand edit; hansa writes it whole
                told = {}
            told = {k: v for k, v in (told.items() if isinstance(told, dict) else ())
                    if notify.waits_review(desk, k)}  # merged, closed or queued again: forgotten
            round_ = m.get("finished")
            if tid in told and told[tid] == round_:
                return "told"
            queued = add(desk, "coordinator", text)
            if queued:
                told[tid] = round_
            desk.state_dir.mkdir(exist_ok=True)
            write_json(reviews_file(desk), told)
        return "queued" if queued else ""
    else:
        return ""
    return "queued" if add(desk, "coordinator", text) else ""


def coordinator_lines(desk: Desk, s: dict, t: float | None = None) -> list:
    """For hansa status: one line for each case of the `coordinator` alert that holds now, alerted or not (the second
    and third as the followers last recorded them; they look only while an alert command is set)."""
    from . import notify
    from .desk import coordinator_alert_settings
    t = time.time() if t is None else t
    mins = coordinator_alert_settings(s)
    lives, out = notify.live_records(desk), []
    items = _waiting(_unheard(desk)) if not lives and mins["no_session_min"] else []
    if items and t - items[0]["at"] >= mins["no_session_min"] * 60:
        out.append(f"no session hears from hansa, so the coordinator's session cannot be reached: {len(items)} "
                   f"thing(s) wait for it, the oldest for {_mins(t - items[0]['at'])} min ({items[0]['line']})")
    for r in lives:
        holds = r.get("coordinator") if isinstance(r.get("coordinator"), dict) else {}
        who = notify.who((r.get("session") or {}).get("pid"), r.get("name") or "")
        since = holds.get("window")
        if mins["window_min"] and _at(since) and t - since >= mins["window_min"] * 60:
            out.append(f"the coordinator's session {who} has waited on its window for {_mins(t - since)} min (a "
                       "permission dialog or a question), with what hansa posted to it unanswered")
        tasks = holds.get("review") if isinstance(holds.get("review"), dict) else {}
        shown = [f"{tid} for {_mins(t - at)} min" for tid, at in sorted(tasks.items()) if _at(at)]
        if mins["review_min"] and shown:
            out.append(f"waiting for review while the coordinator's session {who} is idle: {', '.join(shown)}")
    return out


def start_thread(desk: Desk, every: float = EVERY) -> threading.Thread:
    """hansa serve's alert thread: it sends what gathered, and alerts when the server's own rounds stop coming or
    no session hears from hansa (unheard_check, every UNHEARD_EVERY seconds)."""
    def loop():
        told, unheard = 0.0, 0.0
        while True:
            try:
                told = stuck_check(desk, told)
                if time.time() >= unheard:
                    unheard = time.time() + UNHEARD_EVERY
                    unheard_check(desk)
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
    limit = f.get("limit") if isinstance(f.get("limit"), (int, float)) and not isinstance(f.get("limit"), bool) \
        else TIMEOUT  # recorded before the limit was: the default
    how = f"timed out after {limit:g} s" if f.get("timed_out") else f"exit {f.get('exit')}"
    tail = " | ".join(f.get("tail") or []) or (WITHHELD if f.get("withheld") else "")
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
            "filed (hansa ask), a lane's runs cannot start because their sandbox cannot, free space goes under the "
            "floor (hansa disk-floor), hansa serve finishes no round for a long time, or what hansa posts to your "
            "session cannot reach it (no session hears from hansa, your session waits on a permission dialog or a "
            "question, or a task posted to it waits for review while it is idle). You send no alerts and cannot set "
            "it. A release waiting for the principal gets a "
            "one-time code that only that alert carries: the principal answers on the page, or says the code in the "
            "conversation, and you pass it on with hansa release yes ID CODE (or no ID CODE). Never guess one.")
