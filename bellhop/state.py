"""The single write entry for the desk's state files.

Every change to a state file goes through this module. The coordinator never
opens `state/*.md` in an editor and never writes a one-off script for it.

Four rules hold for every write here:

1. **Previous version first.** Before a file is overwritten, the old bytes are
   copied to `state/.previous/<name>.<UTC timestamp>`. A production desk lost
   ledger rows to one careless overwrite; the copy is what makes
   that recoverable.
2. **Anchored replacement, exactly one match.** A replacement that matches zero
   lines or several lines is refused and says how many matched. An unanchored
   regex with no match count once rewrote a card's own format prose.
3. **Echo what changed.** Every successful write prints the changed line, or the
   new entry's path, first line and byte count. A line that was written correctly
   and a line that was silently truncated look identical in a message that only
   says "done".
4. **Append-only stays append-only.** Ledger entries, the self-catch book, the
   close-out register, retired hot rules and decisions are only ever added to.

Exit codes (`Result` for success, exceptions carry the rest):

* 0 - written
* 2 - refused by a precondition; re-running with the same input refuses again
* 3 - malformed input (the argument itself is wrong shape)
* 4 - environment, missing file or IO

The CLI is a thin layer over the functions here; other modules call the
functions directly.
"""
from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import ledger as ledger_mod
from .config import Desk
from .util import append, read, slugify, today, ts, write

OK = 0
REFUSED = 2
MALFORMED = 3
ENVIRONMENT = 4

ECHO_MAX_BYTES = 300
PLACEHOLDER = "- none"
DATA = "- "


# --------------------------------------------------------------------------
# errors and results
# --------------------------------------------------------------------------
class StateError(Exception):
    """Base for every refusal. `code` is the process exit code."""

    code = ENVIRONMENT
    label = "error"

    def __init__(self, message: str, extra: Sequence[str] = ()):
        super().__init__(message)
        self.extra = list(extra)

    def render(self) -> str:
        out = [f"{self.label}: {self}"]
        out.extend("   " + echo(e) for e in self.extra)
        return "\n".join(out)


class Refused(StateError):
    """A precondition on the file said no. Re-running would refuse again."""

    code = REFUSED
    label = "refused"


class Malformed(StateError):
    """The input itself is the wrong shape."""

    code = MALFORMED
    label = "refused"


class Environment(StateError):
    """Missing file, unreadable desk, IO."""

    code = ENVIRONMENT
    label = "error"


class WrapupStopped(StateError):
    """A wrap-up stopped at a step. Carries the steps that did run."""

    def __init__(self, step: str, cause: StateError, done: Sequence["Result"]):
        super().__init__(f"step {step}: {cause}", cause.extra)
        self.code = cause.code
        self.label = cause.label
        self.step = step
        self.done = list(done)


def echo(line: str) -> str:
    """A line as written, capped so one long row cannot flood the screen."""
    raw = line.encode("utf-8")
    if len(raw) <= ECHO_MAX_BYTES:
        return line
    return raw[:ECHO_MAX_BYTES].decode("utf-8", "ignore") + " ..."


@dataclass
class Result:
    note: str
    echo: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    paths: List[Path] = field(default_factory=list)

    def render(self) -> str:
        out = [self.note]
        out.extend("   " + echo(line) for line in self.echo)
        out.extend(f"warning: {w}" for w in self.warnings)
        return "\n".join(out)


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------
CARD = "state/next.md"
INFLIGHT = "state/in-flight.md"
DEBTS = "state/debts.md"
DEBTS_SETTLED = "state/debts-settled.md"
TODO = "state/human-todo.md"
OWN = "state/own-list.md"
SELFCATCH = "state/self-catch.md"
SELFCATCH_HOOKS = "state/self-catch-hooks.md"
CLOSINGS = "state/closings.md"
CLOSINGS_READ = "state/.closings-read"
CLAUDE_MD = ".claude/CLAUDE.md"
HOT_RETIRED = "rules/hot-rules-retired.md"

