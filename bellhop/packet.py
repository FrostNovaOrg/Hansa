from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

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


# --------------------------------------------------------------------------
# The dispatch side: the id, the fixed head, the body, the fixed tail
# --------------------------------------------------------------------------
# A packet has three parts and only one of them is written by hand. The front
# matter and the head paragraph are computed (the id from the counter, the base
# from git); the body is the coordinator's own sentences, copied in verbatim; the
# tail is fixed prose that every worker needs and nobody should retype. Splitting
# them this way is what makes `bellhop dispatch` one turn instead of four: the
# only part that needs a human is the part in the middle.

HEADING_RE = re.compile(r"^#{1,6}\s+")
ACCEPTANCE_HEADING_RE = re.compile(r"^#{1,6}\s+acceptance\b", re.I)
BULLET_RE = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")


def has_front_matter(text: str) -> bool:
    """Does this text open its own `---` block?

    A body that carries one would give the rendered packet two front-matter blocks,
    and `parse_front_matter` would read the coordinator's block as the body. The
    packet would still parse, so nothing would complain: a packet whose id and base
    came from a stale copy-paste and a packet built from the real ones look identical
    once written.
    """
    return text.lstrip("﻿").lstrip().startswith("---")


def body_acceptance(body: str) -> List[str]:
    """The bullets under the body's `## Acceptance` heading, in order.

    One place, not two. When acceptance lived only in the front matter the coordinator
    wrote the checks twice -- once for the machine and once in prose for the worker --
    and the two drifted within a week.
    """
    items: List[str] = []
    inside = False
    for raw in body.splitlines():
        line = raw.rstrip()
        if ACCEPTANCE_HEADING_RE.match(line):
            inside = True
            continue
        if inside and HEADING_RE.match(line):
            break
        if inside:
            m = BULLET_RE.match(line)
            if m:
                items.append(m.group(1))
    return items


def next_id(desk) -> str:
    """One past the highest packet id the desk has ever issued.

    The inbox alone is not the counter: a packet that was archived out of the inbox
    would let the next id repeat one already in the receipts, and two receipts under
    one id are indistinguishable from one receipt that was written twice. Receipts and
    rung bells are counted too, so the number only ever goes up.
    """
    highest = 0
    if desk.inbox.exists():
        for p in desk.inbox.rglob("*.md"):
            m = re.match(r"^(\d+)-", p.name)
            if m:
                highest = max(highest, int(m.group(1)))
    if desk.receipts.exists():
        for p in desk.receipts.iterdir():
            m = re.match(r"^(\d+)[-.]", p.name)
            if m:
                highest = max(highest, int(m.group(1)))
    for d in (desk.bells_pending, desk.bells_rung):
        if not d.exists():
            continue
        for p in d.glob("*.bell"):
            # `<YYYYmmdd>-<HHMMSS>-<id>.bell`: the leading digits are the clock, not an id.
            m = re.match(r"^\d{8}-\d{6}-(.+)\.bell$", p.name)
            if m and m.group(1).isdigit():
                highest = max(highest, int(m.group(1)))
    return f"{highest + 1:03d}"


def display_pattern(pattern: str) -> str:
    """A forbidden-command pattern as a person would type it (`build\\.sh` -> `build.sh`)."""
    return re.sub(r"\\(.)", r"\1", pattern)


def build_packet_text(
    *,
    id: str,
    lane: str,
    title: str,
    base: str,
    created: str,
    body: str,
    acceptance: Sequence[str],
    budget_minutes: int = 45,
    heavy: bool = False,
    links: Sequence[tuple] = (),
    forbidden: Sequence[str] = (),
    receipt: str = "",
) -> str:
    """Fixed head + the body verbatim + fixed tail."""
    title = " ".join(str(title).split())
    if not title:
        raise PacketError("a packet needs a title")
    if has_front_matter(body):
        raise PacketError(
            "the body opens its own '---' block; `bellhop dispatch` writes the front matter "
            "(id, lane, title, base, acceptance) and the body starts at its first heading"
        )
    if not acceptance:
        raise PacketError(
            "no acceptance items: the body needs an `## Acceptance` heading with one `- ` "
            "bullet per check a command can run"
        )

    head = ["---", f"id: {id}", f"lane: {lane}", f"title: {title}", f"base: {base}",
            f"created: {created}", f"budget_minutes: {budget_minutes}"]
    if heavy:
        # The watchdog may read this and start the worker with a stronger model or a
        # longer leash; a packet that says nothing gets the lane's default.
        head.append("effort: heavy")
    for key, value in links:
        head.append(f"{key}: {value}")
    head.append("acceptance:")
    head.extend(f"  - {item}" for item in acceptance)
    head.append("---")

    intro = [f"# {id} — {title}", "",
             f"Lane `{lane}`, base `{base}`, written by `bellhop dispatch` at {created}."]
    if heavy:
        intro.append("Effort **heavy**: this one is worth a stronger model or a longer leash than the lane's default.")
    for key, value in links:
        if key.endswith("_receipt"):
            intro.append(f"Its receipt is `{value}`.")
        elif key == "continues":
            intro.append(f"Continues packet **{value}**, which stopped without a receipt.")
        elif key == "returns":
            intro.append(f"Returns packet **{value}**: it reported, and the report was not accepted.")
    intro.append("")
    intro.append("Everything above this line is generated. Everything below it, until the last "
                 "section, is what the coordinator wrote.")

    tail = ["## How to report (fixed)", "",
            f"- Write the receipt to `{receipt}`, from `templates/receipt.md`.",
            "- For each acceptance item above: the command you ran and the output it printed. "
            "A sentence with no command behind it is not a check.",
            "- Do not write the receipt's machine header yourself. `bellhop receipt-header` "
            "computes it by running the commands; a header that was typed is a header nobody ran.",
            "- Commit on the lane branch in your own worktree. Do not push: the lane repository's "
            "`pre-push` hook refuses it, and the coordinator's merge is where a push belongs.",
            "- Stop and say so rather than working around a packet that contradicts itself, names "
            "a file that is not there, or asks for something you cannot check.",
            ]
    if forbidden:
        listed = ", ".join(f"`{display_pattern(p)}`" for p in forbidden)
        tail.append(f"- Do not run, and do not add to a script something that runs: {listed}. "
                    "Releasing and deploying are the coordinator's, with gates this packet does not carry.")

    return "\n".join(head) + "\n\n" + "\n".join(intro) + "\n\n" + body.strip("\n") + "\n\n" + "\n".join(tail) + "\n"
