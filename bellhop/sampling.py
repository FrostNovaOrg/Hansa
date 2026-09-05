from __future__ import annotations

import hashlib
from typing import Tuple

from .config import Desk
from .util import iso_week


def sampled(desk: Desk, task_id: str) -> Tuple[bool, str, str]:
    """Deterministic acceptance sampling: one in `sample_rate` ids, seeded per week."""
    rate = max(1, int(desk.cfg.get("sample_rate", 3)))
    seed = desk.cfg.get("weekly_seed") or iso_week()
    digest = hashlib.sha256(f"{seed}:{task_id}".encode("utf-8")).hexdigest()
    hit = int(digest[:8], 16) % rate == 0
    return hit, seed, digest[:12]
