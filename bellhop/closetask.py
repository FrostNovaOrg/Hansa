"""`bellhop close-task` — closing a task is one command, or it did not happen.

On the desk this comes from, closing a task was seven things run by hand: merge the
lane branch, push it, update the acceptance baseline, align the lane's records, take
the row out of in-flight, refresh the card, write the ledger entry. Over three days
the coordinator ran them again and again; each task's records were split over two
or three commits; debts went unsettled for days and then were settled in one
sitting; and many of the desk's commits carried nothing but bookkeeping.

The lesson was one sentence: **a task closed cleanly and a task closed halfway look
the same in the ledger.** Nothing in the records distinguished "merged, recorded,
card refreshed" from "merged, and then the session ended". So closing became one
command with named steps that stops at the first failure, says which step it stopped
at and why, and writes down how far it got in `receipts/<id>.close.json`.

Six steps, always in this order:

1. `evidence`  - a receipt that passes `check-receipt`, a verdict that passes
                 `check-acceptance`, and the verdict says pass.
2. `fileset`   - the files changed on the lane branch equal the files the receipt
                 claims. A receipt that describes a different change than the branch
                 carries is not a receipt for that change.
3. `merge`     - merge into the base branch and run the lane's check on the merged
                 result. The base branch moves only after that check is green.
4. `ledger`    - one entry, through `state.ledger_add`, which owns the byte cap.
5. `state`     - in-flight, debts, card, close-out register.
6. `archive`   - one commit through `archive.py`, and the desk's push policy.

The two gates (1 and 2) are read-only and run again on every rerun; the four steps
that write are skipped when the record says they already ran. That is what makes a
rerun after a stop safe: it picks up where it stopped, and it cannot record the same
close twice.

Exit codes: 0 closed, 1 a red finding at a named step, 2 refused (the verdict says
do not close), 3 the argument is the wrong shape, 4 something outside is missing.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from . import archive as archive_mod
from . import headers as headers_mod
from . import ledger as ledger_mod
from . import merge as merge_mod
from . import state as state_mod
from .accept import acceptance_cap, find_packet
from .locking import acceptance_lock, desk_lock, serialized
from .lanes import lane_lock
from .config import Desk
from .packet import parse_packet, verify_worker_packet
from .util import BellhopError, git, read, ts, write_json

STEPS = ("evidence", "fileset", "merge", "ledger", "state", "archive")
GATES = ("evidence", "fileset")  # read-only; they run again on every rerun

EXIT_OK = 0
EXIT_STOPPED = 1      # ran to the end of a step and the finding is red
EXIT_REFUSED = 2      # refused by design: the verdict says this one does not close
EXIT_MALFORMED = 3    # the argument itself is the wrong shape
EXIT_ENVIRONMENT = 4  # a file that has to exist is not there

SETTLED_TAG = "settled-awaiting-close"
AWAITING_LINE = "settled, awaiting close"
CLOSED_WORD = "closed"


class CloseStopped(BellhopError):
    """A close stopped at a named step. The last line printed says which one."""

    def __init__(self, step: str, reason: str, code: int = EXIT_STOPPED, extra: Sequence[str] = ()):
        super().__init__(reason)
        self.step = step
        self.reason = reason
        self.code = code
        self.extra = list(extra)

    @property
    def last_line(self) -> str:
        return f"🛑 stopped at: {self.step} — {self.reason}"


# --------------------------------------------------------------------------
# the close record
# --------------------------------------------------------------------------
class Record:
    """`receipts/<id>.close.json`: which steps ran, when, and where it stopped.

    This file is the answer to "was that task closed, or closed halfway". It is
    written before the commit that carries it, so the archive step is the one step
    whose own record reaches git one run late; everything else is in the commit.
    """

    def __init__(self, desk: Desk, task_id: str):
        self.path = desk.receipts / f"{task_id}.close.json"
        self.data: dict = {"task": task_id, "steps": [], "closed": False}
        if self.path.exists():
            try:
                loaded = json.loads(read(self.path))
            except (OSError, ValueError):
                loaded = None  # an unreadable record is not a reason to refuse to close
            if isinstance(loaded, dict):
                self.data.update(loaded)
                self.data.setdefault("steps", [])

    def done(self, step: str) -> bool:
        return any(s.get("step") == step for s in self.data.get("steps", []))

    def when(self, step: str) -> str:
        for s in self.data.get("steps", []):
            if s.get("step") == step:
                return s.get("at", "")
        return ""

    def mark(self, step: str, note: str) -> None:
        steps = [s for s in self.data.get("steps", []) if s.get("step") != step]
        steps.append({"step": step, "at": ts(), "note": note})
        steps.sort(key=lambda s: STEPS.index(s["step"]) if s.get("step") in STEPS else 99)
        self.data["steps"] = steps
        self.data["stopped_at"] = None
        self.data["reason"] = None

    def unmark(self, step: str) -> None:
        self.data["steps"] = [s for s in self.data.get("steps", []) if s.get("step") != step]

    def stopped(self, step: str, reason: str) -> None:
        self.data["stopped_at"] = step
        self.data["reason"] = reason

    def save(self) -> None:
        self.data["updated_at"] = ts()
        write_json(self.path, self.data)


@dataclass
class CloseReport:
    task: str
    lane: str = ""
    title: str = ""
    verdict: str = ""
    merged_sha: str = ""
    archive_sha: str = ""
    lines: List[str] = field(default_factory=list)
    ran: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    ledger_paths: List[Path] = field(default_factory=list)
    dry_run: bool = False

    @property
    def closed(self) -> bool:
        return "archive" in (self.ran + self.skipped)


# --------------------------------------------------------------------------
# small readers
# --------------------------------------------------------------------------
_FILES_HEADING = re.compile(r"^#{2,3}[ \t]+files\b", re.I)
_ANY_HEADING = re.compile(r"^#{1,6}[ \t]+\S")
_DURATION_RE = re.compile(r"^[ \t]*[-*][ \t]*duration_s:[ \t]*(\S+)", re.M)


def receipt_files(body: str) -> Optional[List[str]]:
    """The paths under the receipt's `## Files` heading, or None when there is none.

    One path per line, a list marker and backticks optional. A line with a space in
    it is prose, not a path, and is skipped: a sentence that silently became a path
    would fail the set comparison for a reason that has nothing to do with the work.
    """
    lines = body.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _FILES_HEADING.match(line):
            start = i + 1
            break
    if start is None:
        return None
    out: List[str] = []
    for line in lines[start:]:
        if _ANY_HEADING.match(line):
            break
        text = re.sub(r"^[-*][ \t]+", "", line.strip()).strip().strip("`").strip()
        if not text or " " in text or "\t" in text:
            continue
        out.append(text)
    return [] if out == ["none"] else out


def branch_files(repo: Path, base: str, head: str) -> List[str]:
    out = git(repo, "diff", "--name-only", f"{base}..{head}")
    return sorted({l.strip() for l in out.splitlines() if l.strip()})


def _full_sha(repo: Path, rev: str) -> str:
    return git(repo, "rev-parse", f"{rev}^{{commit}}")


def _header(path: Path, kind: str) -> dict:
    values, _span = headers_mod.parse_header(read(path), headers_mod.FENCES[kind])
    return values or {}


def _num(values: dict, key: str) -> str:
    raw = str(values.get(key, "") or "").strip()
    return raw or headers_mod.NOT_AVAILABLE


def verdict_path(desk: Desk, task_id: str) -> Tuple[Optional[Path], str]:
    """The acceptance verdict for a task, and where it was found.

    A verdict written in shadow mode is a rehearsal. It counts as evidence only when
    the desk has said so in `accept_from_shadow`; otherwise a rehearsal and a real
    verdict would look the same at the gate that closes tasks.
    """
    main = desk.receipts / f"{task_id}.acceptance.md"
    if main.exists():
        return main, "receipts"
    shadow = desk.receipts / "shadow" / f"{task_id}.acceptance.md"
    if shadow.exists():
        if desk.cfg.get("accept_from_shadow"):
            return shadow, "shadow"
        return None, "shadow-refused"
    return None, ""


def queue_count(desk: Desk, closing_now: str = "") -> Tuple[int, int, int]:
    """(total, bells waiting, receipts settled but not closed) — the card's `queue`.

    `closing_now` is the task this run is closing: it is counted as closed, because
    the card is refreshed inside the same run and a card that says "one awaiting
    close" the moment that one closed is a number nobody can act on.
    """
    bells = len(list(desk.bells_pending.glob("*.bell"))) if desk.bells_pending.exists() else 0
    awaiting = 0
    if desk.receipts.exists():
        for meta in desk.receipts.glob("*.meta.json"):
            task = meta.name.split(".", 1)[0]
            if closing_now and task == closing_now:
                continue
            record = desk.receipts / f"{task}.close.json"
            closed = False
            if record.exists():
                try:
                    closed = bool(json.loads(read(record)).get("closed"))
                except (OSError, ValueError):
                    closed = False
            if not closed:
                awaiting += 1
    return bells + awaiting, bells, awaiting


def _inflight_rows(desk: Desk, task_id: str) -> List[str]:
    path = desk.path(state_mod.INFLIGHT)
    if not path.exists():
        return []
    hit = state_mod.token(task_id)
    return [l for _i, l in state_mod.data_lines(read(path).split("\n")) if hit(l)]


def _debt_rows(desk: Desk, task_id: str) -> List[str]:
    path = desk.path(state_mod.DEBTS)
    if not path.exists():
        return []
    hit = state_mod.token(task_id)
    return [l for _i, l in state_mod.data_lines(read(path).split("\n")) if hit(l)]


def _closing_said(desk: Desk, task_id: str, phrase: str) -> bool:
    """Has the close-out register already got this line for this task?

    The register is append-only, so the guard against a duplicate row has to be here.
    """
    marker = f"[{task_id}]"
    return any(marker in row and phrase in row for row in state_mod.closings_rows(desk))


def _fit_closing(desk: Desk, line: str) -> str:
    cap = state_mod.cfg_int(desk, "closings_line_chars", 200)
    return line if len(line) <= cap else line[: cap - 4].rstrip() + " ..."


# --------------------------------------------------------------------------
# settle-bell: the three things done the moment the worker exits
# --------------------------------------------------------------------------
@serialized
def settle_bell(desk: Desk, task_id: str, title: str = "", strict: bool = True,
                refresh_card: bool = True,
                say: Optional[Callable[[str], None]] = None) -> List[state_mod.Result]:
    """Tag in flight, refresh the card's queue count, add the close-out line.

    These three are the desk's answer to "the worker exited, and then nothing
    happened for a day". They are not a close: the task is settled and awaiting one.
    Called by the watchdog when the receipt header is written, and by hand for a task
    a coordinator session ran itself.

    `strict=False` turns every refusal into a warning. The watchdog calls it that
    way: a card the coordinator left in a shape `bellhop state` will not write is a
    reason to tell someone, not a reason to lose the receipt that was just archived.

    `refresh_card=False` is what the watchdog passes, and it is not an optimisation.
    The sentinel has exactly one way to notice that a receipt is waiting to be read:
    `state/next.md` is older than the newest receipt, and that is what wakes the
    coordinator. A machine that refreshed the card the moment a receipt landed would
    hand every receipt a green board and wake nobody. So the queue count is
    refreshed by the hand that closes the task — `close-task` does it in its state
    step — or by `bellhop settle-bell` run deliberately, never by the watchdog.
    """
    say = say or (lambda _line: None)
    results: List[state_mod.Result] = []

    def attempt(what: str, fn):
        try:
            result = fn()
        except state_mod.StateError as exc:
            if strict:
                raise
            say(f"note: {what} not done ({exc})")
            return None
        result.note = f"[{what}] {result.note}"
        results.append(result)
        say(result.render())
        return result

    rows = _inflight_rows(desk, task_id)
    if len(rows) > 1:
        exc = state_mod.Refused(
            f"{len(rows)} in-flight rows carry task {task_id}; exactly 1 is required", rows)
        if strict:
            raise exc
        say(f"note: in-flight not tagged ({exc})")
    elif not rows:
        # The label is what the sentinel's in-flight check reads; a row without one is
        # red there. The work is the coordinator's now, so the label says so.
        text = f"[coordinator] {task_id} {title}".strip() + f" — {AWAITING_LINE}"
        attempt("in-flight", lambda: state_mod.inflight_start(desk, text))
    elif SETTLED_TAG in rows[0]:
        say(f"[in-flight] already tagged [{SETTLED_TAG}]")
    else:
        attempt("in-flight", lambda: state_mod.inflight_tag(desk, task_id, SETTLED_TAG))

    if refresh_card:
        total, bells, awaiting = queue_count(desk)
        attempt("card", lambda: state_mod.card_set(
            desk, pool=f"{total} (bells {bells}, awaiting close {awaiting})"))
    else:
        say("[card] left alone; the sentinel wakes the coordinator on a card older "
            "than the newest receipt")

    if _closing_said(desk, task_id, AWAITING_LINE):
        say(f"[closings] already recorded ({AWAITING_LINE})")
    else:
        line = _fit_closing(desk, f"{title or task_id} | {AWAITING_LINE}")
        attempt("closings", lambda: state_mod.closings_add(desk, task_id, line))
    return results


# --------------------------------------------------------------------------
# close-task
# --------------------------------------------------------------------------
class Closing:
    """One run of `close-task`. Each step is a method; they run in `STEPS` order."""

    def __init__(self, desk: Desk, task_id: str, next_card: Optional[str] = None,
                 note: Optional[str] = None, dry_run: bool = False, commit: bool = True,
                 say: Optional[Callable[[str], None]] = None):
        self.desk = desk
        self.task = task_id
        self.next_card = next_card
        self.note = note
        self.dry_run = dry_run
        self.commit = commit
        self.report = CloseReport(task=task_id, dry_run=dry_run)
        self._say = say if say is not None else print
        self._pending: List[str] = []
        self.record = Record(desk, task_id)
        # filled in by the evidence step
        self.packet = None
        self.lane: dict = {}
        self.repo: Optional[Path] = None
        self.receipt: Optional[Path] = None
        self.receipt_values: dict = {}
        self.verdict_file: Optional[Path] = None
        self.verdict_values: dict = {}
        self.verdict = ""
        self.merged = None
        self.check_exit = ""
        self.check_summary = ""

    # -- output ------------------------------------------------------------
    def say(self, line: str) -> None:
        self.report.lines.append(line)
        self._say(line)

    def detail(self, line: str) -> None:
        """A sub-line of the step being run. Held until the step announces itself, so
        the detail always prints under the step it belongs to."""
        self._pending.append("   " + state_mod.echo(line))

    def flush(self) -> None:
        pending, self._pending = self._pending, []
        for line in pending:
            self.say(line)

    def step_line(self, step: str, text: str) -> None:
        self.say(f"{STEPS.index(step) + 1}/{len(STEPS)} {step}: {text}")
        self.flush()

    def stop(self, step: str, reason: str, code: int = EXIT_STOPPED, extra: Sequence[str] = ()):
        self.flush()
        for line in extra:
            self.say(f"   {state_mod.echo(line)}")
        if not self.dry_run:
            self.record.stopped(step, reason)
            self.record.save()
        raise CloseStopped(step, reason, code, extra)

    def would(self, step: str, text: str) -> None:
        self.step_line(step, f"would {text}")
        self.report.skipped.append(step)

    def skip(self, step: str, text: str = "") -> None:
        when = self.record.when(step)
        self.step_line(step, f"already done{f' at {when}' if when else ''}{f'; {text}' if text else ''}")
        self.report.skipped.append(step)

    def did(self, step: str, text: str, note: str = "") -> None:
        self.step_line(step, text)
        self.report.ran.append(step)
        if not self.dry_run:
            self.record.mark(step, note or text)
            self.record.save()

    # -- the run -----------------------------------------------------------
    def run(self) -> CloseReport:
        self.evidence()
        self.fileset()
        for step in ("merge", "ledger", "state", "archive"):
            if self.dry_run:
                getattr(self, step)()
            elif self.record.done(step):
                self.skip(step)
                if step == "merge":
                    self.recall_merge()
            else:
                getattr(self, step)()
        if not self.dry_run:
            self.record.data["closed"] = True
            self.record.save()
        head = self.report.archive_sha or self.report.merged_sha or "-"
        wrote = [s for s in self.report.ran if s not in GATES]
        if self.dry_run:
            self.say(f"✅ dry run: {self.task} would close (nothing was written)")
        elif not wrote:
            self.say(f"✅ already closed: {self.task} (nothing left to do)")
        else:
            self.say(f"✅ closed {self.task} at {head}")
        return self.report

    def recall_merge(self) -> None:
        """A skipped merge still has to hand its numbers to the steps after it.

        The ledger entry and the close-out line quote the merged sha and the full
        board's exit code. On a rerun that picks up at `ledger`, the merge did not
        run in this process, so those numbers come back from the close record — an
        entry that says `merged:` with nothing after it would be a record of a close
        that nobody can check.
        """
        self.report.merged_sha = self.record.data.get("merged_sha", "") or ""
        self.check_exit = self.record.data.get("check_exit", "") or ""
        self.check_summary = self.record.data.get("check_summary", "") or ""

    # -- 1. evidence -------------------------------------------------------
    def evidence(self) -> None:
        desk, task = self.desk, self.task
        try:
            self.packet = parse_packet(find_packet(desk, task))
            self.lane = desk.lane(self.packet.lane)
        except BellhopError as exc:
            self.stop("evidence", str(exc), EXIT_ENVIRONMENT)
        self.repo = Path(self.lane["repo"])
        self.report.lane = self.packet.lane
        self.report.title = self.packet.title
        self.record.data.update({"lane": self.packet.lane, "title": self.packet.title})

        self.receipt = desk.receipts / f"{self.packet.stem}.md"
        if not self.receipt.exists():
            self.stop("evidence", f"no receipt at {self.receipt.relative_to(desk.root)}; "
                                  f"the task has not come back yet", EXIT_ENVIRONMENT)
        report = headers_mod.check_receipt(self.receipt, packet=self.packet)
        if report.reds:
            self.stop("evidence", f"check-receipt is red: {report.reds[0]}", EXIT_STOPPED,
                      report.reds[1:] + [f"warn: {w}" for w in report.warns])
        for warn in report.warns:
            self.detail(f"warn (receipt): {warn}")
        self.receipt_values = _header(self.receipt, "receipt")
        try:
            packet_hash = verify_worker_packet(desk, self.packet)
        except BellhopError as exc:
            self.stop("evidence", f"packet provenance: {exc}; see bellhop accept status {task}")
        recorded_hash = self.record.data.get("packet_sha256")
        if recorded_hash and recorded_hash != packet_hash:
            self.stop("evidence", "packet differs from the input of this partial close; restore its original evidence")
        self.record.data["packet_sha256"] = packet_hash

        path, where = verdict_path(desk, task)
        if path is None:
            if where == "shadow-refused":
                self.stop("evidence",
                          f"the only verdict for {task} is under receipts/shadow/, and the desk's "
                          f"accept_from_shadow is false; a rehearsal does not close a task",
                          EXIT_REFUSED)
            self.stop("evidence", f"no acceptance verdict for {task}; run `bellhop accept {task}`",
                      EXIT_ENVIRONMENT)
        self.verdict_file = path
        meta_file = path.with_name(f"{task}.acceptance.meta.json")
        if not meta_file.exists():
            self.stop("evidence", f"acceptance packet binding missing; run bellhop accept {task} --force")
        try:
            meta = json.loads(read(meta_file))
            if not isinstance(meta, dict) or meta.get("ok") is not True or meta.get("stall"):
                self.stop("evidence", "acceptance run is incomplete or stalled; run acceptance again")
            if meta.get("packet_sha256") != packet_hash:
                self.stop("evidence", f"acceptance packet binding missing or stale; run bellhop accept {task} --force")
            if (_full_sha(self.repo, meta.get("head") or "") !=
                    _full_sha(self.repo, self.receipt_values.get("head") or "") or
                    _full_sha(self.repo, meta.get("base") or "") != _full_sha(self.repo, self.packet.base)):
                self.stop("evidence", "acceptance base/head does not match the current receipt and packet")
        except (ValueError, OSError) as exc:
            self.stop("evidence", f"cannot read acceptance metadata: {exc}; see bellhop accept status {task}")
        except CloseStopped:
            raise
        except BellhopError as exc:
            self.stop("evidence", f"cannot resolve acceptance base/head: {exc}")
        # The cap follows the task's kind: a `kind: report` packet is judged against
        # `acceptance_bytes_report`, so a long report does not warn on every close.
        report = headers_mod.check_acceptance(path, max_bytes=acceptance_cap(desk, self.packet))
        if report.reds:
            self.stop("evidence", f"check-acceptance is red: {report.reds[0]}", EXIT_STOPPED,
                      report.reds[1:] + [f"warn: {w}" for w in report.warns])
        for warn in report.warns:
            self.detail(f"warn (verdict): {warn}")
        self.verdict_values = _header(path, "acceptance")
        self.verdict = (self.verdict_values.get("verdict") or "").lower()
        self.report.verdict = self.verdict
        self.record.data["verdict"] = self.verdict

        if self.verdict != "pass":
            reason = self.verdict_values.get("verdict_reason") or ""
            line = _fit_closing(desk, f"{self.packet.title} | {self.verdict} | not {CLOSED_WORD}"
                                      + (f" | {reason}" if reason else ""))
            if self.dry_run:
                self.detail(f"would record: {line}")
            elif _closing_said(desk, task, f"| {self.verdict} | not {CLOSED_WORD}"):
                self.detail("close-out already recorded for this verdict")
            else:
                self.detail(state_mod.closings_add(desk, task, line).render())
            self.stop("evidence",
                      f"the verdict is {self.verdict}, not pass; recorded as not {CLOSED_WORD}"
                      + (f" ({reason})" if reason else ""), EXIT_REFUSED)

        where_note = "" if where == "receipts" else f" (from {where})"
        self.did("evidence",
                 f"receipt ok, verdict pass{where_note}"
                 f" — criteria {_num(self.verdict_values, 'criteria_pass')}"
                 f"/{_num(self.verdict_values, 'criteria_total')},"
                 f" mutations {_num(self.verdict_values, 'mutations')},"
                 f" blocking doubts {_num(self.verdict_values, 'doubts_blocking')}",
                 note=f"verdict {self.verdict} from {where}")

    # -- 2. fileset --------------------------------------------------------
    def fileset(self) -> None:
        desk = self.desk
        base = self.packet.base
        branch = f"lane/{self.packet.lane}"
        head_claimed = self.receipt_values.get("head") or ""
        try:
            tip = _full_sha(self.repo, branch)
        except BellhopError as exc:
            self.stop("fileset", f"lane branch {branch} is not in {self.repo}: {exc}", EXIT_ENVIRONMENT)
        head = tip
        if head_claimed and head_claimed != headers_mod.NOT_AVAILABLE:
            try:
                head = _full_sha(self.repo, head_claimed)
            except BellhopError:
                self.stop("fileset", f"the receipt's head {head_claimed} is not a commit in {self.repo}",
                          EXIT_STOPPED)
            if head != tip:
                self.stop("fileset",
                          f"{branch} has moved since the receipt: the receipt says {head[:12]}, "
                          f"the branch is at {tip[:12]}; merging would carry commits nobody accepted",
                          EXIT_STOPPED)
        changed = branch_files(self.repo, base, head)
        claimed = receipt_files(headers_mod.strip_header(read(self.receipt), headers_mod.FENCE_RECEIPT))

        if claimed is None:
            counted = self.receipt_values.get("files_changed", headers_mod.NOT_AVAILABLE)
            if counted in ("", headers_mod.NOT_AVAILABLE):
                self.stop("fileset",
                          "the receipt has no `## Files` section and no files_changed in its header, "
                          "so there is nothing to compare the branch against", EXIT_STOPPED)
            if str(counted) != str(len(changed)):
                self.stop("fileset",
                          f"the branch changed {len(changed)} file(s), the receipt header says {counted}",
                          EXIT_STOPPED, changed)
            self.did("fileset", f"{len(changed)} file(s) on {branch}, equal to the receipt's "
                                f"files_changed (no `## Files` section, so the count is the gate)")
            return

        want = sorted(set(claimed))
        if want != changed:
            extra = ["branch:  " + (", ".join(changed) or "(none)"),
                     "receipt: " + (", ".join(want) or "(none)"),
                     "only on the branch: " + (", ".join(sorted(set(changed) - set(want))) or "(none)"),
                     "only in the receipt: " + (", ".join(sorted(set(want) - set(changed))) or "(none)")]
            self.stop("fileset", f"the branch changed {len(changed)} file(s), the receipt names "
                                 f"{len(want)}, and the two sets differ", EXIT_STOPPED, extra)
        self.did("fileset", f"{len(changed)} file(s), equal to the receipt's `## Files` section")

    # -- 3. merge ----------------------------------------------------------
    def merge(self) -> None:
        branch = f"lane/{self.packet.lane}"
        target = self.lane["branch"]
        check = (self.lane.get("check_cmd") or "").strip() or "(none declared)"
        if self.dry_run:
            self.would("merge", f"merge {branch} into {target} in a throwaway worktree and run "
                                f"the full board there: {check}")
            return
        push = bool(self.desk.cfg.get("close_push_lane"))
        try:
            self.merged = merge_mod.merge_checked(self.desk, self.task, push=push)
        except BellhopError as exc:
            self.stop("merge", str(exc), EXIT_STOPPED)
        result = self.merged
        self.report.merged_sha = result.sha
        self.check_exit, self.check_summary = result.check_exit, result.check_summary
        self.record.data.update({"merged_sha": result.sha, "check_exit": result.check_exit,
                                 "check_summary": result.check_summary})
        how = "already merged" if result.already else f"merged into {target}"
        self.did("merge", f"{branch} {how} at {result.sha}; full board exit {result.check_exit}"
                          f" — {result.check_summary}"
                          + ("; pushed" if result.pushed else ""),
                 note=f"{how} at {result.sha}, check {result.check_exit}")

    # -- 4. ledger ---------------------------------------------------------
    def ledger_title(self) -> str:
        return f"{CLOSED_WORD} {self.task} {self.packet.title}".strip()

    def ledger_body(self) -> str:
        values, verdict = self.receipt_values, self.verdict_values
        target = self.lane["branch"]
        merged_sha = self.report.merged_sha or ("(dry run)" if self.dry_run else "(not in this run)")
        acc_body = headers_mod.strip_header(read(self.verdict_file), headers_mod.FENCE_ACCEPTANCE)
        seen = _DURATION_RE.search(acc_body)
        lines = [
            f"- lane: {self.packet.lane} (base {self.packet.base})",
            f"- head: {(values.get('head') or headers_mod.NOT_AVAILABLE)[:12]},"
            f" {_num(values, 'commits')} commit(s), {_num(values, 'files_changed')} file(s)",
            f"- merged: {merged_sha} into {target}",
            f"- full board: exit {self.check_exit or headers_mod.NOT_AVAILABLE}"
            f" — {self.check_summary or headers_mod.NOT_AVAILABLE}",
            f"- verdict: {self.verdict}, criteria {_num(verdict, 'criteria_pass')}"
            f"/{_num(verdict, 'criteria_total')}, mutations {_num(verdict, 'mutations')},"
            f" doubts {_num(verdict, 'doubts_blocking')} blocking"
            f" / {_num(verdict, 'doubts_recorded')} recorded",
            f"- worker: {_num(values, 'model')}, {_num(values, 'turns')} turns,"
            f" {_num(values, 'duration_s')}s, {_num(values, 'cost_usd')} USD",
            f"- verifier: {seen.group(1) if seen else headers_mod.NOT_AVAILABLE}s"
            f" (the acceptance header carries no cost field)",
        ]
        if self.note:
            lines.append(f"- note: {self.note}")
        return "\n".join(lines)

    def ledger(self) -> None:
        title = self.ledger_title()
        if self.dry_run:
            for line in self.ledger_body().split("\n"):
                self.detail(line)
            self.would("ledger", f"add one entry `# {title}`")
            return
        if ledger_mod.entries_mentioning(self.desk, f"# {title}"):
            self.did("ledger", f"an entry `# {title}` is already on the ledger; not added twice")
            return
        handle, tmp = tempfile.mkstemp(prefix="bellhop-close-", suffix=".md")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(f"# {title}\n\n{self.ledger_body()}\n")
            refs = [self.task] + ([self.report.merged_sha] if self.report.merged_sha else [])
            try:
                result = state_mod.ledger_add(self.desk, tmp, refs=refs)
            except state_mod.StateError as exc:
                self.stop("ledger", str(exc), exc.code)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        self.report.ledger_paths = list(result.paths)
        for line in result.echo:
            self.detail(line)
        for warn in result.warnings:
            self.detail(f"warning: {warn}")
        self.did("ledger", result.note)

    # -- 5. state ----------------------------------------------------------
    def state(self) -> None:
        desk, task = self.desk, self.task
        notes: List[str] = []
        rows = _inflight_rows(desk, task)
        debts = _debt_rows(desk, task)
        total, bells, awaiting = queue_count(desk, closing_now=task)
        if self.dry_run:
            next_note = f", next {self.next_card!r}" if self.next_card else ""
            for row in rows + debts:
                self.detail(row)
            self.would("state", f"finish in flight ({len(rows)} row(s) carry {task}),"
                                f" settle {len(debts)} debt(s), set the card"
                                f" (queue {total}{next_note}), record the close-out")
            return

        # in flight: the watchdog takes the worker's own row off the board when it
        # archives the receipt, so "no row" is the normal case and not a failure.
        if len(rows) > 1:
            self.stop("state", f"{len(rows)} in-flight rows carry task {task}; exactly 1 is required",
                      EXIT_REFUSED, rows)
        elif rows:
            try:
                result = state_mod.inflight_finish(desk, task)
            except state_mod.StateError as exc:
                self.stop("state", f"in flight: {exc}", exc.code, exc.extra)
            notes.append("in flight -1")
            self.detail(result.render())
        else:
            notes.append("in flight: no row")

        # debts: every line that carries the id, settled one at a time by its own text.
        for row in debts:
            match = row[len(state_mod.DATA):].strip() if row.startswith(state_mod.DATA) else row.strip()
            try:
                result = state_mod.debts_settle(desk, match)
            except state_mod.StateError as exc:
                self.stop("state", f"debts: {exc}", exc.code, exc.extra)
            self.detail(result.render())
        notes.append(f"{len(debts)} debt(s) settled")

        # card: the queue count always, `next` only when the caller gave one. Whether
        # the desk is ready to dispatch is the coordinator's judgement, not this
        # command's, so `ready` is never written here.
        total, bells, awaiting = queue_count(desk, closing_now=task)
        flags = {"pool": f"{total} (bells {bells}, awaiting close {awaiting})"}
        if self.next_card:
            flags["next"] = self.next_card
        try:
            result = state_mod.card_set(desk, **flags)
        except state_mod.StateError as exc:
            self.stop("state", f"card: {exc}", exc.code, exc.extra)
        notes.append("card " + ", ".join(sorted(flags)))
        self.detail(result.note)

        line = _fit_closing(desk, f"{self.packet.title} | {CLOSED_WORD} {self.report.merged_sha}"
                                  f" | {self.verdict}" + (f" | {self.note}" if self.note else ""))
        if _closing_said(desk, task, f"| {CLOSED_WORD} "):
            notes.append("close-out already recorded")
        else:
            try:
                result = state_mod.closings_add(desk, task, line)
            except state_mod.StateError as exc:
                self.stop("state", f"closings: {exc}", exc.code, exc.extra)
            notes.append("close-out recorded")
            self.detail(line)
        self.did("state", "; ".join(notes))

    # -- 6. archive --------------------------------------------------------
    def archive_paths(self) -> List[Path]:
        desk = self.desk
        # The checker's reading sits beside the verdict it read, so it follows that
        # file's directory. A verdict taken from receipts/shadow/ under
        # `accept_from_shadow` would otherwise name a reading in receipts/ that does
        # not exist, and archive() passes over a missing untracked path in silence:
        # the close would carry the verdict with no reading beside it and still look
        # complete.
        verdict_dir = self.verdict_file.parent if self.verdict_file else desk.receipts
        paths = [self.receipt, self.verdict_file,
                 verdict_dir / f"{self.task}.acceptance.check.txt",
                 verdict_dir / f"{self.task}.acceptance.meta.json",
                 verdict_dir / f"{self.task}.acceptance.json",
                 self.record.path]
        paths += list(self.report.ledger_paths)
        paths += [desk.path(rel) for rel in (state_mod.CARD, state_mod.INFLIGHT, state_mod.DEBTS,
                                             state_mod.DEBTS_SETTLED, state_mod.CLOSINGS)]
        return [p for p in paths if p is not None]

    def archive(self) -> None:
        message = f"{CLOSED_WORD} {self.task} {self.packet.title}".strip()
        if self.report.merged_sha:
            message += f" at {self.report.merged_sha}"
        if self.dry_run:
            self.would("archive", f"commit {len(self.archive_paths())} path(s) as {message!r}"
                                  + (", then push" if self.desk.cfg.get("push") else ""))
            return
        if not self.commit:
            self.say(f"{STEPS.index('archive') + 1}/{len(STEPS)} archive: skipped (--no-commit); "
                     f"the close record and the state files are not committed")
            return
        # The record is written before the commit so that the commit carries it. The
        # one thing it cannot carry is the sha of the commit it is in.
        self.record.mark("archive", message)
        self.record.data["closed"] = True
        self.record.save()
        before = git(self.desk.root, "rev-parse", "HEAD")
        try:
            sha = archive_mod.archive(self.desk, self.archive_paths(), message)
        except BellhopError as exc:
            self.record.unmark("archive")
            self.record.data["closed"] = False
            self.stop("archive", str(exc), EXIT_STOPPED)
        after = git(self.desk.root, "rev-parse", "HEAD")
        self.report.archive_sha = sha
        self.report.ran.append("archive")
        if before == after:
            self.step_line("archive", f"nothing new to commit; HEAD is {sha[:12]}")
        else:
            self.step_line("archive", f"one commit {sha}"
                                      + (" (pushed)" if self.desk.cfg.get("push") else ""))


def close_task(desk: Desk, task_id: str, next_card: Optional[str] = None,
               note: Optional[str] = None, dry_run: bool = False, commit: bool = True,
               say: Optional[Callable[[str], None]] = None) -> CloseReport:
    """Run the six steps. Raises `CloseStopped` at the first one that fails."""
    task_id = (task_id or "").strip()
    if not task_id:
        raise CloseStopped("arguments", "no task id given", EXIT_MALFORMED)
    if next_card is not None and not next_card.strip():
        raise CloseStopped("arguments", "--next is empty; leave it out or say what is next",
                           EXIT_MALFORMED)
    if note is not None and "\n" in note:
        raise CloseStopped("arguments", "--note is one sentence, on one line", EXIT_MALFORMED)
    try:
        packet = parse_packet(find_packet(desk, task_id))
    except BellhopError:
        with desk_lock(desk):
            # Preserve the evidence step's diagnostic and recovery record.
            Closing(desk, task_id, dry_run=dry_run, say=say).evidence()
        raise  # evidence always raises here
    # Always claim the lane before the desk mutex; workers use the same order.
    with acceptance_lock(desk, task_id), lane_lock(desk, packet.lane), desk_lock(desk):
        closing = Closing(desk, task_id, next_card=next_card, note=note, dry_run=dry_run,
                          commit=commit, say=say)
        closing.say(f"── close {task_id}" + (" (dry run)" if dry_run else ""))
        return closing.run()
