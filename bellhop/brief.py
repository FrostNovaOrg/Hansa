"""The principal's page.

The board (`bellhop board`) is written for the coordinator: it is the state a turn
starts from. The principal has a different question, and on a production desk they
asked it again and again - "what is waiting for me", "is the queue empty",
"anything I need to do". Every one of those was answered from the coordinator's
memory of a board it had read some turns earlier, because no page existed that
answered the principal's question. An answer from memory and an answer from the
files look exactly alike in a chat window; only one of them is still true.

So this is a second rendering of the same files, ordered by what the principal can
act on:

1. **Waiting for you** - the lines only the principal can clear. First, always, and
   never trimmed. Everything else on this page is context for it.
2. **Queue** - what is next, how many bells are pending, whether dispatch is
   allowed at all.
3. **Today** - dispatched, closed, and how many times the principal had to ask.
4. **Cost** - today's coordinator cost and the full-chain cost per task, read from
   `state/readings.md` and never estimated.
5. **Returns and doubts** - the close-out lines that say something came back.
6. **Last decisions** - so the principal can see what the desk believes it was told.

Three rules, the same ones the board holds to:

* **Quote, do not summarise.** Every row is a line as it stands in a file.
* **A missing file and an empty one must not look alike.** A missing file says so;
  an empty list says `nothing`. A number the desk's own files cannot answer reads
  `n/a`, which is a reading, not a zero.
* **Over the cap, drop whole sections in a fixed order** - decisions, then cost,
  then the counts, then returns, then the queue - and say which went. "Waiting for
  you" carries no trimmable tag, so it cannot be reached from there. When
  everything trimmable is gone the page goes over the cap honestly rather than
  losing a row silently.
"""
from __future__ import annotations

import re
import subprocess
from typing import Dict, List, Optional, Tuple

from . import decisions as decisions_mod
from . import intake as intake_mod
from . import readings as readings_mod
from . import state as state_mod
from . import stopdispatch as stop_mod
from .config import Desk
from .util import read, read_json, today, ts, write

DEFAULT_BRIEF_BYTES = 1500
SENT_MARKER = "state/.brief-sent"
NOT_AVAILABLE = "n/a"
ROW_CHARS = 120

# The order sections are dropped in when the page is over the cap. "waiting" is
# deliberately absent: it is the reason the page exists.
TRIM_ORDER = ("decisions", "cost", "counts", "returns", "queue")

# A close-out line that says something came back. Matched on whole words so a task
# titled "returns dashboard" does not enter the list as a return.
RETURN_WORDS = re.compile(r"\b(return|returned|returning|doubt|doubts|rejected)\b", re.I)

DECISION_HEAD = re.compile(r"^## (?P<when>\S+) . (?P<kind>\w+) . (?P<who>.*)$")
DECISION_WORDS = re.compile(r'^- words: "(?P<words>.*)"\s*$')

# tag, heading, rows
Section = Tuple[str, str, List[str]]


def _cut(text: str, chars: int = ROW_CHARS) -> str:
    """Cut on a character boundary, never mid-codepoint."""
    return text if len(text) <= chars else text[:chars] + "..."


def _missing(rel: str) -> str:
    return "(%s is not on disk)" % rel


# -- the sections ----------------------------------------------------------

def _waiting(desk: Desk) -> Section:
    """The principal's own list. Never trimmed, so it is built first."""
    p = desk.path(state_mod.TODO)
    if not p.exists():
        return ("waiting", "== waiting for you ==", [_missing(state_mod.TODO)])
    lines = read(p).splitlines()
    try:
        start, end = state_mod.section_bounds(lines, "## Open")
        rows = [l for i, l in state_mod.data_lines(lines) if start <= i < end]
    except state_mod.StateError:
        # A todo file without an `## Open` heading is still a todo file; read the
        # whole of it rather than report nothing waiting.
        rows = [l for _i, l in state_mod.data_lines(lines)]
    heading = "== waiting for you (%d) ==" % len(rows)
    return ("waiting", heading, rows or ["nothing"])


