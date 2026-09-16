"""Machine-written headers for receipts and acceptance verdicts.

A receipt used to be a prose document that a checker had to parse. That made the
checker a format ruler: a section renamed, a fence dropped, a field in a different
order, and the file was rejected for reasons that had nothing to do with whether
the work was done. On one production desk the acceptance pipeline ran in
shadow mode; most runs stalled, many of them because the verifier wrote its
verdict in the wrong shape.

The fix is here: the numbers a machine needs are computed by running the real
commands and written into one fenced block at the top of the file. The checkers
parse only that block. The body is free text for a human reader and can only
produce warnings.

One rule, two readers, one implementation: `RECEIPT_SPEC` and `ACCEPTANCE_SPEC`
below are the only description of the header anywhere. The generator writes the
keys in that order and the checker validates against the same lists. Templates and
documents may describe the header, but they must point here for the spec.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections import namedtuple
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from . import exits
from .config import Desk
from .util import BellhopError, read, run, scrubbed_env, ts, write

# -- exit codes ------------------------------------------------------------
# The one table is bellhop/exits.py and docs/exit-codes.md; these are its numbers under
# the names the checkers use, so a caller can branch on the number alone.
EXIT_OK = exits.OK              # green
EXIT_RED = exits.RED            # ran to the end, the finding is red
EXIT_REFUSED = exits.GATE       # a gate refused; answering it is the only way on
EXIT_MALFORMED = exits.NOT_FOUND  # the file is missing or unreadable; a rerun changes nothing
EXIT_EXTERNAL = exits.EXTERNAL  # something outside failed; running it again may differ

Field = namedtuple("Field", "key type required how")

FENCE_RECEIPT = "receipt-header"
FENCE_ACCEPTANCE = "acceptance-header"

NOT_AVAILABLE = "-"        # the generator could not compute this one
# Different reading, different word: `-` is "this desk could not work it out", `n/a` is
# "the thing that answered did not say". A model that reported no model id and a header
# whose generator crashed before it got there are not the same finding.
NOT_REPORTED = "n/a"
CHECK_NOT_RUN = "not-run"  # the lane declares no check command
SUMMARY_MAX = 120
DEFAULT_ACCEPTANCE_BYTES = 3000
CHECK_TIMEOUT_S = 600

VERDICTS = ("pass", "return", "doubt")

SECTIONS = ("Task", "Criteria", "Mutation", "Extra findings", "Doubts", "Hygiene",
            "Recommendation", "Verdict")

# -- the spec: this is the single source of truth --------------------------

RECEIPT_SPEC: Sequence[Field] = (
    Field("task", "str", True, "packet id (--task, else the leading token of the receipt file name)"),
    Field("lane", "str", True, "--lane"),
    Field("base", "str", True, "the packet's front-matter `base`"),
    Field("head", "str", True, "git rev-parse HEAD in the lane worktree"),
    Field("commits", "int", True, "git rev-list --count base..HEAD"),
    Field("files_changed", "int", True, "git diff --shortstat base..HEAD"),
    Field("insertions", "int", True, "git diff --shortstat base..HEAD"),
    Field("deletions", "int", True, "git diff --shortstat base..HEAD"),
    Field("left_in_tree", "int", True, "lines of git status --porcelain in the lane worktree"),
    Field("check_exit", "str", True, "exit code of the lane's check_cmd, or `not-run` when the lane declares none"),
    Field("check_summary", "str", False, "last non-empty output line of check_cmd, at most %d characters" % SUMMARY_MAX),
    Field("doubts", "int", True, "body lines that start with `DOUBT:`"),
    Field("extras", "int", True, "body lines that start with `EXTRA:`"),
    Field("provider", "str", False, "worker provider, from the result JSON"),
    Field("model_asked", "str", False, "the model the lane asked for, before anyone answered; `n/a` when nothing said"),
    Field("model", "str", False, "the model that answered, from the result JSON; `n/a` when it named none"),
    Field("provider_reasoning", "str", False, "`visible`, `hidden` when the thinking blocks came back empty, `n/a` when there were none"),
    Field("thinking_tokens", "int", False, "tokens billed as reasoning, from the result JSON"),
    Field("turns", "int", False, "turns, from the result JSON"),
    Field("cost_usd", "float", False, "cost in USD, from the result JSON"),
    Field("duration_s", "float", False, "wall-clock seconds, from the result JSON"),
    Field("worker_exit", "int", False, "the worker process exit code, from the result JSON"),
    Field("dead", "str", False, "yes when the watchdog marked the task dead, else no"),
    Field("generated", "str", True, "when this header was written; the only line that moves between two runs"),
)

ACCEPTANCE_SPEC: Sequence[Field] = (
    Field("task", "str", True, "--task, else the first non-empty line under `## Task`, else the file name"),
    Field("verdict", "str", True, "last non-empty line of the body: VERDICT: pass | return | doubt"),
    Field("verdict_reason", "str", False, "the one sentence after `return` or `doubt`"),
    Field("criteria_total", "int", True, "`- [ ]` and `- [x]` items under `## Criteria`"),
    Field("criteria_pass", "int", True, "`- [x]` items under `## Criteria`"),
    Field("criteria_fail", "int", True, "`- [ ]` items under `## Criteria`"),
    Field("mutations", "int", True, "lines under `## Mutation` that start with `- broke:`"),
    Field("doubts_blocking", "int", True, "lines under `## Doubts` that start with `- [blocking]`"),
    Field("doubts_recorded", "int", True, "lines under `## Doubts` that start with `- [recorded]`"),
    Field("extra_findings", "int", True, "list items under `## Extra findings`"),
    Field("sections_missing", "str", True, "which of the eight sections the body does not have, `none` when all eight are there"),
    Field("paths_outside", "int", False, "absolute paths named in the body that are under neither the lane worktree nor the desk"),
    Field("model_asked", "str", False, "the model the verifier was asked for; `n/a` when the lane runs its own accept_cmd"),
    Field("model", "str", False, "the model that answered, from the verifier's result JSON; `n/a` when it named none"),
    Field("provider_reasoning", "str", False, "`visible`, `hidden` when the thinking blocks came back empty, `n/a` when there were none"),
    Field("bytes", "int", True, "size of the body in bytes, header excluded, so writing the header does not change it"),
    Field("generated", "str", True, "when this header was written; the only line that moves between two runs"),
)

SPECS = {"receipt": RECEIPT_SPEC, "acceptance": ACCEPTANCE_SPEC}
FENCES = {"receipt": FENCE_RECEIPT, "acceptance": FENCE_ACCEPTANCE}


def spec_table(kind: str) -> str:
    """The spec as a Markdown table. `bellhop header-spec` and docs/headers.md use it."""
    spec = SPECS[kind]
    rows = ["| key | type | required | how it is computed |", "|---|---|---|---|"]
    rows += [f"| `{f.key}` | {f.type} | {'yes' if f.required else 'no'} | {f.how} |" for f in spec]
    return "\n".join(rows) + "\n"


# -- reading and writing a header block ------------------------------------

_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]{0,31}):\s*(.*)$")


def _fence_re(lang: str) -> "re.Pattern":
    return re.compile(r"^```%s[ \t]*\n(.*?)^```[ \t]*$" % re.escape(lang), re.S | re.M)


def parse_header(text: str, lang: str) -> Tuple[Optional[dict], Optional[Tuple[int, int]]]:
    """-> ({key: value}, (start, end)) for the first block, or (None, None)."""
    m = _fence_re(lang).search(text)
    if not m:
        return None, None
    values = {}
    for line in m.group(1).split("\n"):
        if not line.strip():
            continue
        g = _LINE_RE.match(line.strip())
        if g:
            values[g.group(1)] = g.group(2).strip()
    return values, (m.start(), m.end())


def strip_header(text: str, lang: str) -> str:
    """The body: the file with its header block, and the newline after it, removed.

    Counting is always done over the body. A header that carried its own counts back
    into the next generation would not be idempotent.
    """
    _values, span = parse_header(text, lang)
    if not span:
        return text
    start, end = span
    if text[end:end + 1] == "\n":
        end += 1
    return text[:start] + text[end:]


def _value(v) -> str:
    """One line, no backticks (they would close the fence), never empty."""
    if v is None:
        return NOT_AVAILABLE
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        v = f"{v:g}"
    s = str(v).replace("`", "").replace("\r", " ").replace("\n", " ")
    return " ".join(s.split()) or NOT_AVAILABLE


def render_header(kind: str, values: dict) -> str:
    """The block, keys in spec order. Nothing else decides the order."""
    lang = FENCES[kind]
    lines = ["```" + lang]
    lines += [f"{f.key}: {_value(values.get(f.key))}" for f in SPECS[kind]]
    lines.append("```")
    return "\n".join(lines) + "\n"


def write_header(path: Path, kind: str, values: dict) -> str:
    """Write the block at the top, or replace an existing one where it stands.

    Replacing in place, rather than moving the block to the top, is what makes this
    idempotent: moving it would shift every line number in the body, and a receipt
    whose line numbers moved looks the same in a diff as one whose body was edited.
    Returns "new" or "replaced".
    """
    path = Path(path)
    text = read(path)
    header = render_header(kind, values)
    body_before = strip_header(text, FENCES[kind])
    _old, span = parse_header(text, FENCES[kind])
    if span:
        new = text[:span[0]] + header.rstrip("\n") + text[span[1]:]
        action = "replaced"
    else:
        new = header + text
        action = "new"
    if strip_header(new, FENCES[kind]) != body_before:
        raise BellhopError(f"refusing to write: the body of {path} would change")
    write(path, new)
    return action


# -- computing the receipt header ------------------------------------------

_SHORTSTAT_RE = re.compile(
    r"(?:(\d+) files? changed)?(?:,\s*(\d+) insertions?\(\+\))?(?:,\s*(\d+) deletions?\(-\))?")
_DOUBT_RE = re.compile(r"^[ \t]*(?:[-*][ \t]*)?DOUBT:", re.M)
_EXTRA_RE = re.compile(r"^[ \t]*(?:[-*][ \t]*)?EXTRA:", re.M)


def _git(worktree: Path, *args: str) -> Optional[str]:
    """git, but a failure is a missing number rather than an exception."""
    proc = run(["git", "-C", str(worktree), *args], check=False)
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip()


def _result_fields(result) -> dict:
    """Accept either the watchdog's footer dict or a raw CLI result JSON."""
    if result is None:
        return {}
    if isinstance(result, (str, Path)):
        p = Path(result)
        if not p.exists():
            raise BellhopError(f"result JSON not found: {p}")
        try:
            result = json.loads(read(p))
        except json.JSONDecodeError as exc:
            raise BellhopError(f"result JSON is not JSON: {p} ({exc})")
    if not isinstance(result, dict):
        raise BellhopError("result JSON must be an object")
    duration = result.get("duration_s")
    if duration is None and result.get("duration_ms") is not None:
        duration = round(float(result["duration_ms"]) / 1000.0, 1)
    return {
        "provider": result.get("provider"),
        # Both of them, always: on one desk the coordinator, the native helpers, the
        # verifier and the permission classifier were all quietly one model answering to
        # two different model names, and the only place that was visible was a key
        # in the worker's meta file that nothing printed.
        "model_asked": result.get("model_asked"),
        "model": result.get("model"),
        "provider_reasoning": result.get("provider_reasoning"),
        "thinking_tokens": result.get("thinking_tokens"),
        "turns": result.get("turns", result.get("num_turns")),
        "cost_usd": result.get("cost_usd", result.get("total_cost_usd")),
        "duration_s": duration,
        "worker_exit": result.get("exit_code"),
        "dead": result.get("dead"),
    }


