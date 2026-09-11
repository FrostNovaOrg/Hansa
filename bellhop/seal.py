from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from .locking import serialized
from .config import FRAMEWORK_ROOT, Desk
from .state import HOT_END, HOT_START
from .util import BellhopError, sha256_bytes, sha256_file, ts, write

SEALED_FRAMEWORK = ("bellhop", "bin", "bellhop_main.py", "template/.claude", "template/templates", "skills", "agents", "hooks", ".claude-plugin", ".mcp.json")
SEALED_DESK = ("frozen", "rules", ".claude", "templates")

# Files that sit inside a sealed directory but are append-only registers written
# by the machinery about itself. A seal is a claim that nobody edited these
# files behind your back; a register that grows by one line every time a hot
# rule is retired makes that claim useless, and a seal everyone reseals without
# looking is not a seal.
UNSEALED = ("rules/hot-rules-retired.md", "template/rules/hot-rules-retired.md")


def _files(root: Path, rel: str) -> List[Path]:
    base = root / rel
    if base.is_file():
        return [base]
    if not base.exists():
        return []
    out = []
    for p in sorted(base.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or p.name.endswith(".pyc"):
            continue
        if p.relative_to(root).as_posix() in UNSEALED:
            continue
        out.append(p)
    return out


def _digest(path: Path) -> str:
    """The file's hash, with any hot-rules segment left out of it.

    The segment between the two markers is written by `bellhop state hotrules`,
    and it is *meant* to change between seals: a rule enters when its root cause
    has been caught three times and leaves when it stops earning its slot. If the
    segment were hashed, every promotion would turn the seal red, and a seal that
    is red for a legitimate reason twice a week teaches everyone to reseal without
    reading the diff.

    The marker lines themselves stay in the hash, so deleting a marker, moving
    one, or writing outside the pair still breaks the seal.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return sha256_file(path)
    lines = text.split("\n")
    starts = [i for i, l in enumerate(lines) if l.strip() == HOT_START]
    ends = [i for i, l in enumerate(lines) if l.strip() == HOT_END]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        return sha256_file(path)
    kept = lines[: starts[0] + 1] + lines[ends[0]:]
    return sha256_bytes("\n".join(kept).encode("utf-8"))


def manifest_path(desk: Desk) -> Path:
    return desk.path("seals", "manifest.sha256")


def compute(desk: Desk) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for rel in SEALED_FRAMEWORK:
        for p in _files(FRAMEWORK_ROOT, rel):
            out["framework:" + str(p.relative_to(FRAMEWORK_ROOT))] = _digest(p)
    for rel in SEALED_DESK:
        for p in _files(desk.root, rel):
            out["desk:" + str(p.relative_to(desk.root))] = _digest(p)
    return out


@serialized
def seal(desk: Desk) -> Path:
    entries = compute(desk)
    lines = [f"# sealed {ts()} framework={FRAMEWORK_ROOT}"] + [f"{h}  {name}" for name, h in sorted(entries.items())]
    p = manifest_path(desk)
    write(p, "\n".join(lines) + "\n")
    return p


VENDORED_PREFIXES = ("framework:bellhop/", "framework:bellhop_main.py", "framework:template/templates/")


def running_from_vendored_copy() -> bool:
    """True when this code runs from a desk's `.bellhop/` copy rather than a full checkout."""
    return (FRAMEWORK_ROOT / "VERSION").exists() and not (FRAMEWORK_ROOT / ".claude-plugin").exists()


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
    if running_from_vendored_copy():
        # The vendored copy carries only the package, the entry point, and the templates.
        recorded = {n: h for n, h in recorded.items() if not n.startswith("framework:") or n.startswith(VENDORED_PREFIXES)}
        current = {n: h for n, h in current.items() if not n.startswith("framework:") or n.startswith(VENDORED_PREFIXES)}
    changed = [n for n, h in current.items() if recorded.get(n) != h]
    changed += [n for n in recorded if n not in current]
    return sorted(changed)
