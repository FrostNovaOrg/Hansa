"""Acceptance: an isolated verifier, a case pack the script assembles, a named stall.

The pipeline this replaces was run in shadow mode and stalled most of the time, and
almost none of the stalls were about judgement: most were the shape of the verdict,
the rest the verifier editing the tree it was judging, the case pack, the
session dying, or timeouts. Some of the tree edits reached the real lane tree. So
the mechanics moved here, into code, and the session is left with the reading:

- the verifier never runs in the lane worktree; it runs in a detached copy that is
  measured before and after and deleted either way (`prepare`, `snapshot`, `recheck`);
- the case pack is assembled here, capped, and trimmed in a fixed order, so a long
  receipt cannot cost a run (`prepare`);
- a verdict in the wrong shape is retried once with a reminder before it is a stall
  (`verdict format`);
- the session is launched in its own process group and killed as a group (`timeout`).

Every run ends in one line in `state/closings.md`, verdict or stall, so a task that
stalled and a task nobody ever ran stop looking the same.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from . import headers as headers_mod
from .config import FRAMEWORK_ROOT, Desk
from .packet import Packet, parse_packet
from .util import BellhopError, append, git, now, read, run, scrubbed_env, ts, write, write_json

# -- names -----------------------------------------------------------------
# The steps a run walks, in order. A stall is recorded as the step it stalled at, so
# "where did the 82 runs die" is a question the closings file can answer by itself.
STEPS = ("prepare", "snapshot", "verifier", "recheck", "verdict format", "timeout")
STALL_TOUCHED = "verifier touched the tree"
STALL_TIMEOUT = "timeout"
STALL_FORMAT = "verdict format"

CLOSING_MAX = 200
TRIM_MARK = "[trimmed by bellhop accept to fit the case-pack cap]"
REMINDER = (
    "Reminder: your previous answer was rejected on shape, not on judgement. "
    "The last line must be `VERDICT: pass`, `VERDICT: return - <one sentence>` or "
    "`VERDICT: doubt - <one sentence>`; at least one mutation line must sit under "
    "`## Mutation`, shaped `- broke: <what> / <the red> / restored <hash>`; and a "
    "blocking doubt may not sit under a pass. Send the whole verdict again, in the "
    "eight sections. What the checker found: "
)

# Read-only tools, the four git readers, and the lane's own check command, so a
# criterion of the form "command X prints Y" can be verified rather than believed.
READ_TOOLS = ("Read", "Grep", "Glob")
GIT_TOOLS = ("Bash(git diff:*)", "Bash(git status:*)", "Bash(git log:*)", "Bash(git show:*)")
# The mutation is the one write a verifier makes, and the checker's hard gate demands
# it: break what the criterion claims, watch the check go red, put it back. A verifier
# with no writer at all can only claim a mutation it never made, which is the reading
# the mutation gate exists to refuse. So it edits -- inside the copy, which is thrown
# away, with `git checkout` to restore exactly, and the recheck as the backstop: an
# unrestored mutation is a stall, not a verdict.
# There is no `Write` and the desk is not reachable, so the verdict still cannot be
# saved anywhere. It is the session's final message.
MUTATION_TOOLS = ("Edit", "Bash(git checkout:*)")
VERIFIER_TOOLS = ",".join(READ_TOOLS + GIT_TOOLS + MUTATION_TOOLS)


# -- paths -----------------------------------------------------------------

def terms_path(desk: Desk) -> Path:
    """The verifier's terms: the desk's copy if it has one, else the framework's."""
    p = desk.path("rules", "acceptance-terms.md")
    return p if p.exists() else FRAMEWORK_ROOT / "template" / "rules" / "acceptance-terms.md"


def out_dir(desk: Desk, shadow: bool = False) -> Path:
    """A shadow run lands beside the real ones, never on top of one."""
    return desk.receipts / "shadow" if shadow else desk.receipts


def verdict_path(desk: Desk, task_id: str, shadow: bool = False) -> Path:
    return out_dir(desk, shadow) / f"{task_id}.acceptance.md"


def check_path(desk: Desk, task_id: str, shadow: bool = False) -> Path:
    """Where the checker's reading is recorded, next to the verdict it read."""
    return out_dir(desk, shadow) / f"{task_id}.acceptance.check.txt"


