from __future__ import annotations

import os
import shutil
from collections import namedtuple
from pathlib import Path

from . import archive as archive_mod
from . import exceptions as exceptions_mod
from . import headers as headers_mod
from . import ledger as ledger_mod
from . import state as state_mod
from .accept import find_packet
from .locking import serialized
from .config import Desk
from .packet import parse_packet
from .util import BellhopError, git, read, run

JudgementMerge = namedtuple(
    "JudgementMerge",
    "sha target branch already check_exit check_summary pushed reason exception count red "
    "ledger_path archive_sha notes")


def merge(desk: Desk, task_id: str, reason: str, push: bool = False, commit: bool = True) -> JudgementMerge:
    """Merge on the coordinator's judgement, and write down that that is what happened.

    `close-task` is the ordinary way a task reaches the product: it wants a passing
    acceptance verdict and refuses without one. This command is the other way, and it
    exists because the verdict is **not binding** — the coordinator keeps the final
    say, and a desk with no command for that gets the same thing done by hand, with no
    record at all. A gate with no exception channel is not obeyed more often; it is
    gone round more quietly.

    So the judgement is allowed and it is counted. `--reason` is required, the reason
    goes on the ledger and into `state/exceptions.md`, and two of these inside seven
    days turn the sentinel red until the principal says which way it should be.

    What is *not* waived: the merge still happens in a throwaway worktree and the
    lane's full board still has to pass on the merged result. That is mechanics, not
    judgement — a merge that breaks the base branch is not a decision anybody made.
    """
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise BellhopError(
            'bellhop merge needs --reason "<why the acceptance gate is being gone round>": '
            "an exception nobody wrote down cannot be counted. `bellhop close-task <id>` "
            "is the path that needs no reason, because the verdict carries it")
    packet = parse_packet(find_packet(desk, task_id))
    result = merge_checked(desk, task_id, push=push)

    entry = exceptions_mod.record(desk, "merge", f"{task_id} {packet.lane} {packet.title}", reason)
    hits = exceptions_mod.recent(desk, "merge")
    red = len(hits) >= exceptions_mod.RED_AT

    title = f"merge exception: {task_id} — {reason}"
    body = "\n".join([
        f"- lane: {packet.lane} (base {packet.base})",
        f"- merged: {result.sha} into {result.target}"
        + (" (already there)" if result.already else ""),
        f"- full board: exit {result.check_exit} — {result.check_summary}",
        "- acceptance: not required here; this merge went on the coordinator's judgement",
        f"- reason: {reason}",
        f"- recorded in: {exceptions_mod.EXCEPTIONS} ({len(hits)} merge exception(s) in the "
        f"last {exceptions_mod.WINDOW_DAYS} days)",
    ])
    ledger_path = ledger_mod.add(desk, title, body, refs=[task_id, result.sha])

    notes = []
    rows = _inflight_rows(desk, task_id)
    if len(rows) > 1:
        notes.append(f"{len(rows)} in-flight rows carry {task_id}; left alone, finish one by hand")
    elif rows:
        notes.append(state_mod.inflight_finish(desk, task_id).note)
    else:
        notes.append("in flight: no row")
    line = f"{packet.title} | merged {result.sha} | exception: {reason}"
    cap = state_mod.cfg_int(desk, "closings_line_chars", 200)
    if len(line) > cap:
        line = line[: cap - 4].rstrip() + " ..."
    notes.append(state_mod.closings_add(desk, task_id, line).note)

    archive_sha = ""
    if commit:
        paths = [ledger_path, desk.path(state_mod.INFLIGHT), desk.path(state_mod.CLOSINGS),
                 exceptions_mod.path(desk)]
        archive_sha = archive_mod.archive(desk, paths, f"merge exception {task_id} at {result.sha}")
    return JudgementMerge(sha=result.sha, target=result.target, branch=result.branch,
                          already=result.already, check_exit=result.check_exit,
                          check_summary=result.check_summary, pushed=result.pushed,
                          reason=reason, exception=entry.line, count=len(hits), red=red,
                          ledger_path=ledger_path, archive_sha=archive_sha, notes=notes)


def _inflight_rows(desk: Desk, task_id: str):
    path = desk.path(state_mod.INFLIGHT)
    if not path.exists():
        return []
    carries = state_mod.token(task_id)
    return [l for l in read(path).splitlines()
            if l.startswith("- ") and l.strip() != "- none" and carries(l)]


# --------------------------------------------------------------------------
# merge with the full board run on the merged result
# --------------------------------------------------------------------------
CheckedMerge = namedtuple("CheckedMerge", "sha target branch already check_exit check_summary pushed")


def _drop_worktree(repo: Path, path: Path) -> None:
    run(["git", "-C", str(repo), "worktree", "remove", "--force", str(path)], check=False)
    if Path(path).exists():
        shutil.rmtree(path, ignore_errors=True)
    run(["git", "-C", str(repo), "worktree", "prune"], check=False)


@serialized
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
