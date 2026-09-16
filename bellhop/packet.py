from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from .util import BellhopError, read, sha256_file, slugify, ts, write

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
    sha256: str = ""

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
    raw = path.read_bytes()
    data, body = parse_front_matter(raw.decode("utf-8").replace("\r\n", "\n"))
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
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def verify_worker_packet(desk, packet: Packet) -> str:
    """Bind the parsed packet to the bytes the worker was actually dispatched.

    Missing historical evidence is not permission to hash today's packet and call
    it yesterday's input. The caller supplies the stage-specific diagnostic.
    """
    recovery = ("restore the original packet/worker metadata from Git history, or dispatch a new task; "
                "do not edit hashes or use --force to bypass this check")
    path = desk.receipts / f"{packet.id}.meta.json"
    try:
        meta = json.loads(read(path))
    except (OSError, ValueError) as exc:
        raise PacketError(f"worker packet evidence missing or unreadable ({path.name}); {recovery}") from exc
    expected = meta.get("packet_sha256") if isinstance(meta, dict) else None
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise PacketError(f"worker packet_sha256 missing or invalid ({path.name}); {recovery}")
    if not packet.sha256 or packet.sha256 != expected:
        raise PacketError(f"packet differs from the worker's recorded input; {recovery}")
    try:
        current = sha256_file(packet.path)
    except OSError as exc:
        raise PacketError(f"packet disappeared after it was read; {recovery}") from exc
    if current != packet.sha256:
        raise PacketError(f"packet changed after it was read; {recovery}")
    return expected


# Which body sections are carried -- into an acceptance pack, and into a `--return`
# packet -- is a convention, not a guess. A heading is carried when it reads exactly
# like one of these and never because it happens to start with a word that sounds
# like a prohibition. The first version matched any heading beginning "forbid", "out
# of bounds", "do not change"...; that is a script deciding what the coordinator
# meant, and a section that was carried and one that was overlooked looked the same
# in the packet. Two spellings are listed for the forbids because `## Do not touch`
# is what the packet template has always called that section.
FORBID_HEADINGS = ("forbids", "do not touch")
CONTEXT_HEADINGS = ("required context",)
# The names the acceptance pack builder imports. Same tuples, older spelling.
FORBID_WORDS = FORBID_HEADINGS
CONTEXT_WORDS = CONTEXT_HEADINGS


