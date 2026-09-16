from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

from .locking import serialized
from .config import Desk
from .packet import parse_packet
from .util import BellhopError, append, git, read, read_json, run, ts

PRE_PUSH_HOOK = """#!/bin/sh
# Installed by Bellhop. Workers must not push; the coordinator's merge sets BELLHOP_MERGE=1.
if [ "${BELLHOP_MERGE:-}" != "1" ]; then
  echo "bellhop: push refused from a lane checkout (set BELLHOP_MERGE=1 from the coordinator's merge step)" >&2
  exit 1
fi
exit 0
"""


def install_prepush_hook(repo: Path) -> Path:
    hooks_dir = Path(git(repo, "rev-parse", "--git-path", "hooks"))
    if not hooks_dir.is_absolute():
        hooks_dir = repo / hooks_dir
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-push"
    if hook.exists() and "Installed by Bellhop" not in read(hook):
        raise BellhopError(f"{hook} exists and is not Bellhop's; merge the guard by hand")
    hook.write_text(PRE_PUSH_HOOK, encoding="utf-8")
    hook.chmod(0o755)
    return hook


@serialized
def prepare_worktree(desk: Desk, lane: dict, base: str) -> Path:
    repo = Path(lane["repo"])
    if not repo.exists():
        raise BellhopError(f"lane '{lane['name']}' repo does not exist: {repo}")
    install_prepush_hook(repo)
    wt = Path(lane["worktree"])
    branch = f"lane/{lane['name']}"
    if not wt.exists():
        wt.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "add", "-f", "-B", branch, str(wt), base)
    else:
        dirty = git(wt, "status", "--porcelain")
        if dirty:
            raise BellhopError(f"lane worktree is dirty, refusing to start a task:\n{dirty}")
        # The first task creates the worktree with `-B branch base`; every later task must
        # start from its own packet's base too, or it silently inherits the previous task's
        # commits and the receipt's `base` and the acceptance diff both become fiction.
        git(wt, "checkout", "-q", "-B", branch, base)
    return wt


@contextlib.contextmanager
def lane_lock(desk: Desk, lane_name: str, timeout_s: int = 0):
    lock_dir = desk.path("bells", ".locks")
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = lock_dir / f"{lane_name}.lock"
    start = time.time()
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            if time.time() - start >= timeout_s:
                raise BellhopError(f"lane '{lane_name}' is busy (lock {lock.name})")
            time.sleep(1)
    try:
        yield lock
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def lane_is_busy(desk: Desk, lane_name: str) -> bool:
    return desk.path("bells", ".locks", f"{lane_name}.lock").exists()


# -- stuck lanes -----------------------------------------------------------


class LaneBlocked(BellhopError):
    """The lane holds work the command would destroy or cannot skip. Exit code 2."""


def lane_base(desk: Desk, lane: dict) -> tuple:
    """Where the lane should be standing: (ref, where that came from).

    The current task's packet base first (a bell is pending or being answered), then the
    last task's, then the lane's configured base branch. Resetting to `main` when the
    lane's task started from an older commit would silently move the task off its own
    base, and the receipt's `base` would then describe a diff nobody ran.
    """
    name = lane["name"]
    for bell in sorted(desk.bells_pending.glob("*.bell"), reverse=True):
        data = read_json(bell, {}) or {}
        if data.get("lane") != name:
            continue
        packet_path = desk.root / str(data.get("packet", ""))
        if packet_path.exists():
            return parse_packet(packet_path).base, f"pending bell {bell.name}"
    best = None
    if desk.receipts.exists():
        for meta in desk.receipts.glob("*.meta.json"):
            data = read_json(meta, {}) or {}
            if data.get("lane") != name or not data.get("base"):
                continue
            key = str(data.get("finished_at") or "")
            if best is None or key > best[0]:
                best = (key, str(data["base"]), meta.name)
    if best:
        return best[1], f"last task {best[2]}"
    return lane["branch"], "lane base branch (no task on record)"


def _resolve(repo: Path, ref: str) -> str:
    proc = run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], check=False)
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        raise BellhopError(f"cannot resolve '{ref}' in {repo}")
    return out


def _ahead(repo: Path, base_ref: str, head: str = "HEAD") -> int:
    return int(git(repo, "rev-list", "--count", f"{base_ref}..{head}") or 0)


