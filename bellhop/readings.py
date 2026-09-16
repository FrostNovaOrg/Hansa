"""Weekly readings, and one line per day in `state/readings.md`.

What the first version of this module counted was output: tasks per lane, dead
tasks, cost per receipt, commits per task. On the desk it came from, none of those
numbers explained the bill or the delays. Five others did, and they are what this
module adds:

- **Context per turn.** Turns at or above 250,000 tokens were a small part of the turns and a large part
  of the spend. Every tool call re-sends the whole context, so a long context is a
  multiplier on every remaining turn of that session, not a one-off.
- **Full-chain cost of one task.** A receipt says the worker cost $15. The task cost
  about $21: coordinator share $4, worker $15, verifier $2. Reading the receipt
  alone understates the real price of a task by a third.
- **Repair share.** A large share of packets were the desk repairing its own
  tooling. A desk that spends its capacity on itself is a finding, and no
  count of tasks per lane shows it.
- **Verifier sessions per task.** Median 2, because during a shadow period every
  task was verified twice. Cost per task looked stable while the verification bill
  quietly doubled.
- **Intake duplicates and status questions.** Many of the principal's messages were
  asking what was happening. That is a reading about the desk's own reporting, not
  about the principal.

Every one of these can be `n/a`. A metric whose inputs are missing must not print a
zero: a desk with no transcripts and a desk with no long-context turns would then
look the same.
"""
from __future__ import annotations

import collections
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import ledger as ledger_mod
from . import state as state_mod
from . import transcripts as tr
from .locking import serialized
from . import rolegate as rolegate_mod
from .config import Desk, helper_budget, price_table
from .headers import FENCE_ACCEPTANCE, FENCE_RECEIPT, parse_header
from .util import iso_week, read, read_json, run, write

NOT_AVAILABLE = tr.NOT_AVAILABLE

READINGS_FILE = "state/readings.md"
INTAKE_FILE = "state/intake.md"

COLUMNS = ("date", "turns", "context median", "tail share (turns)", "tail share (cost)",
           "coordinator cost", "compactions", "tasks closed", "full-chain cost per task",
           "repair share", "verifier sessions per task", "intake duplicates",
           "status-question share")

READINGS_HEADER = (
    "# Readings (one line per day; written by `bellhop readings`, never by hand)\n"
    "(re-running a day replaces that day's line rather than adding one; a column the desk's own\n"
    " files cannot answer reads `n/a` and never `0`. Every formula is in docs/metrics.md.)\n"
    "\n"
    "| " + " | ".join(COLUMNS) + " |\n"
    "|" + "---|" * len(COLUMNS) + "\n"
)

# `(duplicate of N` is what the intake hook writes when a message repeats an earlier
# one. The opening bracket is part of the marker: "duplicate of" in a sentence the
# principal wrote is not a hook finding.
_DUP_RE = re.compile(r"\(duplicate of \d+", re.I)
_ENTRY_RE = re.compile(r"^#{2,3}\s+\S")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


# -- the week --------------------------------------------------------------

def _week_bounds(week: str):
    year, w = week.split("-W")
    monday = datetime.strptime(f"{year}-W{int(w):02d}-1", "%G-W%V-%u")
    return monday, monday + timedelta(days=7)


def week_days(week: str) -> List[str]:
    start, _end = _week_bounds(week)
    return [(start + timedelta(days=i)).date().isoformat() for i in range(7)]


def collect(desk: Desk, week: str):
    start, end = _week_bounds(week)
    rows = []
    for p in desk.receipts.glob("*.meta.json"):
        d = read_json(p, {})
        fin = d.get("finished_at", "")
        try:
            t = datetime.strptime(fin, "%Y-%m-%dT%H:%M:%S%z").replace(tzinfo=None)
        except Exception:
            continue
        if start <= t < end:
            rows.append(d)
    return rows


def _meta_day(meta: dict) -> Optional[str]:
    try:
        return datetime.strptime(meta.get("finished_at", ""), "%Y-%m-%dT%H:%M:%S%z").date().isoformat()
    except (ValueError, TypeError):
        return None


def _commits_per_day(desk: Desk, week: str) -> dict:
    start, end = _week_bounds(week)
    out = collections.Counter()
    log = run(["git", "-C", str(desk.root), "log", f"--since={start:%Y-%m-%d}", f"--until={end:%Y-%m-%d}", "--format=%ad", "--date=short"], check=False)
    for line in (log.stdout or "").splitlines():
        out[line.strip()] += 1
    return out


