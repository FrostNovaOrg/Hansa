from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

from .locking import serialized
from .config import COVERS_ALL, COVERS_PACKAGE, Desk, framework_root
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
    root = framework_root().path
    out: Dict[str, str] = {}
    for rel in SEALED_FRAMEWORK:
        for p in _files(root, rel):
            out["framework:" + str(p.relative_to(root))] = _digest(p)
    for rel in SEALED_DESK:
        for p in _files(desk.root, rel):
            out["desk:" + str(p.relative_to(desk.root))] = _digest(p)
    return out


@serialized
def seal(desk: Desk) -> Path:
    root = framework_root()
    entries = compute(desk)
    lines = [
        f"# sealed {ts()} framework={root.path}",
        f"# root: {root.kind} via {root.source}",
        f"# covers: {root.covers}",
    ] + [f"{h}  {name}" for name, h in sorted(entries.items())]
    p = manifest_path(desk)
    write(p, "\n".join(lines) + "\n")
    return p


VENDORED_PREFIXES = ("framework:bellhop/", "framework:bellhop_main.py", "framework:template/templates/")


def running_from_vendored_copy() -> bool:
    """True when this code runs from a desk's `.bellhop/` copy rather than a full checkout."""
    return framework_root().covers == COVERS_PACKAGE


def _in_scope(name: str, scope: str) -> bool:
    if scope == COVERS_ALL or not name.startswith("framework:"):
        return True
    return name.startswith(VENDORED_PREFIXES)


def _read_manifest(p: Path) -> Tuple[Dict[str, str], str, str]:
    """(entries, covers, sealed_from). `covers` comes from the header, or from the names.

    Manifests written before the header carried `covers` are read by looking at what is
    in them, so an upgrade does not turn every desk red for one run.
    """
    recorded: Dict[str, str] = {}
    covers, sealed_from = "", ""
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            m = re.match(r"#\s*covers:\s*(\S+)", line)
            if m:
                covers = m.group(1)
            m = re.search(r"framework=(.+)$", line)
            if m:
                sealed_from = m.group(1).strip()
            continue
        if not line.strip():
            continue
        h, name = line.split("  ", 1)
        recorded[name] = h
    if covers not in (COVERS_ALL, COVERS_PACKAGE):
        framework = [n for n in recorded if n.startswith("framework:")]
        covers = COVERS_PACKAGE if framework and all(n.startswith(VENDORED_PREFIXES) for n in framework) else COVERS_ALL
    return recorded, covers, sealed_from


class Report:
    """What `verify` found, and what it was able to look at.

    The scope is reported rather than assumed because it is the difference between
    "nothing changed" and "nothing I could see changed", and a desk is checked from
    more than one copy of the framework.
    """

    __slots__ = ("changed", "scope", "root", "sealed_from")

    def __init__(self, changed: List[str], scope: str, root, sealed_from: str):
        self.changed = changed
        self.scope = scope
        self.root = root
        self.sealed_from = sealed_from

    @property
    def partial(self) -> bool:
        return self.scope == COVERS_PACKAGE

    def line(self) -> str:
        where = f"framework: {self.root.line()}"
        if self.partial:
            where += "; the skills, hooks and plugin files are not in this copy and were not checked"
        return where


def verify_report(desk: Desk) -> Report:
    """The seal check, with the scope it ran at.

    Both sides are cut to the *narrower* of what the manifest covers and what this copy
    of the framework has on disk. Without that, the same desk reads green from a full
    checkout and red from the daemon's vendored copy -- every skill and hook file
    "missing" -- which is how a real desk ended up with two answers and no explanation.
    """
    p = manifest_path(desk)
    if not p.exists():
        raise BellhopError("no seals manifest; run `bellhop seal`")
    root = framework_root()
    recorded, covers, sealed_from = _read_manifest(p)
    current = compute(desk)
    scope = COVERS_PACKAGE if COVERS_PACKAGE in (covers, root.covers) else COVERS_ALL
    recorded = {n: h for n, h in recorded.items() if _in_scope(n, scope)}
    current = {n: h for n, h in current.items() if _in_scope(n, scope)}
    changed = [n for n, h in current.items() if recorded.get(n) != h]
    changed += [n for n in recorded if n not in current]
    return Report(sorted(changed), scope, root, sealed_from)


def verify(desk: Desk) -> List[str]:
    return verify_report(desk).changed
