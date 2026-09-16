"""The sentinel: red/green checks on a timer, and the three things it may do about them.

Every run writes **one line** to `state/sentinel.log`, green included. On a production
desk only the red runs were logged, and "the sentinel did not run" and "the sentinel ran
and found nothing" then look the same in the file - which is the worse of the two to
mistake for the other, because one of them is a dead sentinel.

The three actions the sentinel may take are all guarded, and each guard exists because
its absence cost something:

* **wake** - a brake and a yield gate. The wake command used to fire on every red run
  forever, poking an idle coordinator with the same sentence; the brake stops after
  `wake_max_repeats` wakes against an unchanged board and asks for a person. The yield
  gate is the busy probe: a wake that lands mid-turn is worse than a late wake, and a
  skipped wake does not count toward the brake.
* **daily clear** - a *window*, not a minute. The old trigger fired at one exact minute
  and missed five nights out of seven whenever a run slipped; the window tries on every
  run inside it and clears on the first idle one. Idle means the busy probe says so and
  no in-flight line is tagged `settled-awaiting-close`; without a probe it does not clear
  at all, because clearing blind is what lost two nights' work.
* **idle compaction** - off by default. See `docs/sentinel.md` for why: an interrupted
  task costs more than the compaction saves.

Everything a check reads, it reads from files. `--dry-run` runs the checks and the probe
(both read-only) and prints what it would have done, writing nothing at all.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import List, Optional, Tuple

from . import extensions as extensions_mod
from . import exceptions as exceptions_mod
from . import ledger as ledger_mod
from . import seal as seal_mod
from . import stopdispatch as stop_mod
from .archive import unpushed_count
from .card import read_card
from .config import Desk
from .decisions import decisions_path
from .util import BellhopError, now, read, read_json, sha256_bytes, today, ts, write, write_json

Check = Tuple[str, bool, str]

SENTINEL_LOG = "state/sentinel.log"
WAKE_STATE = "state/.sentinel/wake.json"
COMPACT_STATE = "state/.sentinel/compact.json"
DAILY_CLEAR_MARKER = "state/.daily-clear-%s"
SETTLED_TAG = "settled-awaiting-close"
DEFAULT_CLEAR_WINDOW = "04:00-09:00"
COMPACT_COOLDOWN_HOURS = 6
PROBE_TIMEOUT_S = 30


def _parse_ts(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
    except Exception:
        return None


def _last_receipt_time(desk: Desk):
    latest = None
    for p in desk.receipts.glob("*.meta.json"):
        data = read_json(p, {})
        t = _parse_ts(data.get("finished_at", ""))
        if t and (latest is None or t > latest):
            latest = t
    return latest


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------
def check_card(desk: Desk) -> Check:
    try:
        card = read_card(desk)
    except BellhopError as exc:
        return ("card", False, str(exc))
    refreshed = _parse_ts(card.get("refreshed", ""))
    if not refreshed:
        return ("card", False, "dispatch card has no valid `refreshed` time")
    last = _last_receipt_time(desk)
    if last and refreshed < last:
        return ("card", False, f"dispatch card ({card['refreshed']}) is older than the last receipt ({ts(last)}); refresh it")
    if card.get("ready", "no").lower() == "no" and not card.get("reason"):
        return ("card", False, "card says ready: no without a reason")
    return ("card", True, f"ready={card.get('ready')} next={card.get('next') or '-'}")


def inflight_lines(desk: Desk) -> List[str]:
    p = desk.state / "in-flight.md"
    if not p.exists():
        return []
    return [l for l in read(p).splitlines() if l.startswith("- ") and l.strip() != "- none"]


def check_inflight(desk: Desk) -> Check:
    p = desk.state / "in-flight.md"
    if not p.exists():
        return ("in-flight", False, "state/in-flight.md is missing")
    rows = inflight_lines(desk)
    bad = [l for l in rows if not (l.startswith("- [worker] ") or l.startswith("- [helper] ") or l.startswith("- [coordinator] "))]
    if bad:
        return ("in-flight", False, f"{len(bad)} line(s) without a [worker]/[helper]/[coordinator] label")
    return ("in-flight", True, f"{len(rows)} in flight")


def check_dead_tasks(desk: Desk) -> Check:
    dead = []
    for p in desk.receipts.glob("*.meta.json"):
        data = read_json(p, {})
        if data.get("dead") and not ledger_mod.entries_mentioning(desk, data.get("id", "\0")):
            dead.append(data.get("id"))
    if dead:
        return ("dead-tasks", False, f"unacknowledged dead task(s): {', '.join(sorted(dead))}")
    return ("dead-tasks", True, "none unacknowledged")


def check_ledger(desk: Desk, snapshot: dict) -> Check:
    count, size = ledger_mod.stats(desk)
    prev = snapshot.get("ledger", {})
    if prev and (count < prev.get("count", 0) or size < prev.get("size", 0)):
        return ("ledger", False, f"ledger shrank: {prev.get('count')}→{count} entries, {prev.get('size')}→{size} bytes")
    snapshot["ledger"] = {"count": count, "size": size}
    return ("ledger", True, f"{count} entries, {size} bytes")


def check_decisions(desk: Desk, snapshot: dict) -> Check:
    p = decisions_path(desk)
    if not p.exists():
        return ("decisions", True, "no decisions file yet")
    data = p.read_bytes()
    prev = snapshot.get("decisions", {})
    if prev:
        n = prev.get("size", 0)
        if len(data) < n or sha256_bytes(data[:n]) != prev.get("prefix_sha256"):
            return ("decisions", False, "decisions.md was shortened or edited above its previous end")
    snapshot["decisions"] = {"size": len(data), "prefix_sha256": sha256_bytes(data)}
    return ("decisions", True, f"{len(data)} bytes, append-only holds")


def check_push(desk: Desk) -> Check:
    if not desk.cfg.get("push"):
        return ("push", True, "push policy off")
    n = unpushed_count(desk)
    if n is None:
        return ("push", False, "push policy on but no upstream configured")
    return ("push", n == 0, f"{n} unpushed commit(s)")


def check_seals(desk: Desk) -> Check:
    try:
        changed = seal_mod.verify(desk)
    except BellhopError as exc:
        return ("seals", True, f"no seals yet ({exc})")
    if changed:
        return ("seals", False, f"{len(changed)} sealed file(s) changed: {', '.join(changed[:3])}{'…' if len(changed) > 3 else ''}")
    return ("seals", True, "all seals hold")


def pending_bells(desk: Desk) -> List[Path]:
    return sorted(desk.bells_pending.glob("*.bell"))


def check_bells(desk: Desk) -> Check:
    pending = pending_bells(desk)
    oldest = None
    for b in pending:
        data = read_json(b, {})
        t = _parse_ts(data.get("created_at", ""))
        if t and (oldest is None or t < oldest):
            oldest = t
    if oldest:
        age = (now() - oldest).total_seconds() / 60
        limit = int(desk.cfg["sentinel_interval_minutes"]) * 3
        if age > limit:
            return ("bells", False, f"{len(pending)} pending bell(s); oldest waited {age:.0f} min (is the watchdog running?)")
    return ("bells", True, f"{len(pending)} pending")


def check_dead_bells(desk: Desk) -> Check:
    """A bell the watchdog gave up on. It stops being red once the ledger names it.

    The watchdog parks a bell in `bells/dead/` after `bell_max_failures` consecutive
    failures rather than answering it forever. That is the right thing to do and it is
    also invisible: the bell leaves `pending/`, the board's pending count falls, and
    the desk reads as if the work went out. So it is red here until somebody writes
    down what happened to it, the same way an unacknowledged dead task is.
    """
    d = desk.path("bells", "dead")
    parked = sorted(d.glob("*.bell")) if d.exists() else []
    unacknowledged = [p.name for p in parked if not ledger_mod.entries_mentioning(desk, p.name)]
    if unacknowledged:
        return ("dead-bells", False,
                f"{len(unacknowledged)} bell(s) parked in bells/dead/ after repeated failures: "
                + ", ".join(unacknowledged[:3]) + ("…" if len(unacknowledged) > 3 else ""))
    return ("dead-bells", True, f"{len(parked)} parked, all acknowledged" if parked else "none parked")


def _times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def check_stop_dispatch(desk: Desk) -> List[Check]:
    """Two separate lines: the order stands, and the order is being pierced.

    They are different facts with different fixes. A stop order that is being kept is a
    red somebody chose; a stop order that has been pierced twice is a red nobody chose,
    and it stays on every run until someone lifts it or obeys it.
    """
    order = stop_mod.read_stop(desk)
    if order is None:
        return [("stop-dispatch", True, "no stop order")]
    checks: List[Check] = [
        ("stop-dispatch", False, f"dispatch stopped since {order['stopped_at']}: {order['reason']}")
    ]
    n = len(order["exceptions"])
    if n >= stop_mod.PIERCED_AT:
        checks.append(("stop-order", False, f"stop order pierced {_times(n)} — ask the principal to lift it"))
    return checks


def run_checks(desk: Desk, persist: bool = True) -> List[Check]:
    snap_path = desk.state / ".sentinel" / "snapshot.json"
    snapshot = read_json(snap_path, {}) or {}
    checks = [
        check_card(desk),
        check_inflight(desk),
        check_dead_tasks(desk),
        check_bells(desk),
        check_dead_bells(desk),
        check_ledger(desk, snapshot),
        check_decisions(desk, snapshot),
        check_push(desk),
        check_seals(desk),
    ]
    checks.extend(check_stop_dispatch(desk))
    # A merge taken on the coordinator's judgement is allowed; two of them in a week
    # means the acceptance gate is not the gate any more, and that is the principal's
    # call rather than something the desk keeps absorbing one reasonable case at a time.
    checks.append(exceptions_mod.check(desk, "merge"))
    # Desk-local extensions run last and cannot raise out of here; a broken add-on
    # becomes one red line, never a sentinel that stopped running.
    checks.extend(extensions_mod.checks(desk, persist))
    if persist:
        write_json(snap_path, snapshot)
    return checks


# --------------------------------------------------------------------------
# the busy probe
# --------------------------------------------------------------------------
BUSY, IDLE, UNKNOWN = "busy", "idle", "unknown"


SHELL_NOT_RUNNABLE = (126, 127)  # the shell's own codes: not executable, not found


def probe_busy(desk: Desk) -> Tuple[str, str]:
    """(state, why). Exit 0 is busy, exit 1 is idle, a probe that could not run is busy.

    A probe that cannot run counts as busy, and *why* goes in the line: a broken probe
    and a permanently busy coordinator look the same in "did not clear today", and only
    one of the two is a fault in the desk. That is also why 126 and 127 are singled out -
    the shell, not the probe, produces them, and under `shell=True` a missing probe would
    otherwise pass for a confident "idle" and clear a coordinator that was mid-task.
    """
    cmd = (desk.cfg.get("busy_probe_cmd") or "").strip()
    if not cmd:
        return UNKNOWN, "no busy probe configured"
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return BUSY, f"busy probe timed out after {PROBE_TIMEOUT_S}s (counted as busy)"
    except OSError as exc:
        return BUSY, f"busy probe did not run ({exc}); counted as busy"
    if proc.returncode == 0:
        first = (proc.stdout or "").strip().splitlines()
        return BUSY, "busy probe says busy" + (f": {first[0][:80]}" if first else "")
    if proc.returncode == 1:
        return IDLE, "busy probe says idle"
    if proc.returncode in SHELL_NOT_RUNNABLE:
        err = ((proc.stderr or "").strip().splitlines() or [""])[0][:80]
        return BUSY, f"busy probe could not be run (exit {proc.returncode}: {err}); counted as busy"
    return BUSY, f"busy probe failed (exit {proc.returncode}); counted as busy"


# --------------------------------------------------------------------------
# the board fingerprint and the wake brake
# --------------------------------------------------------------------------
def board_fingerprint(desk: Desk) -> str:
    """Hash of the card, the in-flight lines and the pending bells: the board as it stands.

    If it has not changed since the last wake, nothing has happened as a result of that
    wake, and saying the same sentence again carries no new information.
    """
    card_path = desk.state / "next.md"
    parts = [read(card_path) if card_path.exists() else ""]
    parts.extend(inflight_lines(desk))
    parts.extend(b.name for b in pending_bells(desk))
    parts.extend(extensions_mod.fingerprint_parts(desk))
    return sha256_bytes("\n".join(parts).encode("utf-8"))[:16]


@dataclass
class Action:
    """One thing the sentinel may do, and what it decided about it."""

    kind: str
    done: bool
    note: str
    red: Optional[Check] = None
    writes: List[Tuple[Path, str]] = field(default_factory=list)

    def line(self) -> str:
        return f"{self.kind}: {self.note}"


def decide_wake(desk: Desk, checks: List[Check], busy: str, why: str, wake: bool) -> Tuple[Action, Optional[dict]]:
    """Whether to run `wake_cmd`, plus the wake state to store.

    Order matters: the yield gate is asked before the brake, so a wake skipped because
    the coordinator was mid-turn does not spend one of the brake's tries.
    """
    reds = [c for c in checks if not c[1]]
    cmd = (desk.cfg.get("wake_cmd") or "").strip()
    if not reds:
        return Action("wake", False, "not needed (green)"), None
    if not wake:
        return Action("wake", False, "suppressed (--no-wake)"), None
    if not cmd:
        return Action("wake", False, "no wake_cmd configured"), None
    if busy == BUSY:
        return Action("wake", False, f"yielded - {why}"), None

    fp = board_fingerprint(desk)
    prev = read_json(desk.path(WAKE_STATE), {}) or {}
    count = int(prev.get("count", 0)) if prev.get("fingerprint") == fp else 0
    limit = int(desk.cfg.get("wake_max_repeats", 3) or 3)
    if count >= limit:
        return (
            Action(
                "wake", False, f"braked - same board {_times(limit)}, waiting for the board to change",
                red=("wake", False, f"wake brake: same board {_times(limit)}, ask the principal"),
            ),
            {"fingerprint": fp, "count": count, "braked_at": ts()},
        )
    return (
        Action("wake", True, f"sent ({len(reds)} red, board {fp}, wake {count + 1}/{limit})"),
        {"fingerprint": fp, "count": count + 1, "last_wake": ts()},
    )


# --------------------------------------------------------------------------
# the daily clear window
# --------------------------------------------------------------------------
def parse_window(spec: str) -> Tuple[Optional[dtime], Optional[dtime], str]:
    """"HH:MM-HH:MM" -> (start, end, warning). A bad window falls back, loudly.

    A window that is silently empty and a window that simply never saw an idle run both
    produce "did not clear today", so a misconfigured window says so on every run rather
    than sending the reader to look for a fault in the coordinator.
    """
    spec = (spec or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$", spec)
    if not m:
        return None, None, f"daily_clear_window {spec!r} is not HH:MM-HH:MM; using {DEFAULT_CLEAR_WINDOW}"
    try:
        start = dtime(int(m.group(1)), int(m.group(2)))
        end = dtime(int(m.group(3)), int(m.group(4)))
    except ValueError:
        return None, None, f"daily_clear_window {spec!r} is not a real time of day; using {DEFAULT_CLEAR_WINDOW}"
    if start >= end:
        return None, None, f"daily_clear_window {spec!r} starts at or after it ends, so it is empty; using {DEFAULT_CLEAR_WINDOW}"
    return start, end, ""


def _default_window() -> Tuple[dtime, dtime]:
    start, end, _ = parse_window(DEFAULT_CLEAR_WINDOW)
    return start, end  # type: ignore[return-value]


def daily_clear_marker(desk: Desk, at: datetime) -> Path:
    return desk.path(DAILY_CLEAR_MARKER % today(at))


def decide_clear(desk: Desk, at: datetime, busy: str, why: str) -> Action:
    spec = (desk.cfg.get("daily_clear_window") or "").strip()
    if not spec:
        return Action("clear", False, "window off")
    start, end, warning = parse_window(spec)
    if warning:
        start, end = _default_window()
    prefix = f"{warning}; " if warning else ""
    window = f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"

    marker = daily_clear_marker(desk, at)
    if marker.exists():
        return Action("clear", False, f"{prefix}already cleared today")
    if not (start <= at.time() < end):
        return Action("clear", False, f"{prefix}outside the window {window} (now {at.strftime('%H:%M')})")
    cmd = (desk.cfg.get("clear_cmd") or "").strip()
    if not cmd:
        return Action("clear", False, f"{prefix}inside {window} but no clear_cmd configured")
    if busy != IDLE:
        return Action("clear", False, f"{prefix}inside {window} but not idle - {why}")
    tagged = [l for l in inflight_lines(desk) if SETTLED_TAG in l]
    if tagged:
        return Action(
            "clear", False,
            f"{prefix}inside {window} but {len(tagged)} in-flight line(s) tagged {SETTLED_TAG}; the close-out would be lost",
        )
    return Action(
        "clear", True, f"{prefix}sent inside {window}",
        writes=[(marker, f"{ts(at)} cleared inside {window}; {why}\n")],
    )


# --------------------------------------------------------------------------
# idle compaction
# --------------------------------------------------------------------------
def transcript_tokens(path: Path) -> Optional[int]:
    """Input + cache-read + cache-creation tokens of the last assistant turn, or None.

    Output tokens are deliberately not counted: what makes a turn expensive is the
    context carried into it, not what came back.
    """
    path = Path(path).expanduser()
    if not path.exists():
        return None
    last = None
    try:
        with path.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    j = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if j.get("type") != "assistant":
                    continue
                usage = ((j.get("message") or {}).get("usage")) or {}
                if not usage:
                    continue
                last = (
                    int(usage.get("input_tokens", 0) or 0)
                    + int(usage.get("cache_read_input_tokens", 0) or 0)
                    + int(usage.get("cache_creation_input_tokens", 0) or 0)
                )
    except OSError:
        return None
    return last


def decide_compact(desk: Desk, at: datetime, busy: str, why: str) -> Action:
    if not desk.cfg.get("idle_compact"):
        return Action("compact", False, "off")
    cmd = (desk.cfg.get("compact_cmd") or "").strip()
    if not cmd:
        return Action("compact", False, "on but no compact_cmd configured")
    transcript = (desk.cfg.get("coordinator_transcript") or "").strip()
    if not transcript:
        return Action("compact", False, "on but no coordinator_transcript configured")
    threshold = int(desk.cfg.get("compact_threshold_tokens", 250000) or 250000)
    tokens = transcript_tokens(Path(transcript))
    if tokens is None:
        return Action("compact", False, f"transcript unreadable: {transcript}")
    if tokens < threshold:
        return Action("compact", False, f"{tokens:,} tokens < {threshold:,}")
    if busy != IDLE:
        return Action("compact", False, f"{tokens:,} tokens >= {threshold:,} but not idle - {why}")
    prev = read_json(desk.path(COMPACT_STATE), {}) or {}
    last = _parse_ts(prev.get("last_sent", ""))
    if last:
        hours = (at - last).total_seconds() / 3600.0
        if hours < COMPACT_COOLDOWN_HOURS:
            return Action(
                "compact", False,
                f"{tokens:,} tokens >= {threshold:,} but sent {hours:.1f}h ago (cool-down {COMPACT_COOLDOWN_HOURS}h)",
            )
    return Action(
        "compact", True, f"sent ({tokens:,} tokens >= {threshold:,})",
        writes=[(desk.path(COMPACT_STATE), json.dumps({"last_sent": ts(at), "tokens": tokens}, indent=2) + "\n")],
    )


# --------------------------------------------------------------------------
# rendering and the run
# --------------------------------------------------------------------------
def status_line(checks: List[Check]) -> str:
    reds = [c for c in checks if not c[1]]
    head = "GREEN" if not reds else f"RED ({len(reds)})"
    parts = [f"{name}={'ok' if ok else 'RED'}" for name, ok, _ in checks]
    return f"{ts()} {head} | " + " ".join(parts)


def log_line(checks: List[Check], actions: List[Action], at: Optional[datetime] = None) -> str:
    """`<time> GREEN|RED <n checks> <summary>` - written on every run, green included."""
    reds = [name for name, ok, _ in checks if not ok]
    verdict = "GREEN" if not reds else "RED"
    summary = "all green" if not reds else f"{len(reds)} red ({', '.join(reds)})"
    tail = "; ".join(a.line() for a in actions)
    return f"{ts(at)} {verdict} {len(checks)} checks {summary}" + (f"; {tail}" if tail else "")


def _run_cmd(cmd: str, text: Optional[str] = None) -> None:
    subprocess.run(cmd, shell=True, input=text, text=True)


def sentinel(desk: Desk, wake: bool = True, dry_run: bool = False, at: Optional[datetime] = None) -> Tuple[bool, str]:
    green, report, _ = run_sentinel(desk, wake=wake, dry_run=dry_run, at=at)
    return green, report


def run_sentinel(desk: Desk, wake: bool = True, dry_run: bool = False,
                 at: Optional[datetime] = None) -> Tuple[bool, str, List[Check]]:
    """Run the checks, decide the three actions, log exactly one line.

    Returns (green, report, checks) so callers can reuse findings without rescanning.
    With `dry_run` nothing is written and no command is run; the
    report says what would have happened, and the read-only busy probe still runs so the
    answer it prints is the real one.
    """
    at = at or now()
    checks = run_checks(desk, persist=not dry_run)

    need_probe = bool(
        (desk.cfg.get("wake_cmd") or "").strip()
        or (desk.cfg.get("daily_clear_window") or "").strip()
        or desk.cfg.get("idle_compact")
    )
    busy, why = probe_busy(desk) if need_probe else (UNKNOWN, "probe not needed")

    wake_action, wake_state = decide_wake(desk, checks, busy, why, wake)
    if wake_action.red:
        checks = list(checks) + [wake_action.red]
    clear_action = decide_clear(desk, at, busy, why)
    compact_action = decide_compact(desk, at, busy, why)
    actions = [wake_action, clear_action, compact_action]

    lines = [status_line(checks)]
    for name, ok, detail in checks:
        lines.append(f"  {'ok ' if ok else 'RED'} {name}: {detail}")
    lines.append(f"  busy probe: {why}")
    for action in actions:
        lines.append(f"  {'DO ' if action.done else '-  '}{action.line()}")
    text = "\n".join(lines) + "\n"
    one_line = log_line(checks, actions, at)
    green = all(ok for _, ok, _ in checks)

    if dry_run:
        return green, (
            "[dry-run] nothing written, no command run\n" + text + f"[dry-run] log line: {one_line}\n"
        ), checks

    write(desk.state / ".sentinel" / "status.txt", text)
    with (desk.state / ".sentinel" / "history.log").open("a", encoding="utf-8") as fh:
        fh.write(lines[0] + "\n")
    log_path = desk.path(SENTINEL_LOG)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(one_line + "\n")

    if wake_state is not None:
        write_json(desk.path(WAKE_STATE), wake_state)
    for action in actions:
        if action.done:
            for path, content in action.writes:
                write(path, content)
    if wake_action.done:
        _run_cmd(desk.cfg["wake_cmd"], text)
    if clear_action.done:
        _run_cmd(desk.cfg["clear_cmd"])
    if compact_action.done:
        _run_cmd(desk.cfg["compact_cmd"])
    return green, text, checks
