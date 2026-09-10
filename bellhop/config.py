from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .util import BellhopError, read_json

CONFIG_NAME = "bellhop.json"

# Everything the desk archives lives under one of these names. `git add -A` would also
# sweep up a scratch file left at the desk root, a note someone was still writing, or a
# lane worktree; the roster says once, in one place, which files are the desk's own.
# Anything outside it never reaches a commit, so keep new desk directories out of it
# unless they are meant to be archived.
ARCHIVE_ROSTER = (
    "inbox",
    "bells",
    "receipts",
    "ledger",
    "state",
    "reports",
    "seals",
    "rules",
    "bellhop.json",
    ".claude",
    # Sealed or tracked from the first commit: a change here that never reaches a commit
    # looks exactly like no change at all, and the seal manifest would disagree with the
    # working tree with nothing in the history to explain it.
    "frozen",
    "templates",
    ".gitignore",
)

DESK_DEFAULTS = {
    "commit_prefix": "[desk]",
    "archive_roster": list(ARCHIVE_ROSTER),
    "push": False,
    "sample_rate": 3,
    "weekly_seed": "",
    "ledger_entry_max_bytes": 1200,
    "acceptance_bytes": 3000,
    "ledger_entry_bytes": 800,
    "inflight_line_chars": 160,
    "closings_line_chars": 200,
    "own_list_warn": 7,
    "hot_rule_promote": 3,
    "hot_rule_max_lines": 10,
    "hot_rule_line_chars": 80,
    "previous_versions_keep": 30,
    "sentinel_interval_minutes": 10,
    "wake_cmd": "",
    "claude_bin": "claude",
    "worktrees_dir": ".worktrees",
    "session_watchdog": True,
    "session_watchdog_interval": 5,
    "lanes": {},
}

LANE_DEFAULTS = {
    "repo": "",
    "branch": "main",
    "provider": "claude",
    "model": "opus",
    "permission_mode": "acceptEdits",
    "max_turns": 80,
    # A worker is told to run the lane's checks and commit on the lane branch. In
    # print mode `acceptEdits` auto-approves file edits only; every other tool call is
    # refused with no one to ask, so a lane without an allowlist produces a worker that
    # can edit files and nothing else.
    #
    # Claude Code matches a `Bash(cmd:*)` rule against the literal start of the command
    # line, so `Bash(git status:*)` does not cover `git -C <path> status` -- and a worker
    # in a worktree writes `-C` all day. `Bash(git:*)` covers git with or without `-C`.
    # It also covers `git push`, on purpose: the push gate is the lane repository's
    # pre-push hook plus the scrubbed environment (no credentials), not this list. An
    # allowlist that pretends to stop a push it cannot see is worse than one that says so.
    # Add the lane's own check commands per lane; do not fall back to a bare `Bash`.
    "allowed_tools": ["Read", "Grep", "Glob", "Edit", "Write", "Bash(git:*)", "Bash(./check.sh:*)"],
    "env_allow": [],
    "worker_cmd": "",
    "accept_cmd": "",
    "check_cmd": "",
    "worktree": "",
}

FRAMEWORK_ROOT = Path(__file__).resolve().parent.parent


class Desk:
    def __init__(self, root: Path, cfg: dict):
        self.root = Path(root).resolve()
        merged = dict(DESK_DEFAULTS)
        merged.update(cfg or {})
        self.cfg = merged

    # -- paths -------------------------------------------------------------
    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    @property
    def inbox(self) -> Path:
        return self.path("inbox")

    @property
    def bells_pending(self) -> Path:
        return self.path("bells", "pending")

    @property
    def bells_rung(self) -> Path:
        return self.path("bells", "rung")

    @property
    def bells_logs(self) -> Path:
        return self.path("bells", "logs")

    @property
    def receipts(self) -> Path:
        return self.path("receipts")

    @property
    def state(self) -> Path:
        return self.path("state")

    @property
    def ledger(self) -> Path:
        return self.path("ledger")

    @property
    def templates(self) -> Path:
        local = self.path("templates")
        return local if local.exists() else FRAMEWORK_ROOT / "template" / "templates"

    def template(self, name: str) -> str:
        p = self.templates / name
        if not p.exists():
            p = FRAMEWORK_ROOT / "template" / "templates" / name
        if not p.exists():
            raise BellhopError(f"template not found: {name}")
        return p.read_text(encoding="utf-8")

    # -- lanes -------------------------------------------------------------
    def lane_names(self) -> list:
        return sorted(self.cfg.get("lanes", {}).keys())

    def lane(self, name: str) -> dict:
        lanes = self.cfg.get("lanes", {})
        if name not in lanes:
            raise BellhopError(f"unknown lane '{name}' (known: {', '.join(self.lane_names()) or 'none'})")
        merged = dict(LANE_DEFAULTS)
        merged.update(lanes[name] or {})
        merged["name"] = name
        if merged["repo"]:
            merged["repo"] = str((self.root / merged["repo"]).resolve()) if not os.path.isabs(merged["repo"]) else merged["repo"]
        if not merged["worktree"]:
            merged["worktree"] = str(self.path(self.cfg["worktrees_dir"], name))
        elif not os.path.isabs(merged["worktree"]):
            merged["worktree"] = str((self.root / merged["worktree"]).resolve())
        return merged


def find_desk(start: Optional[Path] = None) -> Desk:
    env_root = os.environ.get("BELLHOP_DESK")
    candidates = []
    if env_root:
        candidates.append(Path(env_root))
    cur = Path(start or os.getcwd()).resolve()
    candidates.extend([cur, *cur.parents])
    for c in candidates:
        cfg = c / CONFIG_NAME
        if cfg.exists():
            return Desk(c, read_json(cfg, {}))
    raise BellhopError(
        f"no {CONFIG_NAME} found here or above; run `bellhop init <dir>` or set BELLHOP_DESK"
    )
