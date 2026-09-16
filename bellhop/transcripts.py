"""Reading Claude Code transcripts: what a turn carried, and what it cost.

The readings a desk starts with count what came out — tasks, receipts, output
tokens. On the production desk this module comes from, those were not the numbers
that explained the bill. Three others were:

- **Context per turn.** Turns at or above 250,000 tokens were a small part of the turns and a large part
  of the spend, because every tool call re-sends the whole context. Output tokens
  cannot see that: a 300-token answer on a 400k context costs many times the same
  answer on a 40k one.
- **Where the context came from.** Attributing every character entering the context
  to a source — this tool's results, that hook's text, the principal's words, the
  coordinator's own — is what turns "the context is large" into something to fix.
- **Compactions.** A day with several compactions is a day the coordinator lost its
  thread and paid to rebuild it.

Two rules this module keeps:

- **Merge by message id.** Claude Code writes one line per content block and repeats
  the message's `usage` on each of them. Summing without deduplicating inflates
  output tokens two to three times.
- **An unknown model has no price.** A model with no row in the price table yields
  `None`, and every reading built on it reads `n/a`. A guessed price and a real one
  look the same once they are printed as a dollar amount.

Boundary: what leaves this module is timestamps, token counts, tool names and
character counts. Message text is measured, never copied into a reading.
"""
from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

NOT_AVAILABLE = "n/a"

# $ per million tokens. Overridden per desk by the `prices` config key; a prefix is
# matched against the model id and against the id with a leading `claude-` removed,
# longest prefix wins.
PRICE_DEFAULTS = {
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
    "fable": {"input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_write": 12.5},
}
PRICE_KEYS = ("input", "output", "cache_read", "cache_write")

DEFAULT_TAIL_TOKENS = 250_000

USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")

SRC_PRINCIPAL = "the principal's words"
SRC_HOOK = "hooks and reminders"
SRC_ASSISTANT = "the coordinator's own words"
SRC_TOOL_CALL = "tool calls (text sent)"
SRC_TOOL_RESULT = "tool result: {}"

_SYSTEM_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


# -- prices ----------------------------------------------------------------

def price_for(model: Optional[str], prices: Optional[dict] = None) -> Optional[dict]:
    """The price row for a model, or None. None is a reading; it is not zero."""
    table = PRICE_DEFAULTS if prices is None else prices
    if not model:
        return None
    names = [str(model)]
    if names[0].startswith("claude-"):
        names.append(names[0][len("claude-"):])
    best = None
    for prefix, row in table.items():
        if not prefix or not isinstance(row, dict):
            continue
        if any(n.startswith(prefix) for n in names):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, row)
    if best is None:
        return None
    row = best[1]
    if not all(k in row for k in PRICE_KEYS):
        return None  # a half-filled row is not a price
    try:
        return {k: float(row[k]) for k in PRICE_KEYS}
    except (TypeError, ValueError):
        return None


# -- one turn --------------------------------------------------------------

@dataclass
class Turn:
    """One assistant message: what it carried in, what it wrote out, what it cost."""

    day: str
    model: str = ""
    input_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output_tokens: int = 0
    tools: tuple = ()
    compaction: bool = False
    session: str = ""
    # A helper's turn carries the subagent's id; the coordinator's carries none. The
    # two are in the same transcript tree and cost the same money, and a reading that
    # cannot tell them apart reports a helper's bill as the coordinator's.
    agent: str = ""
    #: Tokens the provider billed as reasoning, when it itemised them. `None` is "it did
    #: not say", and it is not `0`: a turn that did no thinking and a turn whose thinking
    #: was billed inside the output tokens are different readings.
    thinking_tokens: Optional[int] = None

    @property
    def context(self) -> int:
        """What the API call actually carried: fresh input plus both cache halves."""
        return self.input_tokens + self.cache_read + self.cache_write

    @property
    def billed(self) -> int:
        """Every token this turn was charged for, carried in and written out."""
        return self.context + self.output_tokens

    def cost(self, prices: Optional[dict] = None) -> Optional[float]:
        p = price_for(self.model, prices)
        if p is None:
            return None
        return (self.input_tokens * p["input"]
                + self.cache_read * p["cache_read"]
                + self.cache_write * p["cache_write"]
                + self.output_tokens * p["output"]) / 1_000_000.0


