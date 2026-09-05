from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from .config import FRAMEWORK_ROOT, Desk
from .util import BellhopError, sha256_file, ts, write

SEALED_FRAMEWORK = ("bellhop", "bin", "template/.claude", "template/templates")
SEALED_DESK = ("frozen", "rules", ".claude", "templates")


def _files(root: Path, rel: str) -> List[Path]:
    base = root / rel
    if base.is_file():
        return [base]
    if not base.exists():
        return []
    return sorted(p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc"))


def manifest_path(desk: Desk) -> Path:
    return desk.path("seals", "manifest.sha256")


def compute(desk: Desk) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for rel in SEALED_FRAMEWORK:
        for p in _files(FRAMEWORK_ROOT, rel):
            out["framework:" + str(p.relative_to(FRAMEWORK_ROOT))] = sha256_file(p)
    for rel in SEALED_DESK:
        for p in _files(desk.root, rel):
            out["desk:" + str(p.relative_to(desk.root))] = sha256_file(p)
    return out


def seal(desk: Desk) -> Path:
    entries = compute(desk)
    lines = [f"# sealed {ts()} framework={FRAMEWORK_ROOT}"] + [f"{h}  {name}" for name, h in sorted(entries.items())]
    p = manifest_path(desk)
    write(p, "\n".join(lines) + "\n")
    return p


def verify(desk: Desk) -> List[str]:
    p = manifest_path(desk)
    if not p.exists():
        raise BellhopError("no seals manifest; run `bellhop seal`")
    recorded: Dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        h, name = line.split("  ", 1)
        recorded[name] = h
    current = compute(desk)
    changed = [n for n, h in current.items() if recorded.get(n) != h]
    changed += [n for n in recorded if n not in current]
    return sorted(changed)
