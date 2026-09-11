"""The hand-window gate: the coordinator does not edit a lane repository unnoticed.

The coordinator's job is to dispatch. On a production desk it also, quietly,
edited the product repositories itself - a `sed` here, a one-line fix there -
and none of it reached any receipt, ledger entry or diff review. **An edit that
went through a worker and an edit the coordinator made by hand look the same in
the repository afterwards**; the only place they differ is in whether anyone
recorded it. So it was never recorded.

Reminding did not work. A hook that printed a warning on every hand edit fired
hundreds of times and changed nothing. What worked was blocking once: the
first write into a lane repository is refused, with the command to open a window
in the refusal. Declaring the intent takes one line; after that the window is
open and writes are allowed and logged.

The shape:

* **A window is a declaration** (`state/.hand-window`): minutes, and a reason.
* **The ledger is the record** (`state/hand-ledger.md`, append-only): what was
  opened, what was actually written, what was blocked. The two are deliberately
  separate - a window that says three edits are coming and a ledger that records
  three edits are not the same claim, and only the second one is evidence.
* **An expired window counts as closed.** A window closed on time and a window
  left open and forgotten look alike in the sentence "there was a window".
* **Reads are never gated.** `Read`, `Grep` and `Glob` cannot change a repository.
* **A broken gate never blocks.** If this module raises, the caller logs the
  failure and lets the tool through: a command stopped by a bug in the gate and a
  command stopped by a real breach look the same from the coordinator's seat, and
  only one of them is worth a stopped session.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .locking import serialized
from .config import Desk
from .util import BellhopError, append, read, write

WINDOW = "state/.hand-window"
LEDGER = "state/hand-ledger.md"

LEDGER_HEADER = (
    "# Hand ledger (append-only; written by `bellhop hand` and the PreToolUse gate, never by hand)\n"
    "(one line per event, all starting `- `:\n"
    "  `- opened: <time> | <N> min | <reason>`\n"
    "  `- closed: <time> | <N> min used | <M> written[ | expired]`\n"
    "  `- written: <time> | <tool> | <path> | <command head>`\n"
    "  `- blocked: <time> | <tool> | <path> | <command head>`\n"
    "A window is the declaration; this file is the record. They are separate on purpose.)\n"
)

# Tools that can change a repository. `Read`, `Grep` and `Glob` are not here and
# are never gated, even though `Read` also carries a `file_path`.
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash")
READ_ONLY = ("Read", "Grep", "Glob", "NotebookRead", "WebFetch", "WebSearch", "TodoWrite", "Task")

COMMAND_HEAD_CHARS = 60
_CD = re.compile(r"(?:^|[;&|]\s*)cd\s+(?P<dir>(?:\"[^\"]*\"|'[^']*'|[^\s;&|]+))")
_ABS = re.compile(r"(?<![\w~])(/[A-Za-z0-9._~+@/\-]*)")
_HOME = re.compile(r"(?:^|(?<=[\s:=\"'(]))(?:~|\$HOME|\$\{HOME\})(?=/|\s|$)")


# -- paths -----------------------------------------------------------------
def window_path(desk: Desk) -> Path:
    return desk.path(WINDOW)


def ledger_path(desk: Desk) -> Path:
    return desk.path(LEDGER)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_ledger(desk: Desk) -> Path:
    p = ledger_path(desk)
    if not p.exists():
        write(p, LEDGER_HEADER)
    return p


@serialized
def note(desk: Desk, line: str) -> None:
    """Append one row. Append-only: this file is never rewritten."""
    append(_ensure_ledger(desk), "- " + line + "\n")


def rows(desk: Desk) -> List[str]:
    p = ledger_path(desk)
    return [l for l in read(p).splitlines() if l.startswith("- ")] if p.exists() else []


def note_failure(desk: Desk, exc: BaseException) -> None:
    """A gate that broke leaves a row. A missed hit and a hit that never happened
    look the same in an empty ledger."""
    note(desk, "gate-failed: %s | %s" % (_now(), type(exc).__name__))


# -- the window ------------------------------------------------------------
def window(desk: Desk) -> Dict[str, object]:
    """`state` is one of none / open / expired.

    An unreadable window file counts as **open**: refusing to read a file is not
    evidence of a breach, and a gate that blocks on its own confusion is worse
    than a gate that lets one edit through.
    """
    p = window_path(desk)
    if not p.exists():
        return {"state": "none"}
    data: Dict[str, str] = {}
    try:
        for line in read(p).splitlines():
            key, sep, value = line.partition(": ")
            if sep:
                data[key.strip()] = value.strip()
        opened_epoch = float(data["opened_epoch"])
        minutes = int(data.get("minutes", "30"))
    except (OSError, KeyError, ValueError):
        return {"state": "open", "reason": "(window file unreadable; counted as open)"}
    expires = opened_epoch + minutes * 60
    return {
        "state": "expired" if time.time() > expires else "open",
        "opened": data.get("opened", ""),
        "opened_epoch": opened_epoch,
        "minutes": minutes,
        "reason": data.get("reason", ""),
        "ledger_lines": int(data.get("ledger_lines", "0") or 0),
        "remaining_s": max(0, int(expires - time.time())),
    }


@serialized
def open_window(desk: Desk, minutes: Optional[int] = None, reason: str = "") -> str:
    reason = (reason or "").strip()
    if not reason:
        raise BellhopError('a window needs a reason: bellhop hand open --minutes 30 --reason "..."')
    if minutes is None:
        minutes = desk.cfg.get("hand_window_minutes", 30)
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise BellhopError("--minutes takes a whole number of minutes")
    if minutes <= 0:
        raise BellhopError("--minutes must be positive")
    current = window(desk)
    closed_note = ""
    if current["state"] == "open":
        raise BellhopError(
            "a hand window is already open (%s, %ss left); close it first with `bellhop hand close`"
            % (current.get("reason") or "no reason recorded", current.get("remaining_s"))
        )
    if current["state"] == "expired":
        closed_note = close_window(desk) + "\n"
    _ensure_ledger(desk)
    started = len(rows(desk))
    now, epoch = _now(), time.time()
    write(
        window_path(desk),
        "opened: %s\nopened_epoch: %d\nminutes: %d\nreason: %s\nledger_lines: %d\n"
        % (now, epoch, minutes, reason, started),
    )
    note(desk, "opened: %s | %d min | %s" % (now, minutes, reason))
    return closed_note + "hand window open for %d minutes: %s" % (minutes, reason)


@serialized
def close_window(desk: Desk) -> str:
    """No window is not an error. It is also not a reason to write a closing row."""
    p = window_path(desk)
    current = window(desk)
    if current["state"] == "none":
        return "no hand window open; nothing closed and nothing written to the ledger"
    _ensure_ledger(desk)
    started = int(current.get("ledger_lines") or 0)
    written = len([l for l in rows(desk)[started:] if l.startswith("- written:")])
    epoch = current.get("opened_epoch")
    used = max(0, int((time.time() - float(epoch)) // 60)) if epoch else 0
    expired = current["state"] == "expired"
    note(desk, "closed: %s | %d min used | %d written%s" % (_now(), used, written, " | expired" if expired else ""))
    try:
        p.unlink()
    except OSError as exc:
        raise BellhopError("could not remove %s: %s" % (p, exc))
    return "hand window closed: %d min used, %d write(s) recorded%s" % (used, written, " (it had expired)" if expired else "")


def status_text(desk: Desk) -> str:
    current = window(desk)
    if current["state"] == "none":
        return "hand window: none open. Lane repositories are gated; the first write is refused.\n"
    lines = ["hand window: %s" % current["state"]]
    if current.get("reason"):
        lines.append("  reason: %s" % current["reason"])
    if current.get("opened"):
        lines.append("  opened: %s (%s min)" % (current["opened"], current.get("minutes")))
    if current["state"] == "open":
        lines.append("  %ss left" % current.get("remaining_s"))
    else:
        lines.append("  expired, so it counts as closed; run `bellhop hand close` to record it")
    return "\n".join(lines) + "\n"


# -- what counts as a lane repository --------------------------------------
def lane_roots(desk: Desk) -> List[Tuple[str, str]]:
    """(lane name, real path) for every lane repository and lane worktree.

    A worktree is part of the repository it came from: a hand edit in
    `.worktrees/api` reaches the product through `bellhop merge` exactly like one
    made in the repository itself.
    """
    out: List[Tuple[str, str]] = []
    for name in desk.lane_names():
        try:
            lane = desk.lane(name)
        except BellhopError:
            continue
        for key in ("repo", "worktree"):
            value = lane.get(key) or ""
            if value:
                out.append((name, os.path.realpath(value)))
    return out


def _expand(text: str) -> str:
    return _HOME.sub(os.path.expanduser("~"), text or "")


def _unquote(word: str) -> str:
    if len(word) >= 2 and word[0] == word[-1] and word[0] in "\"'":
        return word[1:-1]
    return word


def _resolve(candidate: str, cwd: str) -> str:
    candidate = _expand(_unquote(candidate))
    if not candidate:
        return ""
    if not os.path.isabs(candidate):
        candidate = os.path.join(cwd or os.getcwd(), candidate)
    return os.path.realpath(candidate)


def targets(tool: str, tool_input: dict, cwd: str) -> List[str]:
    """Every path this call might write to, resolved to a real absolute path."""
    tool_input = tool_input or {}
    cwd = _expand(cwd or "")
    if tool != "Bash":
        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return [_resolve(raw, cwd)] if raw else []
    command = _expand(" ".join((tool_input.get("command") or "").split()))
    if not command:
        return []
    found: List[str] = []
    for m in _CD.finditer(command):
        found.append(_resolve(m.group("dir"), cwd))
    for m in _ABS.finditer(command):
        found.append(os.path.realpath(m.group(1)))
    return [f for f in found if f]


def _inside(path: str, root: str) -> bool:
    return bool(path) and (path == root or path.startswith(root.rstrip(os.sep) + os.sep))


def _allowed_command(desk: Desk, command: str) -> bool:
    """Commands with a record of their own. `bellhop merge` writes to a lane by design."""
    words = command.split()
    if not words:
        return True
    first = os.path.basename(words[0])
    return first in tuple(desk.cfg.get("hand_allow_commands") or ())


def _short(desk: Desk, path: str) -> str:
    root = os.path.realpath(str(desk.root))
    return path[len(root) + 1:] if _inside(path, root) and path != root else path


# -- the gate --------------------------------------------------------------
def gate(desk: Desk, data: dict) -> Tuple[int, str]:
    """PreToolUse. Returns (exit code, message): 2 blocks the call, 0 lets it through."""
    tool = (data or {}).get("tool_name") or ""
    if tool in READ_ONLY or tool not in WRITE_TOOLS:
        return 0, ""
    if not desk.cfg.get("hand_gate", True):
        return 0, ""
    tool_input = (data or {}).get("tool_input") or {}
    cwd = (data or {}).get("cwd") or ""
    command = " ".join((tool_input.get("command") or "").split()) if tool == "Bash" else ""
    if tool == "Bash" and _allowed_command(desk, command):
        return 0, ""
    roots = lane_roots(desk)
    if not roots:
        return 0, ""
    hit = None
    for path in targets(tool, tool_input, cwd):
        for lane, root in roots:
            if _inside(path, root):
                hit = (lane, path)
                break
        if hit:
            break
    if not hit:
        return 0, ""
    lane, path = hit
    head = command[:COMMAND_HEAD_CHARS] + "…" if len(command) > COMMAND_HEAD_CHARS else (command or "-")
    current = window(desk)
    if current["state"] == "open":
        note(desk, "written: %s | %s | %s | %s" % (_now(), tool, _short(desk, path), head))
        return 0, ""
    expired = current["state"] == "expired"
    note(desk, "blocked: %s | %s | %s | %s%s" % (_now(), tool, _short(desk, path), head, " | expired" if expired else ""))
    return 2, (
        "bellhop hand gate: %s would write into lane '%s' (%s)%s.\n"
        'Open a window first, then repeat the call: bellhop hand open --minutes 30 --reason "why this cannot go to a worker"\n'
        "Recorded as `blocked` in %s. Dispatching a packet instead needs no window."
        % (tool, lane, path, " and the window has expired" if expired else " with no hand window open", LEDGER)
    )