def transcripts_summary(paths: list) -> dict:
    """Sum output tokens per project directory, deduplicating by message id."""
    out = {}
    for root in paths:
        root = Path(root).expanduser()
        if not root.exists():
            continue
        t = tr.parse([root])
        out[str(root)] = {"output_tokens": sum(x.output_tokens for x in t.turns), "messages": len(t.turns)}
    return out


# -- one task, end to end --------------------------------------------------

@dataclass
class Task:
    """One packet that closed in the window, with what the whole chain cost."""

    id: str
    lane: str
    day: Optional[str]
    worker_cost: Optional[float] = None
    verifier_cost: Optional[float] = None
    verifier_sessions: Optional[int] = None
    repair: Optional[bool] = None      # None: the changed files could not be listed
    files: Optional[List[str]] = None


def _float(value) -> Optional[float]:
    """A number, or None. `-` and `n/a` are readings that mean "not computed"."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s in ("-", NOT_AVAILABLE):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def receipt_path(desk: Desk, task_id: str) -> Optional[Path]:
    hits = sorted(desk.receipts.glob(f"{task_id}-*.md"))
    return hits[0] if hits else None


def _header_of(path: Optional[Path], fence: str) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        values, _span = parse_header(read(path), fence)
    except (OSError, UnicodeDecodeError):
        return {}
    return values or {}


def worker_cost(desk: Desk, meta: dict) -> Optional[float]:
    """The receipt header first: it is the number a machine wrote from the result JSON."""
    values = _header_of(receipt_path(desk, str(meta.get("id"))), FENCE_RECEIPT)
    cost = _float(values.get("cost_usd"))
    # The header writes `-` for a number it could not compute, and `-` parses to None,
    # not to zero. Only then is the meta file asked.
    return cost if cost is not None else _float(meta.get("cost_usd"))


def verifier_cost(desk: Desk, task_id: str) -> Optional[float]:
    """The verifier's own result JSON if it left one, else its header, else nothing."""
    data = read_json(desk.receipts / f"{task_id}.acceptance.json", None)
    if isinstance(data, dict):
        cost = _float(data.get("cost_usd"))
        if cost is None:
            cost = _float(data.get("total_cost_usd"))
        if cost is not None:
            return cost
    values = _header_of(desk.receipts / f"{task_id}.acceptance.md", FENCE_ACCEPTANCE)
    cost = _float(values.get("cost_usd"))
    return cost if cost is not None else _float(values.get("total_cost_usd"))


def verifier_sessions(desk: Desk, task_id: str) -> Optional[int]:
    """How many verifier sessions this one task took.

    A run leaves a result file; a second run of the same task must leave a
    differently named one or it would overwrite the first. So distinct file stems
    are distinct sessions. Receipts are looked at first and the bell logs only if
    the receipts hold nothing, because one run that wrote in both places is one run.
    """
    families = [
        [p for pat in ("*.json", "*.log") for p in desk.receipts.glob(f"{task_id}.acceptance{pat}")],
        [p for pat in ("*.json", "*.log") for p in desk.bells_logs.glob(f"accept-{task_id}{pat}")],
    ]
    for group in families:
        if group:
            return len({p.name.rsplit(".", 1)[0] for p in group})
    if (desk.receipts / f"{task_id}.acceptance.md").exists():
        return 1  # a verdict is evidence of one session even with no result file
    return 0


def changed_files(desk: Desk, meta: dict) -> Optional[List[str]]:
    """The paths this task touched, from the lane worktree. None when git cannot say."""
    base = meta.get("base")
    head = meta.get("head_after")
    if not head:
        head = _header_of(receipt_path(desk, str(meta.get("id"))), FENCE_RECEIPT).get("head")
    if not base or not head:
        return None
    try:
        lane = desk.lane(str(meta.get("lane")))
    except Exception:
        return None
    worktree = Path(lane["worktree"])
    if not worktree.exists():
        return None
    proc = run(["git", "-C", str(worktree), "diff", "--name-only", f"{base}..{head}"], check=False)
    if proc.returncode != 0:
        return None
    return [l.strip() for l in (proc.stdout or "").splitlines() if l.strip()]


