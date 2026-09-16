"""The stop-dispatch switch: a file whose presence stops dispatch.

"Stop dispatching" has to work when the thing you want to stop is the thing that
would read the instruction. So the switch is not a sentence in a chat and not a
flag in memory: it is a file. Every dispatch path asks this module, and the answer
does not depend on anyone having read anything.

Two rules come from a production desk where a standing stop order was pierced again and again by "just this one exception", and nobody ever lifted it:

1. **An exception is written down, in the order itself.** Taking one is allowed;
   taking one quietly is not. An exception that was recorded and an exception that
   was rationalised in one turn's reasoning look the same afterwards.
2. **Two exceptions make the order itself red.** At that point the order is no
   longer being kept, and the honest move is to lift it or to obey it - which is a
   decision for the principal, not for the desk. The sentinel says so on every run
   until someone does one or the other.

Lifting is the only way the file goes away, and a lift writes one line to the
close-out register, so the span the order covered stays in the record.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import exits
from . import state as state_mod
from .config import Desk
from .util import BellhopError, append, now, read, ts, write

STOP_FILE = "state/stop-dispatch.md"
EXCEPTIONS_HEADING = "## Exceptions"
PIERCED_AT = 2

# The reason a provider circuit breaker writes. The prefix is load-bearing: the watchdog
# reads it to tell "the desk is out of credit" from "a person stopped the desk", and only
# the first of the two makes it hold before launching the next worker.
QUOTA_PREFIX = "provider quota: "


def stop_path(desk: Desk) -> Path:
    return desk.path(STOP_FILE)


def _parse_ts(s: str):
    from datetime import datetime

    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
    except (ValueError, TypeError):
        return None


def read_stop(desk: Desk) -> Optional[Dict[str, object]]:
    """The order as data, or None when dispatch is not stopped."""
    p = stop_path(desk)
    if not p.exists():
        return None
    keys: Dict[str, str] = {}
    exceptions: List[str] = []
    in_exceptions = False
    for line in read(p).splitlines():
        if line.strip() == EXCEPTIONS_HEADING:
            in_exceptions = True
            continue
        if in_exceptions:
            if line.startswith("- ") and line.strip() != "- none":
                exceptions.append(line[2:].strip())
            continue
        m = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if m:
            keys[m.group(1)] = m.group(2).strip()
    return {
        "stopped_at": keys.get("stopped_at", ""),
        "reason": keys.get("reason", ""),
        "by": keys.get("by", ""),
        # Written by the circuit breaker: the earliest time it is worth lifting this
        # order. Nothing acts on it. It is what the principal reads before deciding.
        "retry_after": keys.get("retry_after", ""),
        "exceptions": exceptions,
        "path": p,
    }


def dispatch_allowed(desk: Desk) -> Tuple[bool, str]:
    """(allowed, reason). The one question a dispatch path asks.

    The reason is written to be printed as-is by the refusing command: it names the
    time, the words of the order, how many exceptions have already been taken, and
    the command that lifts it.
    """
    order = read_stop(desk)
    if order is None:
        return True, ""
    n = len(order["exceptions"])
    taken = "no exceptions taken yet" if not n else f"{n} exception(s) already taken"
    tail = (
        " - the order is not being kept; ask the principal to lift it"
        if n >= PIERCED_AT
        else ""
    )
    return False, (
        f"dispatch stopped since {order['stopped_at']}: {order['reason']} ({taken}{tail}). "
        f"Lift it with `bellhop stop-dispatch lift`, or record an exception with "
        f"`bellhop stop-dispatch exception --reason \"...\"` before dispatching anyway."
    )


def _render(stopped_at: str, reason: str, by: str, retry_after: str = "") -> str:
    return (
        "# Dispatch stopped\n"
        "(the presence of this file is the switch; `bellhop stop-dispatch` is its only writer."
        " Do not delete it by hand - `lift` records the close-out.)\n"
        "\n"
        f"stopped_at: {stopped_at}\n"
        f"reason: {reason}\n"
        f"by: {by}\n"
        + (f"retry_after: {retry_after}\n" if retry_after else "")
        + "\n"
        f"{EXCEPTIONS_HEADING}\n"
        "(one line per dispatch that went out anyway: `- <time> <reason>`."
        " Two of them mean the order is not being kept, and the sentinel says so on every run.)\n"
        "\n"
        "- none\n"
    )


def set_stop(desk: Desk, reason: str, by: str = "coordinator", retry_after: str = "") -> Dict[str, object]:
    reason = (reason or "").strip().replace("\n", " ")
    if not reason:
        raise BellhopError("--reason is empty; a stop order without a reason cannot be lifted by anyone else")
    existing = read_stop(desk)
    if existing is not None:
        raise BellhopError(
            f"dispatch is already stopped since {existing['stopped_at']} ({existing['reason']}); "
            "lift it first, or record an exception",
            code=exits.GATE,
        )
    stamp = ts()
    write(stop_path(desk), _render(stamp, reason, by, retry_after))
    return {"stopped_at": stamp, "reason": reason, "by": by, "retry_after": retry_after, "exceptions": []}


def quota_stop(desk: Desk, provider: str, lane: str, retry_after: str = "",
               by: str = "watchdog") -> Tuple[Dict[str, object], bool]:
    """The circuit breaker's writer: stop dispatch because a provider said no. -> (order, written).

    Not `set_stop` directly, for one reason: this is called from the closing path of a
    worker that has already failed, and a second lane meeting the same wall a second
    later must not turn a stop order into an exception in the watchdog log. An order
    that already stands is left exactly as it is and reported as such -- the first
    answer is the one that describes the desk, and re-dating it would lose when the
    money actually ran out.
    """
    reason = f"{QUOTA_PREFIX}{provider}/{lane}"
    existing = read_stop(desk)
    if existing is not None:
        return existing, False
    return set_stop(desk, reason, by=by, retry_after=retry_after), True


def is_quota_stop(order: Optional[Dict[str, object]]) -> bool:
    return bool(order) and str(order.get("reason", "")).startswith(QUOTA_PREFIX)


def add_exception(desk: Desk, reason: str) -> Dict[str, object]:
    reason = (reason or "").strip().replace("\n", " ")
    if not reason:
        raise BellhopError("--reason is empty; an exception without a reason is not an exception")
    order = read_stop(desk)
    if order is None:
        raise BellhopError("dispatch is not stopped; there is nothing to make an exception to",
                           code=exits.NOT_FOUND)
    p = stop_path(desk)
    text = read(p)
    if "\n- none\n" in text:
        text = text.replace("\n- none\n", "\n", 1)
        write(p, text)
    append(p, f"- {ts()} {reason}\n")
    order = read_stop(desk)
    return order  # type: ignore[return-value]


def lift(desk: Desk, note: str = "") -> Dict[str, object]:
    order = read_stop(desk)
    if order is None:
        raise BellhopError("dispatch is not stopped; there is nothing to lift", code=exits.NOT_FOUND)
    started = _parse_ts(str(order["stopped_at"]))
    held = ""
    if started:
        hours = (now() - started).total_seconds() / 3600.0
        held = f", stood {hours:.1f}h"
    n = len(order["exceptions"])
    line = f"stop order lifted{held}, {n} exception(s) taken; was: {order['reason']}"
    if note:
        line += f"; {note}"
    cap = state_mod.cfg_int(desk, "closings_line_chars", 200)
    if len(line) > cap:
        line = line[: cap - 4].rstrip() + " ..."
    result = state_mod.closings_add(desk, "stop-dispatch", line)
    stop_path(desk).unlink()
    order["closing"] = line
    order["closing_path"] = str(result.paths[0]) if result.paths else ""
    return order


def status_text(desk: Desk) -> str:
    order = read_stop(desk)
    if order is None:
        return "dispatch: allowed (no stop order)\n"
    n = len(order["exceptions"])
    lines = [
        f"dispatch: STOPPED since {order['stopped_at']} (by {order['by'] or 'unknown'})",
        f"  reason: {order['reason']}",
    ]
    if order.get("retry_after"):
        lines.append(f"  not worth lifting before: {order['retry_after']}")
    lines += [
        f"  exceptions: {n}" + ("  <- the order is not being kept; ask the principal to lift it" if n >= PIERCED_AT else ""),
    ]
    lines.extend(f"    - {e}" for e in order["exceptions"])
    lines.append(f"  file: {stop_path(desk)}")
    return "\n".join(lines) + "\n"
