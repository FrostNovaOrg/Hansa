"""Provider adapters: who answers a bell, with what command, and what came back.

A lane names a *provider*. The provider knows three things and nothing else: how to
build the command that starts one fresh worker session, what environment that command
may see, and how to read the result it printed. Everything above this module -- the
bell, the lock, the worktree, the receipt, the archive -- is the same whoever answers.

Why this shape, from a desk that ran three vendors' CLIs on three lanes:

- **A command allowlist per provider, enforced by a guard that returns a reason.**
  The first version refused by cancelling the session, and a worker whose turn was
  cancelled looks exactly like a worker that finished: same silence, same empty log.
  `bellhop provider-guard` exits 2 and *says why*, so the refusal reaches the model as
  a sentence it can act on and the session continues.
- **A sandbox profile per non-Claude provider.** Claude Code has its own permission
  gate; the others get a kernel-level one. The profile is a file the lane names, and a
  named file that is not there refuses the launch instead of running unsandboxed --
  an unsandboxed run and a sandboxed one leave the same receipt.
- **One result parser that never throws.** It runs on the closing path, after the work
  is done; a traceback there turns "the task finished" into a stack trace in the log
  and no receipt footer. A number it cannot find reads `n/a`, which is not the same
  reading as `0`.
- **The schedule override is applied by the RUN time, not the dispatch time.** The
  first version compared the packet's `created:` stamp, so a packet written at 09:00
  and answered at 15:00 was priced as if it had run at 09:00. A bell can sit in
  `pending/` for hours; the only clock that matters is the one at launch.
- **`effort: heavy` on a packet raises the model or the effort one notch.** One
  dispatch path, one marker, no second command.

The verifier remains a separate Claude Code session. `accept_model` and
`accept_effort` deliberately do not inherit worker providers, schedules or --heavy.
Worker settings describe execution; acceptance has its own budget and authority.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

from .config import Desk
from .util import BellhopError, now, scrubbed_env

# A number the parser could not find. It is deliberately not `0`: a task that cost
# nothing and a task whose cost was never reported read the same as `0` and do not
# mean the same thing. `watch.py` turns this back into JSON `null` before it reaches
# `<id>.meta.json`, because the readings tell `null` from a number and would choke on
# a string.
NA = "n/a"

# Highest first would be wrong here: `notch_up` walks right by one.
EFFORT_LADDER = ("low", "medium", "high", "xhigh")

DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
DAY_ALIASES = {
    "*": "mon-sun", "all": "mon-sun", "any": "mon-sun", "daily": "mon-sun",
    "everyday": "mon-sun", "weekdays": "mon-fri", "weekends": "sat,sun",
}

# What a `worker_cmd` template may say. Anything else in braces is left alone, so a
# template can still write `${HOME}` or a shell parameter expansion.
CMD_PLACEHOLDERS = ("prompt_file", "cwd", "model", "log", "allowlist")


class ProviderRefusal(BellhopError):
    """The worker was not started, and this is the reason.

    Raised before the lane lock is taken, so a refusal leaves the bell in `pending/`
    with nothing half-done. The watchdog logs the reason and tries again next tick;
    the bell stalls until a person fixes what it names.
    """


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------
_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})$")
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_DAY_TIME_RE = re.compile(r"^([A-Za-z]{3})\s+(\d{1,2}):(\d{2})$")


def parse_when(text: str, today: Optional[datetime] = None) -> datetime:
    """`2026-09-11T15:30`, `15:30`, or `wed 15:30` -> a datetime on the local clock."""
    text = str(text).strip()
    base = today or now()
    m = _ISO_RE.match(text)
    if m:
        y, mo, d, h, mi = (int(g) for g in m.groups())
        return base.replace(year=y, month=mo, day=d, hour=h, minute=mi, second=0, microsecond=0)
    m = _HHMM_RE.match(text)
    if m:
        return base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    m = _DAY_TIME_RE.match(text)
    if m:
        day = DAY_NAMES.get(m.group(1).lower())
        if day is None:
            raise BellhopError(f"not a weekday: {m.group(1)!r} (mon tue wed thu fri sat sun)")
        shifted = base + timedelta(days=(day - base.weekday()) % 7)
        return shifted.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0)
    raise BellhopError(f"cannot read a time from {text!r} (use `2026-09-11T15:30`, `15:30` or `wed 15:30`)")


def clock() -> datetime:
    """The time a schedule decision is made against: launch time, never dispatch time.

    `BELLHOP_NOW` overrides it for fixtures and for `provider check --at`. An override
    that was left set would move every lane at once, so `clock_note()` exists to put it
    in the log: a desk running against a frozen clock and a desk running against the
    real one otherwise read the same.
    """
    override = os.environ.get("BELLHOP_NOW", "").strip()
    return parse_when(override) if override else now()


def clock_note() -> str:
    override = os.environ.get("BELLHOP_NOW", "").strip()
    return f"clock override in effect: BELLHOP_NOW={override}" if override else ""


# ---------------------------------------------------------------------------
# Schedule windows
# ---------------------------------------------------------------------------
def parse_days(spec: str) -> set:
    """`mon-fri`, `sat,sun`, `wed`, `*` -> a set of weekday numbers (Monday = 0)."""
    text = str(spec or "*").strip().lower()
    text = DAY_ALIASES.get(text, text)
    days: set = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            start, end = DAY_NAMES.get(a.strip()), DAY_NAMES.get(b.strip())
            if start is None or end is None:
                raise BellhopError(f"cannot read the days {spec!r} (mon tue wed thu fri sat sun)")
            # `fri-mon` wraps the weekend rather than being empty.
            days.update((start + i) % 7 for i in range((end - start) % 7 + 1))
        else:
            if part not in DAY_NAMES:
                raise BellhopError(f"cannot read the days {spec!r} (mon tue wed thu fri sat sun)")
            days.add(DAY_NAMES[part])
    if not days:
        raise BellhopError(f"cannot read the days {spec!r}")
    return days


def parse_hhmm(text: str, what: str) -> int:
    """`14:00` -> 840 minutes past midnight.

    Minutes, not a string comparison and not arithmetic on the digits: the shell version
    of this had to match `HHMM` literally because `[ 0900 -ge 1400 ]` reads `0900` as
    octal and errors out -- and a comparison that errored out and a window that was
    correctly judged inactive look the same in the log.
    """
    m = _HHMM_RE.match(str(text or "").strip())
    if not m:
        raise BellhopError(f"schedule {what}: cannot read the time {text!r} (use `14:00`)")
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        raise BellhopError(f"schedule {what}: {text!r} is not a time of day")
    return h * 60 + mi


def window_active(entry: dict, when: datetime) -> bool:
    """Is `when` inside this entry's window? Half-open: `from` is in, `to` is out.

    A window whose `to` is before its `from` wraps midnight and is judged against the
    day the window opens on.
    """
    days = parse_days(entry.get("days", "*"))
    start = parse_hhmm(entry.get("from"), "from")
    end = parse_hhmm(entry.get("to"), "to")
    minute = when.hour * 60 + when.minute
    if start <= end:
        return when.weekday() in days and start <= minute < end
    return (when.weekday() in days and minute >= start) or ((when.weekday() - 1) % 7 in days and minute < end)


def read_lane_model_file(path: Path) -> Tuple[str, str, str]:
    """A three-line file: provider, model, effort. Missing lines are empty strings."""
    lines = (path.read_text(encoding="utf-8").splitlines() + ["", "", ""])[:3]
    return lines[0].strip(), lines[1].strip(), lines[2].strip()


# ---------------------------------------------------------------------------
# Resolution: provider, model and effort for one launch
# ---------------------------------------------------------------------------
@dataclass
class Resolved:
    provider: str
    model: str
    effort: str = ""
    heavy: bool = False
    schedule_label: str = ""
    notes: List[str] = field(default_factory=list)
    #: `<provider>/<model>` this launch was switched away from, after a quota answer.
    fallback_from: str = ""

    @property
    def overridden(self) -> bool:
        return bool(self.schedule_label)

    def override_line(self, bell: str) -> str:
        return f"⏰ {bell}: schedule override → {self.provider}/{self.model}"

    def fallback_line(self, bell: str, signal: str = "") -> str:
        return (f"↩ {bell}: {self.fallback_from} answered with {signal or 'a quota refusal'}; "
                f"switched once to {self.provider}/{self.model}")

    def summary(self) -> str:
        return f"{self.provider}/{self.model}" + (f" (effort {self.effort})" if self.effort else "")


def is_heavy(packet) -> bool:
    """A packet's `effort: heavy` front-matter marker. Nothing else raises the notch."""
    if packet is None:
        return False
    extra = getattr(packet, "extra", None) or {}
    return str(extra.get("effort", "")).strip().lower() == "heavy"