def is_repair(desk: Desk, meta: dict, files: Optional[List[str]] = None) -> Optional[bool]:
    """Did this task work on the desk's own tooling rather than on the product?

    Two ways to be one: the lane is declared maintenance, or every file it touched
    sits in one of the desk's own directories. A task whose files cannot be listed
    is None, not False - "we looked and it was product work" and "we could not look"
    are different readings, and the report prints both.
    """
    if str(meta.get("lane")) in (desk.cfg.get("maintenance_lanes") or []):
        return True
    files = changed_files(desk, meta) if files is None else files
    if files is None:
        return None
    if not files:
        return None  # nothing changed: not evidence of a repair
    own = set(desk.cfg.get("desk_dirs") or [])
    return all(Path(f).parts and Path(f).parts[0] in own for f in files)


def tasks(desk: Desk, metas: Sequence[dict]) -> List[Task]:
    out = []
    for meta in metas:
        tid = str(meta.get("id") or "")
        if not tid:
            continue
        files = changed_files(desk, meta)
        out.append(Task(
            id=tid,
            lane=str(meta.get("lane") or ""),
            day=_meta_day(meta),
            worker_cost=worker_cost(desk, meta),
            verifier_cost=verifier_cost(desk, tid),
            verifier_sessions=verifier_sessions(desk, tid),
            repair=is_repair(desk, meta, files),
            files=files,
        ))
    return out


# -- the full chain --------------------------------------------------------

@dataclass
class Chain:
    """Full-chain cost of a task: the median, and the three parts it is made of."""

    tasks: int = 0
    complete: int = 0
    median: Optional[float] = None
    worker: Optional[float] = None
    verifier: Optional[float] = None
    coordinator: Optional[float] = None

    def line(self) -> str:
        if self.median is None:
            return f"{NOT_AVAILABLE} ({self.complete}/{self.tasks} tasks have all three parts)"
        return (f"{money(self.median)} = coordinator {money(self.coordinator)} + worker {money(self.worker)}"
                f" + verifier {money(self.verifier)} (median over {self.complete}/{self.tasks} tasks)")


def _median(values: Sequence[Optional[float]]) -> Optional[float]:
    xs = [v for v in values if v is not None]
    return statistics.median(xs) if xs else None


def chain(task_rows: Sequence[Task], coordinator_cost: Dict[str, Optional[float]],
          closed_per_day: Dict[str, int]) -> Chain:
    """Worker + verifier + that day's coordinator cost divided by that day's tasks.

    The coordinator's share is per day and not per task because the coordinator does
    not bill per task: it runs one long session that dispatches, reads and decides.
    Dividing the day is the only honest way to attribute it, and it is why a day
    with one task carries the whole day's coordination on that task.
    """
    c = Chain(tasks=len(task_rows))
    totals, workers, verifiers, coords = [], [], [], []
    for t in task_rows:
        closed = closed_per_day.get(t.day or "", 0)
        day_cost = coordinator_cost.get(t.day or "")
        share = (day_cost / closed) if (day_cost is not None and closed) else None
        if t.worker_cost is not None:
            workers.append(t.worker_cost)
        if t.verifier_cost is not None:
            verifiers.append(t.verifier_cost)
        if share is not None:
            coords.append(share)
        if None not in (t.worker_cost, t.verifier_cost, share):
            totals.append(t.worker_cost + t.verifier_cost + share)
    c.complete = len(totals)
    c.median = _median(totals)
    c.worker = _median(workers)
    c.verifier = _median(verifiers)
    c.coordinator = _median(coords)
    return c


def repair_share(task_rows: Sequence[Task]):
    """-> (share or None, repairs, packets, unknown)."""
    if not task_rows:
        return None, 0, 0, 0
    repairs = sum(1 for t in task_rows if t.repair is True)
    unknown = sum(1 for t in task_rows if t.repair is None)
    return repairs / len(task_rows), repairs, len(task_rows), unknown


def verifier_stats(task_rows: Sequence[Task]):
    """-> (median, max) sessions per task, or (None, None)."""
    xs = [t.verifier_sessions for t in task_rows if t.verifier_sessions is not None]
    if not xs:
        return None, None
    return statistics.median(xs), max(xs)


# -- the intake book -------------------------------------------------------

@dataclass
class Intake:
    """What the principal actually sent, counted by the hook's own book."""

    present: bool = False
    entries: int = 0
    duplicates: int = 0
    questions: int = 0
    entries_by_day: collections.Counter = field(default_factory=collections.Counter)
    dups_by_day: collections.Counter = field(default_factory=collections.Counter)
    questions_by_day: collections.Counter = field(default_factory=collections.Counter)
    undated: int = 0

    def question_share(self, day: Optional[str] = None) -> Optional[float]:
        if not self.present:
            return None
        total = self.entries_by_day.get(day, 0) if day else self.entries
        if not total:
            return None
        hits = self.questions_by_day.get(day, 0) if day else self.questions
        return hits / total