def result_path(desk: Desk, task_id: str, shadow: bool = False) -> Path:
    """The session's own result JSON, kept rather than discarded."""
    return out_dir(desk, shadow) / f"{task_id}.acceptance.json"


def meta_path(desk: Desk, task_id: str, shadow: bool = False) -> Path:
    return out_dir(desk, shadow) / f"{task_id}.acceptance.meta.json"


def log_path(desk: Desk, task_id: str, shadow: bool = False) -> Path:
    return desk.bells_logs / f"accept-{task_id}{'-shadow' if shadow else ''}.log"


def pack_path(desk: Desk, task_id: str, shadow: bool = False) -> Path:
    return desk.bells_logs / f"accept-{task_id}{'-shadow' if shadow else ''}.pack.md"


def closings_path(desk: Desk, shadow: bool = False) -> Path:
    return desk.state / ("closings-shadow.md" if shadow else "closings.md")


def find_packet(desk: Desk, task_id: str) -> Path:
    hits = [p for p in desk.inbox.rglob(f"{task_id}-*.md")]
    if not hits:
        raise BellhopError(f"no packet with id {task_id} under {desk.inbox}")
    return hits[0]


# -- the closing record ----------------------------------------------------
# One append-only line per run. This helper is deliberately small: a state module is
# being built on another branch and will take it over.

def append_closing(desk: Desk, line: str, shadow: bool = False) -> Path:
    path = closings_path(desk, shadow)
    cap = int(desk.cfg.get("closings_line_chars", CLOSING_MAX))
    line = line.strip().replace("\n", " ")
    if len(line) > cap:
        line = line[: cap - 1].rstrip() + "..."
    if not path.exists():
        # Whichever writer creates the file, its header has to describe both shapes:
        # `bellhop state closings` writes `- <time> [<task>] <line>` into the same
        # register, and a reader told about only one of them will misread the other.
        write(path, f"# Close-outs{' (acceptance shadow)' if shadow else ''} (append-only)\n\n"
                    "(`bellhop accept`, one line per acceptance run: "
                    "`<time> | <id> <lane> | <verdict> | head <sha> | doubts <n> | <sentence>`,"
                    " or `... | stalled at: <step> | log <path>`)\n"
                    "(`bellhop state closings`, one line per closed task: "
                    "`- <time> [<task id>] <one line>`)\n\n")
    append(path, line + "\n")
    return path


def _field(text) -> str:
    """One field of a closing line. The separator never appears inside a field: a
    verdict sentence carrying a `|` would shift every field after it, and a shifted
    line and a well-formed one look the same to the reader that counts them."""
    return " ".join(str(text).replace("|", "/").split())


def _closing(task_id: str, lane: str, verdict: str, head: str, doubts: int, sentence: str) -> str:
    return (f"{ts()} | {_field(task_id)} {_field(lane)} | {_field(verdict)} | head {head[:7]} | "
            f"doubts {doubts} | {_field(sentence)}")


def _stall_closing(task_id: str, lane: str, step: str, log: Path) -> str:
    return f"{ts()} | {_field(task_id)} {_field(lane)} | stalled at: {_field(step)} | log {_field(log)}"


# -- the case pack ---------------------------------------------------------

_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*$")
_FORBID_WORDS = ("do not touch", "don't touch", "forbid", "out of bounds", "not to touch")


def packet_section(body: str, starts: Tuple[str, ...]) -> Tuple[str, str]:
    """(the named section, the rest of the prose). A heading matches on its opening
    words, because a packet is written by a person and `## Do not touch (yet)` is the
    same section as `## Do not touch`."""
    section: List[str] = []
    rest: List[str] = []
    target = False
    for line in body.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            title = m.group(1).strip().lower()
            target = any(title.startswith(s) for s in starts)
        (section if target else rest).append(line)
    return "\n".join(section).strip(), "\n".join(rest).strip()


