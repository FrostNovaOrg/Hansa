from __future__ import annotations

import os
import shutil
from collections import namedtuple
from pathlib import Path

from . import headers as headers_mod
from . import ledger as ledger_mod
from .accept import find_packet
from .config import Desk
from .packet import parse_packet
from .util import BellhopError, git, run


def merge(desk: Desk, task_id: str, push: bool = False, verdict: str = "") -> str:
    packet = parse_packet(find_packet(desk, task_id))
    lane = desk.lane(packet.lane)
    repo = Path(lane["repo"])
    branch = f"lane/{packet.lane}"
    target = lane["branch"]
    if git(repo, "status", "--porcelain"):
        raise BellhopError(f"{repo} has uncommitted changes; refusing to merge")
    git(repo, "checkout", "-q", target)
    proc = run(["git", "-C", str(repo), "merge", "--no-ff", "-q", "-m", f"merge {branch}: {packet.stem}", branch], check=False)
    if proc.returncode != 0:
        run(["git", "-C", str(repo), "merge", "--abort"], check=False)
        raise BellhopError(f"merge of {branch} into {target} conflicts; resolve by hand:\n{proc.stdout}{proc.stderr}")
    sha = git(repo, "rev-parse", "--short", "HEAD")
    if push:
        env = dict(os.environ, BELLHOP_MERGE="1")
        run(["git", "-C", str(repo), "push", "-q"], env=env, check=True)
    ledger_mod.add(
        desk,
        f"merged {packet.stem}",
        f"{branch} merged into {target} at {sha}.\nAcceptance: {verdict or 'not sampled'}.\nPushed: {'yes' if push else 'no'}.",
        refs=[packet.id, sha],
    )
    return sha


# --------------------------------------------------------------------------
# merge with the full board run on the merged result
# --------------------------------------------------------------------------
CheckedMerge = namedtuple("CheckedMerge", "sha target branch already check_exit check_summary pushed")


def _drop_worktree(repo: Path, path: Path) -> None:
    run(["git", "-C", str(repo), "worktree", "remove", "--force", str(path)], check=False)
    if Path(path).exists():
        shutil.rmtree(path, ignore_errors=True)
    run(["git", "-C", str(repo), "worktree", "prune"], check=False)


def merge_checked(desk: Desk, task_id: str, push: bool = False) -> CheckedMerge:
    """Merge the lane branch into its base branch, but only if the merged tree passes.

    The merge is made in a throwaway worktree and the lane's `check_cmd` — the full
    board, not the checks the worker chose to run — is run there. The base branch is
    fast-forwarded onto that merge commit only after the check comes back green, and
    only if the base branch has not moved in the meantime.

    A base branch that was moved and then found broken and a base branch that was
    never moved look the same in the sentence "the merge failed"; only one of them
    leaves the desk able to dispatch the next task. Writing this the other way round
    — merge first, check afterwards, revert if red — leaves the broken merge in the
    branch's history whatever happens next.

    No ledger entry is written here: the caller records the close in one entry, and
    two entries for one task is the bookkeeping this command exists to remove.
    """
    packet = parse_packet(find_packet(desk, task_id))
    lane = desk.lane(packet.lane)
    repo = Path(lane["repo"])
    branch = f"lane/{packet.lane}"
    target = lane["branch"]

    if not repo.exists():
        raise BellhopError(f"lane '{packet.lane}' repo does not exist: {repo}")
    if run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", branch], check=False).returncode != 0:
        raise BellhopError(f"no branch {branch} in {repo}; nothing to merge")
    if git(repo, "status", "--porcelain"):
        raise BellhopError(f"{repo} has uncommitted changes; refusing to merge")

    before = git(repo, "rev-parse", target)
    scratch = Path(desk.path(desk.cfg["worktrees_dir"], f".merge-{packet.lane}"))
    _drop_worktree(repo, scratch)  # a crashed run leaves one behind; it is not state
    scratch.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-f", "--detach", str(scratch), target)
    try:
        already = run(["git", "-C", str(repo), "merge-base", "--is-ancestor", branch, target],
                      check=False).returncode == 0
        if not already:
            proc = run(["git", "-C", str(scratch), "merge", "--no-ff", "-q", "-m",
                        f"merge {branch}: {packet.stem}", branch], check=False)
            if proc.returncode != 0:
                run(["git", "-C", str(scratch), "merge", "--abort"], check=False)
                raise BellhopError(
                    f"merge of {branch} into {target} conflicts; {target} is untouched at "
                    f"{before[:12]}, resolve by hand:\n{proc.stdout}{proc.stderr}")
        merged = git(scratch, "rev-parse", "HEAD")
        # One ruler for both readings: the receipt's check_exit and this one are
        # produced by the same function, so "green on the branch, red on the board"
        # is a real difference and not two different ways of running the checks.
        check_exit, check_summary = headers_mod.run_lane_check(lane, scratch)
        if check_exit not in ("0", headers_mod.CHECK_NOT_RUN):
            said = check_summary if check_summary != headers_mod.NOT_AVAILABLE else "no output"
            raise BellhopError(
                f"the full board failed on the merged result (check_cmd exited {check_exit}, "
                f"last line: {said}); {target} is untouched at {before[:12]}")
        if not already:
            now = git(repo, "rev-parse", target)
            if now != before:
                raise BellhopError(
                    f"{target} moved from {before[:12]} to {now[:12]} while the merge was being "
                    f"checked; nothing was written, run the close again")
            git(repo, "checkout", "-q", target)
            git(repo, "merge", "--ff-only", "-q", merged)
    finally:
        _drop_worktree(repo, scratch)

    sha = git(repo, "rev-parse", "--short", target)
    pushed = False
    if push:
        env = dict(os.environ, BELLHOP_MERGE="1")
        proc = run(["git", "-C", str(repo), "push", "-q"], env=env, check=False)
        if proc.returncode != 0:
            raise BellhopError(
                f"{branch} is merged into {target} at {sha} but the push failed: "
                f"{(proc.stderr or proc.stdout or '').strip()}")
        pushed = True
    return CheckedMerge(sha=sha, target=target, branch=branch, already=already,
                        check_exit=check_exit, check_summary=check_summary, pushed=pushed)