def status_patterns(desk: Desk) -> List["re.Pattern"]:
    out = []
    for raw in desk.cfg.get("status_question_patterns") or []:
        try:
            out.append(re.compile(raw, re.I))
        except re.error:
            continue  # a desk's bad regex is not a reason to lose the whole reading
    return out


def read_intake(desk: Desk) -> Intake:
    """Parse `state/intake.md`, whatever shape the hook that writes it chose.

    Entries are `##` headings when the book has them and `- ` rows when it does not;
    a day comes from the first date in the entry's first line. An entry with no date
    is counted in the totals and left out of the day lines, because putting it on
    today would move a message the principal sent last week.
    """
    path = desk.path(INTAKE_FILE)
    reading = Intake()
    if not path.exists():
        return reading
    try:
        text = read(path)
    except (OSError, UnicodeDecodeError):
        return reading
    reading.present = True
    patterns = status_patterns(desk)
    lines = text.split("\n")
    entries: List[List[str]] = []
    if any(_ENTRY_RE.match(l) for l in lines):
        for line in lines:
            if _ENTRY_RE.match(line):
                entries.append([line])
            elif entries:
                entries[-1].append(line)
    else:
        for line in lines:
            if line.startswith("- "):
                entries.append([line])
            elif entries:
                entries[-1].append(line)
    for entry in entries:
        head = entry[0]
        body = "\n".join(entry)
        m = _DATE_RE.search(head) or _DATE_RE.search(body)
        day = m.group(1) if m else None
        dups = len(_DUP_RE.findall(body))
        question = any(p.search(body) for p in patterns)
        reading.entries += 1
        reading.duplicates += dups
        reading.questions += 1 if question else 0
        if day:
            reading.entries_by_day[day] += 1
            reading.dups_by_day[day] += dups
            if question:
                reading.questions_by_day[day] += 1
        else:
            reading.undated += 1
    return reading


# -- formatting ------------------------------------------------------------

def money(value: Optional[float]) -> str:
    return NOT_AVAILABLE if value is None else f"${value:.2f}"


def share(value: Optional[float]) -> str:
    return NOT_AVAILABLE if value is None else f"{100 * value:.0f}%"


def number(value) -> str:
    return NOT_AVAILABLE if value is None else str(value)


def grouped(value) -> str:
    """Same number, with separators. The weekly report is read by a person; the
    day lines in state/readings.md are read by a script, and keep the plain form."""
    return NOT_AVAILABLE if value is None else f"{value:,}"


# -- state/readings.md -----------------------------------------------------

def day_line(day: str, reading: Optional[tr.Reading], closed: int, chain_reading: Chain,
             rep, verifiers, intake: Intake) -> str:
    rep_share, _repairs, packets, _unknown = rep
    v_median, v_max = verifiers
    cells = [
        day,
        number(reading.turns if reading else None),
        number(reading.context_median if reading else None),
        share(reading.tail_turn_share if reading else None),
        share(reading.tail_cost_share if reading else None),
        money(reading.cost if reading else None),
        number(reading.compactions if reading else None),
        str(closed),
        money(chain_reading.median),
        share(rep_share) if packets else NOT_AVAILABLE,
        f"{v_median:g}/{v_max}" if v_median is not None else NOT_AVAILABLE,
        number(intake.dups_by_day.get(day, 0) if intake.present and intake.entries_by_day.get(day) else None),
        share(intake.question_share(day)),
    ]
    return "| " + " | ".join(cells) + " |"


@serialized
def write_day_lines(desk: Desk, rows: Dict[str, str]) -> Optional[Path]:
    """Append the day's line, or replace the one already there. Same day, same line.

    One writer, previous version kept: the same two rules every other desk state file
    is written by. A readings file with a second writer drifts, and a day measured
    twice must not become two rows that disagree.
    """
    if not rows:
        return None
    path = desk.path(READINGS_FILE)
    if path.exists():
        try:
            existing = read(path)
        except (OSError, UnicodeDecodeError):
            existing = READINGS_HEADER
        try:
            state_mod.backup(desk, path)
        except state_mod.StateError:
            pass
    else:
        existing = READINGS_HEADER
    head, body = [], {}
    for line in existing.split("\n"):
        if line.startswith("| ") and not line.startswith("|---") and not line.startswith("| date "):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and _DATE_RE.fullmatch(cells[0]):
                body[cells[0]] = line
                continue
        head.append(line)
    while head and not head[-1].strip():
        head.pop()
    body.update(rows)
    text = "\n".join(head + [body[d] for d in sorted(body)]) + "\n"
    write(path, text)
    return path