def notch_up(effort: str) -> str:
    """One rung up the ladder; the top rung stays where it is."""
    e = (effort or "").strip().lower()
    if e not in EFFORT_LADDER:
        return effort or ""
    return EFFORT_LADDER[min(EFFORT_LADDER.index(e) + 1, len(EFFORT_LADDER) - 1)]


def resolve(desk: Desk, lane: dict, packet=None, when: Optional[datetime] = None,
            heavy: Optional[bool] = None) -> Resolved:
    """Who answers this bell, with which model and effort.

    Order: the lane's own keys, then the first matching schedule window, then the heavy
    mapping. Refusals (a named file that is not there, a provider nobody registered) are
    raised, never fallen back from.
    """
    lane_name = lane.get("name") or "?"
    provider = str(lane.get("provider") or "claude").strip()
    model = str(lane.get("model") or "").strip()
    effort = str(lane.get("effort") or "").strip()
    notes: List[str] = []
    at = when or clock()
    note = clock_note()
    if note:
        notes.append(note)

    entry_used: dict = {}
    label = ""
    for index, entry in enumerate(lane.get("schedule") or []):
        if not isinstance(entry, dict):
            raise ProviderRefusal(f"lane {lane_name}: schedule entry {index} is not an object")
        if not window_active(entry, at):
            continue
        label = str(entry.get("name") or f"{entry.get('days', '*')} {entry.get('from')}-{entry.get('to')}")
        want_provider = str(entry.get("provider") or "").strip()
        want_model = str(entry.get("model") or "").strip()
        want_effort = str(entry.get("effort") or "").strip()
        model_file = str(entry.get("model_file") or "").strip()
        if model_file:
            path = Path(model_file) if os.path.isabs(model_file) else desk.root / model_file
            if not path.exists():
                # The one refusal this whole module exists to make loudly. Falling back
                # to the lane's own provider here is what the peak window was configured
                # to avoid, and a fallback leaves a receipt that reads exactly like a
                # working override.
                raise ProviderRefusal(
                    f"lane {lane_name}: the schedule override '{label}' names the lane-model file "
                    f"{model_file}, and it is not there. Refusing to start rather than falling back "
                    f"to {provider}/{model}: a silent fallback and a working override leave the same receipt."
                )
            file_provider, file_model, file_effort = read_lane_model_file(path)
            want_provider = want_provider or file_provider
            want_model = want_model or file_model
            want_effort = want_effort or file_effort
        if not want_provider and not want_model:
            raise ProviderRefusal(
                f"lane {lane_name}: the schedule override '{label}' names neither a provider nor a model")
        if (want_provider or provider) == provider and (want_model or model) == model:
            # The override that lands on what it was meant to move away from is not an
            # override. Say so in red and do not print the ⏰ line: "it moved" and "it
            # did not move" must not read the same.
            notes.append(
                f"🔴 lane {lane_name}: the schedule override '{label}' resolves to {provider}/{model}, "
                f"which is what the lane already runs — the override does nothing; left as it was")
            label = ""
            continue
        entry_used = entry
        provider = want_provider or provider
        model = want_model or model
        effort = want_effort or effort
        break

    heavy_on = is_heavy(packet) if heavy is None else bool(heavy)
    if heavy_on:
        # A schedule override that changed the provider also changed which models exist.
        # The lane's `heavy_model` names one of the lane's provider's models, so it is
        # not applied across a provider change; the window may carry its own.
        entry_heavy_model = str(entry_used.get("heavy_model") or "").strip()
        entry_heavy_effort = str(entry_used.get("heavy_effort") or "").strip()
        lane_heavy_model = str(lane.get("heavy_model") or "").strip()
        lane_heavy_effort = str(lane.get("heavy_effort") or "").strip()
        crossed = bool(entry_used) and provider != str(lane.get("provider") or "claude").strip()
        heavy_model = entry_heavy_model or ("" if crossed else lane_heavy_model)
        heavy_effort = entry_heavy_effort or ("" if crossed else lane_heavy_effort)
        if crossed and lane_heavy_model and not entry_heavy_model:
            notes.append(
                f"heavy: the schedule override moved this lane to {provider}, so the lane's "
                f"heavy_model ({lane_heavy_model}) was not applied; give the window its own heavy_model")
        if heavy_model:
            model = heavy_model
        if heavy_effort:
            effort = heavy_effort
        elif effort:
            effort = notch_up(effort)
        if not heavy_model and not heavy_effort and not effort:
            notes.append(
                "heavy: this lane declares neither heavy_model nor effort, so the marker raised "
                "nothing — a packet marked heavy and one that was not read the same here")

    if provider not in REGISTRY:
        raise ProviderRefusal(
            f"lane {lane_name}: unknown provider {provider!r} (registered: {', '.join(sorted(REGISTRY))})")
    if not model and provider == "claude":
        raise ProviderRefusal(f"lane {lane_name}: no model to start the worker with")
    return Resolved(provider=provider, model=model, effort=effort, heavy=heavy_on,
                    schedule_label=label, notes=notes)


