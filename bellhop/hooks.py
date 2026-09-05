"""Claude Code hook entry points. They print nothing unless the current directory is a desk."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from . import daemon as daemon_mod
from .config import Desk, find_desk
from .util import BellhopError, read


def _desk_from_hook_input() -> Optional[Desk]:
    cwd = None
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        if raw.strip():
            cwd = (json.loads(raw) or {}).get("cwd")
    except Exception:
        cwd = None
    try:
        return find_desk(Path(cwd) if cwd else None)
    except BellhopError:
        return None


def session_start() -> int:
    desk = _desk_from_hook_input()
    if not desk:
        return 0
    out = [f"Bellhop desk: {desk.root}", ""]
    card = desk.state / "next.md"
    if card.exists():
        out.append("== dispatch card ==")
        out.extend(read(card).splitlines()[2:9])
    for name, title in (("in-flight.md", "in flight"), ("debts.md", "debts")):
        p = desk.state / name
        if p.exists():
            out.append(f"\n== {title} ==")
            out.extend([l for l in read(p).splitlines() if l.startswith("- ")][:20])
    status = desk.state / ".sentinel" / "status.txt"
    out.append("\n== sentinel ==")
    out.append(read(status).strip() if status.exists() else "no sentinel status yet")
    out.append("\n== watchdog ==")
    out.append(daemon_mod.status_text(desk).rstrip())
    pending = len(list(desk.bells_pending.glob("*.bell")))
    hb = daemon_mod.heartbeat_age(desk)
    if pending and (hb is None or hb > 120):
        out.append(f"\nNOTE: {pending} pending bell(s) and no fresh watchdog heartbeat. The session watchdog starts with the bellhop MCP server; for always-on, run /bellhop:daemon.")
    dec = desk.state / "decisions.md"
    if dec.exists():
        heads = [l for l in read(dec).splitlines() if l.startswith("## ")][-3:]
        if heads:
            out.append("\n== last decisions ==")
            out.extend(heads)
    print("\n".join(out))
    return 0


def stop() -> int:
    desk = _desk_from_hook_input()
    if not desk:
        return 0
    pending = len(list(desk.bells_pending.glob("*.bell")))
    hb = daemon_mod.heartbeat_age(desk)
    if pending and (hb is None or hb > 120):
        print(f"bellhop: {pending} pending bell(s) but no watchdog heartbeat in the last 2 minutes. Do not wait in a loop; start the resident service (/bellhop:daemon) or keep the MCP session watchdog running.")
    return 0


def main(event: str) -> int:
    if event == "session-start":
        return session_start()
    if event == "stop":
        return stop()
    print(f"bellhop hook: unknown event {event}", file=sys.stderr)
    return 1