# -- the weekly report -----------------------------------------------------

def _tail_line(reading: Optional[tr.Reading], tail_tokens: int) -> str:
    if reading is None or not reading.turns:
        return NOT_AVAILABLE
    cost = share(reading.tail_cost_share)
    return (f"{share(reading.tail_turn_share)} of turns ({reading.tail_turns}/{reading.turns}), "
            f"{cost} of cost")


def _helper_line(parsed: tr.Transcript, prices: dict, desk: Desk) -> str:
    """How many helper sessions passed the declared budget, out of how many.

    No transcripts is `n/a`, not zero: a helper nobody measured and a helper that
    stayed inside its budget are the same line otherwise.
    """
    max_turns, max_cost = helper_budget(desk)
    rows = tr.helpers(parsed, prices)
    if not rows:
        return f"{NOT_AVAILABLE} (no helper sessions in the transcripts given)"
    over = tr.over_budget(rows, max_turns, max_cost)
    unpriced = sum(1 for h in rows if not h.priced)
    text = f"{len(over)} of {len(rows)} (over {max_turns} turns or ${max_cost:.2f})"
    if over:
        worst = max(over, key=lambda h: h.turns)
        text += f"; worst {worst.turns} turns, {money(worst.cost)}"
    if unpriced:
        text += f"; {unpriced} unpriced"
    return text


def _self_written_line(desk: Desk, days: Sequence[str]) -> str:
    """Writes the coordinator made itself, through a hand window, per day."""
    try:
        counted = rolegate_mod.writes_by_day(desk)
    except Exception:
        return NOT_AVAILABLE
    if not rolegate_mod.enabled(desk):
        return f"{NOT_AVAILABLE} (role_gate is off)"
    week_days_counted = {d: n for d, n in counted.items() if d in set(days)}
    total = sum(week_days_counted.values())
    if not total:
        return "0"
    worst_day, worst = max(week_days_counted.items(), key=lambda kv: kv[1])
    return f"{total} this week, most on one day {worst} ({worst_day})"


