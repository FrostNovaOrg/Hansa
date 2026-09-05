from __future__ import annotations

import re
from pathlib import Path
from typing import Dict

from .config import Desk
from .util import BellhopError, read, ts, write

KEYS = ("ready", "next", "lane", "reason", "queue", "refreshed", "by")
FOOTER_MARK = "\n---\n"


def card_path(desk: Desk) -> Path:
    return desk.state / "next.md"


def read_card(desk: Desk) -> Dict[str, str]:
    p = card_path(desk)
    if not p.exists():
        raise BellhopError("state/next.md is missing")
    data = {}
    for line in read(p).splitlines():
        m = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if m and m.group(1) in KEYS:
            data[m.group(1)] = m.group(2).strip()
    return data


def refresh_card(desk: Desk, **fields) -> Dict[str, str]:
    p = card_path(desk)
    current = read_card(desk) if p.exists() else {}
    text = read(p) if p.exists() else ""
    footer = text[text.index(FOOTER_MARK):] if FOOTER_MARK in text else FOOTER_MARK + "Fixed keys above; the sentinel reads them with `^key: `.\n"
    title = text.splitlines()[0] if text.startswith("# ") else "# Dispatch card"
    for k, v in fields.items():
        if k not in KEYS:
            raise BellhopError(f"unknown card key: {k}")
        if v is not None:
            current[k] = str(v)
    current["refreshed"] = ts()
    current.setdefault("by", "coordinator")
    if current.get("ready", "no").lower() == "no" and not current.get("reason"):
        raise BellhopError("a card with ready: no must say why in `reason`")
    if current.get("ready", "no").lower() == "yes" and not current.get("next"):
        raise BellhopError("a card with ready: yes must name `next`")
    body = "\n".join(f"{k}: {current.get(k, '')}" for k in KEYS)
    write(p, f"{title}\n\n{body}\n{footer}")
    return current