@dataclass
class Transcript:
    """Everything parsed out of one or more transcript files."""

    turns: List[Turn] = field(default_factory=list)
    intake: Dict[str, Counter] = field(default_factory=dict)      # day -> source -> characters
    compactions: Counter = field(default_factory=Counter)         # day -> count
    files: int = 0
    unreadable_lines: int = 0

    def days(self) -> List[str]:
        seen = {t.day for t in self.turns} | set(self.intake) | set(self.compactions)
        return sorted(d for d in seen if d)


# -- parsing ---------------------------------------------------------------

def iter_files(paths: Iterable) -> List[Path]:
    """A path may be one transcript or a directory of them. Directories recurse."""
    out: List[Path] = []
    for raw in paths or []:
        p = Path(raw).expanduser()
        if p.is_dir():
            out.extend(sorted(p.rglob("*.jsonl")))
        elif p.exists():
            out.append(p)
    return list(dict.fromkeys(p.resolve() for p in out))


def _local_day(stamp) -> Optional[str]:
    """ISO timestamp -> local calendar day. A day is where the operator lives."""
    if not stamp or not isinstance(stamp, str):
        return None
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().date().isoformat()


def _blocks(content) -> List[dict]:
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _text_len(value) -> int:
    """Characters a value adds to the context, whatever shape it arrives in."""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_text_len(v) for v in value)
    if isinstance(value, dict):
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            return len(value["text"])
        try:
            return len(json.dumps(value, ensure_ascii=False))
        except (TypeError, ValueError):
            return 0
    return len(str(value))


def _add(acc: Transcript, day: str, source: str, chars: int) -> None:
    if chars:
        acc.intake.setdefault(day, Counter())[source] += chars


def parse_file(path: Path, acc: Optional[Transcript] = None, days: Optional[Sequence[str]] = None) -> Transcript:
    """Parse one transcript into `acc`. Bad lines are counted, never fatal."""
    acc = acc if acc is not None else Transcript()
    window = set(days) if days is not None else None
    by_id: Dict[str, Turn] = {}
    tool_names: Dict[str, str] = {}
    try:
        fh = Path(path).open(encoding="utf-8", errors="ignore")
    except OSError:
        return acc
    acc.files += 1
    with fh:
        for n, raw in enumerate(fh):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                acc.unreadable_lines += 1
                continue
            if not isinstance(entry, dict):
                acc.unreadable_lines += 1
                continue
            msg = entry.get("message") if isinstance(entry.get("message"), dict) else {}
            etype = entry.get("type")
            blocks = _blocks(msg.get("content") if msg else entry.get("content"))
            # The id -> tool name map is built whatever the window says: a call made
            # before midnight can have its result arrive after it.
            if etype == "assistant":
                for b in blocks:
                    if b.get("type") == "tool_use":
                        tool_names[b.get("id")] = str(b.get("name") or "?")
            day = _local_day(entry.get("timestamp"))
            if day is None or (window is not None and day not in window):
                continue
            if entry.get("isCompactSummary"):
                acc.compactions[day] += 1
            if etype == "assistant":
                usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
                names = tuple(str(b.get("name") or "?") for b in blocks if b.get("type") == "tool_use")
                if any(k in usage for k in USAGE_KEYS):
                    mid = msg.get("id") or "{}#{}".format(Path(path).name, n)
                    turn = by_id.get(mid)
                    if turn is None:
                        turn = Turn(day=day, model=str(msg.get("model") or ""), session=Path(path).stem,
                                    agent=str(entry.get("agentId") or "") if entry.get("isSidechain") else "")
                        by_id[mid] = turn
                        acc.turns.append(turn)
                    # Same id, several lines, one usage: take the largest reading of
                    # each field rather than adding them up.
                    turn.input_tokens = max(turn.input_tokens, _int(usage.get("input_tokens")))
                    turn.cache_read = max(turn.cache_read, _int(usage.get("cache_read_input_tokens")))
                    turn.cache_write = max(turn.cache_write, _int(usage.get("cache_creation_input_tokens")))
                    turn.output_tokens = max(turn.output_tokens, _int(usage.get("output_tokens")))
                    billed_thinking = _thinking(usage)
                    if billed_thinking is not None:
                        turn.thinking_tokens = max(turn.thinking_tokens or 0, billed_thinking)
                    turn.tools = tuple(dict.fromkeys(turn.tools + names))
                    turn.compaction = turn.compaction or bool(entry.get("isCompactSummary"))
                for b in blocks:
                    if b.get("type") == "tool_use":
                        try:
                            _add(acc, day, SRC_TOOL_CALL, len(json.dumps(b.get("input") or {}, ensure_ascii=False)))
                        except (TypeError, ValueError):
                            pass
                    elif b.get("type") == "text":
                        _add(acc, day, SRC_ASSISTANT, _text_len(b.get("text")))
            elif etype == "user":
                for b in blocks:
                    if b.get("type") == "tool_result":
                        name = tool_names.get(b.get("tool_use_id"), "?")
                        _add(acc, day, SRC_TOOL_RESULT.format(name), _text_len(b.get("content")))
                    elif b.get("type") == "text":
                        text = b.get("text") if isinstance(b.get("text"), str) else ""
                        injected = sum(len(m) for m in _SYSTEM_RE.findall(text))
                        rest = len(text) - injected
                        if entry.get("isMeta"):
                            _add(acc, day, SRC_HOOK, len(text))
                        else:
                            _add(acc, day, SRC_HOOK, injected)
                            _add(acc, day, SRC_PRINCIPAL, rest)
                    else:
                        _add(acc, day, SRC_HOOK if entry.get("isMeta") else SRC_PRINCIPAL, _text_len(b))
            elif etype == "system":
                _add(acc, day, SRC_HOOK, _text_len(entry.get("content")))
    return acc


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


