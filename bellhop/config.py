from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .transcripts import PRICE_DEFAULTS
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

# Commands a task packet must not ask a worker to run. A worker told to cut a release,
# deploy, or push is doing the coordinator's job with none of the coordinator's gates --
# and a packet that slipped one in reads exactly like one that did not. Matched
# case-insensitively as regular expressions against each line of the packet body.
# `bellhop dispatch --allow <pattern>` waives one of these for one packet and stores
# nothing, so the next packet is gated again.
PACKET_FORBIDDEN_PATTERNS = (
    r"build\.sh",
    r"release\.sh",
    r"deploy\.sh",
    r"publish\.sh",
    r"git push",
    r"npm publish",
    r"docker push",
)

DESK_DEFAULTS = {
    "support": {"enabled": False},
    "commit_prefix": "[desk]",
    "archive_roster": list(ARCHIVE_ROSTER),
    "push": False,
    "sample_rate": 3,
    "weekly_seed": "",
    "ledger_entry_max_bytes": 1200,
    "acceptance_bytes": 3000,
    "packet_bytes": 3000,
    "packet_budget_minutes": 45,
    "packet_forbidden_patterns": list(PACKET_FORBIDDEN_PATTERNS),
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
    # -- acceptance: the case pack, the session, the shadow period ---------
    "casepack_bytes": 6000,          # hard cap on the pack; over it, trim, never stop
    "casepack_receipt_lines": 50,    # how much of the receipt body the pack carries
    "accept_timeout_s": 900,         # the verifier's wall clock, then killpg
    "accept_max_turns": 30,
    # Claude Code verifier settings, independent of worker providers/schedules.
    "accept_model": "opus",
    "accept_effort": "",
    "shadow_agreement": 0.9,         # switch criteria, read by `bellhop accept shadow-report`
    "shadow_min_samples": 20,
    "shadow_stall_rate": 0.1,
    "shadow_median_s": 480,
    "sentinel_interval_minutes": 10,
    "wake_cmd": "",
    # How many times the wake command may fire against an unchanged board before the
    # sentinel stops waking and asks for a person. A wake that repeats forever against a
    # stuck coordinator and a wake that is working look the same in the log.
    "wake_max_repeats": 3,
    # Exit 0 = busy, exactly 1 = idle; every other exit is a failed probe (busy).
    # Unset means "unknown": the wake still fires, but the daily clear and the idle
    # compaction refuse to act, because clearing blind is what loses a half-finished task.
    "busy_probe_cmd": "",
    # "HH:MM-HH:MM" local time, empty = off. Inside it the sentinel tries on every run and
    # clears on the first idle one; one exact minute is missed whenever a run slips.
    "daily_clear_window": "",
    "clear_cmd": "",
    # Compaction stays off until someone has read what it costs; see docs/sentinel.md.
    "idle_compact": False,
    "compact_cmd": "",
    "compact_threshold_tokens": 250000,
    "coordinator_transcript": "",
    # Resident services do not come up at boot or login unless this is true; after a
    # reboot the operator starts them by hand.
    "autostart": False,
    "claude_bin": "claude",
    # How a lane's `sandbox_profile` is applied. `{profile}` is replaced with the
    # profile's absolute path and the result is prepended to the worker command, so on
    # macOS this is "sandbox-exec -f {profile}"; another platform substitutes its own
    # (`bwrap --bind ...`, `firejail --profile={profile}`). Empty and a lane that names
    # a profile anyway is a refusal, not a launch: a worker that ran unsandboxed because
    # the wrapper was blank leaves the same receipt as one that ran sandboxed.
    "sandbox_wrapper": "",
    "worktrees_dir": ".worktrees",
    "session_watchdog": True,
    "session_watchdog_interval": 5,
    # Start at one. The lanes can run in parallel and all the machinery for it is
    # here; raise this once two workers landing at the same time is something this
    # desk has watched happen, not something it meets on its first day.
    "max_parallel_workers": 1,
    # How long a writer waits for the desk lock before it gives up and names the
    # holder. Raise it on a desk whose full board runs for minutes inside the lock.
    "lock_timeout_seconds": 30,
    # Consecutive failed attempts on one bell before the watchdog stops answering it
    # and parks it under `bells/dead/`. A bell retried forever is a loop, not a retry.
    "bell_max_failures": 3,
    # Readings. `context_tail_tokens` is the line above which a turn is a long-context
    # turn: such turns cost more than the rest, because each one re-sends the context.
    # `prices` is merged over PRICE_DEFAULTS, key by key, so a
    # desk can price one model without restating the table; an unpriced model reads
    # `n/a` and is never guessed at.
    "context_tail_tokens": 250000,
    "prices": {},
    "maintenance_lanes": ["desk"],
    "desk_dirs": [
        "state", "rules", "receipts", "ledger", "inbox", "bells", "reports", "seals",
        "frozen", "templates", "scripts", "bellhop", "bin", "hooks", "skills", "agents",
        "docs", "template", ".claude", ".bellhop",
    ],
    "status_question_patterns": [
        "what is waiting", "what's waiting", "status", "queue", "remaining", "done yet",
    ],
    # -- the one page ------------------------------------------------------
    "board_bytes": 2500,
    # -- the principal's page ----------------------------------------------
    # `bellhop brief` renders the same files the board does, ordered by what the
    # principal can act on, under this cap. `alert_cmd` reads the page on stdin;
    # empty means print only, and then nothing is recorded as sent.
    "brief_bytes": 1500,
    "brief_times": [],
    "alert_cmd": "",
    # -- releases ----------------------------------------------------------
    # An ordered list of {"name": ..., "cmd": ...}; `{version}` in a command is
    # replaced with the version. The steps are the desk's, never the skill's: a
    # step written into a skill is a step a model can paraphrase. An empty list
    # is an error rather than a release that exits 0 having released nothing.
    "release_steps": [],
    # A stop-dispatch order stops a release too, unless this desk's order means
    # something narrower than "the desk is paused".
    "release_ignores_stop": False,
    # -- the principal's words --------------------------------------------
    "intake_dedupe_minutes": 10,
    # A prompt matching any of these is machine output, not the principal talking.
    # The sentinel's wake command feeds its status text back in through the same
    # channel a person types into, and in the hook's eyes the two look alike.
    "intake_skip_patterns": [
        r"^\s*\d{4}-\d{2}-\d{2}T[0-9:+\-]+ (GREEN|RED)\b",
        r"^\s*(GREEN|RED \(\d+\)) \|",
        r"^Continue from where you left off",
        r"^<",
    ],
    # -- the hand-window gate ---------------------------------------------
    "hand_gate": True,
    "hand_window_minutes": 30,
    # Commands that already have their own record. Matched on the first word's
    # basename, so `bellhop merge` and `./bin/bellhop merge` both count.
    "hand_allow_commands": ["bellhop"],
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
    "accept_model": "",
    "accept_effort": "",
    "check_cmd": "",
    "worktree": "",
    # -- providers (see docs/providers.md) ---------------------------------
    # The model and effort a packet marked `effort: heavy` is raised to. `heavy_model`
    # empty means the model does not change; `heavy_effort` empty means `effort` moves
    # one rung up the ladder (low, medium, high, xhigh). A lane that declares neither
    # gives a heavy packet exactly what an ordinary one gets, and `provider check
    # --heavy` says so rather than letting the marker read as if it did something.
    "heavy_model": "",
    # Passed to providers that take it (`--effort` on Claude Code). Given explicitly or
    # not at all: left off, the effort follows whatever this machine was last set to,
    # and a run at `high` and a run that happened to be `high` read the same.
    "effort": "",
    "heavy_effort": "",
    # A generic provider's command allowlist, enforced by `bellhop provider-guard`.
    # Required for `provider: generic`: Claude Code brings its own permission gate,
    # another vendor's CLI does not, and a lane with no allowlist may run anything.
    "allowlist_file": "",
    # A sandbox profile for this lane, applied through the desk's `sandbox_wrapper`.
    "sandbox_profile": "",
    # Time windows that move this lane to another provider or model, judged against the
    # time the worker starts -- not the time the packet was dispatched. Each entry:
    # {"name": "...", "days": "mon-fri", "from": "14:00", "to": "18:00",
    #  "provider": "generic", "model": "...", "effort": "...", "heavy_model": "...",
    #  "model_file": "state/lane-model-peak.txt"}. The first matching window wins.
    "schedule": [],
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


def price_table(desk: "Desk") -> dict:
    """The desk's prices merged over the defaults, one model row at a time.

    `merged.update(cfg)` would replace the whole table, so a desk that priced one
    model would silently unprice every other one - and an unpriced model reads `n/a`,
    which looks exactly like a model nobody has a price for.
    """
    table = {k: dict(v) for k, v in PRICE_DEFAULTS.items()}
    for prefix, row in (desk.cfg.get("prices") or {}).items():
        if isinstance(row, dict):
            table.setdefault(prefix, {}).update(row)
    return table


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