def _pending_bells(desk: Desk) -> Optional[int]:
    """None when the desk has no bells directory at all, which is not zero bells."""
    if not desk.bells_pending.exists():
        return None
    try:
        return len(list(desk.bells_pending.glob("*.bell")))
    except OSError:
        return None


def _queue(desk: Desk) -> Section:
    rows: List[str] = []
    if not desk.path(state_mod.CARD).exists():
        rows.append(_missing(state_mod.CARD))
    else:
        try:
            keys = state_mod.read_card_keys(desk)
        except Exception as exc:  # a broken card must not take the page with it
            keys = {}
            rows.append("(card unreadable: %s)" % type(exc).__name__)
        if keys:
            rows.append("next: %s" % (keys.get("next") or "(the card names nothing)"))
            ready = (keys.get("ready") or "?").lower()
            line = "dispatchable: %s" % ready
            if ready != "yes" and keys.get("reason"):
                line += " - %s" % keys["reason"]
            rows.append(line)
    pending = _pending_bells(desk)
    rows.append("bells: %s pending" % (NOT_AVAILABLE if pending is None else pending))
    order = stop_mod.read_stop(desk)
    if order is None:
        rows.append("dispatch: allowed")
    else:
        rows.append("dispatch: STOPPED since %s - %s (%d exception(s))" % (
            order["stopped_at"], order["reason"], len(order["exceptions"])))
    return ("queue", "== queue ==", rows)


def _bell_days(desk: Desk) -> Optional[List[str]]:
    """The date of every bell this desk has rung, pending and rung alike."""
    dirs = [d for d in (desk.bells_pending, desk.bells_rung) if d.exists()]
    if not dirs:
        return None
    days: List[str] = []
    for d in dirs:
        for bell in sorted(d.glob("*.bell")):
            data = read_json(bell, None)
            when = (data or {}).get("created_at", "")
            if re.match(r"^\d{4}-\d{2}-\d{2}", when):
                days.append(when[:10])
            elif re.match(r"^\d{8}-", bell.name):
                # A bell whose JSON is damaged still carries its date in its own
                # name (`YYYYmmdd-HHMMSS-<id>.bell`). Dropping it would count a
                # bell that was rung as a bell that never was.
                days.append("%s-%s-%s" % (bell.name[:4], bell.name[4:6], bell.name[6:8]))
    return days


def _closings_days(desk: Desk) -> Optional[List[str]]:
    if not desk.path(state_mod.CLOSINGS).exists():
        return None
    return [l[2:12] for l in state_mod.closings_rows(desk)]


def _counts(desk: Desk) -> Section:
    day = today()
    bells = _bell_days(desk)
    closings = _closings_days(desk)
    asks: Optional[int]
    if intake_mod.path(desk).exists():
        asks = len([e for e in intake_mod.entries(desk) if e.when[:10] == day])
    else:
        asks = None

    def count(days: Optional[List[str]]) -> str:
        return NOT_AVAILABLE if days is None else str(len([d for d in days if d == day]))

    rows = ["dispatched %s, closed %s, %s message(s) from you" % (
        count(bells), count(closings), NOT_AVAILABLE if asks is None else asks)]
    return ("counts", "== today (%s) ==" % day, rows)


def readings_row(desk: Desk, day: str) -> Optional[Dict[str, str]]:
    """One day's line from `state/readings.md`, as {column name: cell}.

    The column names are read from the file's own header row when it has one, and
    fall back to `readings.COLUMNS`. A file whose columns were reordered by a newer
    Bellhop and one that still has the old order look the same to a fixed index.
    """
    p = desk.path(readings_mod.READINGS_FILE)
    if not p.exists():
        return None
    header: Optional[List[str]] = None
    for line in read(p).splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells:
            continue
        if cells[0] == "date":
            header = cells
        elif cells[0] == day:
            names = header or list(readings_mod.COLUMNS)
            return dict(zip(names, cells))
    return None


def _cost(desk: Desk) -> Section:
    row = readings_row(desk, today()) or {}
    coordinator = row.get("coordinator cost") or NOT_AVAILABLE
    chain = row.get("full-chain cost per task") or NOT_AVAILABLE
    rows = ["cost today %s, full chain per task %s" % (coordinator, chain)]
    if not row:
        rows.append("(no line for today in %s; run `bellhop readings --transcripts ...`)"
                    % readings_mod.READINGS_FILE)
    return ("cost", "== money ==", rows)