def cut_utf8(text: str, limit: int) -> str:
    """Cut on a character boundary. A pack that stops inside a multi-byte character
    and a whole pack weigh the same on the byte gate and read differently."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[: max(limit, 0)].decode("utf-8", "ignore").rstrip()


def _trim_to(content: str, budget: int) -> str:
    room = budget - len(TRIM_MARK.encode("utf-8")) - 1
    if room <= 80:
        return TRIM_MARK
    return cut_utf8(content, room) + "\n" + TRIM_MARK


class _Part:
    """One block of the case pack. `rank` is its place in the trimming order; rank 0
    is never trimmed."""

    def __init__(self, key: str, heading: str, content: str, rank: int = 0):
        self.key, self.heading, self.content, self.rank = key, heading, content, rank

    def render(self) -> str:
        return f"{self.heading}\n\n{self.content.strip() or 'none'}"

    def size(self) -> int:
        return len(self.render().encode("utf-8")) + 2  # the blank line that joins it on


def head_matter(desk: Desk, packet: Packet, head: str, base: str) -> Tuple[str, str]:
    """The pack's instructions, from `templates/acceptance-prompt.md`, and where they
    came from. A desk may reword these; it may not reword the material below them,
    which is built in code."""

    def render(text: str) -> str:
        for key, value in (("id", packet.id), ("lane", packet.lane), ("title", packet.title),
                           ("base", base[:12]), ("head", head[:12])):
            text = text.replace("{{" + key + "}}", str(value))
        return text.strip()

    text = render(desk.template("acceptance-prompt.md"))
    if "{{" not in text:
        return text, "desk"
    # A desk copied from an older template still asks for `{{diff}}` and a path to save
    # the verdict to. Both are gone: the material is assembled here, and the session can
    # save nothing. Fall back rather than ship placeholders, and say which was used, because
    # a reworded pack and an ignored one otherwise read the same.
    fallback = read(FRAMEWORK_ROOT / "template" / "templates" / "acceptance-prompt.md")
    return render(fallback), "framework (the desk's template is from an older version)"


def build_case_pack(instructions: str, packet: Packet, lane: dict, receipt_body: str, terms: str,
                    files: List[str], cap: int, receipt_lines: int) -> Tuple[str, List[dict]]:
    """Assemble the pack here, in the script. The coordinator does not hand it over and
    the verifier is not sent to fetch it: the session cannot reach the desk, so a pack
    that names a path it cannot open and a pack that omits the material read alike.

    Trimming order is fixed: the receipt body first, then the packet's prose outside
    the criteria (its own prose before its forbids), and never the criteria.
    """
    lines = receipt_body.splitlines()
    dropped = max(0, len(lines) - receipt_lines)
    body = "\n".join(lines[:receipt_lines])
    forbids, prose = packet_section(packet.body, _FORBID_WORDS)
    head_matter = instructions.strip()
    parts = [
        _Part("criteria", "## Criteria", "\n".join(f"- {a}" for a in packet.acceptance), rank=0),
        _Part("files", f"## Files changed ({len(files)})", "\n".join(f"- {f}" for f in files), rank=0),
        _Part("terms", "## Your terms", terms.strip(), rank=0),
        _Part("receipt", f"## Receipt body ({min(len(lines), receipt_lines)} of {len(lines)} lines)",
              body, rank=1),
        _Part("packet", "## Packet, the rest of it", prose, rank=2),
        _Part("forbids", "## Forbids (verbatim from the packet)", forbids, rank=3),
    ]
    if lane.get("check_cmd"):
        parts.insert(2, _Part("check", "## The lane's check command",
                              f"`{lane['check_cmd']}` - you may run it here, in the copy.", rank=0))

    trims: List[dict] = []
    if dropped:
        trims.append({"part": "receipt", "reason": "receipt line cap", "lines_dropped": dropped})

    def total() -> int:
        """Exactly what the file will weigh: the joiners and the closing newline are in
        the budget too. A cap that is off by the punctuation is a cap that stops runs."""
        return len(head_matter.encode("utf-8")) + sum(p.size() for p in parts) + 1

    for rank in (1, 2, 3):
        for part in [p for p in parts if p.rank == rank]:
            if total() <= cap:
                break
            over = total() - cap
            before = len(part.content.encode("utf-8"))
            part.content = _trim_to(part.content, max(before - over, 0))
            after = len(part.content.encode("utf-8"))
            if after < before:
                trims.append({"part": part.key, "reason": "case-pack cap",
                              "from_bytes": before, "to_bytes": after})
    return "\n\n".join([head_matter] + [p.render() for p in parts]) + "\n", trims


# -- the session -----------------------------------------------------------

def allowed_tools(lane: dict) -> str:
    """Read-only, the four git readers, the mutation pair, and the lane's check."""
    tools = list(READ_TOOLS) + list(GIT_TOOLS) + list(MUTATION_TOOLS)
    check = (lane.get("check_cmd") or "").strip()
    if check:
        tools.append(f"Bash({check.split()[0]}:*)")
    return ",".join(tools)


