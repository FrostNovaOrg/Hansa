from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

from .config import Desk
from .util import BellhopError, git, read

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