def _lane_worktree(desk: Desk, lane: dict) -> Path:
    wt = Path(lane["worktree"])
    if not (wt / ".git").exists():
        raise BellhopError(
            f"lane '{lane['name']}' has no worktree at {wt}; the next task creates it"
        )
    return wt


@serialized
def lane_reset(desk: Desk, lane_name: str, dry_run: bool = False, force: bool = False) -> dict:
    """Throw away everything uncommitted in a lane worktree and put its branch back."""
    lane = desk.lane(lane_name)
    wt = _lane_worktree(desk, lane)
    branch = f"lane/{lane_name}"
    if lane_is_busy(desk, lane_name) and not force:
        raise LaneBlocked(
            f"lane '{lane_name}' is busy: a worker holds the lock. Wait for the receipt, or --force"
        )
    base_ref, source = lane_base(desk, lane)
    base = _resolve(wt, base_ref)
    ahead = _ahead(wt, lane["branch"])
    if ahead and not force:
        raise LaneBlocked(
            f"lane '{lane_name}' has {ahead} commit(s) not on {lane['branch']}: reset would drop them. "
            f"Close the task (`bellhop close-task <id>`) or re-run with --force"
        )
    dirty = [l for l in git(wt, "status", "--porcelain").splitlines() if l.strip()]
    head = git(wt, "rev-parse", "--short", "HEAD")
    steps = [
        f"worktree {wt}",
        f"discard {len(dirty)} uncommitted path(s) and every untracked file",
        f"move {branch} from {head} to {_short(wt, base)} ({base_ref}, from {source})",
    ]
    if ahead:
        steps.append(f"drop {ahead} commit(s) not on {lane['branch']} (--force)")
    if dry_run:
        return {"lane": lane_name, "dry_run": True, "steps": steps, "dirty": len(dirty), "ahead": ahead,
                "base": base, "base_ref": base_ref, "source": source, "head_before": head,
                "last": f"✅ lane {lane_name} reset (dry run): nothing was changed"}
    git(wt, "reset", "-q", "--hard")
    git(wt, "clean", "-fdq")
    git(wt, "checkout", "-q", "-B", branch, base)
    left = git(wt, "status", "--porcelain")
    if left:
        raise BellhopError(f"lane '{lane_name}' is still dirty after the reset:\n{left}")
    after = git(wt, "rev-parse", "--short", "HEAD")
    return {"lane": lane_name, "dry_run": False, "steps": steps, "dirty": len(dirty), "ahead": ahead,
            "base": base, "base_ref": base_ref, "source": source, "head_before": head, "head_after": after,
            "last": f"✅ lane {lane_name} reset to {after} ({base_ref}, from {source}); "
                    f"{len(dirty)} uncommitted path(s) discarded"}


# -- stale started markers -------------------------------------------------


def started_dir(desk: Desk) -> Path:
    return desk.path("bells", ".locks", "started")


def started_markers(desk: Desk, lane_name: str = "") -> list:
    """The launch markers on this lane (all of them when no lane is named).

    `process_bell` writes one before it starts a worker and removes it after the
    receipt is archived. One left behind means a launch that never finished, and it
    deliberately blocks a replay: a worker that died before it wrote anything and a
    worker that finished and then lost its settlement look the same from the bell.
    """
    out = []
    d = started_dir(desk)
    if not d.exists():
        return out
    for marker in sorted(d.glob("*.json")):
        data = read_json(marker, {}) or {}
        if lane_name and data.get("lane") != lane_name:
            continue
        out.append((marker, data))
    return out


def _bell_state(desk: Desk, bell_name: str) -> str:
    if (desk.bells_pending / bell_name).exists():
        return "its bell is still pending"
    if (desk.bells_rung / bell_name).exists():
        return "its bell has been rung and archived"
    return "its bell is in neither pending/ nor rung/"


