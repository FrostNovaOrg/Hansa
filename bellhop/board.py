"""The board: one page, read once, never read back.

A production coordinator opened its state files about ninety times a day, and a
good share of those reads were of a file it had written moments earlier. Every
one of them cost context and none of them told it anything new.

The board is the reply. One command prints everything a turn starts from - the
card, what is in flight, the coordinator's own list, what is owed, close-outs it
has not read, the principal's last messages, the hot rules, the sentinel line,
pending bells and the watchdog's heartbeat - under a byte cap, quoted from the
files. After reading it the coordinator does not open those files again.

Two rules make the page trustworthy enough to replace the reads:

1. **Quote, do not summarise.** Every row here is a line as it stands in the
   file. A summary and the file disagree exactly on the day it matters.
2. **A file that is missing and a file that is empty must not look alike.** A
   missing file says so in its own row; an empty one says `none`. On a page whose
   whole purpose is to be believed without checking, those two reading the same
   would be the worst possible bug.

Over the cap the page is trimmed in a fixed order - oldest debts, then intake
titles, then close-out lines - and never the card or in flight, which are the
reason the page exists. Rows are cut on character boundaries, so a trimmed row is
still readable text. The last line then says how many bytes went, because a page
that silently dropped rows and a page that had nothing to drop look the same
otherwise.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from . import daemon as daemon_mod
from . import intake as intake_mod
from . import state as state_mod
from .config import Desk
from .util import read

DEFAULT_BOARD_BYTES = 2500

# Row kinds. `H` is a heading and `S` a single fact line; neither is ever trimmed.
# The number is how many characters a row of that kind is worth.
ROW_CHARS = {"C": 110, "F": 110, "O": 110, "B": 110, "D": 110, "L": 110, "M": 110, "R": 90, "S": 200}

# The order rows are dropped in when the page is over the cap: oldest debts
# first, then intake titles, then close-out lines. The card and in flight carry
# no trimmable tag at all, so they cannot be reached from here.
TRIM_ORDER = ("D", "M", "L")

DEBT_BASELINE = "BASELINE"
Row = Tuple[str, str]


def _cut(text: str, chars: int) -> str:
    """Cut on a character boundary, never mid-codepoint."""
    return text if len(text) <= chars else text[:chars] + "…"


def _data_rows(path: Path) -> Optional[List[str]]:
    """The `- ` rows of a desk list file, or None when the file is not on disk."""
    if not path.exists():
        return None
    return [l for l in read(path).splitlines() if l.startswith("- ")]


def _open_rows(rows: Optional[List[str]]) -> List[str]:
    return [l for l in (rows or []) if l.strip() != state_mod.PLACEHOLDER]


def _missing(rel: str) -> str:
    return "(%s is not on disk)" % rel


def _card(desk: Desk, out: List[Row]) -> None:
    out.append(("H", "== card (state/next.md) =="))
    if not desk.path("state/next.md").exists():
        out.append(("S", _missing("state/next.md")))
        return
    try:
        keys = state_mod.read_card_keys(desk)
    except Exception as exc:  # a broken card must not take the whole page with it
        out.append(("S", "(card unreadable: %s)" % type(exc).__name__))
        return
    for key in state_mod.CARD_KEYS:
        out.append(("C", "%s: %s" % (key, keys.get(key, "(no such key on the card)"))))


def _inflight(desk: Desk, out: List[Row]) -> None:
    rows = _data_rows(desk.path("state/in-flight.md"))
    open_rows = _open_rows(rows)
    out.append(("H", "== in flight (%d) ==" % len(open_rows)))
    if rows is None:
        out.append(("S", _missing("state/in-flight.md")))
    elif not open_rows:
        out.append(("S", "none"))
    else:
        out.extend(("F", l) for l in open_rows)


def _own(desk: Desk, out: List[Row]) -> None:
    rows = _data_rows(desk.path("state/own-list.md"))
    open_rows = _open_rows(rows)
    warn = state_mod.cfg_int(desk, "own_list_warn", 7)
    over = " OVER THE %d-LINE CAP: stop and clean up" % warn if len(open_rows) > warn else ""
    out.append(("H", "== own list (%d)%s ==" % (len(open_rows), over)))
    if rows is None:
        out.append(("S", _missing("state/own-list.md")))
    elif not open_rows:
        out.append(("S", "none"))
    else:
        out.extend(("O", l) for l in open_rows)


def _debts(desk: Desk, out: List[Row]) -> None:
    rows = _data_rows(desk.path("state/debts.md"))
    open_rows = _open_rows(rows)
    out.append(("H", "== debts (%d) ==" % len(open_rows)))
    if rows is None:
        out.append(("S", _missing("state/debts.md")))
        return
    if not open_rows:
        out.append(("S", "none"))
        return
    baseline = [l for l in open_rows if DEBT_BASELINE in l]
    rest = [l for l in open_rows if DEBT_BASELINE not in l]
    out.extend(("B", l) for l in baseline)
    out.extend(("D", l) for l in rest[-5:])


def _closings(desk: Desk, out: List[Row]) -> None:
    try:
        rows = state_mod.closings_rows(desk)
        unread = max(len(rows) - state_mod.closings_pointer(desk), 0)
    except Exception as exc:
        out.append(("H", "== close-outs =="))
        out.append(("S", "(unreadable: %s)" % type(exc).__name__))
        return
    out.append(("H", "== close-outs (%d unread of %d) ==" % (unread, len(rows))))
    if not rows:
        out.append(("S", "none"))
        return
    out.extend(("L", l) for l in rows[-3:])
    if unread:
        out.append(("S", "read them with `bellhop state closings unread`"))


def _intake(desk: Desk, out: List[Row]) -> None:
    try:
        titles = intake_mod.titles(desk, 3)
        total = len(intake_mod.entries(desk))
    except Exception as exc:
        out.append(("H", "== intake =="))
        out.append(("S", "(unreadable: %s)" % type(exc).__name__))
        return
    out.append(("H", "== intake (last 3 of %d) ==" % total))
    if not titles:
        out.append(("S", "none" if intake_mod.path(desk).exists() else _missing(intake_mod.INTAKE)))
        return
    out.extend(("M", t) for t in titles)


def _hot(desk: Desk, out: List[Row]) -> None:
    try:
        lines = state_mod.hot_rule_lines(desk)
    except Exception:
        lines = []
    out.append(("H", "== hot rules (%d) ==" % len(lines)))
    if not lines:
        out.append(("S", "none"))
    else:
        out.extend(("R", l) for l in lines)


def _sentinel(desk: Desk, out: List[Row]) -> None:
    out.append(("H", "== sentinel =="))
    p = desk.state / ".sentinel" / "status.txt"
    if not p.exists():
        out.append(("S", "no sentinel status yet (run `bellhop sentinel`)"))
        return
    try:
        first = read(p).strip().splitlines()[0]
    except (OSError, IndexError):
        first = "(status file unreadable)"
    out.append(("S", first))


def _bells_and_watchdog(desk: Desk, out: List[Row]) -> None:
    try:
        pending = len(list(desk.bells_pending.glob("*.bell")))
    except OSError:
        pending = 0
    out.append(("H", "== bells =="))
    out.append(("S", "%d pending" % pending))
    out.append(("H", "== watchdog =="))
    try:
        age = daemon_mod.heartbeat_age(desk)
    except Exception:
        age = None
    if age is None:
        out.append(("S", "no heartbeat recorded"))
    else:
        out.append(("S", "heartbeat %ds ago%s" % (round(age), " (fresh)" if age < 120 else " (stale)")))
    if pending and (age is None or age >= 120):
        out.append(("S", "NOTE: bells are waiting and nothing is answering them; start /bellhop:daemon"))


def rows(desk: Desk) -> List[Row]:
    """The whole page as tagged rows, each already cut to its row length."""
    out: List[Row] = []
    for section in (_card, _inflight, _own, _debts, _closings, _intake, _hot, _sentinel, _bells_and_watchdog):
        section(desk, out)
    return [(tag, _cut(text, ROW_CHARS[tag]) if tag in ROW_CHARS else text) for tag, text in out]


def _render(page: List[Row], trimmed: int) -> str:
    body = "\n".join(text for _tag, text in page)
    if trimmed:
        body += "\n(trimmed %d bytes)" % trimmed
    return body + "\n"


def board_text(desk: Desk, cap: Optional[int] = None) -> str:
    if cap is None:
        cap = state_mod.cfg_int(desk, "board_bytes", DEFAULT_BOARD_BYTES)
    page = rows(desk)
    full = len("\n".join(t for _s, t in page).encode("utf-8"))
    while True:
        trimmed = full - len("\n".join(t for _s, t in page).encode("utf-8"))
        text = _render(page, trimmed)
        if len(text.encode("utf-8")) <= cap:
            return text
        for tag in TRIM_ORDER:
            hits = [i for i, (s, _t) in enumerate(page) if s == tag]
            if hits:
                del page[hits[0]]  # oldest first: the rows are in file order
                break
        else:
            # Everything trimmable is gone and the page is still over the cap.
            # Print it as it is rather than pretend it passed: a page that is
            # over by a hundred bytes is a reading, a page that lies is not.
            return text
