"""Claude Code hook entry points. They print nothing unless the current directory is a desk.

Four hooks, and one lesson behind all of them: **a hook that only reminds does not
change behaviour**. A warning printed on every hand edit fired hundreds of times over a few
days on a production desk and the habit was exactly the same at the end of it. A
hook that blocks once, with the way out written into the refusal, changed it in a
single turn. Only `pre-tool` blocks; the other three carry facts, not advice.

| Event | Entry | What it does |
|---|---|---|
| `SessionStart` (startup, clear, compact) | `session-start` | Prints the reading list, then the board. After `/clear` the terminal's own reading list goes with the context; this one is injected into the new one. |
| `UserPromptSubmit` | `intake` | Appends the principal's words to `state/intake.md`, numbered and de-duplicated. |
| `PreToolUse` (Edit, Write, Bash) | `pre-tool` | Refuses the first write into a lane repository when no hand window is open. |
| `Stop` | `stop` | Warns about bells nobody is answering. |

`session-start`, `intake` and `stop` never exit non-zero. A hook that fails a
session start is worse than a hook that prints nothing at all.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from . import board as board_mod
from . import daemon as daemon_mod
from . import hand as hand_mod
from . import intake as intake_mod
from .config import Desk, find_desk
from .util import BellhopError

# Named in the order they are read. The board below the sentence is that read,
# already done - which is the whole point: the coordinator does not open them again.
READING_LIST = (
    "Reading list, in order: state/next.md, state/in-flight.md, state/own-list.md, "
    "state/debts.md, state/closings.md, state/intake.md, then today's ledger only if a "
    "number below is disputed. The board that follows IS that read, quoted from those "
    "files and byte-capped. Take it as the state of the desk and do not open those files "
    "again this turn; write to them only through `bellhop state`."
)


def _hook_input() -> dict:
    """The hook's JSON on stdin. Read once: stdin cannot be read twice."""
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw) or {}
    except Exception:
        return {}


def _desk_from(data: dict) -> Optional[Desk]:
    cwd = (data or {}).get("cwd")
    try:
        return find_desk(Path(cwd) if cwd else None)
    except BellhopError:
        return None


def _pending(desk: Desk) -> int:
    try:
        return len(list(desk.bells_pending.glob("*.bell")))
    except OSError:
        return 0


def session_start() -> int:
    desk = _desk_from(_hook_input())
    if not desk:
        return 0
    out = [f"Bellhop desk: {desk.root}", "", READING_LIST, ""]
    try:
        out.append(board_mod.board_text(desk).rstrip())
    except Exception as exc:
        out.append(f"(the board did not build: {type(exc).__name__}; read the files above yourself)")
    pending = _pending(desk)
    hb = daemon_mod.heartbeat_age(desk)
    if pending and (hb is None or hb > 120):
        out.append(
            f"\nNOTE: {pending} pending bell(s) and no fresh watchdog heartbeat. The session watchdog "
            "starts with the bellhop MCP server; for always-on, run /bellhop:daemon."
        )
    print("\n".join(out))
    return 0


def intake() -> int:
    """UserPromptSubmit: the principal's words, logged by the script that sees them."""
    data = _hook_input()
    desk = _desk_from(data)
    if not desk:
        return 0
    try:
        kind, info = intake_mod.log(desk, data.get("prompt") or "")
    except Exception:
        return 0  # a prompt that failed to be logged is a gap; a failed prompt is a stopped turn
    if kind == intake_mod.LOGGED:
        print(f"bellhop: logged as intake {info} (cite this number; do not write the file by hand)")
    elif kind == intake_mod.DUPLICATE:
        print(f"(duplicate of {info}, not logged)")
    return 0


def pre_tool() -> int:
    """PreToolUse: the hand-window gate. Exit 2 refuses the call."""
    data = _hook_input()
    desk = _desk_from(data)
    if not desk:
        return 0
    try:
        code, message = hand_mod.gate(desk, data)
    except Exception as exc:
        try:
            hand_mod.note_failure(desk, exc)
        except Exception:
            pass
        return 0  # a broken gate never blocks
    if message:
        print(message, file=sys.stderr)
    return code


def stop() -> int:
    desk = _desk_from(_hook_input())
    if not desk:
        return 0
    pending = _pending(desk)
    hb = daemon_mod.heartbeat_age(desk)
    if pending and (hb is None or hb > 120):
        print(f"bellhop: {pending} pending bell(s) but no watchdog heartbeat in the last 2 minutes. Do not wait in a loop; start the resident service (/bellhop:daemon) or keep the MCP session watchdog running.")
    return 0


EVENTS = {
    "session-start": session_start,
    "intake": intake,
    "pre-tool": pre_tool,
    "stop": stop,
}


def main(event: str) -> int:
    fn = EVENTS.get(event)
    if fn is None:
        print(f"bellhop hook: unknown event {event} (known: {', '.join(sorted(EVENTS))})", file=sys.stderr)
        return 1
    return fn()
