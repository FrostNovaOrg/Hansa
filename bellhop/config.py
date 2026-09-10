from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .util import BellhopError, read_json

CONFIG_NAME = "bellhop.json"

DESK_DEFAULTS = {
    "commit_prefix": "[desk]",
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
    # `close-task`: a verdict written in shadow mode is a rehearsal, and a rehearsal
    # does not close a task. Turn this on only while an acceptance pipeline is being
    # run beside the real one and the desk has decided to take its word.
    "accept_from_shadow": False,
    # After the full board passes on the merged result, push the lane repository too.
    # Off by default; the desk's own `push` key is what covers the desk repository.
    "close_push_lane": False,
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
    # can edit files and nothing else. Narrow this per lane once the repository's own
    # guardrails (worktree, pre-push hook, scrubbed environment) are in place.
    "allowed_tools": ["Read", "Grep", "Glob", "Edit", "Write", "Bash"],
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