HEADERS = {
    INFLIGHT: (
        "# In flight (who is doing what right now)\n"
        "(one line per running item: `- <what> <task id>`; `bellhop state inflight` is the only writer;"
        " the rules for the line live in rules/card.md)\n"
        "\n"
        "- none\n"
    ),
    DEBTS: (
        "# Debts (what is owed, one line each)\n"
        "(one line per debt: `- <who is owed> <what> <by when>`; settle through"
        " `bellhop state debts settle`, never by deleting; settled rows land in state/debts-settled.md)\n"
        "\n"
        "- none\n"
    ),
    DEBTS_SETTLED: (
        "# Debts settled (append-only; written by `bellhop state debts settle`)\n"
        "(one line per settled debt: `- <the debt as it stood> - settled <time>`)\n"
        "\n"
    ),
    OWN: (
        "# Own list (the coordinator's own work, as distinct from what was dispatched)\n"
        "(one line per item: `- [<state>] <one sentence> -> next: <one sentence>`, state = doing/waiting/held;"
        " dispatched work belongs in state/in-flight.md; more than seven open lines is the signal to stop and clean up)\n"
        "\n"
        "- none\n"
    ),
    SELFCATCH: (
        "# Self-catch (append-only; written only by `bellhop state selfcatch add`)\n"
        "(one line per catch: `- <time> [<root id>] <kind> | <cause>`, kind = judgement/situational/oneoff."
        " Hook hits are NOT recorded here - they go to state/self-catch-hooks.md, so they cannot drown the"
        " human counts the hot-rule threshold reads. `bellhop state selfcatch table` prints the counts.)\n"
        "\n"
    ),
    SELFCATCH_HOOKS: (
        "# Self-catch hook hits (append-only; written by `bellhop state selfcatch hook-hit`)\n"
        "(one line per hit: `- <time> [<root id>]`; counted separately from the human catches)\n"
        "\n"
    ),
    CLOSINGS: (
        "# Close-outs (append-only; one line per closed task)\n"
        "(one line per close-out: `- <time> [<task id>] <one line, at most 200 characters>`;"
        " `bellhop state closings unread` prints the ones the coordinator has not read yet)\n"
        "\n"
    ),
    HOT_RETIRED: (
        "# Hot rules retired (append-only; written by `bellhop state hotrules retire`)\n"
        "(one line each: `<the rule as it stood> | count <n> | reason <why> | <date>`;"
        " a rule that stops earning its slot leaves, and the register is where its history goes)\n"
        "\n"
    ),
    TODO: (
        "# Only the principal can do these\n"
        "(one line each, under Open: `- <what> - why only you: <reason>`;"
        " a line with no `why only you:` clause is refused. Settled lines move to Done, never deleted.)\n"
        "\n## Open\n\n- none\n\n## Done\n"
    ),
}

HOT_START = "<!-- hot-rules start -->"
HOT_END = "<!-- hot-rules end -->"

CARD_KEYS = ("ready", "next", "lane", "reason", "queue", "refreshed", "by")
# CLI flag -> the key as it is written in the file. The file keys are the ones
# card.py, sentinel.py and hooks.py already read; the flags are the desk's words.
CARD_FLAGS = {
    "dispatchable": "ready",
    "next": "next",
    "lane": "lane",
    "reason": "reason",
    "pool": "queue",
}

SELFCATCH_KINDS = ("judgement", "situational", "oneoff")
CAUSE_MIN_CHARS = 12
HOT_BANNED_WORDS = ("incident", "failure")


def cfg_int(desk: Desk, key: str, default: int) -> int:
    try:
        return int(desk.cfg.get(key, default))
    except (TypeError, ValueError):
        raise Malformed(f"config key {key} is not a number: {desk.cfg.get(key)!r}")