def packet_section(body: str, starts: tuple) -> tuple:
    """Extract named sections, including their nested headings, in source order.

    `starts` is a tuple of exact heading texts, compared case-insensitively. A heading
    that is not one of them stays in the prose, whatever it is about.
    """
    section, rest = [], []
    level = None
    fence = None
    for line in body.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        in_code = fence is not None
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                fence = None
        elif marker:
            fence = (marker[1][0], len(marker[1]))
        heading = None if in_code or marker else re.match(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", line)
        if heading:
            depth, title = len(heading.group(1)), heading.group(2).lower()
            if level is None or depth <= level:
                level = depth if title in starts else None
        (section if level is not None else rest).append(line)
    return "\n".join(section).strip(), "\n".join(rest).strip()


def standalone_sections(body: str, title: str) -> list:
    """Every `## <title>` section of the body, verbatim, in source order.

    `packet_section` takes its headings at any level and lifts them out of the prose,
    which is what carrying a "do not touch" paragraph into a pack needs. It is the
    wrong collector for a section a return inherits: a `### Rollout` written inside
    `## Required context` is part of that context, and lifting it out as a section of
    its own produces a packet nobody wrote. This one takes a level-2 heading whose
    title is exactly `title`, leaves the body alone, and runs the section to the next
    heading at the same level or above — so a `###` inside it is content, not a
    second section.

    Fences are respected: a heading inside a code block is a line of code.
    """
    wanted = title.strip().lower()
    sections: List[str] = []
    current: List[str] = []
    inside = False
    fence = None
    for line in body.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        in_code = fence is not None
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                fence = None
        elif marker:
            fence = (marker[1][0], len(marker[1]))
        heading = None if in_code or marker else re.match(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", line)
        if heading:
            depth, name = len(heading.group(1)), heading.group(2).strip().lower()
            if inside and depth <= 2:
                sections.append("\n".join(current).strip())
                current, inside = [], False
            if depth == 2 and name == wanted:
                inside = True
        if inside:
            current.append(line)
    if inside:
        sections.append("\n".join(current).strip())
    return sections


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
            # `receipt` is desk-relative, and a worker reads it inside its own worktree,
            # where that path is a different file -- or no file. The variable is the
            # same receipt as an absolute path, and the launcher exports it.
            f"- Write the receipt to `$BELLHOP_RECEIPT`: `{receipt}` **relative to the desk**, "
            "not to your working tree. Use the shape in `$BELLHOP_DESK/templates/receipt.md`.",
            "- For each acceptance item above: the command you ran and the output it printed. "
            "A sentence with no command behind it is not a check.",
            "- Do not write the receipt's machine header yourself. `bellhop receipt-header` "
            "computes it by running the commands; a header that was typed is a header nobody ran.",
            "- Commit the task's changes on the lane branch in your own worktree. A task that "
            "changed no product files makes no commit: an empty commit is not a record of work. "
            "Do not push: the lane repository's "
            "`pre-push` hook refuses it, and the coordinator's merge is where a push belongs.",
            "- Stop and say so rather than working around a packet that contradicts itself, names "
            "a file that is not there, or asks for something you cannot check.",
            ]
    if forbidden:
        listed = ", ".join(f"`{display_pattern(p)}`" for p in forbidden)
        tail.append(f"- Do not run, and do not add to a script something that runs: {listed}. "
                    "Releasing and deploying are the coordinator's, with gates this packet does not carry.")

    return "\n".join(head) + "\n\n" + "\n".join(intro) + "\n\n" + body.strip("\n") + "\n\n" + "\n".join(tail) + "\n"


# --------------------------------------------------------------------------
# rebinding a packet to the bytes its worker was dispatched
# --------------------------------------------------------------------------
BELL_EVIDENCE = "bell"
META_RE = re.compile(r"^(?P<id>[A-Za-z0-9_.-]+)\.meta\.json$")


def _bell_sha(desk, task_id: str):
    """The packet digest recorded when the bell was rung, and where it was found.

    This is evidence, and it is the only evidence there is: the bell was written at
    dispatch, before any worker ran, and it has been in git since. Hashing today's
    packet file instead would produce a binding that always verifies and never means
    anything — a packet edited after the fact and one nobody touched would agree.
    """
    for bell in sorted(desk.bells_pending.glob("*.bell")) if desk.bells_pending.exists() else []:
        try:
            data = json.loads(bell.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(data.get("id")) == str(task_id) and data.get("sha256"):
            return str(data["sha256"]), f"pending bell {bell.name}"
    for bell in sorted(desk.bells_rung.glob(f"*-{task_id}.bell")) if desk.bells_rung.exists() else []:
        try:
            data = json.loads(bell.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("sha256"):
            return str(data["sha256"]), f"rung bell {bell.name}"
    return None, ""


def worker_meta_paths(desk) -> List[Path]:
    """`receipts/<id>.meta.json`, and not the acceptance metadata beside it."""
    if not desk.receipts.exists():
        return []
    return [p for p in sorted(desk.receipts.glob("*.meta.json")) if META_RE.match(p.name)]


def rebind(desk, task_id: str = "", every: bool = False, dry_run: bool = False) -> List[dict]:
    """Write the dispatched packet's digest into a worker's metadata, once, from the bell.

    A desk that upgrades into the binding check (acceptance and closing both verify
    that the packet is the one the worker was given) finds that none of its in-flight
    tasks carry that digest, because nothing wrote it when they went out. Every one of
    them is refused, and the only ways forward were re-dispatching finished work or
    editing hashes by hand — which is the thing the check exists to prevent.

    So: one command, run once, that takes the digest from the bell and nowhere else,
    refuses where the bell and the packet on disk disagree, and leaves a line per task
    in `state/sentinel.log`. A rebind and a hand-edited hash must not look the same
    afterwards, and a task that was quietly skipped must not look like one that was
    rebound, so every task examined comes back with a row.
    """
    if bool(task_id) == bool(every):
        raise BellhopError("bellhop packet rebind takes one task id, or --all; not both and not neither")
    paths = worker_meta_paths(desk)
    if task_id:
        paths = [p for p in paths if META_RE.match(p.name).group("id") == str(task_id)]
        if not paths:
            raise BellhopError(f"no worker metadata at receipts/{task_id}.meta.json; "
                               f"there is nothing to rebind for {task_id}")
    rows: List[dict] = []
    for path in paths:
        tid = META_RE.match(path.name).group("id")
        row = {"task": tid, "status": "", "note": ""}
        rows.append(row)
        try:
            meta = json.loads(read(path))
            if not isinstance(meta, dict):
                raise ValueError("not an object")
        except (OSError, ValueError) as exc:
            row.update(status="refused", note=f"{path.name} is unreadable ({exc})")
            continue
        current = meta.get("packet_sha256")
        if isinstance(current, str) and re.fullmatch(r"[0-9a-f]{64}", current):
            row.update(status="already", note=f"bound to {current[:12]}; left alone")
            continue
        recorded, where = _bell_sha(desk, tid)
        if not recorded:
            row.update(status="no-evidence",
                       note="no bell records this packet's digest; recover it from git history "
                            "or dispatch the work again, and do not write a hash by hand")
            continue
        hits = sorted(desk.inbox.rglob(f"{tid}-*.md")) if desk.inbox.exists() else []
        if len(hits) != 1:
            row.update(status="refused", note=f"{len(hits)} packet(s) match id {tid} in the inbox; exactly 1 is required")
            continue
        try:
            on_disk = sha256_file(hits[0])
        except OSError as exc:
            row.update(status="refused", note=f"cannot read {hits[0].name}: {exc}")
            continue
        if on_disk != recorded:
            row.update(status="refused",
                       note=f"the packet on disk ({on_disk[:12]}) is not the one the {where} recorded "
                            f"({recorded[:12]}); that is a changed packet, not a missing binding")
            continue
        row.update(status="would rebind" if dry_run else "rebound",
                   note=f"{recorded[:12]} from the {where}")
        if dry_run:
            continue
        meta["packet_sha256"] = recorded
        meta["packet_sha256_source"] = where
        meta["packet_rebound_at"] = ts()
        write(path, json.dumps(meta, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
        _rebind_line(desk, f"REBIND {tid} packet_sha256 {recorded[:12]} from the {where}")
    return rows


def _rebind_line(desk, text: str) -> None:
    try:
        p = desk.path("state", "sentinel.log")
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts()} {text}\n")
    except OSError:
        pass
