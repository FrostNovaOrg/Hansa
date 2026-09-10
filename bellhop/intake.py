"""The principal's words, logged by the script that sees them.

On a production desk the principal's words were written down twice: once by a
`UserPromptSubmit` hook and once by hand, by the coordinator, a few turns later.
The two records were numbered independently, so the same message existed as
intake 9 and as intake 11, and a decision entry then cited the wrong one. Nobody
could tell from the file which number belonged to which message.

Three rules follow from that, and they are the whole module:

1. **One writer.** The hook writes this file; the coordinator never appends to it
   by hand. What the coordinator wants is a *number*, and the number comes from
   here.
2. **The script assigns the number**, as the largest existing number plus one and
   never as the count of entries. Entries do get removed (a machine sentence that
   slipped past the filter, say), and after a removal a count-based number would
   be issued twice. A fresh entry and a reissued old number look the same in the
   phrase "intake 9".
3. **The same words twice inside the window are one message.** The repeat leaves a
   marker line in the book but no entry, so `bellhop readings` can still count it -
   a duplicate that only ever reached a terminal and a duplicate that never
   happened look the same in next week's reading. A hook that fires
   again on a resubmitted prompt, or a second hook registered by both the plugin
   and the desk, would otherwise turn one message into two entries with two
   numbers - which is exactly the failure above, arriving by a different door.

What is not logged: slash commands, empty prompts, and anything matching
`intake_skip_patterns`. That list exists because the desk's own machinery talks
through the same channel a person types into - the sentinel's wake command feeds
its status text back in as a prompt - and **a machine's sentence and a person's
sentence look alike to a hook**. Nothing in this module ever exits non-zero: a
prompt that failed to be logged is a gap in a book, while a hook that fails a
prompt is a session that will not start.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

from .config import Desk
from .util import append, read, write

INTAKE = "state/intake.md"
TIME_FMT = "%Y-%m-%d %H:%M"
HEADING = re.compile(r"^## Intake (\d+) \((\d{4}-\d{2}-\d{2} \d{2}:\d{2})\)\s*$")
# A duplicate leaves a marker line but no entry. The marker is written into the
# book, not only printed, because `bellhop readings` counts intake duplicates by
# scanning this file: a duplicate that only ever reached a terminal and a
# duplicate that never happened look the same in next week's reading. It is kept
# out of the entry body below, or the second repeat of a message would no longer
# match the first and would be logged as new.
MARKER = re.compile(r"^\(duplicate of \d+")

HEADER = (
    "# Principal intake (append-only; written by the UserPromptSubmit hook, never by hand)\n"
    "(one entry per message: a `## Intake N (YYYY-MM-DD HH:MM)` heading, then the words verbatim.\n"
    "N is assigned by the script as the largest existing number plus one, never as the entry count,\n"
    "so a removed entry never causes a number to be issued twice. `bellhop intake list` and\n"
    "`bellhop intake show N` read it; nothing else writes it.)\n"
)

# Results of `log`.
LOGGED = "logged"
DUPLICATE = "duplicate"
SKIPPED = "skipped"


class Entry(NamedTuple):
    number: int
    when: str
    text: str


def path(desk: Desk) -> Path:
    return desk.path(INTAKE)


def entries(desk: Desk) -> List[Entry]:
    p = path(desk)
    if not p.exists():
        return []
    out: List[Entry] = []
    number: Optional[int] = None
    when = ""
    body: List[str] = []
    for line in read(p).splitlines():
        m = HEADING.match(line)
        if m:
            if number is not None:
                out.append(Entry(number, when, "\n".join(body).strip("\n")))
            number, when, body = int(m.group(1)), m.group(2), []
        elif number is not None and not MARKER.match(line):
            body.append(line)
    if number is not None:
        out.append(Entry(number, when, "\n".join(body).strip("\n")))
    return out


def next_number(desk: Desk) -> int:
    """Largest existing number + 1. Not the count: see rule 2 in the module docstring."""
    known = [e.number for e in entries(desk)]
    return (max(known) if known else 0) + 1


def normalise(text: str) -> str:
    """What counts as "the same message": whitespace collapsed, case folded."""
    return " ".join((text or "").split()).casefold()


def _patterns(desk: Desk) -> List[re.Pattern]:
    out = []
    for raw in desk.cfg.get("intake_skip_patterns") or []:
        try:
            out.append(re.compile(raw))
        except re.error:
            continue  # a bad pattern in config skips nothing; it never breaks the hook
    return out


def skip_reason(desk: Desk, text: str) -> Optional[str]:
    """Why this prompt is not the principal talking, or None if it is."""
    s = (text or "").strip()
    if not s:
        return "empty prompt"
    if s.startswith("/"):
        return "slash command"
    for pat in _patterns(desk):
        if pat.search(s):
            return f"matches intake_skip_patterns /{pat.pattern}/"
    return None


def _parse_when(when: str) -> Optional[datetime]:
    try:
        return datetime.strptime(when, TIME_FMT)
    except (ValueError, TypeError):
        return None


def duplicate_of(desk: Desk, text: str, when: Optional[datetime] = None) -> Optional[int]:
    """The number of a recent entry with the same normalised words, if there is one."""
    minutes = desk.cfg.get("intake_dedupe_minutes", 10)
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = 10
    if minutes <= 0:
        return None
    now = when or datetime.now()
    window = timedelta(minutes=minutes)
    wanted = normalise(text)
    if not wanted:
        return None
    for entry in reversed(entries(desk)):
        stamp = _parse_when(entry.when)
        if stamp is None:
            continue
        if now - stamp > window:
            break  # entries are in order; everything older is further away
        if normalise(entry.text) == wanted:
            return entry.number
    return None


def log(desk: Desk, text: str, when: Optional[datetime] = None) -> Tuple[str, object]:
    """Append the principal's words. Returns (LOGGED, n) / (DUPLICATE, n) / (SKIPPED, why)."""
    reason = skip_reason(desk, text)
    if reason:
        return SKIPPED, reason
    now = when or datetime.now()
    dupe = duplicate_of(desk, text, now)
    if dupe is not None:
        p = path(desk)
        if not p.exists():
            write(p, HEADER)
        append(p, "(duplicate of %d, not logged; %s)\n" % (dupe, now.strftime(TIME_FMT)))
        return DUPLICATE, dupe
    p = path(desk)
    if not p.exists():
        write(p, HEADER)
    number = next_number(desk)
    append(p, "\n## Intake %d (%s)\n%s\n" % (number, now.strftime(TIME_FMT), text.rstrip()))
    return LOGGED, number


def titles(desk: Desk, last: int = 3) -> List[str]:
    """The heading lines of the most recent entries, for the board."""
    return ["## Intake %d (%s)" % (e.number, e.when) for e in entries(desk)[-last:]] if last else []


def list_text(desk: Desk, last: Optional[int] = None) -> str:
    rows = entries(desk)
    if not rows:
        return "no intake entries yet (%s)\n" % INTAKE
    shown = rows[-last:] if last else rows
    out = []
    for e in shown:
        head = " ".join(e.text.split())
        out.append("%d (%s) %s" % (e.number, e.when, head[:100] + "…" if len(head) > 100 else head))
    out.append("%d of %d entr%s" % (len(shown), len(rows), "y" if len(rows) == 1 else "ies"))
    return "\n".join(out) + "\n"


def show_text(desk: Desk, number: int) -> str:
    for e in entries(desk):
        if e.number == number:
            return "## Intake %d (%s)\n%s\n" % (e.number, e.when, e.text)
    known = [str(e.number) for e in entries(desk)]
    raise KeyError("no intake %d (have: %s)" % (number, ", ".join(known) or "none"))