def _result_object(stdout: str) -> Optional[dict]:
    """The last JSON object on stdout, the way the watchdog reads a worker's result."""
    result = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            result = obj
    return result


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the session's whole process group. A verifier is a session that starts
    other processes; killing the leader alone leaves them running, and a shadow run
    that was killed then looks exactly like one that is still working."""
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def run_session(cmd: List[str], cwd: Path, env: dict, timeout_s: int) -> dict:
    """Launch in its own process group, wait, and kill the group on a timeout."""
    started = now()
    try:
        proc = subprocess.Popen([str(c) for c in cmd], cwd=str(cwd), env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
    except OSError as exc:
        return {"exit_code": 127, "stdout": "", "stderr": f"could not launch the verifier: {exc}",
                "timed_out": False, "duration_s": 0.0}
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
    return {"exit_code": 124 if timed_out else proc.returncode, "stdout": stdout or "",
            "stderr": stderr or "", "timed_out": timed_out,
            "duration_s": round((now() - started).total_seconds(), 1)}


# -- the copy --------------------------------------------------------------

def make_copy(repo: Path, head: str, task_id: str) -> Tuple[Path, Path]:
    """A detached worktree at `head`. Returns (holder directory, the copy)."""
    holder = Path(tempfile.mkdtemp(prefix=f"bellhop-accept-{task_id}-"))
    copy = holder / "copy"
    proc = run(["git", "-C", str(repo), "worktree", "add", "--detach", str(copy), head], check=False)
    if proc.returncode != 0:
        shutil.rmtree(holder, ignore_errors=True)
        raise BellhopError(f"could not make a copy of {head[:12]} to judge in: "
                           f"{(proc.stderr or proc.stdout or '').strip()[:300]}")
    return holder, copy


def remove_copy(repo: Path, holder: Path, copy: Path) -> None:
    """Removed either way: after a verdict, after a stall, after a timeout."""
    run(["git", "-C", str(repo), "worktree", "remove", "--force", str(copy)], check=False)
    shutil.rmtree(holder, ignore_errors=True)
    run(["git", "-C", str(repo), "worktree", "prune"], check=False)


def _tree_state(copy: Path) -> Tuple[str, str]:
    """(uncommitted lines, the hash of HEAD's tree). The first catches an edit, the
    second catches a commit; a verifier that did both would otherwise show neither."""
    return git(copy, "status", "--porcelain"), git(copy, "rev-parse", "HEAD^{tree}")


# -- the run ---------------------------------------------------------------

def _receipt_header(receipt: Path) -> dict:
    values, _span = headers_mod.parse_header(read(receipt), headers_mod.FENCE_RECEIPT)
    return values or {}


def _int(values: dict, key: str) -> int:
    try:
        return int(str(values.get(key, "")).strip())
    except (TypeError, ValueError):
        return 0


def changed_files(copy: Path, base: str, head: str) -> List[str]:
    """The files the change touched, over the range the receipt's header names."""
    try:
        names = [l.strip() for l in git(copy, "diff", "--name-only", f"{base}..{head}").splitlines() if l.strip()]
        if names:
            return names
    except BellhopError:
        pass
    try:
        return [l.strip() for l in git(copy, "show", "--name-only", "--format=", head).splitlines() if l.strip()]
    except BellhopError:
        return []


def verdict_stands(desk: Desk, task_id: str, shadow: bool = False) -> bool:
    """A verdict that the checker read without a red is one the coordinator may already
    have acted on; a red one is a stall's leftovers and may be judged again."""
    check = check_path(desk, task_id, shadow)
    if not verdict_path(desk, task_id, shadow).exists() or not check.exists():
        return False
    lines = [l for l in read(check).splitlines() if l.strip()]
    return bool(lines) and not any(l.startswith("red") for l in lines)


def run_accept(desk: Desk, task_id: str, shadow: bool = False, force: bool = False) -> dict:
    """Run acceptance for one task and return the run's meta.

    It does not raise on a stall: a stall is a reading about the run, it is written to
    the meta and to the closings file, and the caller decides what to do with it.
    """
    packet = parse_packet(find_packet(desk, task_id))
    lane = desk.lane(packet.lane)
    receipt = desk.receipts / f"{packet.stem}.md"
    if not receipt.exists():
        raise BellhopError(f"no receipt yet for {packet.stem}")
    out = verdict_path(desk, packet.id, shadow)
    if out.exists() and not force and verdict_stands(desk, packet.id, shadow):
        raise BellhopError(f"a verdict for {packet.id} already stands at {out}; "
                           "pass --force to judge it again")

    log = log_path(desk, packet.id, shadow)
    started = now()
    meta = {
        "id": packet.id, "lane": packet.lane, "mode": "shadow" if shadow else "real",
        "step": "prepare", "ok": False, "stall": None, "reason": None, "started_at": ts(started),
        "attempts": 0, "verifier_s": None, "duration_s": None, "cost_usd": None,
        "turns": None, "model": None, "provider": None, "session_id": None,
        "exit_code": None, "verdict": None, "verdict_path": None, "log": str(log),
        "case_pack_bytes": None, "case_pack_trims": [], "instructions_from": None,
        "copy": None, "head": None, "base": None,
        "tree_before": None, "tree_after": None, "status_after": None,
    }
    cap = int(desk.cfg.get("casepack_bytes", 6000))
    receipt_lines = int(desk.cfg.get("casepack_receipt_lines", 50))
    timeout_s = int(desk.cfg.get("accept_timeout_s", 900))
    max_turns = int(desk.cfg.get("accept_max_turns", 30))

    def finish(step: str, stall: Optional[str] = None, reason: Optional[str] = None) -> dict:
        meta.update({"step": step, "ok": stall is None, "stall": stall, "reason": reason,
                     "finished_at": ts(), "duration_s": round((now() - started).total_seconds(), 1)})
        write_json(meta_path(desk, packet.id, shadow), meta)
        if stall:
            append_closing(desk, _stall_closing(packet.id, packet.lane, step, log), shadow)
        return meta

    # -- prepare: the copy, then the case pack -----------------------------
    values = _receipt_header(receipt)
    head = values.get("head") or ""
    base = values.get("base") or packet.base
    if not head or head == headers_mod.NOT_AVAILABLE:
        if not force:
            return finish("prepare", "no head to judge",
                          f"{receipt.name} carries no machine header, so there is no head to copy; "
                          f"run `bellhop receipt-header --receipt {receipt} --lane {packet.lane}`, "
                          "or pass --force to judge the lane branch as it stands")
        head = git(Path(lane["worktree"]), "rev-parse", "HEAD")
    meta["head"], meta["base"] = head, base
    repo = Path(lane["repo"])
    try:
        holder, copy = make_copy(repo, head, packet.id)
    except BellhopError as exc:
        return finish("prepare", "no copy", str(exc))
    meta["copy"] = str(copy)
    header_values: dict = {}
    try:
        files = changed_files(copy, base, head)
        instructions, meta["instructions_from"] = head_matter(desk, packet, head, base)
        pack, trims = build_case_pack(instructions, packet, lane, read(receipt),
                                      read(terms_path(desk)), files, cap, receipt_lines)
        meta["case_pack_trims"] = trims
        meta["case_pack_bytes"] = len(pack.encode("utf-8"))
        if meta["case_pack_bytes"] > cap:
            return finish("prepare", "case pack over the cap",
                          f"{meta['case_pack_bytes']} bytes after trimming, cap {cap}; the criteria "
                          "are never trimmed, so the criteria alone are over the cap")
        pack_file = pack_path(desk, packet.id, shadow)
        write(pack_file, pack)

        # -- snapshot ------------------------------------------------------
        try:
            status_before, tree_before = _tree_state(copy)
        except BellhopError as exc:
            return finish("snapshot", "could not measure the copy", str(exc))
        meta["tree_before"] = tree_before
        if status_before:
            return finish("snapshot", "the copy was not clean", status_before[:400])

        # No BELLHOP_DESK and no --add-dir: the session cannot reach the desk at all.
        env = scrubbed_env(lane.get("env_allow", []))
        env.update({
            "BELLHOP_ID": packet.id, "BELLHOP_LANE": packet.lane, "BELLHOP_BASE": base,
            "BELLHOP_HEAD": head, "BELLHOP_COPY": str(copy),
            "BELLHOP_CASE_PACK": str(pack_file), "BELLHOP_PROMPT_FILE": str(pack_file),
            "BELLHOP_SHADOW": "1" if shadow else "0",
        })

        transcript: List[str] = []
        for attempt in (1, 2):
            meta["attempts"] = attempt
            meta["step"] = "verifier"
            if lane.get("accept_cmd"):
                cmd = ["/bin/sh", "-c", lane["accept_cmd"]]
                provider = lane.get("provider") or "custom"
            else:
                cmd = [desk.cfg["claude_bin"], "-p", read(pack_file), "--model", lane["model"],
                       "--allowedTools", allowed_tools(lane), "--output-format", "json",
                       "--max-turns", str(max_turns)]
                provider = "claude"
            session = run_session(cmd, copy, env, timeout_s)
            transcript.append(f"## attempt {attempt} (exit {session['exit_code']}, "
                              f"{session['duration_s']}s)\n\n### stdout\n{session['stdout']}\n"
                              f"\n### stderr\n{session['stderr']}\n")
            write(log, f"# bellhop acceptance log {packet.id}{' (shadow)' if shadow else ''}\n\n"
                       + "\n".join(transcript))
            meta["provider"] = provider
            meta["exit_code"] = session["exit_code"]
            meta["verifier_s"] = round((meta["verifier_s"] or 0) + session["duration_s"], 1)
            result = _result_object(session["stdout"])
            write_json(result_path(desk, packet.id, shadow), result if result is not None else {
                "result": None, "exit_code": session["exit_code"], "timed_out": session["timed_out"],
                "note": "the session printed no result object; the verdict was read from stdout"})
            if result:
                meta.update({"model": result.get("model") or lane.get("model"),
                             "turns": result.get("num_turns"), "cost_usd": result.get("total_cost_usd"),
                             "session_id": result.get("session_id")})
            if session["timed_out"]:
                return finish("timeout", STALL_TIMEOUT,
                              f"the verifier passed {timeout_s}s; its process group was killed")
            if session["exit_code"] != 0:
                return finish("verifier", "verifier failed",
                              f"exit {session['exit_code']}: {(session['stderr'] or '')[:300]}")

            # -- recheck ---------------------------------------------------
            meta["step"] = "recheck"
            try:
                status_after, tree_after = _tree_state(copy)
            except BellhopError as exc:
                return finish("recheck", STALL_TOUCHED, f"the copy could not be measured again: {exc}")
            meta["tree_after"], meta["status_after"] = tree_after, status_after
            if status_after or tree_after != tree_before:
                what = status_after or f"HEAD's tree moved {tree_before[:12]} -> {tree_after[:12]}"
                return finish("recheck", STALL_TOUCHED, what[:400])

            body = ((result or {}).get("result") or session["stdout"] or "").strip()
            if not body:
                return finish("verifier", "verifier said nothing",
                              "the session exited 0 and produced no verdict text")

            # -- verdict format --------------------------------------------
            meta["step"] = "verdict format"
            heading = (f"# Acceptance {packet.stem}{' (shadow)' if shadow else ''}\n\n"
                       f"- time: {ts(started)}\n- base: {base[:12]}\n- head: {head[:12]}\n"
                       f"- attempt: {attempt}\n\n")
            write(out, heading + body + "\n")
            try:
                header_values, _action = headers_mod.write_acceptance_header(
                    out, desk=desk, lane=packet.lane, task=packet.id)
                report = headers_mod.check_acceptance(out, max_bytes=int(
                    desk.cfg.get("acceptance_bytes", headers_mod.DEFAULT_ACCEPTANCE_BYTES)))
            except BellhopError as exc:
                write(check_path(desk, packet.id, shadow),
                      f"red: header could not be written: {exc}\nred 1 warn 0\n")
                return finish("verdict format", STALL_FORMAT, str(exc))
            write(check_path(desk, packet.id, shadow), report.text())
            meta["verdict"] = header_values.get("verdict")
            meta["verdict_path"] = str(out)
            if not report.reds:
                break
            if attempt == 2:
                return finish("verdict format", STALL_FORMAT, "; ".join(report.reds)[:300])
            # One more go, with the shape spelled out. The 35 stalls this answers were
            # rejections that said nothing about whether the work had been done. The
            # reminder goes on top of the cap rather than displacing material, and the
            # meta carries the size the second attempt actually received.
            write(pack_file, read(pack_file) + "\n\n## " + REMINDER + "; ".join(report.reds) + "\n")
            meta["case_pack_bytes"] = len(read(pack_file).encode("utf-8"))
    except Exception as exc:  # never lose the run: an unrecorded crash reads as "never started"
        return finish(meta["step"], "the run failed", f"{type(exc).__name__}: {exc}"[:300])
    finally:
        remove_copy(repo, holder, copy)

    doubts = _int(header_values, "doubts_blocking") + _int(header_values, "doubts_recorded")
    sentence = header_values.get("verdict_reason") or ""
    if not sentence or sentence == headers_mod.NOT_AVAILABLE:
        sentence = (f"{header_values.get('criteria_pass')}/{header_values.get('criteria_total')} criteria, "
                    f"{header_values.get('mutations')} mutation(s)")
    append_closing(desk, _closing(packet.id, packet.lane, meta["verdict"] or "-", head, doubts, sentence), shadow)
    return finish("verdict format")


def accept(desk: Desk, task_id: str, shadow: bool = False, force: bool = False) -> Path:
    """Run acceptance and return the verdict file. Raises when there is no verdict."""
    meta = run_accept(desk, task_id, shadow=shadow, force=force)
    if not meta.get("verdict_path"):
        raise BellhopError(f"acceptance stalled at {meta['step']}: {meta.get('reason')}")
    return Path(meta["verdict_path"])


# -- the coordinator's own verdict ----------------------------------------

def record(desk: Desk, task_id: str, verdict: str) -> str:
    """The coordinator's verdict, written beside the verifier's. In shadow mode this is
    the other half of the comparison: the shadow run is the only verifier run, and the
    coordinator reads its verdict rather than launching a second one."""
    if verdict not in headers_mod.VERDICTS:
        raise BellhopError(f"verdict must be one of {', '.join(headers_mod.VERDICTS)}")
    packet = parse_packet(find_packet(desk, task_id))
    line = f"{ts()} | {packet.id} {packet.lane} | {verdict} | by: coordinator"
    append_closing(desk, line, shadow=False)
    return line


# -- reading the closings back --------------------------------------------

_CLOSING_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def parse_closing(line: str) -> Optional[dict]:
    """Read back one line this module wrote, and nothing else.

    `state/closings.md` is shared. `bellhop state closings` and the watchdog's
    settle-bell write their close-outs into it in the state module's shape,
    `- <time> [<task>] <line>`, and that line can carry a `|` of its own. A close-out
    counted as a verdict and a verdict look the same to the reader that counts them,
    so the leading timestamp -- which only these lines have, with no `- ` before it --
    is the discriminator.
    """
    if "|" not in line or not _CLOSING_TIME_RE.match(line.strip()):
        return None
    parts = [p.strip() for p in line.strip().split("|")]
    if len(parts) < 3:
        return None
    who = parts[1].split()
    if not who:
        return None
    third = parts[2]
    stalled = third.lower().startswith("stalled at:")
    return {
        "time": parts[0], "id": who[0], "lane": who[1] if len(who) > 1 else "",
        "verdict": None if stalled else third.lower(),
        "step": third.split(":", 1)[1].strip() if stalled else None,
        "stalled": stalled,
        "by": "coordinator" if any(p == "by: coordinator" for p in parts) else "verifier",
    }


def read_closings(path: Path) -> List[dict]:
    if not Path(path).exists():
        return []
    return [c for c in (parse_closing(l) for l in read(path).splitlines()) if c]


def _verifier_times(desk: Desk, ids: List[str]) -> List[float]:
    """Durations come from the runs' meta files; the closing line carries the verdict,
    not the clock."""
    out = []
    for task_id in dict.fromkeys(ids):
        p = meta_path(desk, task_id, True)
        if not p.exists():
            continue
        try:
            data = json.loads(read(p))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("verifier_s") is not None:
            out.append(float(data["verifier_s"]))
    return out


def shadow_report(desk: Desk) -> Tuple[str, bool]:
    """Samples, agreement, stall rate and median verifier time, against the switch
    criteria in config. One line each, and the answer at the end."""
    runs = read_closings(closings_path(desk, True))
    coordinator = {c["id"]: c for c in read_closings(closings_path(desk, False)) if c["by"] == "coordinator"}
    verdicts = {c["id"]: c for c in runs if not c["stalled"]}
    stalls = [c for c in runs if c["stalled"]]
    paired = sorted(set(verdicts) & set(coordinator))
    agree = [i for i in paired if verdicts[i]["verdict"] == coordinator[i]["verdict"]]
    agreement = (len(agree) / len(paired)) if paired else 0.0
    stall_rate = (len(stalls) / len(runs)) if runs else 1.0
    times = _verifier_times(desk, [c["id"] for c in runs])
    median = statistics.median(times) if times else None

    want_agree = float(desk.cfg.get("shadow_agreement", 0.9))
    want_samples = int(desk.cfg.get("shadow_min_samples", 20))
    want_stall = float(desk.cfg.get("shadow_stall_rate", 0.1))
    want_median = float(desk.cfg.get("shadow_median_s", 480))
    ok_samples = len(paired) >= want_samples
    ok_agree = bool(paired) and agreement >= want_agree
    ok_stall = bool(runs) and stall_rate < want_stall
    ok_median = median is not None and median <= want_median

    def mark(ok: bool) -> str:
        return "✅" if ok else "🛑"

    lines = [
        f"shadow report: {len(runs)} run(s), {len(verdicts)} verdict(s), {len(stalls)} stall(s), "
        f"{len(coordinator)} coordinator record(s)",
        f"{mark(ok_samples)} samples {len(paired)} (a verdict and a coordinator record on the same id), "
        f"needs >= {want_samples}",
        f"{mark(ok_agree)} agreement {agreement:.2f} ({len(agree)}/{len(paired)}), needs >= {want_agree:.2f}",
        f"{mark(ok_stall)} stall rate {stall_rate:.2f} ({len(stalls)}/{len(runs)}), needs < {want_stall:.2f}",
        f"{mark(ok_median)} median verifier "
        + (f"{median:.0f}s" if median is not None else "- (no timings on disk)")
        + f", needs <= {want_median:.0f}s",
    ]
    if stalls:
        by_step: dict = {}
        for c in stalls:
            by_step[c["step"]] = by_step.get(c["step"], 0) + 1
        lines.append("stalls by step: " + ", ".join(f"{k} {v}" for k, v in sorted(by_step.items())))
    ready = ok_samples and ok_agree and ok_stall and ok_median
    lines.append("switch: ready - all four criteria are met" if ready else
                 "switch: not yet - every criterion above has to be met")
    return "\n".join(lines) + "\n", ready


def status_text(desk: Desk, task_id: str) -> str:
    """The last run's meta: the step it reached, what it cost, where the log is."""
    present = [p for p in (meta_path(desk, task_id, False), meta_path(desk, task_id, True)) if p.exists()]
    if not present:
        return f"no acceptance run recorded for {task_id}\n"
    meta = json.loads(read(max(present, key=lambda p: p.stat().st_mtime)))
    rows = [
        ("id", meta.get("id")), ("lane", meta.get("lane")), ("mode", meta.get("mode")),
        ("step", meta.get("step")), ("ok", "yes" if meta.get("ok") else "no"),
        ("stall", meta.get("stall") or "-"), ("reason", meta.get("reason") or "-"),
        ("attempts", meta.get("attempts")), ("verdict", meta.get("verdict") or "-"),
        ("duration_s", meta.get("duration_s")), ("verifier_s", meta.get("verifier_s")),
        ("cost_usd", meta.get("cost_usd")), ("turns", meta.get("turns")),
        ("case_pack_bytes", meta.get("case_pack_bytes")),
        ("case_pack_trims", ", ".join(t["part"] for t in meta.get("case_pack_trims") or []) or "none"),
        ("verdict_path", meta.get("verdict_path") or "-"), ("log", meta.get("log")),
    ]
    return "".join(f"{k}: {v}\n" for k, v in rows)
