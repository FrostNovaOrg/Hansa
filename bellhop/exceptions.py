"""Recorded exceptions: the channel for going round a gate on purpose.

A desk has gates, and now and then the coordinator is right and the gate is wrong.
The question is never whether that happens; it is whether it leaves a mark. An
exception that was recorded and an exception that was reasoned through in one turn
and forgotten look identical the next morning, and only one of them can be counted.

So the same shape the stop-dispatch order already uses is written down here for any
gate: one append-only file, one line per exception, a reason that is not optional,
and a count that turns the sentinel red. Two inside seven days means the gate is no
longer a gate — which is a thing for the principal to decide about, not for the desk
to keep quietly absorbing.

`state/exceptions.md` is append-only and this module is its only writer.
"""
from __future__ import annotations

import re
from collections import namedtuple
from datetime import timedelta
from pathlib import Path
from typing import List, Optional

from .locking import serialized
from .util import BellhopError, append, now, read, ts, write

EXCEPTIONS = "state/exceptions.md"
RED_AT = 2
WINDOW_DAYS = 7

HEADER = (
    "# Recorded exceptions\n"
    "(append-only; `bellhop` writes it, nobody edits it. One line per time a gate was\n"
    "gone round on purpose: `- <time> | <kind> | <what> | <reason>`. Two of one kind\n"
    f"inside {WINDOW_DAYS} days turn the sentinel red — at that point the gate is not a\n"
    "gate, and lifting it or obeying it is the principal's call.)\n"
)

Entry = namedtuple("Entry", "when kind what reason line")

_LINE = re.compile(r"^- (?P<when>\S+) \| (?P<kind>[^|]*) \| (?P<what>[^|]*) \| (?P<reason>.*)$")


def path(desk) -> Path:
    return desk.path(EXCEPTIONS)


def _parse_ts(text: str):
    from datetime import datetime

    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")
    except (ValueError, TypeError):
        return None


def entries(desk) -> List[Entry]:
    p = path(desk)
    if not p.exists():
        return []
    out = []
    for line in read(p).splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        out.append(Entry(_parse_ts(m.group("when")), m.group("kind").strip(),
                         m.group("what").strip(), m.group("reason").strip(), line))
    return out


def recent(desk, kind: str, days: int = WINDOW_DAYS, at=None) -> List[Entry]:
    """The exceptions of one kind inside a rolling window.

    A line whose time will not parse is counted, not dropped: an unreadable timestamp
    and an exception that never happened must not read the same.
    """
    at = at or now()
    edge = at - timedelta(days=days)
    return [e for e in entries(desk) if e.kind == kind and (e.when is None or e.when >= edge)]


@serialized
def record(desk, kind: str, what: str, reason: str) -> Entry:
    """Append one exception and hand back the line as it was written."""
    kind = " ".join(str(kind).split())
    what = " ".join(str(what).split()).replace("|", "/")
    reason = " ".join(str(reason).split()).replace("|", "/")
    if not kind or not reason:
        raise BellhopError("an exception needs a kind and a reason; neither may be empty")
    p = path(desk)
    if not p.exists():
        write(p, HEADER)
    line = f"- {ts()} | {kind} | {what} | {reason}"
    append(p, line + "\n")
    return entries(desk)[-1]


def check(desk, kind: str = "merge", days: int = WINDOW_DAYS, at=None):
    """The sentinel's reading: (name, ok, detail)."""
    hits = recent(desk, kind, days=days, at=at)
    name = f"{kind}-exceptions"
    if len(hits) >= RED_AT:
        return (name, False, f"{len(hits)} {kind} exception(s) in {days} days"
                             f" (last: {hits[-1].reason[:60]}); the gate is being gone round,"
                             f" ask the principal")
    return (name, True, f"{len(hits)} in {days} days")


def status_text(desk, kind: Optional[str] = None) -> str:
    rows = [e for e in entries(desk) if kind is None or e.kind == kind]
    if not rows:
        return f"no recorded exceptions ({EXCEPTIONS})\n"
    out = [e.line for e in rows]
    kinds = sorted({e.kind for e in rows})
    for one in kinds:
        hits = recent(desk, one)
        out.append(f"{one}: {len(hits)} in the last {WINDOW_DAYS} days"
                   + ("  <- red" if len(hits) >= RED_AT else ""))
    return "\n".join(out) + "\n"