# ---------------------------------------------------------------------------
# Allowlists and the guard
# ---------------------------------------------------------------------------
_RULE_RE = re.compile(r"^(?:(allow|deny)\s+)?(?:Bash\((?P<b>.*)\)|(?P<p>.+))$")
_KEYWORDS = {"do", "then", "else", "elif", "fi", "done", "!", "{", "}", "time"}
_BLOCK_HEADS = ("for", "while", "until", "if", "case", "select", "function")


def allowlist_path(desk: Desk, lane: dict) -> Optional[Path]:
    raw = str(lane.get("allowlist_file") or "").strip()
    if not raw:
        return None
    return Path(raw) if os.path.isabs(raw) else desk.root / raw


def load_allowlist(path: Path) -> Tuple[List[str], List[str]]:
    """-> (allow prefixes, deny prefixes).

    One rule per line, `#` comments and blank lines ignored. Every one of these is the
    same rule: `git status:*`, `allow git status:*`, `allow Bash(git status:*)`. A rule
    without the trailing `:*` matches that command exactly and nothing longer.
    """
    allow: List[str] = []
    deny: List[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _RULE_RE.match(line)
        if not m:
            continue
        rule = (m.group("b") if m.group("b") is not None else m.group("p")).strip()
        if not rule:
            continue
        (deny if m.group(1) == "deny" else allow).append(rule)
    return allow, deny


def _matches(segment: str, rule: str) -> bool:
    """Claude Code's prefix rule: `git status:*` covers `git status --short`, not `git statusx`."""
    if rule.endswith(":*"):
        prefix = rule[:-2]
        if segment == prefix:
            return True
        if not segment.startswith(prefix):
            return False
        return prefix.endswith("/") or segment[len(prefix)] in " \t"
    return segment == rule


def segments(command: str) -> List[str]:
    """Every command the shell would actually run in this line.

    A guard that reads only the first word lets `git status && rm -rf /` through: the
    line starts with something on the allowlist, and a chained command and a single one
    look the same from the front.
    """
    text = str(command or "")
    for token in ("&&", "||", "$(", "`", "\n"):
        text = text.replace(token, ";")
    out: List[str] = []
    for raw in re.split(r"[;|]", text):
        seg = raw.strip().strip("()").strip()
        if not seg or seg.startswith("#"):
            continue
        seg = re.sub(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+", "", seg)  # leading VAR=value
        first = seg.split(None, 1)[0] if seg else ""
        if first in _BLOCK_HEADS:
            rest = seg.split(None, 1)[1] if " " in seg else ""
            if first in ("if", "while", "until") and rest:
                seg = rest
            else:
                continue
        while seg and seg.split(None, 1)[0] in _KEYWORDS:
            seg = seg.split(None, 1)[1] if " " in seg else ""
        if seg:
            out.append(seg)
    return out


def guard(desk: Desk, lane: dict, command: str) -> Tuple[bool, str]:
    """-> (allowed, reason). The reason is what the model is told; make it actionable."""
    lane_name = lane.get("name") or "?"
    path = allowlist_path(desk, lane)
    if path is None:
        return False, (f"lane {lane_name} declares no allowlist_file, and a worker without an "
                       f"allowlist may run anything; add one to bellhop.json (see template/providers/allowlist.example.txt)")
    if not path.exists():
        return False, f"lane {lane_name}: allowlist file not found: {path}"
    try:
        allow, deny = load_allowlist(path)
    except OSError as exc:
        return False, f"lane {lane_name}: allowlist file unreadable: {path} ({exc})"
    if not allow and not deny:
        return False, f"lane {lane_name}: allowlist {path} has no rules; deny by default"
    parts = segments(command)
    if not parts:
        return False, f"lane {lane_name}: nothing to check in {command!r}"
    for seg in parts:
        for rule in deny:  # deny wins, and it is read first so it wins loudly
            if _matches(seg, rule):
                return False, (f"denied by rule `{rule}`: `{seg[:120]}`. Use a form the allowlist "
                               f"covers; do not work around it.")
    for seg in parts:
        if not any(_matches(seg, rule) for rule in allow):
            return False, (f"not on the allowlist: `{seg[:120]}`. The lane's allowlist is {path}; "
                           f"use a command it covers, or ask the coordinator to add one.")
    return True, f"{len(parts)} segment(s) matched {path}"


# ---------------------------------------------------------------------------
# The result parser
# ---------------------------------------------------------------------------
def _number(value):
    if isinstance(value, bool) or value is None:
        return NA
    if isinstance(value, (int, float)):
        return value
    return NA


def _last_json_object(text: str) -> Optional[dict]:
    obj = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict):
            obj = candidate
    if obj is None:
        stripped = text.strip()
        if stripped.startswith("{"):
            try:
                candidate = json.loads(stripped)
                if isinstance(candidate, dict):
                    obj = candidate
            except ValueError:
                obj = None
    return obj


def parse_result(text: str, provider: Optional[str] = None) -> dict:
    """Read one worker's stdout. Two JSON shapes, one fallback, and **never raises**.

    - the Claude result JSON: `subtype` / `result` / `num_turns` / `total_cost_usd` /
      `duration_ms`, with the model in `model` or in the keys of `modelUsage`;
    - a generic result JSON: `{result, cost_usd, num_turns, model}`, and the
      `{text, stopReason}` variant another CLI prints;
    - anything else, including an empty stdout and a truncated line: the text, with
      every number `n/a`.

    This runs after the work is finished. A parser that threw here would turn a task
    that ran fine into a stack trace in the watchdog log and a receipt with no footer.
    """
    out = {"model": NA, "turns": NA, "cost_usd": NA, "duration_s": NA,
           "stop_reason": NA, "result_text": "", "raw": {},
           "provider_reasoning": NA, "thinking_tokens": NA}
    try:
        text = text if isinstance(text, str) else ""
        obj = _last_json_object(text)
        if obj is None:
            out["result_text"] = text.strip()
            return out
        out["raw"] = obj
        out["provider_reasoning"] = reasoning_state(obj)
        tokens = thinking_tokens(obj)
        out["thinking_tokens"] = NA if tokens is None else tokens
        usage = obj.get("modelUsage") if isinstance(obj.get("modelUsage"), dict) else {}
        model = obj.get("model") or (",".join(str(k) for k in usage) if usage else "")
        out["model"] = str(model) if model else NA
        if "stopReason" in obj:                     # the {text, stopReason} shape
            out["stop_reason"] = str(obj.get("stopReason") or NA)
            out["result_text"] = str(obj.get("text") or obj.get("result") or "")
        elif "subtype" in obj:                      # the Claude result JSON
            stop = str(obj.get("subtype") or NA)
            out["stop_reason"] = f"{stop} (error)" if obj.get("is_error") else stop
            out["result_text"] = str(obj.get("result") or "")
        else:                                       # {result, cost_usd, num_turns, model}
            stop = obj.get("stop_reason") or obj.get("status") or ""
            out["stop_reason"] = str(stop) if stop else NA
            out["result_text"] = str(obj.get("result") or obj.get("text") or "")
        turns = obj.get("num_turns", obj.get("turns"))
        out["turns"] = _number(turns)
        cost = obj.get("total_cost_usd", obj.get("cost_usd"))
        out["cost_usd"] = _number(cost)
        duration = obj.get("duration_s")
        if duration is None and obj.get("duration_ms") is not None:
            ms = _number(obj.get("duration_ms"))
            duration = round(float(ms) / 1000.0, 1) if ms != NA else None
        out["duration_s"] = _number(duration)
    except Exception:  # noqa: BLE001 -- the whole point of this function
        pass
    return out


def na_none(value):
    """`n/a` -> None, for the JSON files the readings parse as numbers."""
    return None if value == NA else value


# ---------------------------------------------------------------------------
# Reasoning: was there thinking, and could anyone read it?
# ---------------------------------------------------------------------------
REASONING_VISIBLE = "visible"
REASONING_HIDDEN = "hidden"

#: Keys a vendor has used for the count of tokens billed as reasoning.
THINKING_TOKEN_KEYS = ("thinking_tokens", "reasoning_tokens", "reasoning_output_tokens",
                       "thinking_output_tokens")

_THINKING_TYPES = ("thinking", "redacted_thinking", "reasoning")


def _walk(obj, depth: int = 0):
    """Every dict inside a result object, down to a bounded depth."""
    if depth > 6:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v, depth + 1)


