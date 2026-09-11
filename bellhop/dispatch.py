"""`bellhop dispatch` -- the fixed half of dispatching a task, in one command.

Dispatching used to be one turn of real work followed by three or four fixed ones:
write the body, then generate the packet head, then ring, then start the in-flight
line, then refresh the card -- and usually a re-read of the files just written, to
check they said what they were meant to say. On a production desk that was a median
of several turns per packet. Folding the fixed part into one command with a
short echo halved it, and the dispatch-side self-caught errors (a
wrong base in the packet, a card field left behind) stopped appearing at all.

Three lessons are built into the gates here, each of them paid for:

* **The base is never typed.** It is read from the lane at dispatch time. A base
  that was a commit or two old produced a receipt whose diff described work nobody
  did, and an acceptance verdict that checked it against the wrong parent -- and a
  packet with a stale base looks exactly like a packet with a fresh one.
* **Half a chain is worse than none.** If the bell rings and the in-flight line is
  never started, a task that is really running and a task nobody knows was sent look
  identical on the board. So the echo is four lines read back from the files, and the
  last line is either `✅ dispatched` or `🛑 stopped at: <step>` -- never silence.
* **A stop-dispatch order needs a command path.** An order that lives only in prose
  gets pierced by exceptions, one reasonable-sounding exception at a time. Here the
  switch is a file this command reads before it does anything, and every exception is
  numbered and appended to that file where the sentinel can count them.

Exit codes:

* 0 -- dispatched (or rendered, with ``--demo``)
* 2 -- refused by a gate; nothing was written, and re-running refuses again
* 3 -- stopped at `ring`: a bell for this packet is already pending or has run.
  In flight and the card are untouched, so re-running is safe.
* 4 -- stopped part-way: the packet and the bell are real, a later step was not done.
  The echo says which, and what to run by hand.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from . import packet as packet_mod
from . import ring as ring_mod
from . import state as state_mod
from .locking import serialized
from .config import Desk
from .util import BellhopError, append, git, now, read, run, slugify, ts, write

OK = 0
REFUSED = 2
BELL_TAKEN = 3
PART_WAY = 4

# TODO: the `stopdispatch` module (its own branch) will own this file and expose
# `dispatch_allowed(desk)`. Until it lands, the switch is checked here directly --
# because a switch that is nobody's command path is a switch that gets talked past.
STOP_DISPATCH = "state/stop-dispatch.md"

STEP_STOP = "stop order"
STEP_PACKET = "packet"
STEP_RING = "ring"
STEP_INFLIGHT = "in flight"
STEP_CARD = "card"


class Stopped(BellhopError):
    """A step said no. `code` is the process exit code; `step` names the step."""

    def __init__(self, step: str, reason: str, code: int = REFUSED):
        super().__init__(reason)
        self.step = step
        self.reason = reason
        self.code = code


@dataclass
class Report:
    """What the command prints: some notes, then exactly four lines, then a verdict."""

    notes: List[str] = field(default_factory=list)
    packet_line: str = "— not written"
    bell_line: str = "— not rung"
    inflight_line: str = "— not started"
    card_line: str = "— not read"
    last: str = ""
    code: int = OK
    packet_path: Optional[Path] = None
    bell_path: Optional[Path] = None
    packet_text: str = ""

    def render(self) -> str:
        out = list(self.notes)
        out.append(f"1 packet   {self.packet_line}")
        out.append(f"2 bell     {self.bell_line}")
        out.append(f"3 flight   {self.inflight_line}")
        out.append(f"4 card     {self.card_line}")
        out.append(self.last)
        return "\n".join(out)


# --------------------------------------------------------------------------
# the lane: where a packet starts, and whether the lane can take one
# --------------------------------------------------------------------------
@dataclass
class LaneState:
    head: str
    branch: str
    repo: Path
    worktree: Optional[Path]
    dirty: List[str] = field(default_factory=list)
    ahead: int = 0
    pending: List[str] = field(default_factory=list)

    def blockers(self) -> List[str]:
        out = []
        if self.pending:
            out.append(f"a bell is still pending for packet {', '.join(self.pending)}")
        if self.dirty:
            out.append(f"the lane worktree has {len(self.dirty)} uncommitted path(s)")
        if self.ahead:
            out.append(f"the lane branch holds {self.ahead} commit(s) not on {self.branch}")
        return out


def _is_repo(path: Path) -> bool:
    return run(["git", "-C", str(path), "rev-parse", "--git-dir"], check=False).returncode == 0


def lane_state(desk: Desk, lane: dict) -> LaneState:
    """Where this lane stands right now, read from git and from the pending bells.

    The head is the lane's base branch as the lane repository has it this second. It is
    never an argument: the whole point of computing it here is that nobody can type an
    old one.
    """
    repo = Path(lane["repo"])
    if not lane["repo"] or not repo.exists():
        raise Stopped(STEP_PACKET, f"lane '{lane['name']}' has no repository at {repo or '(unset)'}")
    if not _is_repo(repo):
        raise Stopped(STEP_PACKET, f"lane '{lane['name']}' repository is not a git repository: {repo}")
    branch = lane["branch"]
    proc = run(["git", "-C", str(repo), "rev-parse", "--short", f"{branch}^{{commit}}"], check=False)
    head = (proc.stdout or "").strip()
    if proc.returncode != 0 or not head:
        raise Stopped(STEP_PACKET, f"cannot resolve '{branch}' in {repo}: the lane has no base to start from")

    state = LaneState(head=head, branch=branch, repo=repo, worktree=None)
    wt = Path(lane["worktree"])
    if (wt / ".git").exists():
        state.worktree = wt
        state.dirty = [l for l in git(wt, "status", "--porcelain").splitlines() if l.strip()]
        state.ahead = int(git(wt, "rev-list", "--count", f"{branch}..HEAD") or 0)
    if desk.bells_pending.exists():
        for bell in sorted(desk.bells_pending.glob("*.bell")):
            try:
                data = json.loads(bell.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("lane") == lane["name"] and data.get("id"):
                state.pending.append(str(data["id"]))
    return state


# --------------------------------------------------------------------------
# continue / return: the one way past those gates
# --------------------------------------------------------------------------
def find_packet(desk: Desk, lane: str, task_id: str) -> Path:
    hits = sorted(desk.inbox.joinpath(lane).glob(f"{task_id}-*.md")) if desk.inbox.exists() else []
    if len(hits) != 1:
        raise Stopped(
            STEP_PACKET,
            f"{len(hits)} packet(s) match id {task_id} on lane {lane}; exactly 1 is required "
            f"(looked in inbox/{lane}/{task_id}-*.md)",
        )
    return hits[0]


def find_receipt(desk: Desk, task_id: str) -> Optional[Path]:
    if not desk.receipts.exists():
        return None
    hits = [p for p in sorted(desk.receipts.glob(f"{task_id}-*.md")) if not p.name.endswith(".acceptance.md")]
    return hits[0] if hits else None


# --------------------------------------------------------------------------
# the forbidden-command gate
# --------------------------------------------------------------------------
def forbidden_hits(body: str, patterns: Sequence[str]) -> List[Tuple[int, str, str]]:
    """(line number, pattern, the line) for every body line that asks for a gated command."""
    hits = []
    for n, line in enumerate(body.splitlines(), 1):
        for pattern in patterns:
            try:
                if re.search(pattern, line, re.I):
                    hits.append((n, pattern, line.strip()))
            except re.error as exc:
                raise Stopped(STEP_PACKET, f"packet_forbidden_patterns entry {pattern!r} is not a regex: {exc}")
    return hits


def _patterns(desk: Desk) -> List[str]:
    raw = desk.cfg.get("packet_forbidden_patterns")
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise Stopped(STEP_PACKET, "packet_forbidden_patterns must be a list of patterns")
    return [str(p) for p in raw]


def resolve_allowed(patterns: Sequence[str], allow: Sequence[str]) -> List[str]:
    """Which configured patterns this one packet waives. A name that matches none is refused.

    Nothing is stored: the waiver lives for the length of this command. A flag that
    quietly wrote itself into the config would turn one deliberate exception into the
    new default, and the next packet through would look exactly as gated as this one.
    """
    out = []
    for name in allow or ():
        wanted = str(name).strip()
        match = [p for p in patterns if p == wanted or packet_mod.display_pattern(p).lower() == wanted.lower()]
        if not match:
            known = ", ".join(packet_mod.display_pattern(p) for p in patterns) or "none"
            raise Stopped(STEP_PACKET, f"--allow {wanted!r} names no configured pattern (known: {known})")
        out.extend(match)
    return out


# --------------------------------------------------------------------------
# the stop-dispatch switch
# --------------------------------------------------------------------------
def stop_order(desk: Desk) -> Optional[Path]:
    p = desk.path(STOP_DISPATCH)
    return p if p.exists() else None


def exception_count(path: Path) -> int:
    return len(re.findall(r"^- \d+ \|", read(path), flags=re.M))


@serialized
def record_exception(desk: Desk, path: Path, reason: str, what: str) -> Tuple[int, str]:
    """Append one numbered exception line and hand back its number and the line."""
    n = exception_count(path) + 1
    line = f"- {n} | {ts()} | {what} | {reason}"
    text = read(path)
    if not text.endswith("\n"):
        text += "\n"
    if "## Exceptions" not in text:
        text += "\n## Exceptions\n"
        write(path, text)
    append(path, line + "\n")
    return n, line


# --------------------------------------------------------------------------
# the in-flight line
# --------------------------------------------------------------------------
def inflight_text(task_id: str, lane: str, title: str, base: str, bell: str,
                  sentence: str = "", cap: int = 160) -> str:
    """`<id> <lane> <title> | dispatched <time> | base <sha7> | bell <name>[ | <sentence>]`.

    The sentence goes last on purpose. The line has a cap, and whatever falls off the end
    when it is reached should be the prose, not the base and the bell: those two are how a
    row on the board is traced back to a commit and a log weeks later.
    """
    fixed = (f"{task_id} {lane} {title} | dispatched {now().strftime('%Y-%m-%d %H:%M')} "
             f"| base {base} | bell {bell}")
    sentence = " ".join((sentence or "").split())
    if not sentence:
        return fixed
    room = cap - len("- ") - len(fixed) - len(" | ")
    if room < 8:
        return fixed
    if len(sentence) > room:
        sentence = sentence[: room - 1].rstrip() + "…"
    return f"{fixed} | {sentence}"


def already_in_flight(desk: Desk, task_id: str) -> bool:
    path = desk.path(state_mod.INFLIGHT)
    if not path.exists():
        return False
    rows = [l for l in read(path).split("\n") if l.startswith("- ") and l.strip() != "- none"]
    return any(state_mod.token(task_id)(row) for row in rows)


def start_inflight(desk: Desk, task_id: str, text: str) -> Tuple[str, bool]:
    """Add the row unless one for this id is already there. Returns (row as written, skipped)."""
    if already_in_flight(desk, task_id):
        return (inflight_row(desk, task_id) or "", True)
    state_mod.inflight_start(desk, text)
    return (inflight_row(desk, task_id) or "", False)


def inflight_row(desk: Desk, task_id: str) -> Optional[str]:
    """The row as the file has it -- read back, not restated.

    What was written and what was meant to be written look the same in a message that
    only says "done"; `bellhop state` has its own caps and its own prefix.
    """
    path = desk.path(state_mod.INFLIGHT)
    if not path.exists():
        return None
    for line in read(path).split("\n"):
        if line.startswith("- ") and line.strip() != "- none" and state_mod.token(task_id)(line):
            return line
    return None


# --------------------------------------------------------------------------
# the command
# --------------------------------------------------------------------------
@serialized
def dispatch(
    desk: Desk,
    lane: str,
    title: str,
    body_file,
    inflight: str = "",
    next_: Optional[str] = None,
    continue_id: Optional[str] = None,
    return_id: Optional[str] = None,
    heavy: bool = False,
    allow: Sequence[str] = (),
    exception: Optional[str] = None,
    demo: bool = False,
    commit: bool = True,
) -> Report:
    report = Report()
    try:
        _dispatch(report, desk, lane, title, body_file, inflight, next_, continue_id,
                  return_id, heavy, allow, exception, demo, commit)
    except Stopped as exc:
        report.code = exc.code
        report.last = f"🛑 stopped at: {exc.step} — {exc.reason}"
    return report


def _dispatch(report, desk, lane_name, title, body_file, inflight, next_, continue_id,
              return_id, heavy, allow, exception, demo, commit) -> None:
    if continue_id and return_id:
        raise Stopped(STEP_PACKET,
                      "--continue and --return are opposites: continue picks up a packet that "
                      "stopped without a receipt, return re-sends one whose receipt was refused")
    if demo and exception:
        raise Stopped(STEP_STOP,
                      "--demo with --exception: a demo writes nothing, so there is nothing to "
                      "except. Drop --exception to look, or drop --demo to dispatch")

    # -- the switch, before anything else -----------------------------------
    stop_file = stop_order(desk)
    if stop_file and not demo and not exception:
        raise Stopped(STEP_STOP,
                      f"dispatch is stopped ({stop_file.relative_to(desk.root)}). Lift the order, or "
                      f"pass --exception \"<reason>\" and it is recorded and counted there")
    if stop_file and demo:
        report.notes.append(f"⚠ stop-dispatch order in force ({stop_file.relative_to(desk.root)}); "
                            f"--demo writes nothing, so nothing was excepted")

    title = " ".join(str(title).split())
    if not title:
        raise Stopped(STEP_PACKET, "a packet needs a title")
    try:
        lane = desk.lane(lane_name)
    except BellhopError as exc:
        raise Stopped(STEP_PACKET, str(exc))

    # -- the body ------------------------------------------------------------
    body = read(Path(body_file)) if str(body_file) != "-" else _stdin()
    if not body.strip():
        raise Stopped(STEP_PACKET, f"the body file is empty: {body_file}")
    if packet_mod.has_front_matter(body):
        raise Stopped(STEP_PACKET,
                      "the body opens its own '---' block. `bellhop dispatch` writes the front "
                      "matter; the body starts at its first heading")
    acceptance = packet_mod.body_acceptance(body)
    if not acceptance:
        raise Stopped(STEP_PACKET,
                      "the body has no acceptance items: add an `## Acceptance` heading with one "
                      "`- ` bullet per check a command can run")

    patterns = _patterns(desk)
    waived = resolve_allowed(patterns, allow)
    hits = forbidden_hits(body, patterns)
    refused = [h for h in hits if h[1] not in waived]
    if refused:
        listed = "; ".join(f"line {n}: {packet_mod.display_pattern(p)} in {line!r}" for n, p, line in refused)
        raise Stopped(STEP_PACKET,
                      f"the body asks for a command this desk does not dispatch ({listed}). "
                      f"Rewrite it, or pass --allow <pattern> for this one packet")
    for n, p, _line in hits:
        if p in waived:
            report.notes.append(f"⚠ allowed for this packet only: {packet_mod.display_pattern(p)} "
                                f"(body line {n}); nothing is stored, the next packet is gated again")

    cap = int(desk.cfg.get("packet_bytes", 3000) or 0)
    size = len(body.encode("utf-8"))
    if cap and size > cap:
        report.notes.append(f"⚠ body is {size} bytes, over the {cap}-byte guide: one packet, one thing")

    # -- the lane: where this starts -----------------------------------------
    state = lane_state(desk, lane)
    links: List[Tuple[str, str]] = []
    resume_id = continue_id or return_id
    if resume_id:
        prior = find_packet(desk, lane_name, resume_id)
        receipt = find_receipt(desk, resume_id)
        if continue_id and receipt:
            raise Stopped(STEP_PACKET,
                          f"packet {resume_id} has a receipt ({receipt.name}); it did not die, so "
                          f"there is nothing to continue. Use --return {resume_id} to re-send it")
        if return_id and not receipt:
            raise Stopped(STEP_PACKET,
                          f"packet {resume_id} has no receipt; nothing came back to refuse. "
                          f"Use --continue {resume_id} to pick up where it stopped")
        base = packet_mod.parse_packet(prior).base
        key = "continues" if continue_id else "returns"
        links.append((key, resume_id))
        links.append((f"{key}_receipt", str(receipt.relative_to(desk.root)) if receipt else "none"))
        report.notes.append(f"↩ base {base} taken from packet {resume_id} "
                            f"({prior.relative_to(desk.root)}), not from the lane head {state.head}")
        for blocker in state.blockers():
            report.notes.append(f"⚠ {blocker} — waived by --{'continue' if continue_id else 'return'} {resume_id}")
    else:
        blockers = state.blockers()
        if blockers:
            raise Stopped(STEP_PACKET,
                          "the lane cannot take a packet: " + "; ".join(blockers) +
                          ". Merge or accept the task in flight (`bellhop merge <id>`), clear the "
                          "worktree (`bellhop lane reset <lane>`), or name it with --continue/--return")
        base = state.head

    # -- the packet ----------------------------------------------------------
    task_id = packet_mod.next_id(desk)
    slug = slugify(title)
    receipt_rel = f"receipts/{task_id}-{slug}.md"
    created = ts()
    try:
        text = packet_mod.build_packet_text(
            id=task_id, lane=lane_name, title=title, base=base, created=created, body=body,
            acceptance=acceptance, budget_minutes=int(desk.cfg.get("packet_budget_minutes", 45)),
            heavy=heavy, links=links, forbidden=patterns, receipt=receipt_rel,
        )
    except packet_mod.PacketError as exc:
        raise Stopped(STEP_PACKET, str(exc))

    if demo:
        report.packet_text = text
        report.packet_line = f"(demo) inbox/{lane_name}/{task_id}-{slug}.md — not written"
        report.bell_line = "(demo) not rung"
        report.inflight_line = "(demo) not started"
        report.card_line = _card_line(desk, touched=False)
        report.last = f"✅ demo {task_id} {lane_name} — nothing written; id {task_id} is still free"
        return

    if stop_file and exception:
        n, line = record_exception(desk, stop_file, exception, f"{task_id} {lane_name} {title}")
        report.notes.append(f"⚠ exception {n} under stop order")
        report.notes.append(f"   {line}")

    path = desk.inbox / lane_name / f"{task_id}-{slug}.md"
    if path.exists():
        raise Stopped(STEP_PACKET, f"a packet is already at {path.relative_to(desk.root)}")
    write(path, text)
    report.packet_path = path
    report.packet_line = str(path.relative_to(desk.root))
    report.packet_text = text

    # -- ring ---------------------------------------------------------------
    busy = _bell_taken(desk, task_id, path)
    if busy:
        report.code = BELL_TAKEN
        report.card_line = _card_line(desk, touched=False)
        report.last = f"🛑 stopped at: {STEP_RING} — {busy}; in flight and the card were not touched"
        return
    try:
        bell = ring_mod.ring(desk, path, commit=commit)
    except BellhopError as exc:
        report.code = BELL_TAKEN if "already pending" in str(exc) else PART_WAY
        report.card_line = _card_line(desk, touched=False)
        report.last = f"🛑 stopped at: {STEP_RING} — {exc}; in flight and the card were not touched"
        return
    report.bell_path = bell
    report.bell_line = bell.name

    # -- in flight ----------------------------------------------------------
    text_line = inflight_text(task_id, lane_name, title, base, bell.stem, inflight,
                              cap=state_mod.cfg_int(desk, "inflight_line_chars", 160))
    try:
        row, skipped = start_inflight(desk, task_id, text_line)
    except state_mod.StateError as exc:
        report.code = PART_WAY
        report.card_line = _card_line(desk, touched=False)
        report.last = (f"🛑 stopped at: {STEP_INFLIGHT} — {exc}. The bell {bell.name} is rung: finish with "
                       f"`bellhop state inflight start --text \"{text_line}\"`")
        return
    report.inflight_line = row or "(written, but the file does not show it)"
    if skipped:
        report.notes.append(f"in flight already had a row for {task_id}; left as it was")

    # -- the card -----------------------------------------------------------
    if next_ is not None:
        try:
            state_mod.card_set(desk, next=next_)
        except state_mod.StateError as exc:
            report.code = PART_WAY
            report.card_line = _card_line(desk, touched=False)
            report.last = (f"🛑 stopped at: {STEP_CARD} — {exc}. The bell {bell.name} is rung and the "
                           f"in-flight row is up: finish with `bellhop state card set --next ...`")
            return
        report.card_line = _card_line(desk, touched=True)
    else:
        report.card_line = _card_line(desk, touched=False)

    report.last = f"✅ dispatched {task_id} {bell.name}"


def _card_line(desk: Desk, touched: bool) -> str:
    try:
        value = state_mod.read_card_keys(desk).get("next", "")
    except Exception:
        return "— card unreadable"
    shown = value if value else "(empty)"
    return f"next: {shown}" + ("" if touched else " (not touched)")


def _bell_taken(desk: Desk, task_id: str, path: Path) -> Optional[str]:
    """Has a bell for this packet already been rung? `ring` sees pending; this also sees run."""
    for bell in sorted(desk.bells_pending.glob("*.bell")) if desk.bells_pending.exists() else []:
        try:
            data = json.loads(bell.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(data.get("id")) == task_id:
            return f"a bell for {task_id} is already pending ({bell.name})"
    if desk.bells_rung.exists():
        for bell in desk.bells_rung.glob(f"*-{task_id}.bell"):
            return f"a bell for {task_id} has already run ({bell.name})"
    if desk.bells_logs.exists():
        for log in desk.bells_logs.glob(f"*-{task_id}.log"):
            return f"a worker log for {task_id} is already there ({log.name})"
    return None


def _stdin() -> str:
    import sys

    return sys.stdin.read()
