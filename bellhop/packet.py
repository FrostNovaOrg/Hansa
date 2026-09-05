from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .util import BellhopError, read, slugify

REQUIRED = ("id", "lane", "title", "base")


class PacketError(BellhopError):
    pass


@dataclass
class Packet:
    id: str
    lane: str
    title: str
    base: str
    acceptance: List[str]
    budget_minutes: int = 45
    max_turns: Optional[int] = None
    body: str = ""
    path: Optional[Path] = None
    extra: dict = field(default_factory=dict)

    @property
    def slug(self) -> str:
        return slugify(self.title)

    @property
    def stem(self) -> str:
        return f"{self.id}-{self.slug}"


def parse_front_matter(text: str):
    """Parse a small YAML subset: `key: value` lines and `key:` followed by `- item` lines."""
    if not text.startswith("---"):
        raise PacketError("packet must start with a '---' front-matter block")
    parts = text.split("\n---", 1)
    if len(parts) < 2:
        raise PacketError("front-matter block is not closed with '---'")
    head = parts[0][3:]
    body = parts[1].lstrip("\n")
    data: dict = {}
    current_list = None
    for raw in head.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = re.match(r"^\s*-\s+(.*)$", line)
        if m and current_list is not None:
            data[current_list].append(m.group(1).strip())
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if not m:
            raise PacketError(f"cannot parse front-matter line: {line!r}")
        key, val = m.group(1), m.group(2).strip()
        if val == "":
            data[key] = []
            current_list = key
        else:
            data[key] = val.strip('"').strip("'")
            current_list = None
    return data, body


def parse_packet(path: Path) -> Packet:
    path = Path(path)
    if not path.exists():
        raise PacketError(f"packet not found: {path}")
    data, body = parse_front_matter(read(path))
    problems = [k for k in REQUIRED if not data.get(k)]
    acceptance = data.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance:
        problems.append("acceptance (at least one item)")
    if problems:
        raise PacketError(f"{path.name}: missing {', '.join(problems)}")
    if not re.match(r"^[A-Za-z0-9_.-]+$", str(data["id"])):
        raise PacketError(f"{path.name}: id must be a plain token, got {data['id']!r}")
    if not body.strip():
        raise PacketError(f"{path.name}: body is empty; say what to do in plain words")
    try:
        budget = int(data.get("budget_minutes", 45))
    except ValueError:
        raise PacketError(f"{path.name}: budget_minutes must be an integer")
    max_turns = data.get("max_turns")
    known = {"id", "lane", "title", "base", "acceptance", "budget_minutes", "max_turns"}
    extra = {k: v for k, v in data.items() if k not in known}
    return Packet(
        id=str(data["id"]),
        lane=str(data["lane"]),
        title=str(data["title"]),
        base=str(data["base"]),
        acceptance=[str(a) for a in acceptance],
        budget_minutes=budget,
        max_turns=int(max_turns) if max_turns else None,
        body=body,
        path=path,
        extra=extra,
    )


def next_packet_id(inbox: Path) -> str:
    ids = []
    for p in inbox.rglob("*.md"):
        m = re.match(r"^(\d+)-", p.name)
        if m:
            ids.append(int(m.group(1)))
    return f"{(max(ids) + 1) if ids else 1:03d}"


def render_packet(template: str, **fields) -> str:
    out = template
    for k, v in fields.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out
