from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from .config import Desk
from .util import BellhopError, git, run


def archive(desk: Desk, paths: Iterable[Path], message: str, push: Optional[bool] = None) -> str:
    rel = []
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        try:
            rel.append(str(p.resolve().relative_to(desk.root)))
        except ValueError:
            raise BellhopError(f"refusing to archive a path outside the desk: {p}")
    if not rel:
        raise BellhopError("nothing to archive")
    git(desk.root, "add", "-A", "--", *rel)
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
