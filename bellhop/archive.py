from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from .config import ARCHIVE_ROSTER, Desk
from .util import BellhopError, git, run


def roster(desk: Desk) -> List[str]:
    """The desk directories and files that may enter a commit."""
    names = desk.cfg.get("archive_roster") or ARCHIVE_ROSTER
    return [str(n).strip("/") for n in names if str(n).strip("/")]


def _top(rel: str) -> str:
    parts = Path(rel).parts
    return parts[0] if parts else ""


def _tracked(desk: Desk, rel: str) -> bool:
    return bool(git(desk.root, "ls-files", "--", rel))


def _ignored(desk: Desk, rels: Sequence[str]) -> set:
    """The paths git would refuse to add because .gitignore covers them."""
    if not rels:
        return set()
    proc = run(["git", "-C", str(desk.root), "check-ignore", "--", *rels], check=False)
    return {line.strip() for line in (proc.stdout or "").splitlines() if line.strip()}


def archive(desk: Desk, paths: Iterable[Path] = (), message: str = "", push: Optional[bool] = None) -> str:
    """Commit desk files by roster.

    With `paths`, each path is checked against the roster and then archived: a command
    archives what it touched and nothing else. With no paths, the whole roster is
    archived, new files included -- that is what the daily close uses, so a change to
    `bellhop.json`, `rules/` or `.claude/` cannot sit untracked for a week.

    `git add -A` is deliberately absent. It stages whatever else happens to be in the
    desk directory at that moment: a scratch file, a note someone was still writing, a
    lane worktree. The roster is the only way in.
    """
    names = roster(desk)
    requested = [Path(p) for p in (paths or ())]
    rel: List[str] = []
    for p in requested:
        try:
            r = str(p.resolve().relative_to(desk.root))
        except ValueError:
            raise BellhopError(f"refusing to archive a path outside the desk: {p}")
        if _top(r) not in names:
            raise BellhopError(
                f"refusing to archive {r}: it is not on the archive roster ({', '.join(names)})"
            )
        # A path that is gone but still tracked is a deletion to record (a bell that
        # moved from pending/ to rung/, say). A path that is gone and was never tracked
        # is nothing at all.
        if not p.exists() and not _tracked(desk, r):
            continue
        rel.append(r)
    if not requested:
        rel = [n for n in names if desk.path(n).exists()]
    ignored = _ignored(desk, rel)
    rel = [r for r in rel if r not in ignored]
    if not rel:
        raise BellhopError("nothing to archive")
    git(desk.root, "add", "--", *rel)
    staged = git(desk.root, "diff", "--cached", "--name-only")
    if not staged:
        return git(desk.root, "rev-parse", "HEAD")
    prefix = desk.cfg["commit_prefix"]
    git(desk.root, "commit", "-q", "-m", f"{prefix} {message}")
    sha = git(desk.root, "rev-parse", "--short", "HEAD")
    if desk.cfg["push"] if push is None else push:
        proc = run(["git", "-C", str(desk.root), "push", "-q"], check=False)
        if proc.returncode != 0:
            raise BellhopError(f"committed {sha} but push failed: {(proc.stderr or '').strip()}")
    return sha


def unpushed_count(desk: Desk) -> Optional[int]:
    upstream = run(["git", "-C", str(desk.root), "rev-parse", "--abbrev-ref", "@{upstream}"], check=False)
    if upstream.returncode != 0:
        return None
    out = git(desk.root, "rev-list", "--count", "@{upstream}..HEAD")
    return int(out or 0)
