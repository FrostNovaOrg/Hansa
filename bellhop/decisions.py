from __future__ import annotations

from pathlib import Path

from .config import Desk
from .util import BellhopError, append, read, ts

HEADER = "# Decisions (append-only; verbatim; never edit past entries)\n"


def decisions_path(desk: Desk) -> Path:
    return desk.state / "decisions.md"


def decide(desk: Desk, words: str, who: str = "principal", scope: str = "", expires: str = "until revoked",
           standing: bool = False, recorded_by: str = "coordinator") -> str:
    if not words.strip():
        raise BellhopError("a decision needs the decider's words, verbatim")
    p = decisions_path(desk)
    if not p.exists():
        append(p, HEADER)
    kind = "standing" if standing else "decision"
    block = (
        f"\n## {ts()} · {kind} · {who}\n"
        f"- words: \"{words.strip()}\"\n"
        f"- scope: {scope.strip() or 'as stated'}\n"
        f"- expires: {expires.strip() or 'until revoked'}\n"
        f"- recorded by: {recorded_by}\n"
    )
    append(p, block)
    return block


def standing_authorisations(desk: Desk) -> list:
    p = decisions_path(desk)
    if not p.exists():
        return []
    out = []
    current = None
    for line in read(p).splitlines():
        if line.startswith("## ") and "· standing ·" in line:
            current = [line]
        elif current is not None and line.startswith("- "):
            current.append(line)
        elif current is not None and not line.strip():
            out.append("\n".join(current))
            current = None
    if current:
        out.append("\n".join(current))
    return out