def _returns(desk: Desk) -> Section:
    if not desk.path(state_mod.CLOSINGS).exists():
        return ("returns", "== returns and doubts ==", [_missing(state_mod.CLOSINGS)])
    hits = [l for l in state_mod.closings_rows(desk) if RETURN_WORDS.search(l)]
    heading = "== returns and doubts (last 3 of %d) ==" % len(hits)
    return ("returns", heading, hits[-3:] or ["nothing"])


def last_decisions(desk: Desk, last: int = 3) -> List[str]:
    p = decisions_mod.decisions_path(desk)
    if not p.exists():
        return []
    out: List[str] = []
    head: Optional[re.Match] = None
    for line in read(p).splitlines():
        m = DECISION_HEAD.match(line)
        if m:
            head = m
            continue
        w = DECISION_WORDS.match(line)
        if w and head is not None:
            out.append("- %s %s: \"%s\"" % (
                head.group("when")[:10], head.group("kind"), w.group("words")))
            head = None
    return out[-last:]


def _decisions(desk: Desk) -> Section:
    if not decisions_mod.decisions_path(desk).exists():
        return ("decisions", "== last decisions ==", [_missing("state/decisions.md")])
    rows = last_decisions(desk)
    return ("decisions", "== last decisions (%d) ==" % len(rows), rows or ["nothing"])


# -- the page --------------------------------------------------------------

def sections(desk: Desk) -> List[Section]:
    built = [f(desk) for f in (_waiting, _queue, _counts, _cost, _returns, _decisions)]
    return [(tag, head, [_cut(r) for r in rows]) for tag, head, rows in built]


def _render(page: List[Section], dropped: List[str], cap: int) -> str:
    out: List[str] = []
    for _tag, head, rows in page:
        out.append(head)
        out.extend(rows)
    if dropped:
        out.append("(over the %d-byte cap; dropped: %s)" % (cap, ", ".join(dropped)))
    return "\n".join(out) + "\n"


def brief_text(desk: Desk, cap: Optional[int] = None) -> str:
    if cap is None:
        cap = state_mod.cfg_int(desk, "brief_bytes", DEFAULT_BRIEF_BYTES)
    page = sections(desk)
    dropped: List[str] = []
    while True:
        text = _render(page, dropped, cap)
        if len(text.encode("utf-8")) <= cap:
            return text
        for tag in TRIM_ORDER:
            if any(s[0] == tag for s in page):
                page = [s for s in page if s[0] != tag]
                dropped.append(tag)
                break
        else:
            # Everything trimmable is gone and the page is still over. Print it as
            # it is: a page that is over by a hundred bytes is a reading; a page
            # that dropped what the principal has to do is not.
            return text


def send(desk: Desk, text: str) -> Tuple[bool, str]:
    """Pipe the page to `alert_cmd`. Returns (sent, why).

    The marker is written only after a zero exit. A brief that went out and a brief
    whose alert command was missing look the same in the word "sent", and the
    marker is the only thing that can tell them apart tomorrow.
    """
    cmd = (desk.cfg.get("alert_cmd") or "").strip()
    if not cmd:
        return False, "no alert_cmd configured: printed only, nothing recorded"
    try:
        proc = subprocess.run(cmd, shell=True, input=text, text=True)
    except OSError as exc:
        return False, "alert_cmd could not be run (%s); nothing recorded" % exc
    if proc.returncode != 0:
        return False, "alert_cmd exited %d; nothing recorded" % proc.returncode
    write(desk.path(SENT_MARKER), "%s %d bytes\n" % (ts(), len(text.encode("utf-8"))))
    return True, "sent through alert_cmd, recorded in %s" % SENT_MARKER


def brief_times(desk: Desk) -> List[str]:
    """The times of day the skill sends the page unasked."""
    raw = desk.cfg.get("brief_times") or []
    return [str(t).strip() for t in raw if str(t).strip()]