# --------------------------------------------------------------------------
# general machinery
# --------------------------------------------------------------------------
def utc_stamp(when: Optional[datetime] = None) -> str:
    return (when or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def previous_dir(desk: Desk) -> Path:
    return desk.state / ".previous"


def backup(desk: Desk, path: Path) -> Path:
    """Copy the current bytes aside before they are overwritten. Rule 1."""
    path = Path(path)
    if not path.exists():
        raise Environment(f"cannot back up a file that is not there: {path}")
    keep = cfg_int(desk, "previous_versions_keep", 30)
    folder = previous_dir(desk)
    folder.mkdir(parents=True, exist_ok=True)
    stem = path.name
    dst = folder / f"{stem}.{utc_stamp()}"
    n = 1
    while dst.exists():
        dst = folder / f"{stem}.{utc_stamp()}-{n}"
        n += 1
    try:
        shutil.copy2(path, dst)
    except OSError as exc:
        raise Environment(f"could not write the previous version: {exc}")
    if keep > 0:
        mine = sorted(p for p in folder.iterdir() if p.name.startswith(stem + "."))
        for old in mine[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass
    return dst


def _path(desk: Desk, rel: str, create: bool = False) -> Path:
    p = desk.path(rel)
    if not p.exists():
        if not create:
            raise Environment(f"missing state file: {rel} (run `bellhop init` or create it from the template)")
        header = HEADERS.get(rel)
        if header is None:
            raise Environment(f"missing state file and no template header for it: {rel}")
        write(p, header)
    return p


def _lines(path: Path) -> List[str]:
    try:
        return read(path).split("\n")
    except OSError as exc:
        raise Environment(f"could not read {path}: {exc}")


def _save(path: Path, lines: Sequence[str]) -> None:
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    try:
        write(path, text)
    except OSError as exc:
        raise Environment(f"could not write {path}: {exc}")


def _append(path: Path, text: str) -> None:
    try:
        append(path, text)
    except OSError as exc:
        raise Environment(f"could not append to {path}: {exc}")


def data_lines(lines: Sequence[str]) -> List[Tuple[int, str]]:
    return [(i, l) for i, l in enumerate(lines) if l.startswith(DATA) and l.strip() != PLACEHOLDER]


def match_one(lines: Sequence[str], predicate: Callable[[str], bool], what: str) -> int:
    """Anchored replacement, rule 2: exactly one line or nothing happens."""
    hits = [(i, l) for i, l in data_lines(lines) if predicate(l)]
    if len(hits) != 1:
        raise Refused(
            f"{len(hits)} line(s) matched {what}; exactly 1 is required, so nothing was written",
            [f"line {i + 1}: {l}" for i, l in hits],
        )
    return hits[0][0]


def token(task_id: str) -> Callable[[str], bool]:
    """A task id matches as a whole token: `017` does not match `2017` or `0170`."""
    pattern = re.compile(r"(?<![0-9A-Za-z])" + re.escape(task_id) + r"(?![0-9A-Za-z])")
    return lambda line: bool(pattern.search(line))


def contains(needle: str) -> Callable[[str], bool]:
    return lambda line: needle in line


def insert_data_line(lines: List[str], new_line: str, section: Optional[str] = None) -> int:
    """Put a data line after the last one (of the section), dropping `- none`."""
    start, end = (0, len(lines)) if section is None else section_bounds(lines, section)
    for i in range(end - 1, start - 1, -1):
        if lines[i].strip() == PLACEHOLDER:
            del lines[i]
            end -= 1
    here = [i for i, l in enumerate(lines[start:end], start) if l.startswith(DATA)]
    if here:
        at = here[-1] + 1
    else:
        body = [i for i, l in enumerate(lines[start:end], start) if l.strip()]
        at = (body[-1] + 1) if body else end
    lines.insert(at, new_line)
    return at


def restore_placeholder(lines: List[str], section: Optional[str] = None) -> None:
    """A file with no rows says `- none`; an empty section and a missing file must not look alike."""
    start, end = (0, len(lines)) if section is None else section_bounds(lines, section)
    if any(l.startswith(DATA) for l in lines[start:end]):
        return
    body = [i for i, l in enumerate(lines[start:end], start) if l.strip()]
    lines.insert((body[-1] + 1) if body else end, PLACEHOLDER)


def section_bounds(lines: Sequence[str], heading: str) -> Tuple[int, int]:
    start = None
    for i, l in enumerate(lines):
        if l.strip().lower() == heading.strip().lower():
            start = i
            break
    if start is None:
        raise Environment(f"section {heading!r} is not in the file")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return start + 1, end


def edit(desk: Desk, rel: str, mutate: Callable[[List[str]], Result], create: bool = False) -> Result:
    """Read, let `mutate` raise or change the lines, back up, write. Rules 1 and 3."""
    path = _path(desk, rel, create=create)
    lines = _lines(path)
    result = mutate(lines)          # every refusal happens here, before any write
    saved = backup(desk, path)
    _save(path, lines)
    result.note += f" [previous version: {saved.name}]"
    result.paths.append(path)
    return result


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------
def compress_entry(text: str) -> str:
    """Deterministic squeeze, in a fixed order, before an entry is split."""
    out: List[str] = []
    for raw in text.split("\n"):
        line = re.sub(r" {2,}", " ", raw.rstrip())
        if not line and out and not out[-1]:
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    while out and not out[0]:
        out.pop(0)
    return "\n".join(out)


def _entry_title_body(text: str) -> Tuple[str, str]:
    lines = [l for l in text.split("\n")]
    title = ""
    rest_at = 0
    for i, l in enumerate(lines):
        if l.strip():
            title = l.strip().lstrip("#").strip()
            rest_at = i + 1
            break
    if not title:
        raise Malformed("the entry file is empty; a ledger entry needs a title line")
    return title, "\n".join(lines[rest_at:]).strip("\n")


def _render_entry(title: str, body: str, refs: Sequence[str], when: str) -> str:
    ref_text = ", ".join(r for r in refs if r) or "none"
    return f"# {title}\n\n- time: {when}\n- refs: {ref_text}\n\n{body}\n"


def ledger_add(desk: Desk, entry_file, refs: Sequence[str] = (), day: Optional[str] = None) -> Result:
    """Add one ledger entry. The number comes from the last existing entry, never from the caller."""
    if entry_file == "-":
        text = sys.stdin.read()
    else:
        p = Path(entry_file)
        if not p.exists():
            raise Environment(f"entry file not found: {entry_file}")
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise Environment(f"could not read {entry_file}: {exc}")
    if not text.strip():
        raise Malformed("the entry file is empty; a ledger entry needs a title line")

    cap = cfg_int(desk, "ledger_entry_bytes", 800)
    day = day or today()
    when = ts()
    title, body = _entry_title_body(text)

    whole = _render_entry(title, body, refs, when)
    notes: List[str] = []
    if len(whole.encode("utf-8")) > cap:
        body = compress_entry(body)
        whole = _render_entry(title, body, refs, when)
        notes.append(f"compressed to {len(whole.encode('utf-8'))} bytes")

    parts: List[Tuple[str, str]] = []
    if len(whole.encode("utf-8")) <= cap:
        parts.append((title, body))
    else:
        body_lines = body.split("\n")
        keep = 0
        for k in range(1, len(body_lines) + 1):
            trial = _render_entry(f"{title} (part 1 of 2)", "\n".join(body_lines[:k]), refs, when)
            if len(trial.encode("utf-8")) > cap:
                break
            keep = k
        keep = max(keep, 1)
        if keep >= len(body_lines):
            keep = len(body_lines) - 1 if len(body_lines) > 1 else 1
        parts.append((f"{title} (part 1 of 2)", "\n".join(body_lines[:keep])))
        parts.append((f"{title} (part 2 of 2)", "\n".join(body_lines[keep:])))
        notes.append("split at a line boundary")

    result = Result(note="")
    written: List[str] = []
    for title_i, body_i in parts:
        seq = ledger_mod.next_seq(desk, day)
        path = desk.ledger / day / f"{seq:03d}-{slugify(title_i)}.md"
        rendered = _render_entry(title_i, body_i, refs, when)
        try:
            write(path, rendered)
        except OSError as exc:
            raise Environment(f"could not write {path}: {exc}")
        size = len(rendered.encode("utf-8"))
        result.paths.append(path)
        written.append(f"{seq:03d}")
        result.echo.append(f"{path.relative_to(desk.root)}  |  # {title_i}  |  {size} bytes")
        if size > cap:
            result.warnings.append(
                f"{path.name} is {size} bytes, over the {cap}-byte cap; written over the cap rather than truncated"
            )
    head = "ledger entry " + " and ".join(written)
    result.note = head + (f" ({'; '.join(notes)})" if notes else "")
    return result


# --------------------------------------------------------------------------
# card
# --------------------------------------------------------------------------
def card_set(desk: Desk, **flags) -> Result:
    """Set only the given keys, one anchored `^key: ` replacement each."""
    given = {k: v for k, v in flags.items() if v is not None}
    unknown = sorted(set(given) - set(CARD_FLAGS))
    if unknown:
        raise Malformed(f"unknown card key(s): {', '.join(unknown)} (known: {', '.join(sorted(CARD_FLAGS))})")

    values: Dict[str, str] = {}
    for flag, value in given.items():
        value = str(value)
        if flag == "dispatchable":
            if value.lower() not in ("yes", "no"):
                raise Malformed("--dispatchable takes yes or no")
            value = value.lower()
        if flag == "pool":
            if value.strip().lower() in ("none", "empty"):
                value = "0 (none)"
            elif not re.match(r"^\d", value.strip()):
                raise Malformed(
                    "--pool must start with a digit (readers take the leading number), or be the word `none`"
                )
        values[CARD_FLAGS[flag]] = value

    def mutate(lines: List[str]) -> Result:
        current = {}
        for line in lines:
            m = re.match(r"^([a-z_]+): ?(.*)$", line)
            if m and m.group(1) in CARD_KEYS:
                current[m.group(1)] = m.group(2).strip()
        current.update({k: v for k, v in values.items()})
        if current.get("ready", "no").lower() == "no" and not current.get("reason"):
            raise Refused("a card with ready: no must say in `reason` what we are waiting on and who wakes us")
        if current.get("ready", "no").lower() == "yes" and not current.get("next"):
            raise Refused("a card with ready: yes must name `next`")

        stamped = dict(values)
        stamped["refreshed"] = ts()
        stamped["by"] = "bellhop state"
        for key, value in stamped.items():
            pattern = re.compile(r"^" + re.escape(key) + r": ?.*$")
            hits = [i for i, l in enumerate(lines) if pattern.match(l)]
            if len(hits) != 1:
                raise Refused(
                    f"the card has {len(hits)} `{key}: ` line(s); exactly 1 is required, so nothing was written",
                    [f"line {i + 1}: {lines[i]}" for i in hits],
                )
            lines[hits[0]] = f"{key}: {value}"
        shown = [l for l in lines if re.match(r"^([a-z_]+): ", l) and l.split(":", 1)[0] in CARD_KEYS]
        return Result(note=f"card set: {', '.join(sorted(stamped))}", echo=shown)

    return edit(desk, CARD, mutate)


def read_card_keys(desk: Desk) -> Dict[str, str]:
    path = _path(desk, CARD)
    out = {}
    for line in _lines(path):
        m = re.match(r"^([a-z_]+): ?(.*)$", line)
        if m and m.group(1) in CARD_KEYS:
            out[m.group(1)] = m.group(2).strip()
    return out


# --------------------------------------------------------------------------
# in flight
# --------------------------------------------------------------------------
def _fit(text: str, cap: int) -> Tuple[str, bool]:
    """Truncate rather than refuse: an in-flight row is a pointer, not the record."""
    if len(text) <= cap:
        return text, False
    return text[: max(cap - 4, 1)].rstrip() + " ...", True


def inflight_start(desk: Desk, text: str) -> Result:
    text = (text or "").strip()
    if not text:
        raise Malformed("--text is empty; an in-flight line says who is doing what")
    cap = cfg_int(desk, "inflight_line_chars", 160)

    def mutate(lines: List[str]) -> Result:
        body, cut = _fit(DATA + text, cap)
        insert_data_line(lines, body)
        r = Result(note="in flight +1", echo=[body])
        if cut:
            r.warnings.append(f"line truncated to the {cap}-character cap")
        return r

    return edit(desk, INFLIGHT, mutate, create=True)


def inflight_finish(desk: Desk, task: str) -> Result:
    if not (task or "").strip():
        raise Malformed("--task is empty")

    def mutate(lines: List[str]) -> Result:
        i = match_one(lines, token(task.strip()), f"task {task}")
        gone = lines.pop(i)
        restore_placeholder(lines)
        return Result(note=f"in flight -1 (task {task}, line {i + 1})", echo=[gone])

    return edit(desk, INFLIGHT, mutate, create=True)


def inflight_tag(desk: Desk, task: str, tag: str) -> Result:
    if not (task or "").strip():
        raise Malformed("--task is empty")
    tag = (tag or "").strip()
    if not tag:
        raise Malformed("--tag is empty")
    cap = cfg_int(desk, "inflight_line_chars", 160)
    marker = f"[{tag}]"

    def mutate(lines: List[str]) -> Result:
        i = match_one(lines, token(task.strip()), f"task {task}")
        if marker in lines[i]:
            raise Refused(f"that line already carries {marker}", [f"line {i + 1}: {lines[i]}"])
        room = cap - len(marker) - 1
        head, cut = _fit(lines[i], max(room, 1))
        lines[i] = f"{head} {marker}"
        r = Result(note=f"in flight tagged {marker} (task {task}, line {i + 1})", echo=[lines[i]])
        if cut:
            r.warnings.append(f"line truncated to the {cap}-character cap to make room for the tag")
        return r

    return edit(desk, INFLIGHT, mutate, create=True)


# --------------------------------------------------------------------------
# debts
# --------------------------------------------------------------------------
def debts_add(desk: Desk, text: str) -> Result:
    text = (text or "").strip()
    if not text:
        raise Malformed("--text is empty; a debt line says who is owed what")
    line = text if text.startswith(DATA) else DATA + text

    def mutate(lines: List[str]) -> Result:
        if any(l.strip() == line.strip() for l in lines):
            raise Refused("that debt line is already on the file", [line])
        insert_data_line(lines, line)
        return Result(note="debts +1", echo=[line])

    return edit(desk, DEBTS, mutate, create=True)


def debts_settle(desk: Desk, match: str, keep: bool = False) -> Result:
    if not (match or "").strip():
        raise Malformed("--match is empty")
    when = ts()

    def mutate(lines: List[str]) -> Result:
        i = match_one(lines, contains(match), f"{match!r}")
        original = lines[i]
        if keep:
            lines[i] = f"- ~~{original[len(DATA):]}~~ settled {when}"
            shown = lines[i]
            how = "struck in place"
        else:
            del lines[i]
            restore_placeholder(lines)
            shown = original
            how = "moved out"
        archived = f"- {original[len(DATA):]} - settled {when}\n"
        _append(_path(desk, DEBTS_SETTLED, create=True), archived)
        return Result(
            note=f"debt settled, {how} (line {i + 1}); recorded in {DEBTS_SETTLED}",
            echo=[shown, archived.rstrip("\n")],
        )

    return edit(desk, DEBTS, mutate, create=True)


def debts_set(desk: Desk, match: str, text: str) -> Result:
    """In-place anchored replacement, for a baseline row that must keep its position."""
    if not (match or "").strip():
        raise Malformed("--match is empty")
    text = (text or "").strip()
    if not text:
        raise Malformed("--text is empty")
    line = text if text.startswith(DATA) else DATA + text

    def mutate(lines: List[str]) -> Result:
        i = match_one(lines, contains(match), f"{match!r}")
        before = lines[i]
        lines[i] = line
        return Result(note=f"debt line {i + 1} replaced in place", echo=[f"was: {before}", f"now: {line}"])

    return edit(desk, DEBTS, mutate, create=True)


# --------------------------------------------------------------------------
# human todo
# --------------------------------------------------------------------------
WHY_ONLY_YOU = "why only you:"


def todo_add(desk: Desk, text: str) -> Result:
    text = (text or "").strip()
    if not text:
        raise Malformed("--text is empty")
    if WHY_ONLY_YOU not in text.lower():
        raise Malformed(
            f"a principal-only line must carry a `{WHY_ONLY_YOU}` clause; without it the list fills up with"
            " work the coordinator could have done"
        )
    line = text if text.startswith(DATA) else DATA + text

    def mutate(lines: List[str]) -> Result:
        insert_data_line(lines, line, section="## Open")
        return Result(note="human todo +1 (Open)", echo=[line])

    return edit(desk, TODO, mutate, create=True)


def todo_settle(desk: Desk, match: str) -> Result:
    if not (match or "").strip():
        raise Malformed("--match is empty")
    day = today()

    def mutate(lines: List[str]) -> Result:
        start, end = section_bounds(lines, "## Open")
        hits = [(i, l) for i, l in data_lines(lines) if start <= i < end and match in l]
        if len(hits) != 1:
            raise Refused(
                f"{len(hits)} open line(s) matched {match!r}; exactly 1 is required, so nothing was written",
                [f"line {i + 1}: {l}" for i, l in hits],
            )
        i, original = hits[0]
        del lines[i]
        restore_placeholder(lines, section="## Open")
        done = f"- {original[len(DATA):]} - done {day}"
        insert_data_line(lines, done, section="## Done")
        return Result(note=f"human todo settled (was line {i + 1})", echo=[done])

    return edit(desk, TODO, mutate, create=True)


# --------------------------------------------------------------------------
# own list
# --------------------------------------------------------------------------
def own_add(desk: Desk, text: str) -> Result:
    text = (text or "").strip()
    if not text:
        raise Malformed("--text is empty")
    line = text if text.startswith(DATA) else DATA + text
    warn_at = cfg_int(desk, "own_list_warn", 7)

    def mutate(lines: List[str]) -> Result:
        insert_data_line(lines, line)
        open_rows = len(data_lines(lines))
        r = Result(note=f"own list +1 ({open_rows} open)", echo=[line])
        if open_rows > warn_at:
            r.warnings.append(
                f"{open_rows} open lines, over {warn_at}: crossing the cap is itself the signal to stop and clean up"
            )
        return r

    return edit(desk, OWN, mutate, create=True)


def own_settle(desk: Desk, match: str) -> Result:
    if not (match or "").strip():
        raise Malformed("--match is empty")

    def mutate(lines: List[str]) -> Result:
        i = match_one(lines, contains(match), f"{match!r}")
        gone = lines.pop(i)
        restore_placeholder(lines)
        return Result(note=f"own list -1 (line {i + 1}, {len(data_lines(lines))} open)", echo=[gone])

    return edit(desk, OWN, mutate, create=True)


def own_list(desk: Desk) -> Result:
    path = _path(desk, OWN, create=True)
    rows = [l for _i, l in data_lines(_lines(path))]
    warn_at = cfg_int(desk, "own_list_warn", 7)
    r = Result(note=f"own list: {len(rows)} open", echo=rows or ["- none"])
    if len(rows) > warn_at:
        r.warnings.append(f"{len(rows)} open lines, over {warn_at}")
    return r


# --------------------------------------------------------------------------
# self-catch
# --------------------------------------------------------------------------
ROW = re.compile(r"^- (?P<time>\S+) \[(?P<root>[^\]]+)\] (?P<kind>\w+) \| (?P<cause>.*)$")
HOOK_ROW = re.compile(r"^- (?P<time>\S+) \[(?P<root>[^\]]+)\]\s*$")


def selfcatch_add(desk: Desk, root: str, kind: str, cause: str) -> Result:
    root = (root or "").strip()
    if not root:
        raise Malformed("--root is empty")
    kind = (kind or "").strip().lower()
    if kind not in SELFCATCH_KINDS:
        raise Malformed(f"--kind must be one of {'/'.join(SELFCATCH_KINDS)}, not {kind!r}")
    cause = (cause or "").strip()
    # The placeholder bug: a row whose cause repeats the root id, or is a stub,
    # counts towards a promotion threshold while saying nothing about the mistake.
    if not cause:
        raise Malformed("--cause is empty; a catch with no cause counts towards a threshold and teaches nothing")
    if cause.lower() == root.lower():
        raise Malformed("--cause repeats the root id; that is a placeholder, not a cause")
    if len(cause) < CAUSE_MIN_CHARS:
        raise Malformed(f"--cause is {len(cause)} characters, under {CAUSE_MIN_CHARS}; write the sentence")
    cause = cause.replace("\n", " ").replace("|", "/")
    line = f"- {ts()} [{root}] {kind} | {cause}"
    _append(_path(desk, SELFCATCH, create=True), line + "\n")
    return Result(note=f"self-catch +1 ({root}, {kind})", echo=[line], paths=[desk.path(SELFCATCH)])


def selfcatch_hook_hit(desk: Desk, root: str) -> Result:
    root = (root or "").strip()
    if not root:
        raise Malformed("--root is empty")
    line = f"- {ts()} [{root}]"
    _append(_path(desk, SELFCATCH_HOOKS, create=True), line + "\n")
    return Result(
        note=f"hook hit +1 ({root}); the human count is untouched",
        echo=[line],
        paths=[desk.path(SELFCATCH_HOOKS)],
    )


def selfcatch_rows(desk: Desk) -> List[Dict[str, str]]:
    p = desk.path(SELFCATCH)
    if not p.exists():
        return []
    out = []
    for line in _lines(p):
        m = ROW.match(line)
        if m:
            out.append(m.groupdict())
    return out


def selfcatch_hook_rows(desk: Desk) -> List[Dict[str, str]]:
    p = desk.path(SELFCATCH_HOOKS)
    if not p.exists():
        return []
    out = []
    for line in _lines(p):
        m = HOOK_ROW.match(line)
        if m:
            out.append(m.groupdict())
    return out


def selfcatch_counts(desk: Desk) -> Dict[str, Dict[str, object]]:
    """Counts recomputed from the rows. A remembered number is not a measured one."""
    table: Dict[str, Dict[str, object]] = {}
    for row in selfcatch_rows(desk):
        e = table.setdefault(row["root"], {"count": 0, "hook_hits": 0, "last": "-", "kinds": set()})
        e["count"] = int(e["count"]) + 1
        e["last"] = max(str(e["last"]), row["time"]) if e["last"] != "-" else row["time"]
        e["kinds"].add(row["kind"])
    for row in selfcatch_hook_rows(desk):
        e = table.setdefault(row["root"], {"count": 0, "hook_hits": 0, "last": "-", "kinds": set()})
        e["hook_hits"] = int(e["hook_hits"]) + 1
        e["last"] = max(str(e["last"]), row["time"]) if e["last"] != "-" else row["time"]
    promoted = {r for r, _ in hot_rule_rows(desk)}
    retired = retired_roots(desk)
    for root, e in table.items():
        e["promoted"] = "yes" if root in promoted else ("retired" if root in retired else "no")
    return table


def selfcatch_table(desk: Desk) -> Result:
    table = selfcatch_counts(desk)
    header = "| root | count | hook hits | last seen | promoted |"
    rows = [header, "|---|---|---|---|---|"]
    for root in sorted(table):
        e = table[root]
        rows.append(f"| {root} | {e['count']} | {e['hook_hits']} | {e['last']} | {e['promoted']} |")
    if not table:
        rows.append("| (no rows yet) | 0 | 0 | - | - |")
    return Result(note=f"self-catch: {len(table)} root cause(s), recomputed from the rows", echo=rows)


# --------------------------------------------------------------------------
# hot rules
# --------------------------------------------------------------------------
def _hot_segment(desk: Desk) -> Tuple[Path, List[str], int, int]:
    path = _path(desk, CLAUDE_MD)
    lines = _lines(path)
    starts = [i for i, l in enumerate(lines) if l.strip() == HOT_START]
    ends = [i for i, l in enumerate(lines) if l.strip() == HOT_END]
    if len(starts) != 1 or len(ends) != 1:
        raise Environment(
            f"{CLAUDE_MD} needs exactly one `{HOT_START}` and one `{HOT_END}`;"
            f" found {len(starts)} and {len(ends)}"
        )
    if ends[0] < starts[0]:
        raise Environment(f"{CLAUDE_MD}: the hot-rules end marker comes before the start marker")
    return path, lines, starts[0], ends[0]


HOT_LINE = re.compile(r"^- \[(?P<root>[^\]]+)\] (?P<text>.*)$")


def _hot_root(line: str) -> Optional[str]:
    m = HOT_LINE.match(line)
    return m.group("root") if m else None


def hot_rule_rows(desk: Desk) -> List[Tuple[str, str]]:
    try:
        _p, lines, start, end = _hot_segment(desk)
    except StateError:
        return []
    out = []
    for line in lines[start + 1 : end]:
        m = HOT_LINE.match(line)
        if m:
            out.append((m.group("root"), m.group("text")))
    return out


def hot_rule_lines(desk: Desk) -> List[str]:
    try:
        _p, lines, start, end = _hot_segment(desk)
    except StateError:
        return []
    return [l for l in lines[start + 1 : end] if l.startswith(DATA)]


def retired_roots(desk: Desk) -> set:
    p = desk.path(HOT_RETIRED)
    if not p.exists():
        return set()
    out = set()
    for line in _lines(p):
        m = re.match(r"^- \[([^\]]+)\]", line)
        if m:
            out.add(m.group(1))
    return out


def hotrules_promote(desk: Desk, root: str, text: str) -> Result:
    root = (root or "").strip()
    if not root:
        raise Malformed("--root is empty")
    text = (text or "").strip()
    if not text:
        raise Malformed("--text is empty")

    # Shape: a hot rule is an instruction for next time, not a record of last time.
    bad = []
    if re.search(r"\d{4}-\d{2}-\d{2}|(?<!\d)\d{1,2}-\d{2}(?!\d)", text):
        bad.append("it contains a date")
    if re.search(r"#\d+|(?<![0-9A-Za-z])\d{3,}(?![0-9A-Za-z])|\btask \d+\b", text, re.I):
        bad.append("it contains a task id")
    hits = [w for w in HOT_BANNED_WORDS if w in text.lower()]
    if hits:
        bad.append("it contains " + "/".join(hits))
    limit = cfg_int(desk, "hot_rule_line_chars", 80)
    if len(text) > limit:
        bad.append(f"it is {len(text)} characters, over {limit}")
    if bad:
        raise Malformed(
            "a hot rule is a rule, not a case record: " + "; ".join(bad) + " (the case belongs in the self-catch book)"
        )

    threshold = cfg_int(desk, "hot_rule_promote", 3)
    max_lines = cfg_int(desk, "hot_rule_max_lines", 10)
    counts = selfcatch_counts(desk)
    human = int(counts.get(root, {}).get("count", 0))
    if human < threshold:
        raise Refused(
            f"root {root} has {human} human catch(es), under the promotion threshold of {threshold}"
            " (hook hits do not count)"
        )

    path, lines, start, end = _hot_segment(desk)
    rows = [i for i in range(start + 1, end) if lines[i].startswith(DATA)]
    if len(rows) >= max_lines:
        raise Refused(
            f"the hot-rules segment already holds {len(rows)} line(s), the maximum is {max_lines};"
            f" retire one first (`bellhop state hotrules retire`)"
        )
    same = [i for i in rows if _hot_root(lines[i]) == root]
    if same:
        raise Refused(
            f"root {root} already holds a slot; one root, one rule",
            [f"line {i + 1}: {lines[i]}" for i in same],
        )

    new_line = f"- [{root}] {text}"
    saved = backup(desk, path)
    lines.insert((rows[-1] + 1) if rows else end, new_line)
    _save(path, lines)
    return Result(
        note=f"hot rule promoted ({len(rows) + 1}/{max_lines} slots used) [previous version: {saved.name}]",
        echo=[new_line],
        paths=[path],
    )


def hotrules_retire(desk: Desk, root: str, reason: str) -> Result:
    root = (root or "").strip()
    if not root:
        raise Malformed("--root is empty")
    reason = (reason or "").strip()
    if not reason:
        raise Malformed("--reason is empty; the register is where a retired rule's history goes")

    path, lines, start, end = _hot_segment(desk)
    rows = [i for i in range(start + 1, end) if _hot_root(lines[i]) == root]
    if len(rows) != 1:
        raise Refused(
            f"{len(rows)} hot rule(s) in the segment carry root {root}; exactly 1 is required, so nothing was written",
            [f"line {i + 1}: {lines[i]}" for i in rows],
        )
    i = rows[0]
    gone = lines[i]
    count = int(selfcatch_counts(desk).get(root, {}).get("count", 0))
    saved = backup(desk, path)
    del lines[i]
    _save(path, lines)
    record = f"- [{root}] {HOT_LINE.match(gone).group('text')} | count {count} | reason {reason} | {today()}\n"
    _append(_path(desk, HOT_RETIRED, create=True), record)
    return Result(
        note=f"hot rule retired [previous version: {saved.name}]; recorded in {HOT_RETIRED}",
        echo=[gone, record.rstrip("\n")],
        paths=[path, desk.path(HOT_RETIRED)],
    )


def hotrules_list(desk: Desk) -> Result:
    rows = hot_rule_lines(desk)
    max_lines = cfg_int(desk, "hot_rule_max_lines", 10)
    return Result(note=f"hot rules: {len(rows)}/{max_lines} slots used", echo=rows or ["(the segment is empty)"])


# --------------------------------------------------------------------------
# close-outs
# --------------------------------------------------------------------------
def closings_add(desk: Desk, task: str, line: str) -> Result:
    task = (task or "").strip()
    if not task:
        raise Malformed("--task is empty")
    line = (line or "").strip().replace("\n", " ")
    if not line:
        raise Malformed("--line is empty")
    cap = cfg_int(desk, "closings_line_chars", 200)
    if len(line) > cap:
        raise Malformed(f"--line is {len(line)} characters, over the {cap}-character cap; shorten it")
    row = f"- {ts()} [{task}] {line}"
    _append(_path(desk, CLOSINGS, create=True), row + "\n")
    return Result(note=f"close-out recorded ({task})", echo=[row], paths=[desk.path(CLOSINGS)])


def closings_rows(desk: Desk) -> List[str]:
    p = desk.path(CLOSINGS)
    if not p.exists():
        return []
    return [l for l in _lines(p) if l.startswith(DATA)]


def closings_pointer(desk: Desk) -> int:
    p = desk.path(CLOSINGS_READ)
    if not p.exists():
        return 0
    try:
        return int(read(p).strip() or 0)
    except (ValueError, OSError):
        return 0


def closings_unread(desk: Desk, advance: bool = True) -> Result:
    rows = closings_rows(desk)
    seen = min(closings_pointer(desk), len(rows))
    fresh = rows[seen:]
    if advance and fresh:
        # The pointer is machinery, not desk state: it is not backed up.
        try:
            write(desk.path(CLOSINGS_READ), f"{len(rows)}\n")
        except OSError as exc:
            raise Environment(f"could not advance the close-out pointer: {exc}")
    note = f"{len(fresh)} unread close-out(s) of {len(rows)}"
    if advance and fresh:
        note += f"; pointer advanced to {len(rows)}"
    return Result(note=note, echo=fresh or ["(none)"])


# --------------------------------------------------------------------------
# wrap-up
# --------------------------------------------------------------------------
WRAPUP_ORDER = ("own", "in-flight", "debts", "card")


def wrapup(
    desk: Desk,
    own: Optional[str] = None,
    inflight_finish_id: Optional[str] = None,
    debts_add_text: Optional[str] = None,
    card: Optional[Dict[str, str]] = None,
) -> List[Result]:
    """Own list, then in flight, then debts, then the card. Always that order."""
    card = {k: v for k, v in (card or {}).items() if v is not None}
    steps = [
        ("own", (lambda: own_add(desk, own)) if own else None),
        ("in-flight", (lambda: inflight_finish(desk, inflight_finish_id)) if inflight_finish_id else None),
        ("debts", (lambda: debts_add(desk, debts_add_text)) if debts_add_text else None),
        ("card", (lambda: card_set(desk, **card)) if card else None),
    ]
    done: List[Result] = []
    for name, fn in steps:
        if fn is None:
            continue
        try:
            result = fn()
        except StateError as exc:
            raise WrapupStopped(name, exc, done)
        result.note = f"[{name}] {result.note}"
        done.append(result)
    if not done:
        raise Malformed("wrapup was given nothing to do (--own, --inflight-finish, --debts-add, --card-*)")
    return done