def report(desk: Desk, week: Optional[str] = None, transcripts: Optional[list] = None) -> Path:
    week = week or iso_week()
    days = week_days(week)
    rows = collect(desk, week)
    by_lane = collections.Counter(r.get("lane") for r in rows)
    by_model = collections.Counter(f"{r.get('provider')}/{r.get('model')}" for r in rows)
    dead = [r for r in rows if r.get("dead")]
    costs = [float(r["cost_usd"]) for r in rows if r.get("cost_usd") is not None]
    durs = [float(r["duration_s"]) for r in rows if r.get("duration_s") is not None]
    titles = collections.Counter()
    for r in rows:
        titles[r.get("id")] += 1
    commits = _commits_per_day(desk, week)
    count, size = ledger_mod.stats(desk)

    tail_tokens = int(desk.cfg.get("context_tail_tokens") or tr.DEFAULT_TAIL_TOKENS)
    prices = price_table(desk)
    parsed = tr.parse(transcripts or [], days=days)
    daily = tr.by_day(parsed, tail_tokens=tail_tokens, prices=prices)
    whole = tr.overall(parsed, label=week, tail_tokens=tail_tokens, prices=prices) if parsed.turns or parsed.intake else None

    task_rows = tasks(desk, rows)
    closed_per_day = collections.Counter(t.day for t in task_rows if t.day)
    coordinator_cost = {d: r.cost for d, r in daily.items()}
    chain_week = chain(task_rows, coordinator_cost, closed_per_day)
    rep_week = repair_share(task_rows)
    ver_week = verifier_stats(task_rows)
    intake = read_intake(desk)

    lines = [f"# Weekly readings {week}", "", "## Outcome numbers (fill by hand)", "",
             "| Number | Value |", "|---|---|",
             "| Report-to-release (hours, median) | _fill_ |",
             "| Post-release defects found by the principal | _fill_ |",
             f"| Rework rate | {'n/a' if not rows else f'{sum(1 for c in titles.values() if c > 1)}/{len(titles)}'} |",
             "", "## Process numbers (diagnostics)", "",
             "| Number | Value |", "|---|---|",
             f"| Tasks | {len(rows)} |",
             f"| By lane | {', '.join(f'{k}: {v}' for k, v in sorted(by_lane.items())) or '-'} |",
             f"| By provider/model | {', '.join(f'{k}: {v}' for k, v in sorted(by_model.items())) or '-'} |",
             f"| Dead tasks | {len(dead)} |",
             f"| Median duration (s) | {statistics.median(durs):.0f} |" if durs else "| Median duration (s) | - |",
             f"| Cost known for | {len(costs)}/{len(rows)} tasks, total ${sum(costs):.2f} |",
             f"| Desk commits per day | {', '.join(f'{d}: {n}' for d, n in sorted(commits.items())) or '-'} |",
             f"| Commits per task | {(sum(commits.values()) / len(rows)):.1f} |" if rows else "| Commits per task | - |",
             f"| Ledger | {count} entries, {size} bytes |"]

    rep_share_value, repairs, packets, unknown = rep_week
    v_median, v_max = ver_week
    lines += ["", "## Cost and context", "",
              "The numbers a receipt cannot show: what the coordinator carried, what the whole",
              "chain cost, and how much of the week the desk spent on itself.", "",
              "| Number | Value |", "|---|---|",
              f"| Coordinator turns | {number(whole.turns if whole else None)} |",
              f"| Context per turn (median / p90 / max) | {grouped(whole.context_median if whole else None)} / "
              f"{grouped(whole.context_p90 if whole else None)} / {grouped(whole.context_max if whole else None)} |",
              f"| Turns at or above {tail_tokens:,} tokens | {_tail_line(whole, tail_tokens)} |",
              f"| Coordinator cost | {money(whole.cost if whole else None)}"
              + (f" ({whole.priced_turns}/{whole.turns} turns priced)" if whole and whole.turns else "") + " |",
              f"| Compactions | {number(whole.compactions if whole else None)} |",
              f"| Full-chain cost per task | {chain_week.line()} |",
              f"| Repair share | {share(rep_share_value) if packets else NOT_AVAILABLE}"
              + (f" ({repairs}/{packets} packets, {unknown} could not be listed)" if packets else "") + " |",
              f"| Verifier sessions per task | "
              + (f"median {v_median:g}, max {v_max}" if v_median is not None else NOT_AVAILABLE) + " |",
              f"| Intake duplicates | {number(intake.duplicates) if intake.present else NOT_AVAILABLE} |",
              f"| Status questions | {share(intake.question_share())}"
              + (f" of {intake.entries} intake entries" if intake.present else "") + " |",
              # Two rows about the desk's own habits, at the end of the table. A helper
              # is read-only and never appears in the chain from bell to verdict, so
              # nothing else in this report can show what one cost; a coordinator's own
              # write leaves no packet, no receipt and no verdict, so nothing else can
              # show that it happened.
              f"| Helpers over budget | {_helper_line(parsed, prices, desk)} |",
              f"| Self-implemented writes | {_self_written_line(desk, days)} |"]

    lines += ["", "## Where the coordinator's turns went", "",
              "Characters entering the context, by source. This is the table to read when the",
              "context median is high: it says which mouth is filling the window.", "",
              "| Source | Characters | Share |", "|---|---|---|"]
    shares = whole.intake_shares() if whole else []
    if shares:
        lines += [f"| {name} | {grouped(chars)} | {share(part)} |" for name, chars, part in shares]
    else:
        lines.append(f"| {NOT_AVAILABLE} (no transcripts given) | - | - |")

    if transcripts:
        lines += ["", "## Output tokens (message ids deduplicated)", "", "| Path | Output tokens | Messages |", "|---|---|---|"]
        for k, v in transcripts_summary(transcripts).items():
            lines.append(f"| {k} | {v['output_tokens']} | {v['messages']} |")

    day_rows = {}
    for day in days:
        reading = daily.get(day)
        closed = closed_per_day.get(day, 0)
        if reading is None and not closed and not intake.entries_by_day.get(day):
            continue
        day_tasks = [t for t in task_rows if t.day == day]
        day_rows[day] = day_line(day, reading, closed,
                                 chain(day_tasks, coordinator_cost, closed_per_day),
                                 repair_share(day_tasks), verifier_stats(day_tasks), intake)
    state_path = write_day_lines(desk, day_rows)
    if state_path is not None:
        lines += ["", f"One line per day is in `{READINGS_FILE}`."]

    out = desk.path("reports", "weekly", f"{week}.md")
    write(out, "\n".join(lines) + "\n")
    return out