@serialized
def lane_recover(desk: Desk, lane_name: str, dry_run: bool = False, force: bool = False) -> dict:
    """Clear a stale launch marker on a lane, and leave a line saying so.

    The marker is the desk being careful: it refuses to answer the same bell twice
    after an interrupted launch, because Bellhop cannot tell from a missing process
    whether the worker's external effects happened. That care turns into a stuck lane
    the moment nobody has a command to end it — the only way forward was deleting a
    file under `bells/.locks/`, by hand, with nothing written down. This is that
    command, and the line it writes is the record the hand deletion never left.

    It refuses while the lane claim is held: a claim plus a marker is a launch that may
    still be running, and that is not stale, it is live. `--force` says you have looked.
    """
    desk.lane(lane_name)  # validates the lane exists
    markers = started_markers(desk, lane_name)
    busy = lane_is_busy(desk, lane_name)
    if busy and not force:
        raise LaneBlocked(
            f"lane '{lane_name}' still holds its claim: a worker may be running. Establish that its "
            f"process is gone and preserve its receipt and log first, then re-run with --force"
        )
    steps = []
    for marker, data in markers:
        steps.append(f"clear {marker.name} (bell {data.get('bell', '?')}, started {data.get('started_at', '?')}; "
                     f"{_bell_state(desk, str(data.get('bell', '')))})")
    if not markers:
        return {"lane": lane_name, "dry_run": dry_run, "cleared": 0, "steps": steps,
                "last": f"✅ lane {lane_name}: no launch marker to clear"}
    if dry_run:
        return {"lane": lane_name, "dry_run": True, "cleared": 0, "steps": steps,
                "last": f"✅ lane {lane_name} recover (dry run): {len(markers)} marker(s), nothing was changed"}
    log = desk.bells_logs / "watchdog.log"
    for marker, data in markers:
        bell_name = str(data.get("bell", marker.stem))
        append(log, f"{ts()}\tRECOVER\t{lane_name}\tcleared started marker {marker.name} "
                    f"(started {data.get('started_at', '?')}; {_bell_state(desk, bell_name)})"
                    + ("\t(--force: the lane claim was still held)" if busy else "") + "\n")
        marker.unlink()
    return {"lane": lane_name, "dry_run": False, "cleared": len(markers), "steps": steps,
            "last": f"✅ lane {lane_name}: {len(markers)} stale launch marker(s) cleared, "
                    f"one line each in bells/logs/watchdog.log"}


@serialized
def lane_align(desk: Desk, lane_name: str) -> dict:
    """Fast-forward an idle, clean, fully merged lane branch onto its base branch head.

    The difference from a reset: a reset throws work away and says so; align only moves a
    lane that has nothing to lose. Anything else is a refusal, because a lane that is
    quietly rewound looks exactly like a lane that was already up to date.
    """
    lane = desk.lane(lane_name)
    wt = _lane_worktree(desk, lane)
    branch = f"lane/{lane_name}"
    target = lane["branch"]
    if lane_is_busy(desk, lane_name):
        raise LaneBlocked(f"lane '{lane_name}' is busy: a worker holds the lock")
    current = git(wt, "rev-parse", "--abbrev-ref", "HEAD")
    if current != branch:
        raise LaneBlocked(f"lane '{lane_name}' is on '{current}', not {branch}; `bellhop lane reset {lane_name}` first")
    dirty = [l for l in git(wt, "status", "--porcelain").splitlines() if l.strip()]
    if dirty:
        raise LaneBlocked(
            f"lane '{lane_name}' has {len(dirty)} uncommitted path(s); align only moves a clean lane "
            f"(`bellhop lane reset {lane_name}` throws them away)"
        )
    ahead = _ahead(wt, target)
    if ahead:
        raise LaneBlocked(
            f"lane '{lane_name}' has {ahead} commit(s) not on {target}; close the task first (`bellhop close-task <id>`)"
        )
    before = git(wt, "rev-parse", "--short", "HEAD")
    proc = run(["git", "-C", str(wt), "merge", "--ff-only", "-q", target], check=False)
    if proc.returncode != 0:
        raise LaneBlocked(
            f"cannot fast-forward {branch} onto {target}: {(proc.stderr or proc.stdout or '').strip()}"
        )
    after = git(wt, "rev-parse", "--short", "HEAD")
    steps = [f"worktree {wt}", f"fast-forward {branch} onto {target}"]
    return {"lane": lane_name, "steps": steps, "head_before": before, "head_after": after,
            "moved": before != after,
            "last": f"✅ lane {lane_name} aligned to {after} ({target})"
                    + ("" if before != after else " — already there")}


def _short(repo: Path, sha: str) -> str:
    return git(repo, "rev-parse", "--short", sha)