def find_task_packet(desk: Desk, task_id: str):
    from .accept import find_packet  # imported here: accept.py wires this module in
    from .packet import parse_packet
    return parse_packet(find_packet(desk, task_id))


def run_lane_check(lane: dict, worktree: Path) -> Tuple[str, str]:
    """-> (check_exit, check_summary). `not-run` is not the same reading as `0`."""
    cmd = (lane.get("check_cmd") or "").strip()
    if not cmd:
        return CHECK_NOT_RUN, NOT_AVAILABLE
    env = scrubbed_env(lane.get("env_allow", []))
    try:
        proc = run(["/bin/sh", "-c", cmd], cwd=worktree, env=env, timeout=CHECK_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return "124", f"check_cmd timed out after {CHECK_TIMEOUT_S}s"
    out = [l.strip() for l in (proc.stdout or "").splitlines() if l.strip()]
    err = [l.strip() for l in (proc.stderr or "").splitlines() if l.strip()]
    last = out[-1] if out else (err[-1] if err else NOT_AVAILABLE)
    return str(proc.returncode), last[:SUMMARY_MAX]


def compute_receipt_values(desk: Desk, receipt: Path, lane_name: str, packet=None,
                           task: Optional[str] = None, result=None, run_check: bool = True) -> dict:
    """Every number here comes from a command run now, not from the receipt's prose."""
    receipt = Path(receipt)
    lane = desk.lane(lane_name)
    worktree = Path(lane["worktree"])
    if not worktree.exists():
        raise BellhopError(f"lane '{lane_name}' has no worktree at {worktree}")
    body = strip_header(read(receipt), FENCE_RECEIPT) if receipt.exists() else ""
    if task is None and packet is not None:
        task = packet.id
    if task is None:
        task = receipt.stem.split("-", 1)[0] or NOT_AVAILABLE
    base = packet.base if packet is not None else None

    head = _git(worktree, "rev-parse", "HEAD")
    commits = files_changed = insertions = deletions = None
    if base:
        count = _git(worktree, "rev-list", "--count", f"{base}..HEAD")
        commits = int(count) if count and count.isdigit() else None
        shortstat = _git(worktree, "diff", "--shortstat", f"{base}..HEAD")
        if shortstat is not None:
            m = _SHORTSTAT_RE.search(shortstat)
            files_changed = int(m.group(1) or 0) if m else 0
            insertions = int(m.group(2) or 0) if m else 0
            deletions = int(m.group(3) or 0) if m else 0

    porcelain = _git(worktree, "status", "--porcelain")
    left = None
    if porcelain is not None:
        lines = [l for l in porcelain.splitlines() if l.strip()]
        # The receipt never counts itself: writing it is what put it in the tree.
        try:
            rel = str(receipt.resolve().relative_to(worktree.resolve()))
        except ValueError:
            rel = None
        if rel:
            lines = [l for l in lines if l[3:].strip().strip('"') != rel]
        left = len(lines)

    check_exit, check_summary = run_lane_check(lane, worktree) if run_check else (CHECK_NOT_RUN, NOT_AVAILABLE)

    values = {
        "task": task,
        "lane": lane_name,
        "base": base,
        "head": head,
        "commits": commits,
        "files_changed": files_changed,
        "insertions": insertions,
        "deletions": deletions,
        "left_in_tree": left,
        "check_exit": check_exit,
        "check_summary": check_summary,
        "doubts": len(_DOUBT_RE.findall(body)),
        "extras": len(_EXTRA_RE.findall(body)),
        # No result JSON at all is still an answer to "which model wrote this": nobody
        # said. `-` would claim the generator failed at it.
        "model_asked": NOT_REPORTED,
        "model": NOT_REPORTED,
        "provider_reasoning": NOT_REPORTED,
        "generated": ts(),
    }
    values.update({k: v for k, v in _result_fields(result).items() if v is not None})
    return values


def write_receipt_header(desk: Desk, receipt: Path, lane_name: str, packet=None,
                         task: Optional[str] = None, result=None, run_check: bool = True) -> Tuple[dict, str]:
    receipt = Path(receipt)
    if not receipt.exists():
        raise BellhopError(f"receipt not found: {receipt}")
    values = compute_receipt_values(desk, receipt, lane_name, packet=packet, task=task,
                                    result=result, run_check=run_check)
    return values, write_header(receipt, "receipt", values)


# -- computing the acceptance header ---------------------------------------

_HEADING_RE = re.compile(r"^#{2,3}[ \t]+(.+?)[ \t]*$")
_CRITERION_RE = re.compile(r"^[ \t]*[-*][ \t]*\[([ xX])\]")
_BROKE_RE = re.compile(r"^[ \t]*[-*][ \t]*broke:", re.I)
_BLOCKING_RE = re.compile(r"^[ \t]*[-*][ \t]*\[blocking\]", re.I)
_RECORDED_RE = re.compile(r"^[ \t]*[-*][ \t]*\[recorded\]", re.I)
_ITEM_RE = re.compile(r"^[ \t]*[-*][ \t]+\S")
_VERDICT_RE = re.compile(r"^VERDICT:\s*(pass|return|doubt)\b\s*(?:[—–:-]+\s*(.*))?$", re.I)
_ABS_PATH_RE = re.compile(r"(?<![\w~])/(?:[A-Za-z0-9_.+-]+/)+[A-Za-z0-9_.+-]*")


def split_sections(body: str) -> dict:
    """{canonical section name: [lines]} for the sections the body actually has.

    A heading matches when it starts with the section's name, so `## Criteria (3)`
    is still the Criteria section. Strict heading matching is exactly the kind of
    format gate this module exists to remove.
    """
    found: dict = {}
    current = None
    for line in body.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            title = m.group(1).strip().lower()
            current = None
            for name in SECTIONS:
                if title.startswith(name.lower()):
                    current = name
                    found.setdefault(name, [])
                    break
            continue
        if current is not None:
            found[current].append(line)
    return found


def compute_acceptance_values(path: Path, desk: Optional[Desk] = None, lane: Optional[str] = None,
                              task: Optional[str] = None, result=None) -> dict:
    path = Path(path)
    if not path.exists():
        raise BellhopError(f"acceptance verdict not found: {path}")
    body = strip_header(read(path), FENCE_ACCEPTANCE)
    sections = split_sections(body)

    if task is None:
        head_lines = [l.strip() for l in sections.get("Task", []) if l.strip()]
        task = head_lines[0] if head_lines else path.name.split(".", 1)[0]

    criteria = sections.get("Criteria", [])
    marks = [m.group(1).lower() for m in (_CRITERION_RE.match(l) for l in criteria) if m]
    total, passed = len(marks), marks.count("x")

    verdict, reason = NOT_AVAILABLE, None
    non_empty = [l.strip() for l in body.splitlines() if l.strip()]
    if non_empty:
        m = _VERDICT_RE.match(non_empty[-1])
        if m:
            verdict = m.group(1).lower()
            reason = (m.group(2) or "").strip() or None

    outside = None
    if desk is not None and lane:
        roots = [str(Path(desk.lane(lane)["worktree"]).resolve()), str(Path(desk.root).resolve())]
        hits = {p for p in _ABS_PATH_RE.findall(body) if not any(p.startswith(r) for r in roots)}
        outside = len(hits)

    missing = [n for n in SECTIONS if n not in sections]
    # Who actually judged this. The verdict is the desk's most expensive reading, and a
    # verdict from the model the work was meant to be checked against is not one.
    asked = answered = reasoning = NOT_REPORTED
    if isinstance(result, dict):
        fields = _result_fields(result)
        asked = fields.get("model_asked") or NOT_REPORTED
        answered = fields.get("model") or NOT_REPORTED
        reasoning = fields.get("provider_reasoning") or NOT_REPORTED
    return {
        "task": task,
        "verdict": verdict,
        "verdict_reason": reason,
        "criteria_total": total,
        "criteria_pass": passed,
        "criteria_fail": total - passed,
        "mutations": sum(1 for l in sections.get("Mutation", []) if _BROKE_RE.match(l)),
        "doubts_blocking": sum(1 for l in sections.get("Doubts", []) if _BLOCKING_RE.match(l)),
        "doubts_recorded": sum(1 for l in sections.get("Doubts", []) if _RECORDED_RE.match(l)),
        "extra_findings": sum(1 for l in sections.get("Extra findings", []) if _ITEM_RE.match(l)),
        "sections_missing": ", ".join(n.lower() for n in missing) if missing else "none",
        "paths_outside": outside,
        "model_asked": asked,
        "model": answered,
        "provider_reasoning": reasoning,
        "bytes": len(body.encode("utf-8")),
        "generated": ts(),
    }


def write_acceptance_header(path: Path, desk: Optional[Desk] = None, lane: Optional[str] = None,
                            task: Optional[str] = None, result=None) -> Tuple[dict, str]:
    values = compute_acceptance_values(path, desk=desk, lane=lane, task=task, result=result)
    return values, write_header(path, "acceptance", values)


# -- the checkers: they read the header and nothing else -------------------

class Report:
    def __init__(self, kind: str, path: Path):
        self.kind = kind
        self.path = Path(path)
        self.reds: List[str] = []
        self.warns: List[str] = []
        self.code = EXIT_OK

    def red(self, msg: str) -> None:
        self.reds.append(msg)
        if self.code == EXIT_OK:
            self.code = EXIT_RED

    def warn(self, msg: str) -> None:
        self.warns.append(msg)

    @property
    def summary(self) -> str:
        if not self.reds and not self.warns:
            return "ok"
        return f"red {len(self.reds)} warn {len(self.warns)}"

    def lines(self) -> List[str]:
        return [f"red: {m}" for m in self.reds] + [f"warn: {m}" for m in self.warns] + [self.summary]

    def text(self) -> str:
        return "\n".join(self.lines()) + "\n"


def _read_or_none(path: Path) -> Optional[str]:
    try:
        return read(path)
    except (OSError, UnicodeDecodeError):
        return None


def _shape_warnings(rep: Report, kind: str, values: dict) -> None:
    spec = SPECS[kind]
    known = {f.key for f in spec}
    for f in spec:
        raw = values.get(f.key)
        if raw is None or raw == "":
            if f.required:
                rep.warn(f"{f.key} is missing from the header")
            continue
        if raw == NOT_AVAILABLE:
            if f.required:
                rep.warn(f"{f.key} could not be computed by the generator")
            continue
        if f.type in ("int", "float"):
            try:
                float(raw)
            except ValueError:
                rep.warn(f"{f.key} is not a number: {raw}")
    for key in values:
        if key not in known:
            rep.warn(f"unknown key in the header: {key} (the spec is bellhop/headers.py)")


def _as_int(values: dict, key: str) -> Optional[int]:
    raw = values.get(key)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _same_commit(a: str, b: str) -> bool:
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return len(short) >= 7 and long.startswith(short)


def check_receipt(path: Path, packet=None) -> Report:
    """Hard gates: a header, a head, a base that matches the packet, a check_exit."""
    rep = Report("receipt", path)
    text = _read_or_none(Path(path))
    if text is None:
        rep.red(f"receipt not found or not readable: {path}")
        rep.code = EXIT_MALFORMED
        return rep
    values, _span = parse_header(text, FENCE_RECEIPT)
    if values is None:
        rep.red(f"no ```{FENCE_RECEIPT}``` block; run `bellhop receipt-header --receipt {path} --lane <lane>`")
        return rep
    head = values.get("head", "")
    if not head or head == NOT_AVAILABLE:
        rep.red("head is missing from the header")
    if packet is not None:
        base = values.get("base", "")
        if not _same_commit(base, packet.base):
            rep.red(f"base in the header is {base or 'missing'}, the packet says {packet.base}")
    if not values.get("check_exit"):
        rep.red("check_exit is missing from the header")

    _shape_warnings(rep, "receipt", values)
    exit_raw = values.get("check_exit", "")
    if exit_raw.isdigit() and int(exit_raw) != 0:
        rep.warn(f"the lane check exited {exit_raw}: {values.get('check_summary', NOT_AVAILABLE)}")
    elif exit_raw == CHECK_NOT_RUN:
        rep.warn("no check_cmd on this lane, so nothing was run against the tree")
    left = _as_int(values, "left_in_tree")
    if left:
        rep.warn(f"{left} file(s) left uncommitted in the lane worktree")
    if values.get("dead") == "yes":
        rep.warn("the watchdog marked this task dead")
    return rep


def check_acceptance(path: Path, max_bytes: int = DEFAULT_ACCEPTANCE_BYTES) -> Report:
    """Hard gates: a header, a verdict in the set, at least one mutation, and no
    blocking doubt hiding under a pass."""
    rep = Report("acceptance", path)
    text = _read_or_none(Path(path))
    if text is None:
        rep.red(f"acceptance verdict not found or not readable: {path}")
        rep.code = EXIT_MALFORMED
        return rep
    values, _span = parse_header(text, FENCE_ACCEPTANCE)
    if values is None:
        rep.red(f"no ```{FENCE_ACCEPTANCE}``` block; run `bellhop acceptance-header --file {path}`")
        return rep
    verdict = (values.get("verdict") or "").lower()
    if verdict not in VERDICTS:
        rep.red(f"verdict is {values.get('verdict') or 'missing'}; the last line must be "
                f"VERDICT: {' | '.join(VERDICTS)}")
    mutations = _as_int(values, "mutations")
    if mutations is None:
        rep.red("mutations is missing from the header")
    elif mutations < 1:
        rep.red("mutations is 0: a verdict with no mutation is a reading, not a verification")
    blocking = _as_int(values, "doubts_blocking") or 0
    if blocking > 0 and verdict == "pass":
        rep.red(f"{blocking} blocking doubt(s) and the verdict is pass; a blocking doubt stops the pass")

    _shape_warnings(rep, "acceptance", values)
    size = _as_int(values, "bytes")
    if size is not None and size > max_bytes:
        rep.warn(f"body is {size} bytes, over the {max_bytes}-byte cap; the coordinator has to read this")
    missing = values.get("sections_missing", NOT_AVAILABLE)
    if missing and missing not in (NOT_AVAILABLE, "none"):
        rep.warn(f"sections not in the body: {missing}")
    outside = _as_int(values, "paths_outside")
    if outside:
        rep.warn(f"{outside} path(s) named in the body are outside the lane worktree and the desk")
    total = _as_int(values, "criteria_total")
    passed = _as_int(values, "criteria_pass")
    failed = _as_int(values, "criteria_fail")
    if total == 0:
        rep.warn("no `- [ ]` or `- [x]` criteria found under ## Criteria")
    elif None not in (total, passed, failed) and passed + failed != total:
        rep.warn(f"criteria_pass + criteria_fail is {passed + failed}, criteria_total is {total}")
    if verdict in ("return", "doubt") and (values.get("verdict_reason") or NOT_AVAILABLE) == NOT_AVAILABLE:
        rep.warn(f"a {verdict} verdict should carry one sentence saying why")
    return rep
