from __future__ import annotations

import os
from pathlib import Path

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
