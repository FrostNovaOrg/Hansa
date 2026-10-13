"""Views: the facts behind `hansa status`, `hansa show`, `hansa explain` and the web page.
Each view returns plain data; the *_text helpers turn it into short lines."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from . import SESSION_NAMES, SESSION_PREFIXES, account, alert, build, cost, disk, installed, installed_text, notify, plugin, release
from .desk import (DENY_AT, HOME_SECRETS, hidden_note, no_read_unusable, MAC_SECRETS, Desk, event_record, no_read_parts, no_read_parts_shown, stop_text, bad_messages, claude_home, allow_fix, refused_rule, kept_from_workers, web_sites, listed_targets, read_json,
                   read_split, write_json, PROJECT_FILE, PROJECT_LONG)
from .run import (disk_short, LINUX_SANDBOX_NOTES, LINUX_TRY_NOTE, output_hints, lane_identity, lane_locale, PREVIEW_FIRST_LOG, ROUND_QUIET, STUCK_AFTER, add_usage, branch_holds, is_local, join_command, lane_occupied, occupied_lane, merge_lane, merge_check_lane, merge_target, last_round,
                  markers_in_change, markers_text, merge_check_ahead, merge_command, merge_minutes, not_looking, own_merge_check, own_rest_minutes, progress_of,
                  provider_problem, rest_minutes, rest_text, rethink_text, rounds_line, sandbox_held, sandbox_held_text, serve_code, serve_session, server_state,
                  lane_env, server_text, srt_needed, srt_problem, srt_record, srt_when, tried_lanes, waiting_looks, waiting_reason, write_list)

STATES = {
    "queued": "waiting for a free lane and a ready provider",
    "running": "a worker is on it, or hansa is running the lane's check on its work",
    "done": "the worker finished; waiting for your review (hansa show / hansa merge / hansa retry)",
    "merging": ("hansa merge is checking it together with what the base gained since it was made: merged onto the "
                "base as it is now, with the lane's merge check (its check, unless it has a merge_check of its own) "
                "run there; it merges if that passes. Or it needs no check "
                "and waits its turn behind a merge check on the same base"),
    "failed": "the run ended with an error; hansa show says why",
    "merged": "merged into the lane's base branch",
    "cancelled": "stopped or closed on request",
    "replaced": "a follow-up task took its place",
}


# a terminal's escape sequences (colours, cursor moves, titles), which a test runner writes into its output
ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")


def plain(line: str, width: int | None = 200) -> str:
    """A last line of a setup or check as shown: without escape sequences (the stored log keeps them)."""
    return ESCAPES.sub("", line)[:width]


# What marks a failing test, or an error that kept the tests from running, in a check's output, by each runner's
# own markers (never a line merely holding the word FAIL), for hansa show to lift out (failure_spans)
RULE = re.compile(r"={20,}|-{20,}")
UNITTEST_HEAD = re.compile(r"^(FAIL|ERROR): \S")  # Python's unittest, after a line of =, to its next = or - line
PYTEST_SECTION = re.compile(r"^=+ (FAILURES|ERRORS) =+$")  # pytest, to its next ==== heading ====
PYTEST_SUMMARY = re.compile(r"^=+ short test summary info =+$")  # its FAILED/ERROR lines
PYTEST_HEADING = re.compile(r"^=+ .+ =+$")
PYTEST_TEST = re.compile(r"^_{3,} .+ _{3,}$")  # one test's part of its FAILURES section
SUREFIRE = re.compile(r"<<< (FAILURE|ERROR)!")  # JUnit run by Maven's Surefire, with its stack trace
SUREFIRE_SUMMARY = re.compile(r"^\[ERROR\] (Failures|Errors|Tests in error|Failed tests): *$")
SUREFIRE_RUN = re.compile(r"^\[ERROR\] Tests run: \d+")  # its count of a run that failed ([INFO] when it passed)
MAVEN_COMPILE = re.compile(r"^\[ERROR\] COMPILATION ERROR")
MAVEN_ERROR = re.compile(r"^\[ERROR\] .+:\[\d+,\d+\] ")  # a compiler's error at path:[line,col], in any language
GO_FAIL = re.compile(r"^\s*--- FAIL: (\S+)")  # go test, its messages above it (-v) or under it
GO_PASS = re.compile(r"^\s*--- (PASS|SKIP): ")
GO_RUN = re.compile(r"^=== (RUN|CONT|NAME|PAUSE)\s+(\S+)")
GO_PACKAGE = re.compile(r"^(ok|FAIL|\?)\s+\S+\s+(\(cached\)|\d+(\.\d+)?s\b|\[[^\]]*\])")  # a package's result line
GO_PANIC = re.compile(r"^panic: ")  # with the first part of its goroutine trace
GO_BUILD = re.compile(r"^# \S+( \[\S+\])?$")  # go's build errors: # pkg, # pkg [pkg.test] or vet's # [pkg], file.go:l:c: under it
GO_ERROR = re.compile(r"^\S+\.go:\d+:\d+: ")
JEST_BLOCK = re.compile(r"^\s+● (?!Console$)\S")  # jest's failing test, with its Expected/Received and code
JEST_FILE = re.compile(r"^ ?FAIL  \S")
GRADLE_FAIL = re.compile(r"^\S.* > .+ FAILED$")  # Gradle: Class > test() FAILED, its exception under it
BLOCK_LINES = 30  # of one failing test hansa show gives
FAILURE_LINES = 60  # of all of them; hansa show ID --check: the whole output
TAIL_LINES = 15  # and the output's last lines after them, always
COMPILE_ERRORS = 5  # of a compiler's errors, the first


def failure_spans(lines: list) -> list:
    """The parts of a check's output that name a failing test and say why, or an error that kept the tests from
    running, as (kind, line numbers) in order, kind 'test' or 'error': unittest's FAIL:/ERROR: blocks, pytest's
    FAILURES and ERRORS sections (a part each test) and its short summary, Surefire's <<< FAILURE!/<<< ERROR!
    blocks with their stack traces, its Failures:/Errors: summary and its Tests run count, Maven's COMPILATION
    ERROR and its first path:[line,col] errors, go's --- FAIL blocks with the messages under them and, by the
    names those fail, under each === RUN/CONT/NAME line of a failing test or subtest wherever it is in its package,
    a go panic with the first part of its trace, go's build errors, jest's FAIL file lines and ● blocks, Gradle's
    FAILED tests. A line a runner marks as passing (go's --- PASS, say) is never one of them."""
    spans, i, n, floor, errors, seen = [], 0, len(lines), 0, 0, set()
    pkg, packages = [], 0  # go test ./... : a part each package, to its result line (ok, FAIL or ?), names
    for x in lines:  # matched within it, since a name may pass in one package and fail in another
        pkg.append(packages)
        packages += bool(GO_PACKAGE.match(x))
    failing, go_run = [set() for _ in range(packages + 1)], [set() for _ in range(packages + 1)]
    for k, x in enumerate(lines):  # go's failing tests and subtests, by name
        if m := GO_FAIL.match(x):
            failing[pkg[k]].add(m.group(1))
    go_parts, part = [], []  # go test -v prints a test's messages as it runs, under each === RUN, === CONT and
    for k, x in enumerate(lines + [""]):  # === NAME line of its name, wherever in its package: the failing ones'
        m = GO_RUN.match(x)
        if m or not (x.strip() and x[:1].isspace()) or GO_FAIL.match(x) or GO_PASS.match(x):
            if len(part) > 1:  # its header, with the messages under it
                go_parts.append(("test", part))
            part = [k] if m and m.group(1) != "PAUSE" and m.group(2) in failing[pkg[k]] else []
            if m:
                go_run[pkg[k]].add(m.group(2))
        elif part:
            part.append(k)

    def until(j, stop, cap=None):
        k = j + 1
        while k < n and not stop(k) and (cap is None or k - j < cap):
            k += 1
        while k > j + 1 and not lines[k - 1].strip():
            k -= 1
        return k

    def indented(k):
        return bool(lines[k].strip()) and lines[k][:1].isspace()
    while i < n:
        x, kind, start = lines[i], "test", i
        if PYTEST_SECTION.match(x):  # a part each test, the first with the section's heading
            end = until(i, lambda k: bool(PYTEST_HEADING.match(lines[k])))
            cuts = [k for k in range(i + 1, end) if PYTEST_TEST.match(lines[k])] or [i + 1]
            for a, b in zip([i] + cuts[1:], cuts[1:] + [end]):
                b = until(a, lambda k: k >= b) if b > a + 1 else b  # without the blank lines it ends with
                spans.append(("test", list(range(a, b))))
            floor = i = end
            continue
        if PYTEST_SUMMARY.match(x):
            end = until(i, lambda k: bool(PYTEST_HEADING.match(lines[k])))
            keep = [i] + [k for k in range(i + 1, end) if lines[k].startswith(("FAILED ", "ERROR "))]
        elif UNITTEST_HEAD.match(x) and i and re.fullmatch(r"={20,}", lines[i - 1]):
            end = until(i, lambda k: bool(re.fullmatch(r"={20,}", lines[k])) or bool(
                re.fullmatch(r"-{20,}", lines[k]) and k + 1 < n and lines[k + 1].startswith("Ran ")))
            keep = list(range(i - 1, end))  # with the line of = above it
        elif SUREFIRE.search(x):
            keep = list(range(i, until(i, lambda k: not lines[k].strip() or lines[k].startswith("["))))
        elif SUREFIRE_SUMMARY.match(x):
            keep = list(range(i, until(i, lambda k: not lines[k].startswith(("[ERROR]   ", "[ERROR] \t", "  ", "\t")))))
        elif SUREFIRE_RUN.match(x):
            keep = [i]
        elif MAVEN_COMPILE.match(x):
            keep, kind = [i], "error"
        elif MAVEN_ERROR.match(x):
            end = until(i, lambda k: not re.match(r"(\[ERROR\]\s{2,}|\s+)\S", lines[k]), 5)
            key = x.strip()
            if key in seen or errors >= COMPILE_ERRORS:  # Maven prints them twice: the first time, the first few
                i = end
                continue
            seen.add(key)
            errors += 1
            keep, kind = list(range(i, end)), "error"
        elif GO_FAIL.match(x):  # with the failing results under it, never a passing one or what is under that
            keep, k, passed = [i], i + 1, None
            while k < n and indented(k):
                at = len(lines[k]) - len(lines[k].lstrip())
                if GO_PASS.match(lines[k]):
                    passed = at
                elif passed is None or at <= passed:
                    keep.append(k)
                    passed = None
                k += 1
            if GO_FAIL.match(x).group(1) not in go_run[pkg[i]]:  # without go test -v: its messages under it, or just above
                while start > floor and indented(start - 1) and not GO_PASS.match(lines[start - 1]) and i - start < 20:
                    start -= 1
                keep = list(range(start, i)) + keep
            spans.append(("test", keep))
            floor = i = k
            continue
        elif GO_PANIC.match(x):
            k = i + 1
            while k < n and k - i < 8 and not re.match(r"goroutine \d+ \[", lines[k]):
                k += 1
            if k < n and lines[k].startswith("goroutine "):
                end = until(k, lambda j: not lines[j].strip() or lines[j].startswith(("exit status ", "FAIL")), 16)
            else:
                end = until(i, lambda j: not indented(j), 8)
            keep = list(range(i, end))
        elif GO_BUILD.match(x) and i + 1 < n and GO_ERROR.match(lines[i + 1]):
            end = until(i, lambda k: not GO_ERROR.match(lines[k]) and not indented(k), 12)
            keep, kind = list(range(i, end)), "error"
        elif JEST_BLOCK.match(x):
            at = len(x) - len(x.lstrip())  # to the next line as far left as it, or further
            end = until(i, lambda k: bool(lines[k].strip()) and len(lines[k]) - len(lines[k].lstrip()) <= at, 40)
            keep = list(range(i, end))
        elif JEST_FILE.match(x):
            keep = [i]
        elif GRADLE_FAIL.match(x):
            keep = list(range(i, until(i, lambda k: not indented(k), 20)))
        else:
            i += 1
            continue
        spans.append((kind, keep))
        floor = i = max(keep[-1] + 1, i + 1)
    out, used = [], set()  # in the output's order, each line once
    for kind, keep in sorted(spans + go_parts, key=lambda s: s[1][0]):
        keep = [k for k in keep if k not in used]
        used.update(keep)
        if keep:
            out.append((kind, keep))
    return out


def failed_check_lines(log: str, tail: list, tid: str, option: str = "--check-log") -> list:
    """For hansa show, a check or merge check that failed: the failing tests (or the errors that kept them from
    running) lifted out of its whole output (failure_spans), those of their lines above its last TAIL_LINES lines
    (a failure's message above them even when its marker is among them), each cut to BLOCK_LINES and all to
    FAILURE_LINES, then those last lines, always: never less than its last lines alone, which is all it gives
    when nothing in it is recognised, or when the output is not kept (tail: the last lines as recorded). Then a
    line for each known cause its output shows (output_hints). option: the hansa show option that prints it whole."""
    hints = ["  " + h for h in output_hints(log or "\n".join(tail))]
    lines = [plain(x, None) for x in log.rstrip().splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return ["  " + plain(x) for x in tail] + hints
    whole = [f"  the whole output: hansa show {tid} {option}"]
    last = max(0, len(lines) - TAIL_LINES)  # the lines from here are shown as its last lines
    picked, kinds = [], set()
    for kind, keep in failure_spans(lines):  # the lines above the last ones, a failure's message above them too
        keep = [k for k in keep if k < last]
        if not any(lines[k].strip() and not RULE.fullmatch(lines[k].strip()) for k in keep):
            continue
        kinds.add(kind)
        picked += ["    " + lines[k][:200] for k in keep[:BLOCK_LINES]]
        if len(keep) > BLOCK_LINES:
            picked.append(f"      ... {len(keep) - BLOCK_LINES} more lines of it")
    rest = [x[:200] for x in lines[last:]]
    if not picked:
        return ["  " + x for x in rest] + whole + hints
    what = {frozenset({"test"}): "the failing tests", frozenset({"error"}): "the errors"}.get(
        frozenset(kinds), "the failing tests and errors")
    out = [f"  {what}, from its output:"] + picked[:FAILURE_LINES]
    if len(picked) > FAILURE_LINES:
        out.append(f"    ... {len(picked) - FAILURE_LINES} more lines of them")
    return out + ["  its last lines:"] + ["    " + x for x in rest] + whole + hints


def as_closed_now(m: dict) -> dict:
    """A verification that had ended when its task closed, recorded before hansa kept such a one as it ended
    (state cancelled, the closed line as its error, and its verdict), as the rule has it now: done, with the
    closed line and no error. The record itself stays as it is. Any other task as it is."""
    if m.get("kind") == "verify" and m.get("state") == "cancelled" and "verdict" in m \
            and str(m.get("error") or "").startswith("closed: "):
        return {**m, "state": "done", "closed": m["error"], "error": ""}
    return m


def verification(desk: Desk, m: dict):
    """The latest verification of a task: {task, state, verdict, lane}, or None."""
    vid = m.get("verify")
    if not vid:
        return None
    v = as_closed_now(desk.meta(vid))
    return {"task": vid, "state": v["state"], "verdict": v.get("verdict", ""), "lane": v.get("lane", "")}


def _verification_line(v) -> str:
    if v["state"] in ("queued", "running"):
        return f"{v['task']} {v['state']}"
    return f"{v['task']}: {v['verdict']}" if v["verdict"] else f"{v['task']} {v['state']} without a verdict"


def _progress_line(p: dict) -> str:
    if p["idle_min"] is None:
        return "no reply from the model yet"
    return (f"{p['replies']} model replies, last activity {p['idle_min']} min ago"
            + (f"; last note: {p['note']}" if p["note"] else ""))


def progress_text(desk: Desk, m: dict) -> str:
    """How far a running task's worker has got, in words."""
    return ("checking now" if m.get("phase") == "checking" else "hansa is preparing it" if m.get("phase") == "starting"
            else _progress_line(progress_of(desk, m)))


def _mins(since) -> int:
    return int((time.time() - (since or time.time())) // 60)


def merge_check_busy_with(desk: Desk, m: dict, tasks: list | None = None):
    """The task that holds the lane a merging task's check waits for; None when the lane is free."""
    lane = merge_check_lane(desk.settings(), m)
    return next((x["id"] for x in (tasks if tasks is not None else desk.tasks())
                 if occupied_lane(x) == lane and lane_occupied(x) and x["id"] != m["id"]), None)


def merge_check_waits_for(desk: Desk, m: dict, tasks: list | None = None) -> str:
    """The merge check on the same repository and base branch that a queued one waits for, in words ("t0123's
    merge check on lane a"); '' when it is first in line."""
    s = desk.settings()
    x = merge_check_ahead(s, m, tasks if tasks is not None else desk.tasks())
    what = "merge" if ((x or {}).get("merge_check") or {}).get("plain") else "merge check"
    if x:
        return f"{x['id']}'s {what} on lane {merge_check_lane(s, x)}"
    lane = s["lanes"].get(m.get("lane"))
    r = release.running_on(desk, lane["repo"]) if lane and m["state"] == "merging" and \
        (m.get("merge_check") or {}).get("phase", "queued") == "queued" else None
    return release.running_text(r) if r else ""


def merge_lane_wait(desk: Desk, m: dict, tasks: list | None = None) -> str:
    """A queued check on a separate merge lane can wait for that lane's try, or for a concurrency slot."""
    s = desk.settings()
    lane = merge_check_lane(s, m)
    if lane == m.get("lane") or (m.get("merge_check") or {}).get("phase") != "queued":
        return ""
    if lane in tried_lanes(desk):
        return "try"
    return "slot" if sum(lane_occupied(x) for x in (desk.tasks() if tasks is None else tasks)) >= s["concurrency"] else ""


def merge_check_text(desk: Desk, m: dict, tasks: list | None = None, sentence: bool = False) -> str:
    """Where a merging task's merge check is, in one line. sentence: one just queued, as hansa merge says it."""
    mc = m.get("merge_check") or {}
    lane = merge_check_lane(desk.settings(), m)
    base = desk.settings()["lanes"].get(m.get("lane"), {}).get("base") or "main"
    again = f" (again: {base} moved while it was checked)" if mc.get("moves") else ""
    if mc.get("phase") in ("starting", "running"):
        where = f" on lane {lane}" if lane != m.get("lane") else ""
        return (f"merge check running for {_mins(mc.get('started'))} min{where}{again}: {m['id']} merged onto {base} at "
                f"{mc.get('onto', '')[:10]}, then " + ("the lane's setup and " if mc.get("setup") else "")
                + ("merge check" if mc.get("own") else "check") + f": {mc.get('command', '')}")
    ahead = merge_check_waits_for(desk, m, tasks)
    if mc.get("plain"):
        how = "without a check" + ("" if mc.get("despite") else f" if {base} has still not moved")
        return (f"merge queued, to go in {how}: " + (f"waits for {ahead}" if ahead else disk_short(desk) or not_looking(desk)
                                                      or "merges when `hansa serve` next looks (within seconds)"))
    other = merge_check_busy_with(desk, m, tasks)
    waiting = merge_lane_wait(desk, m, tasks)
    return (("The merge check is queued" if sentence else "merge check queued") + f"{again}: "
            + (f"waits for {ahead}" if ahead else
               f"waits for lane {lane}, busy with {other}" if other else
               f"waits for lane {lane}, held by a try" if waiting == "try" else
               "waits for a free concurrency slot" if waiting == "slot" else
               disk_short(desk) or not_looking(desk)
               or "starts when `hansa serve` next looks (within seconds)") + ("." if sentence else ""))


def silent(r: dict) -> bool:
    """A check or merge check that failed printing nothing, with nothing else said about why."""
    return bool(r) and not r.get("could_not_run") and not r.get("ok") and not r.get("note") and not r.get("tail")


def recheck_step(r: dict, tid: str) -> str:
    """The --recheck step for a check that did not pass and may be the machine's doing."""
    return (f"if it should have printed something, once that is fixed: hansa merge {tid} --recheck" if silent(r)
            else f"once the machine is fixed (hansa explain server): hansa merge {tid} --recheck")


def machine_hint(r: dict) -> str:
    """Why a check or merge check that did not pass may be the machine's doing and not the task's; '' when
    nothing says so."""
    if r.get("could_not_run"):
        return ("it never ran, so the task is not judged on it: this is the machine's problem, not the task's "
                "(hansa explain server)")
    if r.get("ok") or r.get("note"):  # stopped, over the time ceiling, its setup failed: said in the note
        return ""
    if silent(r):
        return ("nothing came out: it printed nothing at all, which usually means the command could not start "
                "(hansa explain server); a check can also fail silently on purpose (a plain exit 1)")
    if r.get("exit") in (126, 127):
        return (f"exit {r['exit']} usually means a command was not found or could not start, which may be the "
                f"machine's problem rather than the task's (hansa explain server)")
    return ""


WAITED = "it waited its turn (it needed no check, and none ran), then was not merged"


def waited_text(r: dict) -> str:
    """Why a merge that waited its turn without a check was not merged (merge_check_result {waited: True})."""
    return f"{WAITED}: {str(r.get('note') or 'it could not be made').removesuffix('; nothing was merged')}"


def merge_result_lines(m: dict, log: str = "") -> list:
    """What the last merge check found, for hansa show. log: its whole output, when it is kept."""
    r = m.get("merge_check_result")
    if not r:
        return []
    if r.get("waited"):  # it needed no check: it waited its turn behind other merges, and no check ran
        return ["merge: " + (f"{WAITED}: " + r["conflict"].removeprefix("hansa: ") if r.get("conflict")
                             else waited_text(r))]
    if r.get("conflict"):
        return ["merge check: nothing was merged; " + r["conflict"].removeprefix("hansa: ")]
    where = f"hansa merged it onto the base at {r.get('onto', '')[:10]}"
    if r.get("lane") and r["lane"] != m.get("lane"):
        where += f" in lane {r['lane']}'s worktree"
    if r.get("ok"):
        return [f"merge check: passed in {r.get('seconds', 0)} s ({where}): {r.get('command', '')}"]
    if r.get("could_not_run"):
        return [f"merge check: {r['note']}; nothing was merged", "  " + machine_hint(r)]
    how = f"FAILED ({r['note']})" if r.get("note") else f"FAILED (exit {r.get('exit')})"
    hint = machine_hint(r)
    return ([f"merge check ({where} and ran the lane's {'merge check' if r.get('own') else 'check'} there): {how}"
             + (f" in {r['seconds']} s: {r['command']}" if r.get("command") else "") + "; nothing was merged"]
            + failed_check_lines(log, r.get("tail", []), m["id"], "--merge-check-log") + (["  " + hint] if hint else []))


def ready_now(desk: Desk, s: dict | None = None) -> list:
    """The providers that can take work now."""
    s = s or desk.settings()
    return [n for n in sorted(s["providers"]) if not provider_problem(desk, s, n)]


def ask_knows_ready(a: dict) -> bool:
    """Whether hansa recorded which providers were ready when the request was made. ready_then lists them (so a
    provider not set up then counts as not ready). A request made before that was recorded has none: hansa cannot
    tell."""
    return "ready_then" in a


def ask_names(a: dict, ready: list) -> list:
    """The providers an open request names (as whole words) that were not ready when it was made and are ready
    now. None for a request made before hansa recorded which were ready (ask_knows_ready: it cannot tell)."""
    if not ask_knows_ready(a):
        return []
    return [n for n in ready if n not in (a.get("ready_then") or [])
            and re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", str(a.get("text", "")), re.IGNORECASE)]


def ask_note(a: dict, ready: list) -> str:
    """For an open request that may be done by now (ask_names): a hint saying so. hansa closes nothing itself."""
    named = ask_names(a, ready)
    return f"{', '.join(named)} {'is' if len(named) == 1 else 'are'} ready now: done?" if named else ""


def ready_news(desk: Desk) -> None:
    """After a provider's key or settings changed, its check ended or its rest ran out (hansa serve looks): for
    each open request, the providers it names that became ready since it was made (ask_names) and it was not told
    of yet, as an ask_ready event the follower posts. Each is told once; again only after the provider stopped
    being ready and became ready again."""
    ready = ready_now(desk)
    news, changed = {}, False
    with desk.lock():
        asks = desk.asks()
        for a in asks:
            if a.get("closed") or not ask_knows_ready(a):
                continue
            told = [n for n in a.get("told_ready") or [] if n in ready]  # stopped being ready: may be told again
            for n in ask_names(a, ready):
                if n not in told:
                    told.append(n)
                    news.setdefault(n, []).append(a["id"])
            if told != (a.get("told_ready") or []):
                a["told_ready"], changed = told, True
        if changed:
            write_json(desk.asks_path, asks)
    for n, ids in news.items():
        desk.event("ask_ready", provider=n, ids=ids)


def project_file(desk: Desk) -> dict:
    """notes/PROJECT.md, the project's own rules the card imports: whether the desk has it, and its size."""
    p = desk.project_path
    try:
        data = p.read_bytes() if p.is_file() else None
    except OSError:
        data = None
    if data is None:
        return {"file": PROJECT_FILE, "present": False, "bytes": 0, "lines": 0, "long": False}
    # an old desk's card, from before it imported the file, is replaced at the coordinator's next start
    card = read_text(desk.root / "CLAUDE.md")
    return {"file": PROJECT_FILE, "present": True, "bytes": len(data), "lines": data.count(b"\n")
            + (not data.endswith(b"\n") and bool(data)), "long": len(data) > PROJECT_LONG,
            **({} if f"@{PROJECT_FILE}" in card.split() else {"card_lacks_it": True})}


def project_text(pf: dict) -> str:
    """hansa status's line on notes/PROJECT.md."""
    if not pf["present"]:
        return (f"project rules: no {pf['file']} (nothing loads with the card; the coordinator may write one, short: "
                "the project's own rules)")
    return (f"project rules: {pf['file']}, {pf['bytes']:,} byte{'' if pf['bytes'] == 1 else 's'}, {pf['lines']} line{'' if pf['lines'] == 1 else 's'}, "
            "loaded with the card" + (" from the coordinator's next start" if pf.get("card_lacks_it") else "")
            + (f"; long: the coordinator rereads it at every turn, so keep it under {PROJECT_LONG // 1024} KB"
               if pf["long"] else ""))


def web_address(desk: Desk) -> tuple:
    """The address and port this desk's hansa serve last served the page on (the defaults before it ever ran)."""
    try:
        port = int((desk.state_dir / "web-port").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        port = 8765
    host = read_text(desk.state_dir / "web-host").strip() or "127.0.0.1"
    return host, port


def page_text(page: dict) -> str:
    """Where the desk's web page is, and whether it is served now, for hansa status and hansa explain. A server
    listening on every address (0.0.0.0, ::) is given at 127.0.0.1 (or ::1), an address one can open."""
    every = page["host"] in ("0.0.0.0", "", "::")
    host = ("[::1]" if page["host"] == "::" else "127.0.0.1") if every else page["host"]
    at = f"http://{host}:{page['port']}/" + (" (it listens on every address)" if every else "")
    if page["up"] and not page.get("answers", True):
        return (f"web page: {at}, but hansa serve is not answering now (its process is stopped or stuck: "
                "hansa status says more), so neither is the page")
    if page["up"]:
        return (f"web page: {at}, served now by hansa serve" + (", though its rounds are stuck (hansa status says more)"
                if page.get("stuck") else " (hansa web prints it with its key, for the principal)"))
    return (f"web page: not served now, since hansa serve is not running; it serves it at {at}"
            + (" (where it last did)" if page.get("served") else " (the default)"))


def page_of(desk: Desk, server: dict) -> dict:
    host, port = web_address(desk)
    stuck = server["state"] == "stuck"  # its rounds stopped coming; its page answers while its process does
    return {"host": host, "port": port, "up": server["state"] != "gone", "stuck": stuck,
            "answers": not stuck or bool(server.get("answers")), "served": (desk.state_dir / "web-port").exists()}


def status(desk: Desk) -> dict:
    s = desk.settings()
    tasks = desk.tasks()
    pst = desk.provider_state()
    ready = ready_now(desk, s)
    designing = [m for m in tasks if m["state"] == "running" and m.get("kind") == "design"]
    running = [m for m in tasks if m["state"] == "running" and m.get("kind") != "design"]
    merging = [m for m in tasks if m["state"] == "merging"]
    tried = tried_lanes(desk)
    busy = {occupied_lane(m) for m in tasks if lane_occupied(m)} | tried
    held = sandbox_held(desk)
    last = last_round(desk)
    server = server_state(desk)
    day = time.time() - 86400  # the last 24 h, each thing by when it happened: not reset at midnight
    rows = cost.worker_rows(desk, day, s, tasks)  # by when the run ended
    no_read = no_read_parts_shown(s, desk.root)
    queued = [m for m in tasks if m["state"] == "queued"]
    looks = waiting_looks(desk, s, tasks, server) if queued else None  # once for all of them
    return {
        "desk": str(desk.root),
        "build": build(),
        "installed": installed(),  # when the code this runs was put in place
        "serve_code": serve_code(desk, server),  # hansa serve on older code than is installed: what to do
        "serve_session": serve_session(desk, server),  # hansa serve started inside a Claude Code session: what it dropped
        "audits_cost": bool(s["coordinator"].get("audits_cost")),
        "paused": desk.paused(),
        "server_up": server["state"] != "gone",  # alive, busy or not: run.server_state is the one rule
        "server": server,
        "page": page_of(desk, server),  # where the web page is, and whether it is served now
        "server_seconds_ago": int(time.time() - last) if last else None,
        "srt_missing": srt_problem(desk, s),  # what stops setups and checks, as hansa serve last saw srt
        "srt": {**srt_record(desk), "needed": srt_needed(s)},
        "try_note": LINUX_TRY_NOTE if account.LINUX else "",  # what the sandbox try leaves to hansa's checks there
        "notifying": notify.names(desk),  # the sessions hansa posts to (hansa notify)
        "heard": notify.hears(desk),  # whether the session asking is one of them
        "mod": (mod := plugin.status(desk)),  # what hansa did at the coordinator's start: the mod loaded or not, and why
        "mod_line": plugin.status_line(mod) if mod else "",
        "clear_line": plugin.clear_line(mod) if mod else "",  # its daily clear: the window, and the clears recorded
        "compact_line": plugin.settings_line(s) if mod else "",  # the desk's compaction settings
        "project": project_file(desk),  # notes/PROJECT.md, which the card imports
        "release_codes": bool(alert.command(s)),  # a waiting release's alert carries a one-time code
        "alert_failed": alert.failure_line(desk),  # the principal's alert command's last failed run, if any
        "bad_messages": bad_messages(s),  # message forms written by hand that hansa cannot use
        # entries of what workers may not read that stop every run, and those left out of runs (desk.no_read_parts)
        "no_read_stop": stop_text(no_read[2]), "no_read_left_out": no_read[1],
        "concurrency": s["concurrency"],
        # free space: the desk's volume now, the floor, and the volumes for which hansa serve starts nothing new
        "disk": {"free": disk.desk_free(desk)[0], "floor": disk.floor(s), "line": disk.desk_line(desk, s),
                 "low": (low := disk.held(desk)),
                 "low_text": disk.held_text(desk, low, disk.floor(s)) if low else "",
                 "unknown": disk.unknown_lines(desk)},  # what its look could not measure, and what was known
        "lanes": [{"name": n, "providers": ln["providers"], "repo": ln["repo"],
                   "task": next((m["id"] for m in tasks if lane_occupied(m) and occupied_lane(m) == n), None),
                   **({"merge_lane": True, "merge_for": [x for x in sorted(s["lanes"]) if x != n
                                                        and merge_target(s, x) == merge_target(s, n)]}
                      if ln.get("merge_lane") else {}),
                   "trying": n in tried,  # a `hansa lane try` queued or running holds the lane
                   "held": held.get(n)}  # its runs whose sandbox could not start, by group (run.sandbox_held)
                  for n, ln in sorted(s["lanes"].items())],
        "providers": [{"name": n, "kind": p["kind"], "model": p["model"], "effort": p.get("effort", ""),
                       "rest_minutes": rest_minutes(s, n), "rest_own": own_rest_minutes(s, n) is not None,
                       "problem": provider_problem(desk, s, n), "check": pst.get(n, {}).get("check")}
                      for n, p in sorted(s["providers"].items())],
        "running": [{"id": m["id"], "title": m["title"], "lane": m.get("lane"), "provider": m.get("provider"),
                     "model": m.get("model_asked"), "minutes": _mins(m.get("started")),
                     "effort": m.get("effort_used", ""), "effort_from": m.get("effort_from"),
                     "effort_said": effort_text(m.get("effort_used", ""), m.get("effort_from"),
                                                s["providers"].get(m.get("provider") or "")),
                     "checking": m.get("phase") == "checking", "starting": m.get("phase") == "starting",
                     "check_minutes": _mins(m.get("check_started")) if m.get("phase") == "checking" else None,
                     "browser": bool(m.get("browser")),
                     "progress": None if m.get("phase") in ("checking", "starting") else progress_of(desk, m)}
                    for m in running],
        "design": [{"id": m["id"], "title": m["title"], "lane": m.get("lane"), "minutes": _mins(m.get("started")),
                    "join": join_command(m), "checking": m.get("phase") == "checking",
                    "check_minutes": _mins(m.get("check_started")) if m.get("phase") == "checking" else None,
                    "progress": None if m.get("phase") == "checking" else progress_of(desk, m)} for m in designing],
        "queued": [{"id": m["id"], "title": m["title"], "lane": m.get("lane") or "any",
                    "waiting": waiting_reason(desk, s, m, busy, looks)} for m in queued],
        "merging": [{"id": m["id"], "title": m["title"], "lane": merge_check_lane(s, m),
                     **({"waiting_for": merge_lane_wait(desk, m, tasks)} if merge_check_lane(s, m) != m.get("lane") else {}),
                     "phase": (m.get("merge_check") or {}).get("phase", "queued"),
                     "minutes": _mins((m.get("merge_check") or {}).get("started")),
                     "busy_with": merge_check_busy_with(desk, m, tasks),
                     "waits_for": {"id": x["id"], "lane": merge_check_lane(s, x)}
                     if (x := merge_check_ahead(s, m, tasks)) else None,
                     "again": bool((m.get("merge_check") or {}).get("moves")),
                     "where": merge_check_text(desk, m, tasks)} for m in merging],
        "review": [{"id": m["id"], "title": m["title"], "lane": m.get("lane"), "stat": m.get("stat", ""),
                    "commits": m.get("commits", 0), "denied": len(m.get("denials", [])), "leaked": m.get("leaked", []),
                    "markers": sorted(m.get("markers") or {}),
                    "check": None if not m.get("check") else m["check"]["ok"],
                    "check_could_not_run": bool((m.get("check") or {}).get("could_not_run")), "verify": verification(desk, m),
                    "merge_check": (m.get("merge_check_result") or {}).get("ok"),
                    "merge_waited": bool((m.get("merge_check_result") or {}).get("waited")),
                    "merge_note": (m.get("merge_check_result") or {}).get("note")
                    or f"exit {(m.get('merge_check_result') or {}).get('exit')}"}
                   for m in tasks if m["state"] == "done" and m.get("kind") != "verify"],
        "failed": [{"id": m["id"], "title": m["title"], "lane": m.get("lane"), "error": m.get("error", "")}
                   for m in tasks if m["state"] == "failed"][-5:],
        "asks": [{**a, "note": ask_note(a, ready)} for a in desk.asks() if not a.get("closed")],
        # the open releases (hansa release) and the latest that ended: the page asks the principal about each waiting one
        "releases": release.page_rows(desk, s, tasks),
        # for the page: the latest requests the coordinator closed itself in the past week, with its reason
        "asks_closed": [{k: a.get(k) for k in ("id", "text", "closed", "reason")} for a in desk.asks()
                        if a.get("closed_by") == "coordinator" and a["closed"] >= time.time() - 7 * 86400][-5:],
        "last_24h": {"finished": sum((m.get("finished") or 0) >= day for m in tasks),
                     "merged": sum(m["state"] == "merged" and (m.get("merged") or 0) >= day for m in tasks),
                     "worker_tokens": sum(sum(r[k] for k in cost.TOKEN_FIELDS) for r in rows),
                     "worker_usd": round(sum(r["usd"] for r in rows if r["usd"] is not None), 2)},
    }


def status_text(st: dict) -> str:
    out = [f"desk {st['desk']}",
           ("PAUSED: " + st["paused"]) if st["paused"] else f"dispatch on, {st['concurrency']} at a time",
           server_text(st.get("server") or {"state": "up" if st["server_up"] else "gone"})]
    out += ["cost audit on"] if st.get("audits_cost") else []
    out = [" · ".join(out + [f"hansa {st['build']}", installed_text(st.get("installed") or 0) or "installed ?",
                              time.strftime("now %H:%M")])]
    out += [st["serve_code"]] if st.get("serve_code") else []  # what asks for action first, then information
    out += [st["serve_session"]] if st.get("serve_session") else []
    out += [st["srt_missing"]] if st.get("srt_missing") else []
    out += [st["alert_failed"]] if st.get("alert_failed") else []
    out += [st["disk"]["low_text"] + ". More: hansa help free-space"] if (st.get("disk") or {}).get("low_text") else []
    out += (st.get("disk") or {}).get("unknown") or []
    out += [f"WARNING: {x}" for x in st.get("bad_messages") or []]
    out += [f"STOPPED: {st['no_read_stop']}"] if st.get("no_read_stop") else []
    out += [f"what workers may not read, entry left out: {x.removeprefix(DENY_AT)}"
            for x in st.get("no_read_left_out") or []]
    out += [page_text(st["page"])] if st.get("page") else []
    out += [st["disk"]["line"]] if st.get("disk") else []
    out.append("hansa posts to: " + ", ".join(st["notifying"]) if st.get("notifying") else
               "hansa posts to no session (hansa notify on, from the coordinator's session)")
    out += [x for x in (f"{st['mod_line']} {plugin.HELP}", st.get("clear_line"), st["compact_line"]) if x] \
        if st.get("mod_line") else []
    out += [project_text(st["project"])] if st.get("project") else []
    out += [f"sandbox probe: {st['try_note']}"] if st.get("try_note") else []
    out.append("lanes:")
    lanes = st["lanes"] or [{"name": "(none)", "providers": [], "task": None}]
    wn, wp = max(len(ln["name"]) for ln in lanes), max(len(" > ".join(ln["providers"])) for ln in lanes)
    for ln in lanes:
        out.append(f"  {ln['name']:<{wn}}  {' > '.join(ln['providers']):<{wp}}  "
                   + (ln["task"] or ("held by a try (hansa lane try)" if ln.get("trying") else
                                     "UNABLE TO RUN" if "work" in (ln.get("held") or {}) else "free"))
                   + (f" · merge lane for lanes {', '.join(ln['merge_for']) or '(none yet)'}; takes no work"
                      if ln.get("merge_lane") else "")
                   + ("; its design sessions UNABLE TO RUN" if "design" in (ln.get("held") or {}) else "")
                   + "".join(f"\n      {sandbox_held_text(h)}" for h in (ln.get("held") or {}).values())
                   + ("\n      More: hansa help sandbox-probe" if ln.get("held") else ""))
    if st["running"]:
        out.append("running:")
        out += [f"  {m['id']} {m['lane']} " + (f"worker done, hansa is running the lane's check, "
                                               f"{m['check_minutes']} min so far" if m["checking"]
                                               else "hansa is preparing it" if m.get("starting")
                                               else f"on {m['provider']} ({m['model']}"
                                               + (f", {m['effort_said']}" if m.get("effort_said") else "") + ")")
                + (" with a browser" if m["browser"] else "")
                + (f" ({m['minutes']} min in all)" if m["checking"] else f", {m['minutes']} min") + f": {m['title']}"
                + (f"\n      so far: {_progress_line(m['progress'])}" if m.get("progress") else "")
                for m in st["running"]]
    if st.get("design"):
        out.append("design sessions (the principal works in them directly; each comes back for review when they end it):")
        out += [f"  {m['id']} lane {m['lane']}, {m['minutes']} min: {m['title']}"
                + (f"\n      ended; hansa is running the lane's check, {m['check_minutes']} min so far" if m["checking"] else
                   f"\n      so far: {_progress_line(m['progress'])}" if m.get("progress") else "")
                for m in st["design"]]
    if st["queued"]:
        out.append("queued:")
        out += [f"  {m['id']} lane {m['lane']}: {m['title']}\n      waiting: {m['waiting']}" for m in st["queued"]]
    if st.get("merging"):
        out.append("merging (each merges by itself if its check passes on the base as it is now, or in its turn; "
                   + ("hansa tells this session when each ends):" if st.get("heard") else "hansa wait <id>):"))
        out += [f"  {m['id']} {m['lane']}: {m['title']}\n      {m['where']}" for m in st["merging"]]
    if st["review"]:
        out.append("waiting for review (hansa show <id>):")
        out += [f"  {m['id']} {m['lane']}: {m['title']} [{m['stat'] or 'no changes'}"
                + ("; check could not run (the machine's problem, not the task's)" if m.get("check_could_not_run")
                   else {True: "; check passed", False: "; check FAILED", None: ""}[m["check"]])
                + (f"; merge: {waited_text({'note': m['merge_note']})}" if m.get("merge_waited") else
                   f"; merge check FAILED ({m['merge_note']}), nothing merged" if m.get("merge_check") is False else "")
                + (f"; verification {_verification_line(m['verify'])}" if m.get("verify") else "")
                + (f"; {m['denied']} command(s) refused" if m["denied"] else "")
                + (f"; holds the key of {', '.join(m['leaked'])}: cannot be merged" if m.get("leaked") else "")
                + (f"; holds conflict markers in {', '.join(m['markers'])}: cannot be merged"
                   if m.get("markers") else "")
                + "]" for m in st["review"]]
    if st["failed"]:
        out.append("failed (latest):")
        out += [f"  {m['id']} {m['lane']}: {m['error']}" for m in st["failed"]]
    out.append("providers:")
    wn, wm = (max((len(p[k]) for p in st["providers"]), default=0) for k in ("name", "model"))
    rests = [rest_words(p) for p in st["providers"]]
    wr = max(map(len, rests), default=0)
    for p, rest in zip(st["providers"], rests):
        out.append(f"  {p['name']:<{wn}}  {p['kind']:<12}  {p['model']:<{wm}}  {rest:<{wr}}  {p['problem'] or 'ready'}")
    if [r for r in st.get("releases") or [] if r["state"] in release.OPEN]:
        out.append("releases (hansa release):")
        out += [f"  #{r['id']} {release.what(r)} at {(r['commit'] or '')[:10]}: {r['what']}" for r in st["releases"]
                if r["state"] in release.OPEN]
        if any(r["state"] == "waiting" for r in st["releases"]):
            out.append("  the principal answers on the page, or says the one-time code of their alert in the "
                       "conversation: pass it with hansa release yes|no ID CODE" if st.get("release_codes") else
                       "  the principal answers on the page only: no alert command is set, so no code is sent")
    if st["asks"]:
        out.append("open asks to the principal:")
        out += [f"  #{a['id']}: {a['text'][:100]}" + (f" · {a['note']}" if a.get("note") else "") for a in st["asks"]]
    t = st["last_24h"]
    out.append(f"last 24 h: {t['finished']} finished, {t['merged']} merged; workers used {t['worker_tokens']:,} tokens"
               f" (worth ${t['worker_usd']:.2f}; hansa cost for everything)")
    return "\n".join(out)


def rest_words(p: dict) -> str:
    """A provider's rest after a usage limit whose error gives no reset time, for hansa status."""
    if p.get("rest_minutes") is None:
        return ""
    return f"rest {p['rest_minutes']} min" + ("" if p.get("rest_own") else " (desk)")


def limit_wait(desk: Desk, m: dict) -> str:
    """For hansa show: the line of a queued task whose last run ended on its provider's usage limit while that
    provider still rests; '' otherwise."""
    last = (m.get("attempts") or [{}])[-1]
    if m.get("state") != "queued" or last.get("kind") != "limit":
        return ""
    st = desk.provider_state().get(last["provider"]) or {}
    if st.get("cool_until", 0) <= time.time():
        return ""
    how = rest_text(st)
    return (f"waits for the rest of {last['provider']} after its usage limit, until "
            f"{time.strftime('%m-%d %H:%M', time.localtime(st['cool_until']))}" + (f" ({how})" if how else "")
            + f"; it starts again then, or sooner on another provider of lane {m.get('lane') or 'any'} that is ready."
            + (" That return used a try." if last.get("counted") else " That return used no try.")
            + " More: hansa help provider-limits")


def markers_of(desk: Desk, m: dict) -> dict:
    """A finished task's conflict markers: as stored when it ended, or looked for now in a task that ended
    before hansa looked for them (only what is shown; hansa merge stores what it finds)."""
    if "markers" in m or m["state"] != "done" or not m.get("commits") or m.get("kind") == "verify":
        return m.get("markers") or {}
    ln = desk.settings()["lanes"].get(m.get("lane"))
    if not ln or not m.get("base") or not m.get("head"):
        return {}
    try:
        return markers_in_change(ln["repo"], m["base"], m["head"])
    except OSError:  # the repository is not there
        return {}


def branch_moved(desk: Desk, m: dict) -> str:
    """For hansa show: the line saying that a finished task's branch no longer holds the commit recorded as its
    work (run.branch_holds), or ''."""
    ln = desk.settings()["lanes"].get(m.get("lane"))
    repo = m.get("repo") or (ln or {}).get("repo")  # the repository the task ran in, where its branch is
    if m.get("state") not in ("done", "merging") or not m.get("head") or not repo or not Path(repo).is_dir():
        return ""
    return branch_holds(repo, m)


def background_note(m: dict) -> str:
    """The line for a worker that ended while background work it started still ran; empty otherwise."""
    if not m.get("background_left"):
        return ""
    return (f"the worker ended while its background work was still running ({'; '.join(m['background_left'])}); "
            "its report may be incomplete: read the changes before merging")


TEXT_LINES = 3  # the first lines of a task's text that hansa show gives, enough to know the task; --full: all
TEXT_WIDTH = 200  # and of each line, the first characters
REPORT_LINES = 400  # a report longer than this is cut in hansa show; --report and --full give it whole
REPORT_CHARS = 40000


def task_text_lines(desk: Desk, m: dict, full: bool = False) -> list:
    """The task's text as hansa read it when it was queued (tasks/ID/task.md), for hansa show: the first lines,
    or all of it."""
    f = desk.tasks_dir / m["id"] / "task.md"
    lines = f.read_text(encoding="utf-8").strip().splitlines() if f.exists() else []
    if not lines:
        return []
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["created"])) if m.get("created") else ""
    shown = lines if full else [x if len(x) <= TEXT_WIDTH else x[:TEXT_WIDTH] + " ..." for x in lines[:TEXT_LINES]]
    out = [f"text (as hansa read it when it was queued{', ' + when if when else ''}; the worker is given this):"]
    out += ["  " + x for x in shown]
    if not full and (len(lines) > TEXT_LINES or any(len(x) > TEXT_WIDTH for x in lines[:TEXT_LINES])):
        out.append(f"  ({len(lines) - len(shown)} more lines; the whole text: hansa show {m['id']} --full)")
    return out


def report_of(desk: Desk, tid: str) -> str:
    """The worker's report (tasks/ID/result.md), keys removed as it was kept; empty if there is none yet."""
    f = desk.tasks_dir / tid / "result.md"
    return f.read_text(encoding="utf-8").strip() if f.exists() else ""


def read_text(f: Path) -> str:
    """A file's text, '' when it is not there."""
    try:
        return f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def merge_log(m: dict) -> str:
    """The file in the task's folder with what its last merge check printed: its setup's, when that failed."""
    note = str((m.get("merge_check_result") or {}).get("note") or "")
    return "merge-setup.log" if note.startswith("the lane's setup failed") else "merge-check.log"


CHECK_LOG_TAIL = 20  # the last lines of each log hansa show ID --check gives


def _check_logs(desk: Desk, m: dict) -> list:
    """The task's last check and last merge check, those that ran: (what, the option that prints it whole, its
    first line, its log file)."""
    d = desk.tasks_dir / m["id"]
    out = []
    chk = m.get("check")
    if chk:
        how = ("could not run" if chk.get("could_not_run") else "passed" if chk.get("ok")
               else "FAILED" + (f" ({chk['note']})" if chk.get("note") else f" (exit {chk.get('exit')})"))
        out.append(("check", "--check-log", f"check (run by hansa, not the worker): {how}: {chk.get('command', '')}",
                    d / "check.log"))
    r = m.get("merge_check_result") or {}
    if r and not r.get("waited") and not r.get("conflict"):
        how = ("could not run" if r.get("could_not_run") else "passed" if r.get("ok")
               else "FAILED" + (f" ({r['note']})" if r.get("note") else f" (exit {r.get('exit')})"))
        out.append(("merge check", "--merge-check-log",
                    f"merge check (on the task merged onto the base at {str(r.get('onto', ''))[:10]}): {how}: "
                    f"{r.get('command', '')}" + (" (what the lane's setup printed)" if merge_log(m) == "merge-setup.log"
                                                 else ""), d / merge_log(m)))
    return out


def _no_check_text(m: dict, what: str = "check") -> str:
    return (f"{m['id']}: no {what} has run on it" + (": it is checked now; hansa wait, then this again"
                                                    if m.get("phase") == "checking" or m["state"] == "merging" else ""))


def check_output_text(desk: Desk, tid: str) -> str:
    """hansa show ID --check: for the task's last check and its last merge check, each apart: what ran and how it
    ended, the file with its whole output and that file's size, and its last CHECK_LOG_TAIL lines, keys removed.
    The first line says what follows; --check-log and --merge-check-log print one of them whole."""
    m = desk.meta(tid)
    logs = _check_logs(desk, m)
    if not logs:
        return _no_check_text(m)
    out = [f"{tid}: its {' and its '.join(w for w, *_ in logs)}, each apart: how it ended, the file with its whole "
           f"output, and its last {CHECK_LOG_TAIL} lines (keys removed). One of them whole: "
           + " or ".join(f"hansa show {tid} {opt}" for _, opt, *_ in logs)]
    for what, opt, head, f in logs:
        lines = desk.redact(read_text(f)).rstrip().splitlines()
        out += ["", f"== {head}"]
        if not f.exists():
            out.append("   its output is not kept: hansa show has its last lines")
            continue
        size = f.stat().st_size
        out.append(f"   whole output: {f} ({len(lines)} line{'s' if len(lines) != 1 else ''}, {size:,} bytes)")
        if not lines:
            out.append("   (it printed nothing)")
            continue
        last = lines[-CHECK_LOG_TAIL:]
        out.append(f"   its last {len(last)} lines:" if len(last) < len(lines) else "   all of it:")
        out += last
    return "\n".join(out)


def check_log_text(desk: Desk, tid: str, merge: bool = False) -> str:
    """hansa show ID --check-log / --merge-check-log: the whole output of the task's last check, or of its last
    merge check, keys removed, under a first line that says which it is and how long."""
    m = desk.meta(tid)
    what = "merge check" if merge else "check"
    logs = [x for x in _check_logs(desk, m) if x[0] == what]
    if not logs:
        return _no_check_text(m, what)
    _, opt, head, f = logs[0]
    if not f.exists():
        return f"{head}\n(its output is not kept: hansa show has its last lines)"
    log = desk.redact(read_text(f)).rstrip()
    n = len(log.splitlines())
    return (f"the whole output of {tid}'s {head} ({n} line{'s' if n != 1 else ''}, keys removed; from {f})\n"
            + (log if log else "(it printed nothing)"))


def report_text(desk: Desk, tid: str) -> str:
    """hansa show ID --report: the report alone, whole; for a verification, its verdict first."""
    m = desk.meta(tid)
    report = report_of(desk, tid)
    out = []
    if m.get("kind") == "verify" and (m.get("state") == "done" or m.get("verdict")):
        out.append(f"verdict: {m.get('verdict') or 'none given'}")
    out.append(report or f"(no report: {tid} is {m['state']})")
    return "\n\n".join(out)


EFFORT_FROM = {"task": "the task's --effort", "provider": "the provider's default", "design": "the desk's design "
               "setting", "coordinator": "the coordinator's setting"}


def effort_text(effort: str, came_from, provider: dict | None) -> str:
    """The effort a run was started with and where it came from (run.start records both: effort_used and
    effort_from), and, on a provider whose endpoint is not Claude's own, that it may ignore it. '' for a run
    started before hansa kept where it came from and with no effort."""
    if came_from is None and not effort:
        return ""
    said = (f"effort {effort}" + (f" from {EFFORT_FROM.get(came_from, came_from)}" if came_from else "") if effort
            else "claude's default effort")
    if (provider or {}).get("kind") == "compatible":
        said += "; its endpoint is not Claude's own and may ignore effort"
    return said


def run_text(m: dict, provider: dict | None) -> str:
    """A run's provider, model and effort: `name (model, effort high from the task's --effort)`."""
    e = effort_text(m.get("effort_used", m.get("effort", "")), m.get("effort_from"), provider)  # a task's, an attempt's
    return f"{m['provider']} ({m.get('model_asked', m.get('model', ''))}" + (f", {e}" if e else "") + ")"


def show_text(desk: Desk, tid: str, full: bool = False, heard=None) -> str:
    """hansa show. full: the whole text and report. heard: whether the session it is for hears from hansa
    (default: the session this command runs in)."""
    m = as_closed_now(desk.meta(tid))
    m["markers"] = markers_of(desk, m)
    d = desk.tasks_dir / tid
    head = f"{tid} · {m['state']} · lane {m.get('lane') or 'any'}"
    if m.get("provider"):
        head += (f" · {run_text(m, desk.settings()['providers'].get(m['provider']))}"
                 + (f" · {m['turns']} turns" if m.get("turns") else ""))
    if m.get("started"):
        head += f" · {max(0, int(((m.get('finished') or time.time()) - m['started']) // 60))} min"
    if m.get("browser"):
        head += " · with a browser"
    verifying = m.get("kind") == "verify"
    meaning = (f"a verification of {m.get('parent')}: a worker ran its change to see whether it holds; never merged"
               if verifying else STATES.get(m["state"], ""))
    if verifying:
        own = (read_json(desk.tasks_dir / str(m.get("parent")) / "meta.json", {}) or {}).get("lane") or ""
        on = "runs" if m["state"] in ("queued", "running") else "ran"
        meaning += (f"; it {on} on lane {m.get('lane') or 'any'}"
                    + (f", not {m['parent']}'s own lane {own}" if own and own != m.get("lane") else ""))
    if m.get("kind") == "design":
        meaning = ("a design session: the principal shapes this feature with it directly, on the real code; "
                   + ("it is open now" if m["state"] == "running" and m.get("phase") != "checking"
                      else "it comes back for review when they end it" if m["state"] == "queued" else meaning))
    out = [head, f"title: {m['title']}", *task_text_lines(desk, m, full), f"meaning: {meaning}"]
    out += [x for x in (rounds_line(desk, m), "" if verifying else rethink_text(desk, m)) if x]
    if m.get("error"):
        out.append(f"error: {m['error']}")
    out += [f"message: {m[k]}" for k in ("commit_message_note", "merge_message_note") if m.get(k)]
    out += [disk.trimmed_text(m)] if disk.trimmed_text(m) else []
    if m.get("closed"):  # a verification that had ended when its task closed: kept as it ended
        out.append(m["closed"])
    if m.get("machine_problem") and m["state"] == "failed":
        out.append("  this is the machine's problem, not the task's: srt, which runs the lane's setup, could not run "
                   "on the server, so the worker never started (hansa explain server)")
    out += [f"write list: {x}" for x in m.get("write_left_out") or []]
    out += [f"env: {x}" for x in m.get("env_left_out") or []]
    out += [f"what workers may not read: {x}" for x in m.get("no_read_left_out") or []]

    def started_from(start):
        return (f"{start['source']} {start['commit'][:12]}"
                + (f" ({start['why']})" if start.get("why") else ""))

    for a in m.get("attempts", []):
        on = a["provider"]  # with its model and effort when they were not those of the run above
        if a.get("model") and (a["provider"], a["model"], a.get("effort", ""), a.get("effort_from")) != (
                m.get("provider"), m.get("model_asked"), m.get("effort_used", ""), m.get("effort_from")):
            on = run_text(a, desk.settings()["providers"].get(a["provider"]))
        out.append(f"earlier try on {on}: {a['error']}" if a.get("kind") != "limit" else
                   f"earlier run on {on}: {a['error']} (its usage limit; "
                   + ("counted as a try)" if a.get("counted") else "no try used)"))
        start = a.get("run_start")
        if (not start and a.get("kind") in ("network", "limit") and m.get("base")
                and m.get("kind") not in ("verify", "design")):
            start = {"commit": m.get("start_from") or m["base"],
                     "source": "earlier task's commit" if m.get("start_from") else "base"}
        if start:
            out.append(f"  started from: {started_from(start)}")
    if m["state"] == "queued" and m.get("restart"):
        out.append(f"next run starts from: {started_from(m['restart'])}")
    elif m["state"] != "queued" and m.get("run_start"):
        out.append(f"run started from: {started_from(m['run_start'])}")
    if limit_wait(desk, m):
        out.append(limit_wait(desk, m))
    report = report_of(desk, tid)
    if report:
        lines = report.splitlines()
        shown, size, cut = [], 0, 0
        for x in lines if full else lines[:REPORT_LINES]:
            if not full and size + len(x) + 1 > REPORT_CHARS:
                if not shown or len(x) > REPORT_CHARS:  # a line longer than the limit is cut there, not given whole
                    keep = max(REPORT_CHARS - size, 0)
                    shown.append(x[:keep])
                    cut = len(x) - keep
                break
            size += len(x) + 1
            shown.append(x)
        out += ["report:"] + ["  " + x for x in shown]
        more = len(lines) - len(shown)
        if cut or more:
            out.append("  ... " + (f"{cut} more characters of this line" if cut else "")
                       + (" and " if cut and more else "") + (f"{more} more line{'s' if more > 1 else ''}" if more else "")
                       + f"; the whole report: hansa show {tid} --report")
    if background_note(m):
        out.append(background_note(m))
    if m.get("replaced_by"):
        out.append(f"replaced by: {m['replaced_by']}")
    if m.get("files") is not None and m.get("state") != "queued":
        out.append(f"changes: {m.get('stat') or 'none'}; {m.get('commits', 0)} commit(s) on {m.get('branch')}"
                   + (f" up to {m['head'][:12]}" if m.get("head") else "")
                   + ("; nothing to merge, so the lane's check was not run" if m.get("state") == "done"
                      and not m.get("commits") else ""))
        out += ["  " + f for f in m.get("files", [])[:30]]
        held = branch_moved(desk, m)
        if held:
            out.append(f"BRANCH CHANGED: {held}. hansa verifies, merges and continues only from that commit, and "
                       f"refuses to while the branch does not hold it")
    if m.get("leaked"):
        out.append(f"KEY IN THE CHANGE: it holds the key of {', '.join(m['leaked'])}, so hansa will not merge it")
    if m.get("markers"):
        out.append(f"CONFLICT MARKERS IN THE CHANGE: git's <<<<<<< / ======= / >>>>>>> lines, left from a conflict, "
                   f"in {markers_text(m['markers'])}; hansa will not merge it until they are settled")
    if m.get("setup_exit") is not None and (d / "setup.log").exists():
        tail = (d / "setup.log").read_text(encoding="utf-8", errors="replace").strip().splitlines()[-8:]
        out += [f"setup (the lane's command that installs dependencies) failed with exit {m['setup_exit']}; "
                "its last lines:"] + ["  " + plain(x) for x in tail]
    if m.get("denials"):
        out.append(f"calls the worker had no permission for ({len(m['denials'])}):")
        reasons = m.get("denial_reasons") or []
        for i, x in enumerate(m["denials"] if full else m["denials"][:8]):
            first, *more = x.splitlines() or [""]
            why = reasons[i] if i < len(reasons) else ""
            if full:  # whole, so that which part was refused can be seen
                out.append("  " + "\n    ".join([first, *more]) + (f"\n    Claude Code said: {plain(why, None)}" if why else ""))
                continue
            out.append(f"  {first[:160]}" + (f"  (+{len(more)} more lines)" if more else "")
                       + (f"; Claude Code said: {plain(why)[:200]}" if why else ""))
        if not full and (len(m["denials"]) > 8 or any(len(x) > 160 or "\n" in x for x in m["denials"][:8])):
            out.append(f"  (whole: hansa show {tid} --full)")
    tmps = m.get("worker_tmps") or []
    for i, r in enumerate(tmps):
        how = ("removed when the run ended" if r.get("removed") else "still there: removing it failed"
               if r.get("removed") is False else "in use by the run" if m["state"] == "running" and i == len(tmps) - 1
               else "not removed yet")
        out.append(f"temporary folder{f' of run {i + 1} of {len(tmps)}' if len(tmps) > 1 else ''}: "
                   f"{r.get('path')} ({how})")
    if m.get("left_running"):
        out.append(f"left running, stopped by hansa when the task ended ({len(m['left_running'])}):")
        out += [f"  pid {x['pid']}: {x['command']}" for x in m["left_running"]]
    shots = sorted((d / "browser").glob("*")) if (d / "browser").is_dir() else []
    if shots:
        out.append(f"browser files ({len(shots)}) in {d / 'browser'}: " + ", ".join(x.name for x in shots[:6])
                   + (" ..." if len(shots) > 6 else ""))
    pv = m.get("preview") or {}
    if pv:
        log = d / "preview.log"
        where = f"; its output, keys removed: {log}" if log.exists() else ""
        if not pv.get("started"):
            out.append(f"preview (started by hansa for the browser): not started: {pv.get('why', '')}")
        elif pv.get("ready"):
            out.append(f"preview (started by hansa for the browser): ready at {pv.get('address')} after "
                       f"{pv.get('waited', 0)} s, stopped when the worker ended{where}")
        elif pv.get("stopped"):
            out.append(f"preview (started by hansa for the browser): did not start: {pv.get('why') or 'no answer'}"
                       f"{where}")
        else:
            out.append(f"preview (started by hansa for the browser): running now: {pv.get('command', '')}")
        if (d / PREVIEW_FIRST_LOG).exists():
            out.append(f"preview of the run before hansa resumed it, stopped then; its output: {d / PREVIEW_FIRST_LOG}")
    chk = m.get("check")
    if chk and chk.get("could_not_run"):
        out.append(f"check (run by hansa, not the worker): {chk['note']}: {chk['command']}")
        out.append("  " + machine_hint(chk))
    elif chk:
        how = "passed" if chk["ok"] else "FAILED" + (f" ({chk['note']})" if chk["note"] else f" (exit {chk['exit']})")
        out.append(f"check (run by hansa, not the worker): {how} in {chk['seconds']} s: {chk['command']}")
        if not chk["ok"]:
            out += failed_check_lines(desk.redact(read_text(d / "check.log")), chk["tail"], tid)
            if machine_hint(chk):
                out.append("  " + machine_hint(chk))
    elif m.get("phase") == "checking":
        out.append(f"check: running now: {m.get('check_command', '')}")
    elif m["state"] == "done" and m.get("commits") and not verifying:
        s = desk.settings()
        ln = s["lanes"].get(m.get("lane"))
        check_name = merge_lane(s, m.get("lane"))
        if ln is not None and not ln.get("check") and check_name != m.get("lane"):
            out.append(f"check: none after each task. Merge checks use lane {check_name}: "
                       f"{merge_command(s['lanes'][check_name]) or 'no check to run'}")
        elif ln is not None and not ln.get("check") and ln.get("merge_check"):
            out.append(f"check: none after each task. Lane {m['lane']} has only a merge check, which hansa merge runs "
                       f"on this task merged with the base before it merges it: {ln['merge_check']}")
        elif ln is not None and not ln.get("check"):
            out.append(f"check: none. Lane {m['lane']} has no check, so this task will be merged untested; set one "
                       f"with: hansa lane set {m['lane']} check \"<the command that runs the tests>\"")
    if m["state"] == "merging":
        out.append(merge_check_text(desk, m))
    elif m["state"] in ("done", "merged"):
        out += merge_result_lines(m, desk.redact(read_text(d / merge_log(m))))
    if m["state"] == "merged" and m.get("unverified"):
        out.append(f"note: {m['unverified']}")
    if m["state"] == "running" and m.get("phase") == "starting":
        out.append("so far: hansa is preparing it; the worker starts in a moment")
    elif m["state"] == "running" and m.get("phase") != "checking":
        out.append(f"so far: {_progress_line(progress_of(desk, m))} (turns and tokens are recorded when it ends)")
        if m.get("kind") == "design":
            out.append(f"the principal joins it with: {join_command(m)}")
    v = verification(desk, m)
    if v:
        out.append(f"verification (a worker ran the change): {_verification_line(v)}"
                   + (f"; on lane {v['lane']}" if v["lane"] and v["lane"] != m.get("lane") else "")
                   + ("" if v["state"] in ("queued", "running") else f"; hansa show {v['task']} says what it ran"))
    if verifying and (m.get("state") == "done" or m.get("verdict")):  # kept after it closes with its task
        out.append(f"verdict: {m.get('verdict') or 'none given'}")
        out += [x for x in (rethink_text(desk, m),) if x]
    runs, usage = cost.task_runs(m), {}  # every try's, each once, as hansa cost counts them
    for r in runs:
        usage = add_usage(usage, r["usage"])
    for model, u in usage.items():
        out.append(f"usage {model}" + (f" ({len(runs)} runs, every try)" if len(runs) > 1 else "")
                   + f": in {u.get('in') or 0:,} · out {u.get('out') or 0:,} · cache read {u.get('cache_read') or 0:,}"
                   f" · cache write {u.get('cache_write') or 0:,}")
    review = (f"hansa merge {tid}" if v and v["verdict"] == "holds"
              else f"hansa diff {tid} --stat, then hansa verify {tid} (code or tests) or hansa merge {tid} (text only)")
    nxt = {"done": f"hansa retry {tid} \"what to change\" · hansa close {tid} to close it" if not m.get("commits")
           else f"{review} · hansa retry {tid} \"what to change\"" if not chk or chk["ok"]
           else (f"{recheck_step(chk, tid)} (runs the check "
                 f"again and merges if it passes) · hansa retry {tid} \"make the check pass: ...\" · "
                 f"hansa merge {tid} --despite-check") if machine_hint(chk)
           else f"hansa retry {tid} \"make the check pass: ...\" · hansa diff {tid} · hansa merge {tid} --despite-check",
           "failed": (f"read its notes, then split the task or make it clearer (hansa add ...); do not re-run it unchanged"
                      if str(m.get("error", "")).startswith("stopped at the ceiling")
                      else f"once the machine is fixed (hansa explain server): hansa retry {tid} to run it again"
                      if m.get("machine_problem")
                      else f"hansa retry {tid} \"what to change\" (or --fresh to start over)"),
           "queued": "hansa status shows what it is waiting for", "running": f"hansa cancel {tid} to stop it"}
    if v and v["verdict"] == "does not hold" and m["state"] == "done":
        nxt["done"] = f"hansa retry {tid} \"fix what {v['task']} found: ...\" · hansa merge {tid} --despite-check"
    if v and v["state"] in ("queued", "running") and m["state"] == "done":
        nxt["done"] = (f"wait for the verification: {notify.wait_step(desk, v['task'], heard)} "
                       f"(hansa merge {tid} is held until it ends)")
    mr = m.get("merge_check_result") or {}
    if mr.get("waited") and m["state"] == "done":
        nxt["done"] = (f"hansa retry {tid} --update \"<how to settle the conflict>\" · hansa retry {tid} --fresh "
                       f"\"<the same change on the current base>\"" if mr.get("conflict") else
                       f"once what stopped it is fixed (the principal fixes a checkout; tell them with hansa ask): "
                       f"hansa merge {tid} again")
    elif mr and not mr.get("ok") and m["state"] == "done" and machine_hint(mr):
        nxt["done"] = (f"{recheck_step(mr, tid)} (runs the check "
                       f"again and merges if it passes) · hansa retry {tid} \"...\" · hansa merge {tid} --despite-check")
    elif mr and not mr.get("ok") and m["state"] == "done":
        nxt["done"] = (f"hansa retry {tid} --update \"make it work with what the base has now: ...\" · "
                       f"hansa merge {tid} again (it checks the merge again) · hansa merge {tid} --despite-check")
    nxt["merging"] = (f"{notify.wait_step(desk, tid, heard)}: it merges by itself if the check passes, and comes "
                      f"back for review if not · hansa cancel {tid} stops it")
    if (m.get("merge_check") or {}).get("plain"):
        nxt["merging"] = (f"{notify.wait_step(desk, tid, heard)}: it merges by itself in its turn, and comes back for "
                          f"review if it cannot · hansa cancel {tid} stops it")
    if m.get("markers") and m["state"] == "done":
        nxt["done"] = f"hansa retry {tid} --update \"settle the markers in {', '.join(m['markers'])}\""
    if m.get("leaked") and m["state"] == "done":
        nxt["done"] = (f"hansa retry {tid} --fresh \"...\" (the same work, without the key) · tell the principal with "
                       f"hansa ask (they may want to change that key) · hansa close {tid}")
    if verifying:
        nxt = {"done": f"read the report above, then merge or send back {m.get('parent')} · hansa close {tid} to close it",
               "failed": f"hansa verify {m.get('parent')} to try again", "running": f"hansa cancel {tid} to stop it",
               "queued": "hansa status shows what it is waiting for"}
        if m.get("closed"):  # its task is closed: nothing is left to do with it
            nxt = {}
    if m["state"] in nxt:
        out.append("next: " + nxt[m["state"]])
    return "\n".join(out)


def merge_lane_text(s: dict, name: str) -> str:
    if s["lanes"][name].get("merge_lane"):
        served = [n for n in sorted(s["lanes"]) if n != name and merge_target(s, n) == merge_target(s, name)]
        return f"  merge lane for lanes {', '.join(served) or '(none yet)'}; takes no work"
    selected = merge_lane(s, name)
    return f"  merge checks on lane {selected}" if selected != name else ""


def lane_origin(desk: Desk, name: str) -> str:
    """Which command made the lane, and which last changed it, as the event log has them."""
    made, last = "", ""
    try:
        lines = desk.events_path.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
    except OSError:
        lines = []
    for line in reversed(lines):
        if f'"{name}"' not in line:  # most lines are about something else: not parsed
            continue
        e = event_record(line)
        if e is None:
            continue
        if e.get("lane") != name:
            continue
        if e.get("event") == "lane_set" and not last:
            last = f"hansa lane set {name} {e.get('field', '')}".strip()
        elif e.get("event") in ("lane_added", "desk_created"):
            made = "hansa lane add" if e["event"] == "lane_added" else "hansa init"
            break
    if not made and not last:  # a desk from before hansa recorded either
        return "set by the coordinator with hansa lane add or hansa lane set"
    return "; ".join(x for x in (made and f"made with {made}", last and f"last changed with {last}") if x)


def local_servers_text(mac: bool, listed: bool) -> str:
    """What localhost on a lane's network list means on this machine, for hansa explain lane."""
    if mac:
        return ("local servers a worker starts (on macOS): they need localhost on the list; with it a worker, the"
                " setup and the check can bind ports and reach every service on this machine's loopback, local"
                " proxies among them, which reach the internet whatever the list says"
                + ("; this lane lists it, so keep it only if this Mac's loopback holds nothing a worker must not"
                   " reach" if listed else "; this lane does not list it, so its workers cannot bind a port"))
    return ("local servers a worker starts (on Linux): they need nothing on the list, since each sandbox has a"
            " loopback of its own; localhost on it adds nothing for them, and lets a program told to use the"
            " sandbox's proxy for localhost reach this machine's loopback through it"
            + ("; this lane lists it, and need not" if listed else ""))


JAVA_LINES = (
    "  a Java lane (its env names JAVA_HOME): hansa adds -Djava.io.tmpdir to JAVA_TOOL_OPTIONS in the sandbox, so"
    " every JVM prints `Picked up JAVA_TOOL_OPTIONS: ...` on stderr before anything else. java -version and java"
    " -XshowSettings print on stderr, so their output starts with it; javac -version and mvn -v print on stdout,"
    " so theirs does when stderr is read with it (2>&1). A script that reads the version should look for the"
    " line holding `version \"`, not the first line",
    "  Maven gets the sandbox's proxy through MAVEN_ARGS (-gs <file>): a build script that sets MAVEN_ARGS itself"
    " drops it (in bash MAVEN_ARGS=(-B) makes an array, which is not exported), and Maven then gets 407 Proxy"
    " Authentication Required for what its local repository lacks; keep it: MAVEN_ARGS=(-B ${MAVEN_ARGS:-})")
MKTEMP_LINE = ("  on macOS, mktemp -d, mktemp -t PREFIX and a bare mktemp ignore TMPDIR and write under /var/folders,"
               " which a lane may not write (mktemp: mkdtemp failed on ...: Operation not permitted); a script should"
               " give it a template in TMPDIR: mktemp -d \"${TMPDIR:-/tmp}/name.XXXXXX\" or mktemp -d -p \"$TMPDIR\"")


def tool_lines(mac: bool, env: dict) -> list:
    """For hansa explain lane: what JVMs and macOS's mktemp do differently in a lane, where it applies."""
    return [*(JAVA_LINES if "JAVA_HOME" in env else ()), *([MKTEMP_LINE] if mac else [])]


def preview_text(mac: bool, ln: dict) -> list:
    """What becomes of a lane's preview on this machine, for hansa explain lane: its lines."""
    if not ln.get("preview"):
        return ["  preview: none (a design session or a task with a browser has no app started for it)"]
    head = (f"  preview: {ln['preview']} (a design session starts it itself with its Bash tool; for a task or"
            " verification with a browser, hansa starts it")
    if mac:
        return [head + " before the worker, in the worktree after the lane's setup, in srt with the lane's sandbox"
                " plus local binding for this one process, waits up to"
                f" {ln.get('preview_wait_s', 120)} s (preview_wait_s) for the first http://localhost:PORT address it"
                " prints or a port it names that answers, tells the worker the address, and stops it with its whole"
                " process group, and each process seen below it (looked for every half second), when the worker"
                " ends; its output is kept in the task's folder, keys removed, its first and last 2 MB at most)",
                "    on macOS, while it runs this preview can reach every service on this machine's loopback, as a"
                " lane with localhost on its network list can: the price of looking at the app in a browser on a Mac"]
    return [head + " on macOS only)",
            "    on Linux hansa does not start it: srt runs it with a network of its own (a namespace with its own"
            " loopback), so the browser, outside the sandbox, could not reach a server bound there; the worker is"
            " told so"]


def web_lines(allow: list) -> list:
    """What a lane's allowed commands give of Claude Code's web tools, which reach the network from its own
    process, outside the sandbox's network list."""
    sites, search = web_sites(allow)
    if not sites and not search:
        return ["  web tools (Claude Code's own, outside the sandbox's network list): none"]
    return ["  web tools (Claude Code's own, outside the sandbox's network list; workers, verifiers and design"
            " sessions alike):",
            *(["    WebFetch reaches only: " + ", ".join(sites) + " (unlike the network list, it runs in Claude"
               " Code's own process, outside the sandbox; a *. wildcard opens every name under it; any listed name can be pointed at any address, this"
               " machine included: list only sites you know, and the principal sees every change)"] if sites else []),
            *(["    WebSearch: sends search queries out of the machine"] if search else [])]


def explain_text(desk: Desk, what: str = "", name: str = "", whole: bool = False) -> str:
    """hansa explain. whole (--all): for a lane, the built-in list of what workers may not read, name by name."""
    s = desk.settings()
    if what == "lane":
        ln = s["lanes"].get(name)
        if not ln:
            return f"no lane {name}; lanes: {', '.join(sorted(s['lanes'])) or 'none'}"
        wt = desk.worktrees_dir / name
        brief = desk.lanes_dir / name / "brief.md"
        may_write, left_out = write_list(desk, ln)  # as a run is given it: a fence may have come since it was saved
        env, env_left = lane_env(desk, ln)
        who, email, where = lane_identity(ln)
        check_name = merge_lane(s, name)
        check_lane = s["lanes"][check_name]
        separate = check_name != name
        served = ([n for n in sorted(s["lanes"]) if n != name and merge_target(s, n) == merge_target(s, name)]
                  if ln.get("merge_lane") else [])
        return "\n".join([
            f"lane {name} ({lane_origin(desk, name)}; not by the principal on the web page)",
            f"  repo: {ln['repo']} · base branch: {ln.get('base', 'main')} · worktree: {wt}",
            *([merge_lane_text(s, name)] if merge_lane_text(s, name) else []),
            f"  read them with hansa git {name} log|show|diff|grep ... (--worktree: the worktree), git commands that"
            " read only, run as the desk's account",
            f"  author and committer of every commit hansa makes for the lane (its tasks' commits, their merges into"
            f" the base, merges of the base into a task's branch, design sessions' commits,"
            f" the commit hansa lane remove --keep makes): {who} <{email}>, from"
            f" {where}" + ("" if ln.get("identity") else f"; a lane whose release goes to a public repository should"
                           f" have one: hansa lane set {name} identity \"Name <email>\", or user.name and user.email"
                           f" in the repository's own config (an account's global identity is never taken)"),
            f"  providers, in order: {' > '.join(ln['providers'])} (the next one takes over when one hits its limit)",
            *([] if ln.get("merge_lane") else  # it takes no work; its merge check's limit is below
              [f"  per task: stopped at {ln.get('max_turns', 300)} turns or {ln.get('timeout_min', 240)} minutes"
               " (check included); set high, so reaching it means something went wrong"]),
            "  work: none; a merge lane takes no work" if ln.get("merge_lane") else
            "  check after each task: " + (f"{ln['check']} (hansa runs it in the worktree after the worker ends,"
                                            " in the lane's sandbox;"
                                            " hansa merge refuses a failed check"
                                            + ("" if separate or own_merge_check(ln) else
                                               "; when the base has moved since a task"
                                               " was made, hansa merge runs it on the two merged together first;"
                                               " hansa merge ID --recheck runs it again that way in any case, for a"
                                               " check that failed or could not run for the machine's sake") + ")"
                                            if ln.get("check") else "none" if separate or ln.get("merge_check")
                                            else "none, so its tasks are merged untested"),
            *([f"  merge checks for lanes {', '.join(served) or '(none yet)'}: {merge_command(ln) or 'none'};"
               f" in this worktree and sandbox, after its setup, stopped at {merge_minutes(ln)} minutes; needed"
               " when the base moved or its command differs from the task lane's check (or with --recheck)"]
              if ln.get("merge_lane") else
              [f"  merge check before merging: {merge_command(check_lane) or 'none'}; in lane {check_name}'s"
               f" worktree and sandbox, after its setup, stopped at {merge_minutes(check_lane)} minutes; needed"
               " when the base moved or its command differs from this lane's check (or with --recheck)"] if separate else
              [f"  merge check before merging: {ln['merge_check']} (hansa merge runs it in place of the check, on"
               " the task merged with the base as it is then, in the lane's worktree and sandbox after the lane's"
               " setup, before it merges any task that changed something, whether or not the base has moved; it"
               " merges exactly that if it passes; hansa merge ID --recheck runs it again in any case)"
               f" · stopped at {merge_minutes(ln)} minutes"] if own_merge_check(ln) else
              [f"  merge check before merging: the check, stopped at {merge_minutes(ln)} minutes"]
              if ln.get("check") and ln.get("merge_timeout_min") else []),
            "  setup before each merge check: " + (ln.get("setup") or "none") if ln.get("merge_lane") else
            "  setup before each task: " + (f"{ln['setup']} (hansa runs it in the worktree before the worker starts,"
                                             " in the lane's sandbox, so one that downloads needs its registry on"
                                             " the network list;"
                                             " what it changes in the repository is set aside; if it fails, the"
                                             " task fails and hansa show has its last lines)" if ln.get("setup")
                                             else "none"),
            *(["  the setup and check run in srt with the lane's sandbox: the network and write lists below"
               " hold for them as for workers",
               f"  to see that they work where tasks run, with no worker: hansa lane try {name} (after changing the"
               " setup, check, network or write list, or moving the desk to another machine)"
               + (f"; its merge check: hansa lane try {name} --merge-check" if own_merge_check(ln) else "")]
              if ln.get("setup") or ln.get("check") or ln.get("merge_check") else []),
            *preview_text(sys.platform == "darwin", ln),
            *([f"  {x[0].upper()}{x[1:]}." for x in LINUX_SANDBOX_NOTES] if account.LINUX else []),
            "  environment set for workers: " + (", ".join(f"{k}={v}" for k, v in env.items()) or "none"),
            *["    a run leaves out " + x.removeprefix("left out of this run: ") for x in env_left],
            *tool_lines(sys.platform == "darwin", {**ln.get("env", {}), **env}),
            f"  locale of the lane's commands and workers: {lane_locale(desk, ln)}"
            + ("" if env.get("LC_ALL") else f"; a lane whose results must not depend on who started hansa sets"
               f" its own: hansa lane set {name} env ... LC_ALL=C.UTF-8"),
            "  what a worker may use (it runs in Claude Code's sandbox, which the operating system enforces;"
            " what it refuses shows up in hansa show):",
            "    any command inside the sandbox, but it changes files only in the lane's worktree, its own"
            " temporary folder (made for each run and removed when it ends) and the folders listed below;"
            " it cannot read the desk's keys and state or what is listed below; it reaches no network but the hosts below; git history is left to hansa (no commit, merge,"
            " reset, branch and the like), which commits its changes",
            *no_read_lines(desk, s, name, whole),
            "  network during a task: " + (", ".join(ln.get("network", [])) or "none"),
            "    " + local_servers_text(sys.platform == "darwin", any(is_local(h) for h in ln.get("network", []))),
            *web_lines(ln.get("allow", [])),
            "  folders a worker may also write (a build or test tool's cache): "
            + (", ".join(may_write) or "none"),
            *["    a run leaves out " + x.removeprefix("left out of this run: ") for x in left_out],
            "  allowed commands (given to the lane's workers, verifiers and design sessions; workers run any command"
            " in their sandbox and need no Bash rule, design sessions run outside it and may run these plus their"
            " own git add, commit and merge):" + ("" if ln.get("allow") else " none"),
            *["    " + (r if isinstance(r, str) else json.dumps(r))
              + (f" (ignored: it {refused_rule(r)})" if refused_rule(r) else "") for r in ln.get("allow", [])],
            *(["    runs leave the ignored ones out; the coordinator removes them with: "
               + allow_fix(name, ln.get("allow", []))] if any(map(refused_rule, ln.get("allow", []))) else []),
            *release.lane_lines(desk, name, ln),
            f"  {len(set(release.lane_exceptions(s, name)) & set(s.get('release_forbidden') or []))} "
            "forbidden entry/entries excepted on this lane (the principal's, on the page)",
            *[f"  {f} (the form of the {what}): {json.dumps(ln[f], ensure_ascii=False)}"
              for f, what in (("commit_message", "message of the commit of a task's work, and of the merge of the base"
                                                 " into a task's branch"),
                              ("merge_message", "message of a task's merge into the base")) if ln.get(f)],
            f"  standing instructions: {brief} ({'set' if brief.is_file() and brief.read_text().strip() else 'empty'};"
            f" read and change them with hansa lane brief {name})"])
    if what == "provider":
        p = s["providers"].get(name)
        if not p:
            return f"no provider {name}; providers: {', '.join(sorted(s['providers']))}"
        st = desk.provider_state().get(name, {})
        where = p.get("config_dir") or "the default Claude Code login" if p["kind"] == "subscription" \
            else (p.get("base_url") or "no endpoint yet")
        return "\n".join([
            f"provider {name} (set by the principal on the web page)",
            f"  kind: {p['kind']} · model: {p['model']} · effort: {p.get('effort') or 'default'}",
            f"  rest after a usage limit: until the reset time the error gives, and a minute; else "
            f"{rest_minutes(s, name)} minutes"
            + (" (its own rest_minutes)" if own_rest_minutes(s, name) is not None else " (the desk's cool_minutes)"),
            f"  {'login' if p['kind'] == 'subscription' else 'endpoint'}: {where}"
            + ("" if p["kind"] == "subscription" else f" · key: {'present' if desk.has_key(name) else 'missing'}"),
            f"  now: {provider_problem(desk, s, name) or 'ready'}",
            f"  last ping: {st.get('check') or 'never'}"])
    if what == "task":
        return show_text(desk, name)
    if what == "states":
        return "\n".join(f"{k:<10} {v}" for k, v in STATES.items())
    if what == "server":
        rec, server = srt_record(desk), server_state(desk)
        seen = ("hansa serve has not tried srt yet" if not rec else
                "hansa serve tried srt" + (f" at {srt_when(rec)}" if srt_when(rec) else "") + ": "
                + ("it ran" + (f" ({rec.get('path')})" if rec.get("path") else "") if rec.get("ran") else
                   f"found at {rec.get('path')}, but it could not run: {rec.get('said')}" if rec.get("found")
                   else "not found on its PATH"))
        return "\n".join([
            "`hansa serve` starts queued tasks, finishes ended ones and serves the web page. It runs on the "
            "desk's machine, usually in a terminal multiplexer. Without it, tasks stay queued. One runs per "
            "desk: a second one refuses to start.",
            f"  now: {server_text(server)}",
            f"  {page_text(page_of(desk, server))}",
            "What it starts outside a worker's sandbox (a release's command, the alert command, design sessions, the "
            "preview, git) runs with its environment, less what carries a Claude Code session's identity or "
            f"messaging ({', '.join(p + '*' for p in SESSION_PREFIXES)}, {', '.join(SESSION_NAMES)}): with those a "
            "process could post into other sessions as that session. Every hansa command takes them out of its own "
            "environment as it starts, so started from a Claude Code session (its Bash tool, a tmux it opened, the "
            "! prefix) hansa serve runs without them, and says so as it starts, in hansa status and here. Start it "
            "from a plain shell all the same.",
            *([f"  now: {sess}"] if (sess := serve_session(desk)) else []),
            f"It counts as running while a hansa serve holds serve.lock, which names its process, and as not "
            f"running once none holds it, whatever process that number names by then. Once a round (starting and finishing tasks) it writes its heartbeat; a round can "
            f"take a while, when a worker's processes are stopped (up to 20 s each) or what a task left is swept "
            f"on a busy machine. Quiet for {ROUND_QUIET} s, it counts as busy, still running; quiet for "
            f"{STUCK_AFTER // 60} minutes, hansa status and the posts to the coordinator's session say it is "
            f"busy or stuck, with its pid. Stuck for good (a task it waits on that never ends, a terminal stopped "
            f"with Ctrl-Z): see what it is doing, e.g. with `ps -p PID` and its terminal, before stopping it; "
            f"stop it with Ctrl-C and start it again, and tasks running meanwhile are taken over.",
            "It runs each lane's setup and check in srt (Anthropic's sandbox runtime), with its own PATH, which can "
            "differ from the coordinator's. It tries srt (a run of `true` in a sandbox) when it starts and before "
            "each setup or check, and records what it saw; hansa status reports that record.",
            f"  now: {seen}",
            "When srt cannot run, the setup or check does not run either, and the task is not judged on it: its "
            "check reads \"could not run\". That is the machine's problem, not the task's. Before each setup or "
            "check, hansa looks for srt on the PATH hansa serve starts with, never on a lane's (a lane's env "
            "reaches only the command inside the sandbox). node must be on that PATH "
            "too, since srt is a node script (an error such as `env: node: No such file or directory`, exit 127, "
            "means it is not). Install srt with `npm install -g @anthropic-ai/sandbox-runtime`, or fix the "
            "server's PATH, then restart hansa serve. Then, for a task whose "
            "check could not run or failed for the machine's sake: hansa merge ID --recheck runs the merge check "
            "again and merges if it passes; a task whose setup could not run: hansa retry ID.",
            "Before it starts a worker, a verifier or a design session, it probes that run's sandbox the same way: "
            "srt with the settings Claude Code is given, on /usr/bin/true, then a write of what git needs open "
            "there (the worktree's own index and git folder; for a design session, its branch's ref's .lock and "
            "objects/), and of what it must keep, to see that the deny patterns match (a new file atop the "
            "shared git folder, a name next to the worktree's own folder; for a design session, next to its "
            "branch's ref and in refs/heads/; what such a write makes is removed), once for each set of settings "
            "while it runs. When srt refuses them, keeps what git needs or lets through what it must keep (a "
            "sandbox that does not keep what it should runs no one), the run does not start: its task is held (queued, no try used) and its "
            "lane shows as unable to run in hansa status, with srt's first line and the part of the settings it "
            "refuses (found by trying each part alone, or the path written); the principal's alert is told once for each lane "
            "and reason. Only what was refused is held: a design session's sandbox is another than its lane's "
            "workers', so its refusal holds the lane's design sessions, and a worker's or a verifier's holds the "
            "lane's workers and verifiers; hansa status says which. Held tasks start by themselves once a probe "
            "passes: after a fix, restart hansa serve, which as it starts probes again the sandbox of each held lane "
            "(a worker's in the lane's worktree, a design session's in its design worktree), clears the hold when "
            "the probe passes and keeps it with the new reason when it fails (so does a change of the settings, at "
            "the next task's start). Without srt the run starts as before, in Claude Code's own sandbox, "
            "which never runs a command outside it. Claude Code carries its own copy of the sandbox library, which "
            "may differ from the srt installed, so the probe is a guard, not proof: when a run's first Bash calls "
            "say the sandbox could not start (sandbox-exec: or bwrap: before any call succeeded), hansa stops the "
            "run and holds its task the same way.",
            *([f"  {LINUX_TRY_NOTE[0].upper()}{LINUX_TRY_NOTE[1:]}."] if account.LINUX else []),
            *[f"  held: lane {ln} cannot run, {sandbox_held_text(h)}" for ln, groups in sorted(sandbox_held(desk).items())
              for h in groups.values()],
            "  More: hansa help sandbox-probe",
            *disk.explain_lines(desk, desk.settings())])
    if what == "notify":
        who = notify.names(desk)
        return "\n".join([
            "hansa posts to the coordinator's Claude Code session through the session's inbox (a local socket every "
            "interactive session listens on), so the session need not keep hansa wait running in the background.",
            "A follower does it: a small background process per session and desk, which reads the event log from "
            "where it left off and ends when the session's process ends. hansa coordinator starts one for the "
            "session it launches; hansa notify on starts one for the session it is run from; hansa notify off stops "
            "it; hansa notify lists them.",
            "It posts: a task that ended (after hansa's own check; or failed, was cancelled, stopped at its ceiling, "
            "could not start), a verification's verdict, a design session that ended, a queued merge check that "
            "ended, a request the principal answered or closed (with the answer), the principal's answer to a release "
            "(hansa release), a release that ended (done, or failed with its exit code and last lines) or became void, "
            "a provider that an open request names "
            "becoming ready when it was not as the request was made (once; the request may be done; looked at when a "
            "key, the settings or a check change it, and when its rest after a usage limit runs out, which hansa "
            "serve sees), hansa serve stopping or "
            f"finishing no round for {STUCK_AFTER // 60} minutes (busy or stuck), and then running again, srt not "
            "running. Not what you did yourself and saw. Each message starts with [hansa] and the desk, with one "
            "line per event and the next command; events seconds apart come in one message.",
            "Who hears what: a task or request goes to the session that made it (hansa add, retry, verify, merge, "
            "design and ask record it) while that session has a follower; otherwise to every follower of the desk, "
            "as do desk-wide events. A cancel is not posted to the session whose hansa cancel made it (hansa cancel "
            "records it); others hear it, as all do one hansa made itself. A follower that starts after one ended "
            "first posts what was missed meanwhile, as it is now.",
            "These messages come from hansa, not from the principal, and approve nothing. A session whose settings "
            "hold messages from other sessions (crossSessionInbound) does not get them; only the principal can "
            "change that.",
            "hansa coordinator --clear-daily HH-HH hands a window of hours (local time; 22-03 runs past midnight) to "
            "hansa's mod, for that start only: once in each window it clears the session, when the session has been "
            "idle for 5 minutes and did not start inside that window. Inside the window, idle compaction waits for "
            "the clear. After any clear the session reads hansa status by itself, and hansa records when; hansa "
            "status says the last clear, and warns when windows passed without one. hansa notify on no longer clears.",
            "  now: " + (", ".join(who) if who else "no session")])
    return "\n".join([
        "What you can look up: hansa explain lane <name> | provider <name> | server | notify",
        f"lanes: {', '.join(sorted(s['lanes'])) or 'none'} · providers: {', '.join(sorted(s['providers']))}",
        f"coordinator: {s['coordinator']['provider']} ({s['coordinator']['model']}) · "
        f"cost audit by the coordinator: {'on' if s['coordinator'].get('audits_cost') else 'off'}",
        *read_lines(desk, s),
        "Providers, models, endpoints, keys, effort defaults and prices belong to the principal (web page). "
        "You change tasks, lanes (hansa lane), how many run at once (hansa concurrency), the floor of free space under "
        "which nothing new starts (hansa disk-floor), lanes' standing instructions (hansa lane brief) and "
        "pause/resume.",
        release_settings_line(s),
        alert.explain_line(s)])


def release_settings_line(s: dict) -> str:
    """For hansa explain: the principal's release settings."""
    def named(k):
        return k[0] + (f" target {k[1]}" if k[1] else "")
    listed = [named(k) for k in listed_targets(s) if release.lane_listed(s, *k)]
    changed = [named(k) for k in listed_targets(s) if named(k) not in listed]
    words = s.get("release_forbidden") or []
    mailbox_allow = s.get("release_mailbox_allow") or []
    allowed = ", ".join(mailbox_allow[:20]) if mailbox_allow else "(empty)"
    if len(mailbox_allow) > 20:
        allowed += f", and {len(mailbox_allow) - 20} more, all on the page"
    return ("Releases (hansa release L [TARGET]) go out only after the principal's yes on the page"
            + (f", but for lanes {', '.join(listed)}, which the principal lets go out without asking while the desk is "
               "not paused (each target ticked on its own)" if listed else "")
            + (f" (lanes {', '.join(changed)} are on that list, but their repository, base branch or release command "
               "is no longer what it was when the principal saved it, so theirs wait for the yes)" if changed else "")
            + f"; {len(words)} word(s) the principal keeps from going out are checked at hansa add (a task's title), at each merge and before each release. Both lists are the "
            "principal's, on the web page; you cannot set them (hansa ask). "
            f"The mailbox rule (release_mailboxes) is {'on' if s.get('release_mailboxes') else 'off'}: "
            "when on, mailboxes in every added line must use reserved example domains at merges and releases, "
            "or be allowed by release_mailbox_allow: " + allowed
            + ". A full address allows that address; @domain allows the domain and its subdomains, ignoring case. "
            "The rule and allowlist are also the principal's, on the same page (hansa ask). Do not push or deploy yourself.")


def no_read_lines(desk: Desk, s: dict, name: str, whole: bool = False) -> list:
    """For hansa explain lane: what the lane's workers and verifiers, its setup and its check may not read. The
    built-in list as a count, the principal's own entries whole; whole: the built-in list name by name too."""
    homes = dict.fromkeys(str(claude_home(desk.state_dir, n, p)) for n, p in s["providers"].items() if isinstance(p, dict))
    kept = no_read_parts(s, desk.root)[0]
    mac = (f"; on macOS also, under ~/Library: {', '.join(x.removeprefix('Library/') for x in MAC_SECRETS)}"
           if sys.platform == "darwin" else "")
    built_in = (f"{', '.join('~/' + x for x in HOME_SECRETS)}{mac}" if whole else
                f"{len(HOME_SECRETS)} names" + (f", and {len(MAC_SECRETS)} more on macOS under ~/Library"
                                                if sys.platform == "darwin" else "")
                + f" (the whole list: hansa explain lane {name} --all)")
    return ["  what its workers and verifiers, its setup and its check may not read (the sandbox refuses it to any"
            " command; a name that does not exist is refused all the same):",
            f"    the desk's keys and state: {desk.secrets_dir}, {desk.state_dir}",
            f"    every provider's Claude Code folder, whichever provider the run uses: {', '.join(homes)}; and ~/.claude",
            "    logins, keys, tokens, browsing data, messages and histories under the home folder (built in): "
            + built_in,
            "    folders and files the principal keeps from workers (the principal's, on the web page, Settings; you"
            " add to it with hansa hide, and only the principal removes an entry; Read is denied there to you, workers,"
            " verifiers and design sessions as well; no lane's repository or write list may be one, inside one or"
            " above one): " + (", ".join(x + hidden_note(s, x) for x in kept) or "none"),
            *("      " + x for x in no_read_unusable(s, desk.root))]


def read_lines(desk: Desk, s: dict) -> list:
    """For hansa explain: the folders outside the desk the coordinator reads without asking, and those listed
    that it is not given."""
    # as the start checks them (cli.read_folders): with the Claude Code folder this session runs from
    own = [os.path.abspath(os.path.expanduser(os.environ["CLAUDE_CONFIG_DIR"]))] if os.environ.get("CLAUDE_CONFIG_DIR") else []
    good, refused = read_split(s.get("coordinator_read", []), desk.root, s["providers"], own,
                               kept_from_workers(s, desk.root)[0])
    return ["folders outside the desk you (and your helpers) read without asking, besides the lanes' repositories: "
            + (", ".join(good) or "none") + " (set by the principal on the web page; reading only: you cannot "
            "edit there; a change holds from the next hansa coordinator start; when you keep needing one, hansa ask)",
            *(f"  not given: {r.removeprefix('the coordinator cannot be let read ')}" for r in refused)]


def try_text(lane: str, t: dict) -> str:
    """What `hansa lane try` prints: for the setup and the check, the exit code, the time taken, the last lines."""
    if t.get("busy"):
        return f"hansa: {t['note']}. Try again when it is free: hansa wait {t['busy']}, then hansa lane try {lane}"
    if t.get("could_not_run"):  # srt did not run in the lane's sandbox: the machine's problem, not the commands'
        return (f"lane {lane}: {t['note']}\nnothing was run: the setup and check run in srt "
                f"(hansa explain server). Fix it, then hansa lane try {lane}")
    out = [f"lane {lane} tried at {(t.get('onto') or '')[:10]}: " + ("ok" if t.get("ok") else "FAILED")
           + (f" ({t['note']})" if t.get("note") else "")]
    for step, x in t.get("steps", {}).items():
        if not x["ran"]:
            out.append(f"{step.replace('_', ' ')}: {x['command']}\n  did not run: the setup failed")
            continue
        how = "interrupted" if x["exit"] is None else f"exit {x['exit']}"
        out.append(f"{step.replace('_', ' ')}: {x['command']}\n  {how}" + (f" in {x['seconds']}s" if x["seconds"] is not None else "")
                   + (", last lines:" if x["tail"] else ", no output"))
        out += [f"    {plain(line, None)}" for line in x["tail"]]
        out += [f"  {h}" for h in x.get("hints", [])]
    if t.get("log"):
        out.append(f"each step's whole output, keys removed: {t['log']} (hansa lane try {lane}"
                   f"{' --merge-check' if t.get('merge') else ''} --log)")
    if t.get("clean"):
        out.append("the worktree is back clean at the base's tip, as the lane's next task expects it")
    return "\n".join(out)


def cost_text(rep: dict, by: str = "day") -> str:
    rows = rep["by"].get(by, {})
    window = rep.get("window") or f"last {rep['days']} day(s)"
    out = [f"{window}, by {by}: {rep['total']['tokens']:,} tokens, "
           f"worth ${rep['total']['usd']:.2f}"]
    for k, v in rows.items():
        extra = f" (+{v['unpriced_tokens']:,} tokens with no price)" if v["unpriced_tokens"] else ""
        if by == "task" and v.get("runs"):  # every run of the task: its earlier tries too
            extra += f"  {v['runs']} run(s), {v.get('turns', 0):,} turn(s)"
        out.append(f"  {k or '-':<22} {v['tokens']:>14,}  ${v['usd']:.2f}{extra}")
    if rep["unpriced_models"]:
        out.append("no price yet for: " + ", ".join(rep["unpriced_models"]) + " (the principal sets prices on the page)")
    if rep.get("coordinator_uncounted"):
        out.append(rep["coordinator_uncounted"])
    out.append(rep["note"])
    return "\n".join(out)


def cost_turns_text(rep: dict) -> str:
    """hansa cost --turns: one line per turn of the coordinator, then totals by what started them."""
    t = rep["total"]
    out = [f"the coordinator's turns, {rep['window']}: {t['turns']} turn(s), {t['steps']} step(s), worth ${t['usd']:.2f}"]
    sessions = {}  # each session once, in the order of its first turn: two sessions' turns may alternate
    for r in rep["turns"]:
        sessions.setdefault(r["session"], []).append(r)
    for session, rows in sessions.items():
        out.append(f"session {session[:8]}")
        for r in rows:
            tools = ", ".join(f"{k} {n}" for k, n in r["tools"].items())
            out.append(f"  {time.strftime('%m-%d %H:%M', time.localtime(r['start']))}  {r['kind']:<13} "
                       f"{r['steps']:>4} step(s)  length {r['length']:>9,}  {'$%.2f' % r['usd']:>8}"
                       + (f"  {tools}" if tools else "")
                       + ("  (from before the window: its steps, worth and length from its first step in it)"
                          if r.get("before_window") else ""))
    if rep["by_kind"]:
        out.append("by what started the turns:")
        for k, v in rep["by_kind"].items():
            out.append(f"  {k:<13} {v['turns']:>4} turn(s) {v['steps']:>5} step(s)  {'$%.2f' % v['usd']:>8}  {v['share']:>4.0%}")
        sh = rep["token_shares"]
        out.append(f"the worth in cache reads {sh['cache_read']:.0%}, cache writes {sh['cache_write']:.0%}, "
                   f"fresh input {sh['in']:.0%}, output (thinking included) {sh['out']:.0%}")
    if rep["helpers"]:
        out.append("helpers, not in the turns above: " + ", ".join(
            f"{k} {v['steps']} step(s) ${v['usd']:.2f}" for k, v in rep["helpers"].items()))
    if rep["unpriced_models"]:
        out.append("no price yet for: " + ", ".join(rep["unpriced_models"]) + ": their steps are worth $0 here, "
                   "as they are left out of hansa cost's dollars, so the tokens do not match")
    if rep.get("coordinator_uncounted"):
        out.append(rep["coordinator_uncounted"])
    out.append("The total is the coordinator's row of hansa cost --by seat over the same window. A turn's length is "
               "the conversation it reread at its first step (input + cache read + cache write).")
    return "\n".join(out)


def diff_text(desk: Desk, tid: str, stat_only: bool = False, paths=(), new: bool = False) -> str:
    """hansa diff: a task's changes against its base, limited to these paths when given. new: only what this
    attempt added on top of the attempt it continues from (hansa retry), without the base merged into it."""
    from .run import GitFileChanged, wgit
    m = desk.meta(tid)
    if not m.get("head") or not m.get("worktree"):
        return f"{tid} has no changes recorded yet (state: {m['state']})"
    wt = Path(m["worktree"])
    since, head = m["base"], ""
    try:  # the worktree's .git is checked first; the task working there now fails for it when it ends
        if new:
            if not m.get("start_from") or m.get("kind") == "verify":
                return (f"hansa: {tid} did not continue from an earlier attempt, so all of it is new: "
                        f"hansa diff {tid}" + (" --stat" if stat_only else ""))
            since = m["start_from"]
            head = f"what {tid} added on top of {m.get('parent') or 'the earlier attempt'}'s last commit ({since[:10]})"
            if m.get("update"):  # the base's tip was merged in when it started: from that merge, when it was clean
                rows = [x.split() for x in wgit(desk, wt, "rev-list", "--first-parent", "--parents",
                                                f"{since}..{m['head']}").splitlines()]
                merge = next((x[0] for x in rows if len(x) > 2 and x[1] == since), "")
                if merge and not m.get("update_conflicts"):
                    since = merge
                    head += f", without the base merged into it at {merge[:10]}"
                else:
                    head += ("; the base was merged into it with conflicts the worker settled, so what the base "
                             "gained is in it too")
        args = ["diff", "--stat" if stat_only else "--patch", since, m["head"]] + (["--", *paths] if paths else [])
        out = desk.redact(wgit(desk, wt, *args))
    except GitFileChanged as e:
        return f"hansa: {e}; the diff is not shown"
    out = out or (f"(no changes in {', '.join(paths)})" if paths else "(no changes)")
    return f"{head}:\n{out}" if head else out