def thinking_blocks(obj) -> List[dict]:
    return [d for d in _walk(obj) if str(d.get("type") or "") in _THINKING_TYPES]


def reasoning_state(obj) -> str:
    """`visible`, `hidden`, or `n/a` — three readings, and `hidden` is not `n/a`.

    One desk's model returned thinking blocks with an empty `thinking` field and an
    encrypted signature, and billed thinking tokens against them. Nothing on the
    desk could tell that run from one that did no thinking at all: both left a result
    with no reasoning text in it. `hidden` says the blocks were there and unreadable,
    which is the reading that explains the bill.
    """
    blocks = thinking_blocks(obj)
    if not blocks:
        return NA
    for b in blocks:
        for key in ("thinking", "text", "reasoning", "content"):
            value = b.get(key)
            if isinstance(value, str) and value.strip():
                return REASONING_VISIBLE
    return REASONING_HIDDEN


def thinking_tokens(obj) -> Optional[int]:
    """Tokens billed as reasoning, from wherever the vendor put the number."""
    for d in _walk(obj):
        for key in THINKING_TOKEN_KEYS:
            value = d.get(key)
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (int, float)):
                return int(value)
    return None


# ---------------------------------------------------------------------------
# Quota: the one failure that used to be nobody's state
# ---------------------------------------------------------------------------
# Desk A ran four lanes on one vendor. A balance-exhausted 402 stopped the whole desk
# for 55 minutes and left two dead tasks, and no stop-dispatch order was ever written:
# a provider saying "not you, not now" was a per-task error, so every lane met it
# separately and nobody's state said the desk was out of credit. Desk B, reaching a
# second vendor through a relay, took repeated `429`s with `Retry-After`
# values up to six days, and the coordinator kept walking into the wall until a person
# stopped it.
#
# A quota answer is a *desk* fact, not a task fact. It is classified here, in the one
# module that reads what a worker printed, and acted on in `watch.py`.
QUOTA_STATUSES = (402, 429)