#: Where a provider puts the reasoning it billed for. A vendor that itemises it is the
#: only reason this number exists; a vendor that folds it into `output_tokens` reports
#: nothing here, and the reading stays `n/a` rather than becoming a zero.
THINKING_USAGE_KEYS = ("thinking_tokens", "reasoning_tokens", "thinking_output_tokens")


def _thinking(usage: dict) -> Optional[int]:
    for key in THINKING_USAGE_KEYS:
        if isinstance(usage.get(key), (int, float)) and not isinstance(usage.get(key), bool):
            return int(usage[key])
    details = usage.get("output_tokens_details")
    if isinstance(details, dict):
        value = details.get("reasoning_tokens")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return None


def parse(paths: Iterable, days: Optional[Sequence[str]] = None) -> Transcript:
    acc = Transcript()
    for f in iter_files(paths):
        parse_file(f, acc, days=days)
    return acc


# -- aggregates ------------------------------------------------------------

def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Nearest-rank percentile. Defined for one value, which `quantiles` is not."""
    xs = sorted(values)
    if not xs:
        return None
    k = max(1, min(len(xs), int(-(-q * len(xs) // 1))))
    return xs[k - 1]


@dataclass
class Reading:
    """One row of context-and-cost numbers, over a day or over a whole week."""

    label: str
    turns: int = 0
    context_median: Optional[int] = None
    context_p90: Optional[int] = None
    context_max: Optional[int] = None
    tail_turns: int = 0
    tail_turn_share: Optional[float] = None
    tail_cost_share: Optional[float] = None
    cost: Optional[float] = None
    priced_turns: int = 0
    output_tokens: int = 0
    billed_tokens: int = 0
    thinking_tokens: Optional[int] = None
    thinking_share: Optional[float] = None
    compactions: int = 0
    intake: Counter = field(default_factory=Counter)
    models: Counter = field(default_factory=Counter)

    @property
    def intake_total(self) -> int:
        return sum(self.intake.values())

    def intake_shares(self) -> List[tuple]:
        """[(source, characters, share)] biggest first. Empty when nothing was read."""
        total = self.intake_total
        if not total:
            return []
        return [(k, v, v / total) for k, v in self.intake.most_common()]


def summarise(turns: Sequence[Turn], label: str = "", tail_tokens: int = DEFAULT_TAIL_TOKENS,
              prices: Optional[dict] = None, intake: Optional[Counter] = None,
              compactions: int = 0) -> Reading:
    """Turns -> one Reading. Every field an empty list cannot answer stays None."""
    r = Reading(label=label, intake=Counter(intake or {}), compactions=compactions)
    r.turns = len(turns)
    if not turns:
        return r
    contexts = [t.context for t in turns]
    r.context_median = int(statistics.median(contexts))
    r.context_p90 = int(percentile(contexts, 0.9) or 0)
    r.context_max = max(contexts)
    r.output_tokens = sum(t.output_tokens for t in turns)
    r.billed_tokens = sum(t.billed for t in turns)
    # Only the turns whose provider itemised it. A day where nobody said reads `n/a`;
    # a day where one turn said 1,100,000 reads 1,100,000 and says what share of the
    # whole day's billed tokens that was.
    itemised = [t.thinking_tokens for t in turns if t.thinking_tokens is not None]
    r.thinking_tokens = sum(itemised) if itemised else None
    if r.thinking_tokens is not None and r.billed_tokens:
        r.thinking_share = r.thinking_tokens / r.billed_tokens
    r.models = Counter(t.model or NOT_AVAILABLE for t in turns)
    tail = [t for t in turns if t.context >= tail_tokens]
    r.tail_turns = len(tail)
    r.tail_turn_share = len(tail) / len(turns)
    priced = [(t, t.cost(prices)) for t in turns]
    known = [(t, c) for t, c in priced if c is not None]
    r.priced_turns = len(known)
    if known:
        total = sum(c for _t, c in known)
        r.cost = total
        # The tail's share of the money, over the turns that have a price. A share
        # computed over token counts and one computed over money look the same
        # printed as "46%", and cache reads are a tenth of the price of fresh input.
        r.tail_cost_share = (sum(c for t, c in known if t.context >= tail_tokens) / total) if total else None
    return r


# -- helpers ---------------------------------------------------------------
# A scout, a walkthrough and an acceptance reader are cheap per turn and invisible
# per task: nothing in the chain from bell to verdict mentions them. On one desk
# read-only scouts cost **more than the verifiers**, against no cap at
# all. So a helper session is counted here as its own thing, and the only judgement
# offered is whether it passed the budget the config declares.

@dataclass
class Helper:
    """One subagent session: its turns, and its cost when every turn had a price."""

    agent: str
    day: str = ""
    turns: int = 0
    cost: Optional[float] = None
    priced: bool = False


def helpers(transcript: Transcript, prices: Optional[dict] = None) -> List[Helper]:
    """One row per helper session, oldest day first.

    A session with one unpriced turn has no cost at all rather than a partial sum: a
    total that is missing a turn and a complete one read the same as a dollar amount,
    and only one of them can be held against a budget.
    """
    groups: Dict[str, List[Turn]] = {}
    for turn in transcript.turns:
        if turn.agent:
            groups.setdefault(turn.agent, []).append(turn)
    out: List[Helper] = []
    for agent, turns in groups.items():
        costs = [t.cost(prices) for t in turns]
        priced = all(c is not None for c in costs)
        out.append(Helper(agent=agent, day=min(t.day for t in turns), turns=len(turns),
                          cost=sum(c for c in costs if c is not None) if priced else None,
                          priced=priced))
    return sorted(out, key=lambda h: (h.day, h.agent))


def over_budget(rows: Sequence[Helper], max_turns: int, max_cost: float) -> List[Helper]:
    """The helper sessions that passed the declared budget, on turns or on money."""
    out = []
    for h in rows:
        if max_turns and h.turns > max_turns:
            out.append(h)
        elif max_cost and h.cost is not None and h.cost > max_cost:
            out.append(h)
    return out


def by_day(transcript: Transcript, tail_tokens: int = DEFAULT_TAIL_TOKENS,
           prices: Optional[dict] = None) -> Dict[str, Reading]:
    per_day: Dict[str, List[Turn]] = {}
    for t in transcript.turns:
        per_day.setdefault(t.day, []).append(t)
    out: Dict[str, Reading] = {}
    for day in transcript.days():
        out[day] = summarise(per_day.get(day, []), label=day, tail_tokens=tail_tokens, prices=prices,
                             intake=transcript.intake.get(day), compactions=transcript.compactions.get(day, 0))
    return out


def overall(transcript: Transcript, label: str = "week", tail_tokens: int = DEFAULT_TAIL_TOKENS,
            prices: Optional[dict] = None) -> Reading:
    intake = Counter()
    for c in transcript.intake.values():
        intake.update(c)
    return summarise(transcript.turns, label=label, tail_tokens=tail_tokens, prices=prices,
                     intake=intake, compactions=sum(transcript.compactions.values()))
