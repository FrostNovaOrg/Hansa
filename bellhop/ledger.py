from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional, Tuple

from .locking import serialized
from .config import Desk
from .util import BellhopError, slugify, today, ts, write


def next_seq(desk: Desk, day: Optional[str] = None) -> int:
    """One past the highest entry number in that day's folder.

    Both `bellhop ledger add` and `bellhop state ledger add` take the number from
    here, so the entry number is never accepted from the caller and never reused.
    """
    folder = desk.ledger / (day or today())
    highest = 0
    if folder.exists():
        for p in folder.glob("*.md"):
            m = re.match(r"^(\d+)-", p.name)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


@serialized
def add(desk: Desk, title: str, body: str, refs: Iterable[str] = (), day: Optional[str] = None) -> Path:
    if not title.strip():
        raise BellhopError("ledger entry needs a title")
    day = day or today()
    folder = desk.ledger / day
    folder.mkdir(parents=True, exist_ok=True)
    seq = next_seq(desk, day)
    path = folder / f"{seq:03d}-{slugify(title)}.md"
    while path.exists():
        seq += 1
        path = folder / f"{seq:03d}-{slugify(title)}.md"
    refs = [r for r in refs if r]
    text = f"# {title}\n\n- time: {ts()}\n- refs: {', '.join(refs) if refs else 'none'}\n\n{body.strip()}\n"
    limit = int(desk.cfg["ledger_entry_max_bytes"])
    size = len(text.encode("utf-8"))
    write(path, text)
    if size > limit:
        print(f"note: ledger entry is {size} bytes, above the {limit}-byte guideline; move data to the receipt")
    return path


def stats(desk: Desk) -> Tuple[int, int]:
    count = 0
    size = 0
    if desk.ledger.exists():
        for p in desk.ledger.rglob("*.md"):
            count += 1
            size += p.stat().st_size
    return count, size


def entries_mentioning(desk: Desk, token: str) -> list:
    hits = []
    if not desk.ledger.exists():
        return hits
    for p in sorted(desk.ledger.rglob("*.md")):
        if token in p.read_text(encoding="utf-8"):
            hits.append(p)
    return hits