#: Words a vendor uses for the same thing, matched case-insensitively.
QUOTA_WORDS = (
    "usage_limit", "usage limit", "insufficient_balance", "insufficient balance",
    "quota_exceeded", "quota exceeded", "out of credit", "credit balance",
    "rate_limit_error", "rate limit exceeded", "too many requests", "payment required",
    "billing_hard_limit", "over capacity",
)

_STATUS_RE = re.compile(
    r"(?:status(?:_code)?|http|code|error)\D{0,16}(402|429)\b|\b(402|429)\b\s*(?:error|status)", re.I)
# The value runs to the closing quote or brace, commas included: `Retry-After` may be a
# number of seconds or an HTTP date, and an HTTP date has a comma in it.
_RETRY_AFTER_RE = re.compile(r"retry[-_ ]?after[\"']?\s*[:=]\s*[\"']?\s*([^\"'}\n]+)", re.I)

# A retry window longer than this is not something a desk waits out; the order stands
# and the principal decides. Desk B was handed six days.
LONG_RETRY_S = 24 * 3600


@dataclass
class Quota:
    """What a worker's answer said about the provider's willingness to answer at all."""

    hit: bool = False
    signal: str = ""                      # what was seen, in words, for the log line
    status: Optional[int] = None
    retry_after_s: Optional[int] = None

    def __bool__(self) -> bool:
        return self.hit

    @property
    def long_wait(self) -> bool:
        return self.retry_after_s is not None and self.retry_after_s > LONG_RETRY_S


