"""The role gate: a coordinator dispatches, and a desk can tell when it stopped.

A coordinator that writes the code itself is not a coordinator with a shortcut; it
is a lane with no packet, no receipt, no verdict and no diff anybody reads. On one
production desk the coordinator wrote code **often** before anyone
counted, and the count was only possible afterwards because the transcripts were
still there. On another, **a large share of every packet** was the desk repairing its own
tooling. The first desk installed a role guard by hand; self-written code was **zero
the next day**. That is the whole evidence for this module, and it is the reason the
gate blocks rather than reminds (principle 3 in the rationale).

What it refuses, in one sentence: **a source file anywhere, and anything inside the
desk that is not one of the desk's own records.**

* A source file is one whose name matches `role_gate_code_globs`. The desk's own
  `bin/`, `scripts/` and vendored framework are not exempt -- repairing the tooling
  by hand is the largest single class of coordinator-written code on record, and a
  script the coordinator wrote and a script a worker wrote look identical in git.
* Inside the desk, writing is allowed under `role_gate_write_dirs` -- state, rules,
  packets, the ledger, reports and receipts. That is the coordinator's desk work.
* Outside the desk and not a source file -- a scratch packet body in `/tmp`, a note --
  is not this gate's business.

The way forward is named in the refusal and it is the same one the hand gate uses: a
declared window. A write that goes through an open window is logged as `role-open`
and counted into the readings as a **self-implemented write**, because a rule with an
exception nobody counts is a rule that decays into a habit.

Turning it off is one line in `bellhop.json`: `"role_gate": "off"` (or `false`).
"""
from __future__ import annotations

import collections
import fnmatch
import os
from typing import Dict, List, Optional, Tuple

from . import hand as hand_mod
from .config import Desk

# Names that mean "this is code". Matched against the file's name, not its path, so
# a `.py` in a lane, in the desk's `bin/`, or in a vendored copy all read the same.
CODE_GLOBS = (
    "*.py", "*.pyi", "*.js", "*.jsx", "*.ts", "*.tsx", "*.mjs", "*.cjs", "*.go",
    "*.rs", "*.rb", "*.java", "*.kt", "*.kts", "*.swift", "*.c", "*.h", "*.cc",
    "*.cpp", "*.hpp", "*.cs", "*.m", "*.mm", "*.php", "*.scala", "*.lua", "*.pl",
    "*.sh", "*.bash", "*.zsh", "*.fish", "*.sql", "*.tf", "*.gradle", "*.ipynb",
    "Makefile", "*.mk", "Dockerfile", "*.podspec", "*.gemspec",
)

# Where the coordinator's own writing belongs, relative to the desk root.
WRITE_DIRS = ("state", "rules", "inbox", "ledger", "reports", "receipts", "bells")

WAY_FORWARD = "coordinators dispatch; open a packet or `bellhop hand open --reason`"

ROW_REFUSED = "role-refused"
ROW_OPEN = "role-open"


def enabled(desk: Desk) -> bool:
    """`role_gate: off`, `false`, `no` or `0` turns it off; anything else leaves it on."""
    value = desk.cfg.get("role_gate", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("off", "false", "no", "0", "")
    return bool(value)


def code_globs(desk: Desk) -> Tuple[str, ...]:
    raw = desk.cfg.get("role_gate_code_globs")
    if raw is None:
        return CODE_GLOBS
    if isinstance(raw, (list, tuple)):
        return tuple(str(g) for g in raw if str(g).strip())
    return CODE_GLOBS


def write_dirs(desk: Desk) -> Tuple[str, ...]:
    raw = desk.cfg.get("role_gate_write_dirs")
    if isinstance(raw, (list, tuple)) and raw:
        return tuple(str(d).strip("/") for d in raw if str(d).strip())
    return WRITE_DIRS


def is_code(path: str, globs: Tuple[str, ...]) -> bool:
    name = os.path.basename(path or "")
    return any(fnmatch.fnmatch(name, g) for g in globs)


def _under_desk(desk: Desk, path: str) -> Optional[str]:
    """The path relative to the desk root, or None when it is outside the desk."""
    root = os.path.realpath(str(desk.root))
    if path == root:
        return ""
    prefix = root.rstrip(os.sep) + os.sep
    return path[len(prefix):] if path.startswith(prefix) else None


def refusal_for(desk: Desk, path: str) -> Optional[str]:
    """Why this path is not the coordinator's to write, or None if it is."""
    if is_code(path, code_globs(desk)):
        return "a source file"
    rel = _under_desk(desk, path)
    if rel is None:
        return None  # outside the desk, and not code: not this gate's business
    top = rel.split(os.sep, 1)[0]
    if top in write_dirs(desk):
        return None
    return "inside the desk but not under %s" % "/".join(write_dirs(desk))


def check(desk: Desk, data: dict) -> Tuple[int, str]:
    """PreToolUse, after the hand gate. (exit code, message): 2 refuses the call."""
    tool = (data or {}).get("tool_name") or ""
    if tool in hand_mod.READ_ONLY or tool not in hand_mod.WRITE_TOOLS:
        return 0, ""
    if not enabled(desk):
        return 0, ""
    tool_input = (data or {}).get("tool_input") or {}
    cwd = (data or {}).get("cwd") or ""
    command = " ".join((tool_input.get("command") or "").split()) if tool == "Bash" else ""
    if tool == "Bash" and hand_mod._allowed_command(desk, command):
        return 0, ""  # `bellhop` writes what it writes with a record of its own
    hit = None
    for _verb, path in hand_mod.write_targets(tool, tool_input, cwd):
        why = refusal_for(desk, path)
        if why:
            hit = (path, why)
            break
    if hit is None:
        return 0, ""
    path, why = hit
    head = command[:hand_mod.COMMAND_HEAD_CHARS] + "…" if len(command) > hand_mod.COMMAND_HEAD_CHARS else (command or "-")
    short = hand_mod._short(desk, path)
    if hand_mod.window(desk)["state"] == "open":
        hand_mod.note(desk, "%s: %s | %s | %s | %s | %s"
                            % (ROW_OPEN, hand_mod._now(), tool, short, head, why))
        return 0, ""
    hand_mod.note(desk, "%s: %s | %s | %s | %s | %s"
                        % (ROW_REFUSED, hand_mod._now(), tool, short, head, why))
    return 2, (
        "bellhop role gate: %s writes %s (%s).\n"
        "%s.\n"
        "The window is the exception and it is counted: every write through one lands in "
        "the readings as a self-implemented write. `role_gate: off` in bellhop.json turns "
        "this gate off entirely." % (tool, path, why, WAY_FORWARD)
    )


# -- the reading -----------------------------------------------------------

def writes_by_day(desk: Desk) -> Dict[str, int]:
    """`role-open` rows per calendar day: the coordinator's own writes, counted.

    A refusal is not counted. A refused write did not happen, and a gate that put its
    own blocks in the same column as the writes that got through would report the
    habit and the cure as one number.
    """
    out: "collections.Counter[str]" = collections.Counter()
    for row in hand_mod.rows(desk):
        body = row[2:]
        if not body.startswith(ROW_OPEN + ":"):
            continue
        fields = [f.strip() for f in body.split("|")]
        stamp = fields[0].split(":", 1)[1].strip() if ":" in fields[0] else ""
        day = stamp[:10]
        if len(day) == 10:
            out[day] += 1
    return dict(out)


def self_implemented(desk: Desk, days: Optional[List[str]] = None) -> int:
    counted = writes_by_day(desk)
    if days is None:
        return sum(counted.values())
    return sum(counted.get(d, 0) for d in days)