def _retry_seconds(raw: str) -> Optional[int]:
    """`Retry-After` as seconds: a number of seconds, or an HTTP/ISO date."""
    text = str(raw or "").strip().strip('"\'').rstrip(",").strip()
    if not text:
        return None
    try:
        return max(0, int(float(text)))
    except ValueError:
        pass
    when = None
    try:
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        when = None
    if when is None:
        try:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    try:
        base = now().astimezone(when.tzinfo) if when.tzinfo else now().replace(tzinfo=None)
        return max(0, int((when - base).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return None


def searchable(result) -> str:
    """Everything a worker's answer said, as one bounded string."""
    parts: List[str] = []
    if isinstance(result, dict):
        for key in ("stop_reason", "result_text", "stderr", "error", "message"):
            value = result.get(key)
            if isinstance(value, str):
                parts.append(value)
        raw = result.get("raw") if isinstance(result.get("raw"), dict) else None
        for obj in (raw, result):
            if obj is None:
                continue
            try:
                parts.append(json.dumps(obj, ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                pass
    elif isinstance(result, str):
        parts.append(result)
    return "\n".join(parts)[:40000]


def classify(result, text: str = "") -> Quota:
    """Is this answer the provider refusing to answer? -> `Quota`. **Never raises.**

    Runs on the closing path beside `parse_result`, and for the same reason: a traceback
    here would turn a provider refusal into a stack trace in the watchdog log, which is
    exactly the silence the breaker exists to end.
    """
    q = Quota()
    try:
        if isinstance(result, dict) and "exit_code" in result:
            # A session that ran to the end was not refused, whatever its transcript
            # happens to quote. Without this, a task *about* rate limiting, whose result
            # text says "429", would stop the desk -- and a breaker that fires on the
            # word rather than on the refusal is worse than no breaker.
            if not result.get("is_error") and result.get("exit_code") in (0, None):
                return q
        body = "\n".join(p for p in (searchable(result), text or "") if p)
        if not body:
            return q
        signals: List[str] = []
        m = _STATUS_RE.search(body)
        if m:
            q.status = int(m.group(1) or m.group(2))
            signals.append(f"HTTP {q.status}")
        for word in QUOTA_WORDS:
            if word in body.lower():
                signals.append(word)
                break
        m = _RETRY_AFTER_RE.search(body)
        if m:
            q.retry_after_s = _retry_seconds(m.group(1))
            signals.append("Retry-After" + (f" {q.retry_after_s}s" if q.retry_after_s is not None else ""))
        q.hit = bool(signals)
        q.signal = ", ".join(signals)
    except Exception:  # noqa: BLE001 -- see the docstring
        return Quota()
    return q


def fallback_for(lane: dict, resolved: Resolved) -> Optional[Resolved]:
    """The lane's one declared step sideways, or None.

    The rule "never silently fall back" is kept because this switch is never silent: it
    is logged, it reaches the card, and the receipt header carries `model_asked` beside
    the model that actually answered. A fallback nobody can see afterwards is the thing
    that rule forbids; this one is written down three times.
    """
    provider = str(lane.get("fallback_provider") or "").strip()
    model = str(lane.get("fallback_model") or "").strip()
    if not provider and not model:
        return None
    switched = Resolved(
        provider=provider or resolved.provider,
        model=model or resolved.model,
        effort=resolved.effort,
        heavy=resolved.heavy,
        schedule_label=resolved.schedule_label,
        fallback_from=f"{resolved.provider}/{resolved.model}",
    )
    if (switched.provider, switched.model) == (resolved.provider, resolved.model):
        return None  # a fallback onto the thing that just refused is not a fallback
    if switched.provider not in REGISTRY:
        raise ProviderRefusal(
            f"lane {lane.get('name')}: fallback_provider {switched.provider!r} is not registered "
            f"(registered: {', '.join(sorted(REGISTRY))})")
    return switched


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
@dataclass
class LaunchContext:
    desk: Desk
    lane: dict
    packet: object
    prompt: str
    resolved: Resolved
    worktree: Path
    prompt_file: Optional[Path] = None
    log_path: Optional[Path] = None

    @property
    def allowlist(self) -> str:
        path = allowlist_path(self.desk, self.lane)
        return str(path) if path else ""


class Provider:
    name = ""
    supports_effort = False
    #: Does this provider have a permission gate of its own, or does it need ours?
    has_own_gate = False

    def build(self, ctx: LaunchContext) -> List[str]:
        raise NotImplementedError

    def scrub(self, ctx: LaunchContext) -> dict:
        """The environment a worker sees: PATH, HOME, LANG, the lane's declared names."""
        return scrubbed_env(ctx.lane.get("env_allow", []))

    def parse(self, text: str) -> dict:
        return parse_result(text, self.name)


class ClaudeProvider(Provider):
    name = "claude"
    supports_effort = True
    has_own_gate = True

    def build(self, ctx: LaunchContext) -> List[str]:
        lane, packet = ctx.lane, ctx.packet
        max_turns = getattr(packet, "max_turns", None) or lane["max_turns"]
        cmd = [
            ctx.desk.cfg["claude_bin"],
            "-p",
            ctx.prompt,
            "--model",
            ctx.resolved.model,
            "--permission-mode",
            lane["permission_mode"],
            "--add-dir",
            str(ctx.desk.root),
            "--output-format",
            "json",
            "--max-turns",
            str(max_turns),
        ]
        if ctx.resolved.effort:
            # Given explicitly or not at all. Left off, the effort follows whatever this
            # machine's settings were last set to -- and a run at `high` and a run that
            # happened to be `high` read the same in the log until someone lowers it.
            cmd += ["--effort", ctx.resolved.effort]
        if lane.get("allowed_tools"):
            cmd += ["--allowedTools", ",".join(lane["allowed_tools"])]
        return cmd


class GenericProvider(Provider):
    """Any other vendor's CLI, behind a `worker_cmd` template.

    The template gets `{prompt_file} {cwd} {model} {log} {allowlist}`. Everything the
    session needs is on that line or in the scrubbed environment; this module does not
    learn a second vendor's flags.
    """

    name = "generic"
    supports_effort = False
    has_own_gate = False

    def build(self, ctx: LaunchContext) -> List[str]:
        template = str(ctx.lane.get("worker_cmd") or "").strip()
        if not template:
            raise ProviderRefusal(
                f"lane {ctx.lane.get('name')}: provider `generic` needs a worker_cmd template "
                f"(placeholders: {', '.join('{%s}' % p for p in CMD_PLACEHOLDERS)})")
        if allowlist_path(ctx.desk, ctx.lane) is None:
            raise ProviderRefusal(
                f"lane {ctx.lane.get('name')}: provider `generic` needs an allowlist_file. "
                f"Claude Code brings its own permission gate; another CLI does not, and a lane "
                f"with no allowlist may run anything.")
        return ["/bin/sh", "-c", render(template, ctx)]


REGISTRY = {p.name: p for p in (ClaudeProvider(), GenericProvider())}


def provider_for(name: str) -> Provider:
    try:
        return REGISTRY[str(name).strip()]
    except KeyError:
        raise ProviderRefusal(f"unknown provider {name!r} (registered: {', '.join(sorted(REGISTRY))})")


def render(template: str, ctx: LaunchContext) -> str:
    """Replace the known `{name}` placeholders and leave every other brace alone.

    `str.format` would raise on a template holding `${HOME}` or an awk program, and a
    command template that cannot hold a shell variable is not a command template.
    """
    values = {
        "prompt_file": str(ctx.prompt_file or ""),
        "cwd": str(ctx.worktree),
        "model": ctx.resolved.model,
        "log": str(ctx.log_path or ""),
        "allowlist": ctx.allowlist,
    }
    out = template
    for key in CMD_PLACEHOLDERS:
        out = out.replace("{" + key + "}", values[key])
    return out


def sandbox_prefix(desk: Desk, lane: dict) -> List[str]:
    """The wrapper argv for a lane that names a sandbox profile, or an empty list.

    A named profile that is not on disk refuses the launch. Running unsandboxed because
    the profile was missing, and running sandboxed, leave the same receipt.
    """
    raw = str(lane.get("sandbox_profile") or "").strip()
    if not raw:
        return []
    profile = Path(raw) if os.path.isabs(raw) else desk.root / raw
    if not profile.exists():
        raise ProviderRefusal(
            f"lane {lane.get('name')}: sandbox_profile {raw} is not there. Refusing to start: "
            f"a worker that ran unsandboxed leaves the same receipt as one that did not.")
    wrapper = str(desk.cfg.get("sandbox_wrapper") or "").strip()
    if not wrapper:
        raise ProviderRefusal(
            f"lane {lane.get('name')}: sandbox_profile is set but the desk has no sandbox_wrapper "
            f"(for example \"sandbox-exec -f {{profile}}\" on macOS; other platforms substitute their own)")
    return shlex.split(wrapper.replace("{profile}", str(profile)))


def build_command(desk: Desk, lane: dict, packet, prompt: str, resolved: Resolved,
                  worktree: Path, prompt_file: Optional[Path] = None,
                  log_path: Optional[Path] = None) -> List[str]:
    """The argv that starts one worker session, sandbox wrapper included.

    `worker_cmd` wins wherever it is set, whatever the lane's provider says: it is the
    escape hatch, and the test stub's entry. The `provider` key still names who is
    answering, and that is what reaches the receipt.
    """
    ctx = LaunchContext(desk=desk, lane=lane, packet=packet, prompt=prompt, resolved=resolved,
                        worktree=Path(worktree), prompt_file=prompt_file, log_path=log_path)
    if lane.get("worker_cmd") and resolved.provider != "generic":
        cmd = ["/bin/sh", "-c", render(str(lane["worker_cmd"]), ctx)]
    else:
        cmd = provider_for(resolved.provider).build(ctx)
    return sandbox_prefix(desk, lane) + cmd


def claude_command(desk: Desk, lane: dict, packet, prompt: str,
                   resolved: Optional[Resolved] = None) -> List[str]:
    """The Claude worker command line on its own: no `worker_cmd`, no sandbox prefix.

    `build_command` is what a launch goes through. This is for looking at the command a
    Claude lane would produce -- the lane's `allowed_tools` have to arrive as
    `--allowedTools`, and a lane whose allowlist never arrives produces a worker in
    `acceptEdits` that can edit files and run nothing.
    """
    resolved = resolved or Resolved(provider="claude", model=str(lane.get("model") or ""),
                                    effort=str(lane.get("effort") or ""))
    ctx = LaunchContext(desk=desk, lane=lane, packet=packet, prompt=prompt, resolved=resolved,
                        worktree=desk.root)
    return REGISTRY["claude"].build(ctx)


def worker_env(desk: Desk, lane: dict, resolved: Resolved) -> dict:
    """The scrub, per provider. One place, so a new provider cannot forget it."""
    return provider_for(resolved.provider).scrub(
        LaunchContext(desk=desk, lane=lane, packet=None, prompt="", resolved=resolved, worktree=desk.root))


# ---------------------------------------------------------------------------
# `bellhop provider check`
# ---------------------------------------------------------------------------
def check(desk: Desk, lane_name: str, when: Optional[datetime] = None) -> dict:
    """What this lane would do if a bell rang now, and which named files are missing."""
    lane = desk.lane(lane_name)
    at = when or clock()
    report = {"lane": lane_name, "at": at.strftime("%Y-%m-%d %H:%M (%a)"), "problems": [], "lines": []}
    try:
        plain = resolve(desk, lane, when=at, heavy=False)
        report["plain"] = plain
    except ProviderRefusal as exc:
        report["plain"] = None
        report["problems"].append(str(exc))
    try:
        report["heavy"] = resolve(desk, lane, when=at, heavy=True)
    except ProviderRefusal as exc:
        report["heavy"] = None
        if str(exc) not in report["problems"]:
            report["problems"].append(str(exc))

    windows = lane.get("schedule") or []
    active = []
    for index, entry in enumerate(windows):
        if not isinstance(entry, dict):
            report["problems"].append(f"schedule entry {index} is not an object")
            continue
        try:
            if window_active(entry, at):
                active.append(entry)
        except BellhopError as exc:
            report["problems"].append(f"schedule entry {index}: {exc}")
    report["windows"] = len(windows)
    report["active_window"] = active[0] if active else None

    path = allowlist_path(desk, lane)
    report["allowlist"] = path
    if path is None:
        report["allowlist_state"] = "not declared"
        if (report["plain"] and report["plain"].provider == "generic"):
            report["problems"].append("provider `generic` with no allowlist_file")
    elif path.exists():
        allow, deny = load_allowlist(path)
        report["allowlist_state"] = f"{len(allow)} allow, {len(deny)} deny"
    else:
        report["allowlist_state"] = "MISSING"
        report["problems"].append(f"allowlist_file not found: {path}")

    raw_profile = str(lane.get("sandbox_profile") or "").strip()
    if not raw_profile:
        report["sandbox"] = None
        report["sandbox_state"] = "not declared"
    else:
        profile = Path(raw_profile) if os.path.isabs(raw_profile) else desk.root / raw_profile
        report["sandbox"] = profile
        wrapper = str(desk.cfg.get("sandbox_wrapper") or "").strip()
        if not profile.exists():
            report["sandbox_state"] = "MISSING"
            report["problems"].append(f"sandbox_profile not found: {profile}")
        elif not wrapper:
            report["sandbox_state"] = "no sandbox_wrapper on the desk"
            report["problems"].append("sandbox_profile is set but the desk has no sandbox_wrapper")
        else:
            report["sandbox_state"] = f"wrapped by `{wrapper}`"
    return report


def check_text(report: dict) -> str:
    lane = report["lane"]
    lines = [f"lane {lane} at {report['at']}"]
    plain, heavy = report.get("plain"), report.get("heavy")
    lines.append(f"  now:      {plain.summary() if plain else '-'}")
    lines.append(f"  --heavy:  {heavy.summary() if heavy else '-'}")
    if report["active_window"] is not None and plain is not None and plain.overridden:
        lines.append(f"  schedule: override '{plain.schedule_label}' is active at this minute "
                     f"→ {plain.provider}/{plain.model}")
    elif report["windows"]:
        lines.append(f"  schedule: no override active at this minute ({report['windows']} window(s) configured)")
    else:
        lines.append("  schedule: none configured")
    lines.append(f"  allowlist: {report['allowlist'] or '-'} ({report['allowlist_state']})")
    lines.append(f"  sandbox:   {report['sandbox'] or '-'} ({report['sandbox_state']})")
    # Both resolutions' notes, deduplicated in order: the note that says a lane's
    # heavy_model was skipped is only ever raised on the heavy pass, and a heavy packet
    # that raised nothing and one that raised something read the same without it.
    seen = []
    for note in list(plain.notes if plain else []) + list(heavy.notes if heavy else []):
        if note not in seen:
            seen.append(note)
            lines.append(f"  note: {note}")
    for problem in report["problems"]:
        lines.append(f"  🛑 {problem}")
    return "\n".join(lines) + "\n"
