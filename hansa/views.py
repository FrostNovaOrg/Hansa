"""Views: the facts behind `hansa status`, `hansa show`, `hansa explain` and the web page.
Each view returns plain data; the *_text helpers turn it into short lines."""
from __future__ import annotations

import itertools
import json
import re
import shlex
import statistics
import sys
import time
from pathlib import Path

from . import SESSION_NAMES, SESSION_PREFIXES, account, alert, alert_push, build, cost, disk, gaps, goals, history, installed, installed_text, memory, notify, plugin, release, up
from .desk import (DENY_AT, HOME_SECRETS, desks_list_problem, listed_desks, shared_writes, hidden_note, no_read_unusable, MAC_SECRETS, Desk, event_record, no_read_parts, no_read_parts_shown, stop_text, bad_messages, claude_home, allow_fix, refused_rule, web_sites, listed_targets, read_json,
                   release_files_in, merge_targets_problem, work_lane_problem, write_json, PROJECT_FILE, PROJECT_LONG, SettingsError)
from .run import (disk_short, repo_gone_text, session_private, HOLDING_VERDICTS, LINUX_SANDBOX_NOTES, LINUX_TRY_NOTE, output_hints, lane_identity, lane_locale, lane_wait_text, ROUND_QUIET, STUCK_AFTER, add_usage, branch_holds, is_local, lane_occupied, occupied_lane, merge_lane, merge_check_lane, merge_target, last_round,
                  markers_in_change, markers_text, merge_check_ahead, merge_command, merge_minutes, not_looking, oom_lines, own_merge_check, progress_of, standing_verification,
                  credit_text, model_text, provider_problem, providers_waited_for, quota_probe_text, refused_by, rest_text, rethink_text, rounds_line, sandbox_held, sandbox_held_text, serve_code, serve_session, server_state,
                  lane_env, server_text, srt_needed, srt_problem, srt_record, srt_when, task_providers, tried_lanes, waiting_detail, waiting_looks, write_list)

STATES = {
    "queued": "waiting for a free lane and a ready provider",
    "running": "a worker is on it, or hansa is running the lane's check on its work",
    "done": "the worker finished; waiting for the coordinator's review (hansa show / hansa merge / hansa retry)",
    "merging": ("hansa merge is checking it, as the base's tip is not in the task's recorded head or the lane has a "
                "merge_check of its own: merged onto the base as it is now, with that merge_check (else the lane's "
                "check) run there; it merges if that passes. Or it needs no check (a task with no commits never does) "
                "and waits behind other merges on the same repository and base, or a release running there"),
    "failed": ("the run ended with an error; hansa show says why. hansa retry sends it back; hansa close puts it "
               "away (a task becomes cancelled, a verification stays failed), and hansa show still says why it failed"),
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


def verification(desk: Desk, m: dict, *, latest: bool = False):
    """The standing verification, with the newest attempt alongside it; latest=True for run/cancel guards."""
    vid = m.get("verify")
    if not vid:
        return None
    def row(v):
        v = as_closed_now(v)
        return {"task": v["id"], "state": v["state"], "verdict": v.get("verdict", ""), "lane": v.get("lane", "")}
    newest = row(desk.meta(vid))
    if latest:
        return newest
    v = row(standing_verification(desk, m))
    if v["task"] != vid and newest["state"] in ("queued", "running"):
        return {**newest, "standing": v}
    return {**v, **({"latest": newest} if v["task"] != vid else {})}


def task_list(desk: Desk, *, page: int = 1, state: str = "", lane: str = "", words: str = "",
              days: int | None = None) -> dict:
    """One page of work tasks, from metadata only, showing their standing verdicts."""
    records = desk.tasks()
    by_id = {m.get("id"): m for m in records}
    tasks = [m for m in records if m.get("kind") != "verify"]
    states = sorted({m["state"] for m in tasks if m.get("state")})
    lanes = sorted({m["lane"] for m in tasks if m.get("lane")})
    since = time.time() - days * 86400 if days is not None else None
    terms = words.casefold().split()
    tasks = [m for m in tasks if (not state or m.get("state") == state)
             and (not lane or m.get("lane") == lane)
             and all(word in (m.get("title") or "").casefold() for word in terms)
             and (since is None or (m.get("created") is not None and m["created"] >= since))]
    tasks.sort(key=lambda m: (m.get("created") or 0, int(m["id"][1:])), reverse=True)
    pages = max(1, (len(tasks) + 49) // 50)
    page = max(1, min(page, pages))
    rows = [{**{k: m.get(k) for k in ("id", "title", "lane", "state", "created", "finished")},
             "verdict": (standing_verification(desk, m, by_id) or {}).get("verdict", "")}
            for m in tasks[(page - 1) * 50:page * 50]]
    return {"rows": rows, "page": page, "pages": pages, "total": len(tasks), "states": states, "lanes": lanes}


def _verification_line(v) -> str:
    if v.get("standing"):
        return (_verification_line(v["standing"]) + "; latest: "
                + _verification_line({k: value for k, value in v.items() if k != "standing"}))
    if v.get("latest"):
        return _verification_line({k: value for k, value in v.items() if k != "latest"}) + "; latest: " + _verification_line(v["latest"])
    if v["state"] in ("queued", "running"):
        return f"{v['task']} {v['state']}"
    return f"{v['task']}: {v['verdict']}" if v["verdict"] else f"{v['task']} {v['state']} without a verdict"


def _verification_next(task: dict, v: dict) -> str:
    """Verdict guidance shared by the task, its verification and the completion post."""
    tid, vid = task.get("id") or v.get("parent"), v["id"]
    report = f"hansa show {vid} --report"
    if v.get("verdict") == "holds":
        return f"hansa merge {tid}"
    if v.get("verdict") == "holds, with notes":
        return f"{report} (its notes), then hansa merge {tid}"
    if v.get("verdict") not in ("does not hold", "unsure"):
        return f"{report}, then merge or send back {tid}"
    # Preserve queue-time choices, leaving automatic lane/effort selection automatic.
    # A verification requested by hand has its lane and effort on the verification itself.
    request = task.get("verify_after") or v
    flags = "--verify"
    for field in ("lane", "effort"):
        if request.get(field):
            flags += f" --verify-{field} {shlex.quote(request[field])}"
    merge = f"hansa merge {tid}" + (" --despite-check" if v["verdict"] == "does not hold" else "")
    return (f'{report}, then hansa retry {tid} "fix what {vid} found: ..." {flags}'
            f" · {merge}")


def _progress_line(p: dict) -> str:
    if "setup_min" in p:
        return f"the lane's setup is running ({p['setup_min']} min)"
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
    """A queued merge check can wait for its lane's try, or for a concurrency slot."""
    s = desk.settings()
    lane = merge_check_lane(s, m)
    if (m.get("merge_check") or {}).get("phase") != "queued":
        return ""
    if lane != m.get("lane") and lane in tried_lanes(desk):
        return "try"
    return "slot" if sum(lane_occupied(x) for x in (desk.tasks() if tasks is None else tasks)) >= s["concurrency"] else ""


def merge_check_text(desk: Desk, m: dict, tasks: list | None = None, sentence: bool = False) -> str:
    """Where a merging task's merge check is, in one line. sentence: one just queued, as hansa merge says it."""
    mc = m.get("merge_check") or {}
    lane = merge_check_lane(desk.settings(), m)
    base = desk.settings()["lanes"].get(m.get("lane"), {}).get("base") or "main"
    if len(mc.get("members", [])) > 1:
        owner = mc["leader"]
        running = desk.meta(owner).get("merge_check") or {}
        return (f"merge check running on lane {lane}: checked with {', '.join(x for x in mc['members'] if x != m['id'])}; "
                f"{owner}'s check runs on {str(running.get('commit', ''))[:10]}; "
                f"output: hansa show {owner} --merge-check-log ({desk.tasks_dir / owner / 'merge-check.log'})")
    again = f" (again: {base} moved while it was checked)" if mc.get("moves") else " (alone)" if mc.get("alone") else ""
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


def check_interruption(r: dict) -> str:
    """A recorded stop that a worker fixing a failed check cannot remedy. Read notes too, so results
    saved before this guidance existed get the same advice as new ones."""
    if not r or r.get("ok"):
        return ""
    if r.get("conflict"):
        return "conflict"
    note = r.get("note") or ""
    if "has uncommitted changes" in note:
        return "checkout"
    if "hansa help forbidden-words" in note or "holds words the principal keeps from going out" in note:
        return "forbidden"
    if note.startswith("the lane's setup failed"):
        return "setup"
    if note == "stopped at the lane's time ceiling":
        return "timeout"
    if r.get("direct") or r.get("waited") or r.get("merge_error"):
        return "merge"
    return ""


def interruption_next(s: dict, m: dict, r: dict, merge: bool = False) -> str:
    """Next steps shared by show and its coordinator post; '' leaves the ordinary check advice alone."""
    why, tid = check_interruption(r), m["id"]
    lane = (r.get("lane") if merge else None) or m.get("lane")
    if why == "timeout":
        setting = "merge_timeout_min" if merge and s["lanes"].get(lane, {}).get("merge_timeout_min") else "timeout_min"
        return (f"if the check needs more time: hansa lane set {lane} {setting} N · "
                f"hansa merge {tid} --recheck (runs the check again and merges if it passes)")
    if why == "setup":
        return (f'fix the lane\'s setup: hansa lane set {lane} setup "<command>" · '
                f"hansa {'merge' if merge else 'retry'} {tid}")
    if why == "checkout":
        return f"the checkout's owner commits or puts aside the uncommitted changes, then hansa merge {tid}"
    if why == "forbidden":
        return (f'hansa retry {tid} --fresh "<take the forbidden words out>" (continuing keeps the commits; '
                f"a word in the title: add again with another --title; in merge_message or identity: change the lane) "
                f"· or ask the principal with hansa ask · hansa help forbidden-words")
    if why == "conflict":
        return (f'hansa retry {tid} --update "<how to settle the conflict>" · hansa retry {tid} --fresh '
                f'"<the same change on the current base>"')
    if why == "merge":
        return (f"once what stopped it is fixed (the principal fixes a checkout; tell them with hansa ask): "
                f"hansa merge {tid} again")
    return ""


def merge_failure_reason(r: dict) -> str:
    """The saved reason a merge stopped, including older results with advice but no note."""
    reason = (r.get("note") or r.get("conflict") or r.get("merge_error")
              or (f"the check failed (exit {r['exit']})" if r.get("exit") is not None else "the check failed"))
    return reason.removeprefix("hansa: ")


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
    if r.get("ok") or r.get("note") or check_interruption(r):
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
    lines = _merge_result_lines(m, log)
    interruption = check_interruption(m.get("merge_check_result") or {})
    for step in (m.get("merge_check_result") or {}).get("steps", []):
        how = ("did not run (the setup failed)" if interruption == "setup" else
               "passed (exit 0)" if step.get("ok") else
               "did not run or was interrupted" if step.get("exit") is None else
               f"did not finish (exit {step['exit']})" if interruption == "timeout" else
               f"FAILED (exit {step['exit']})")
        lines.append(f"  at merge, {step['what']}: {how}: {step['command']}")
    return lines


def _merge_result_lines(m: dict, log: str = "") -> list:
    """What the last merge check found, for hansa show. log: its whole output, when it is kept."""
    r = m.get("merge_check_result")
    if not r:
        return []
    past = m.get("state") == "merged" and not r.get("ok")
    label = "previous merge check" if past else "merge check"
    interruption = check_interruption(r)
    advice = (r.get("conflict") or r.get("merge_error") or "").removeprefix("hansa: ")
    outcome = (("merged later" if advice or interruption else "merged despite this failed check")
               + f" at {notify.when(m.get('merged'))}" if past else "nothing was merged")
    if past:
        advice = advice.replace("nothing was merged", outcome, 1)
    if r.get("direct"):
        return [f"{'previous merge' if past else 'merge'}: last attempt at {notify.when(r.get('at'))}; {advice}"]
    if r.get("waited"):  # it needed no check: it waited its turn behind other merges, and no check ran
        return [f"{'previous merge' if past else 'merge'}: " + (f"{WAITED}: " + advice if advice
                             else waited_text(r))]
    if advice:
        return [f"{label}: " + advice]
    if interruption in ("checkout", "forbidden"):
        return [f"{'previous merge' if past else 'merge'} could not be made: "
                + merge_failure_reason(r).replace("nothing was merged", outcome)]
    if interruption == "setup":
        lane = f" in lane {r['lane']}" if r.get("lane") and r["lane"] != m.get("lane") else ""
        return ([f"{label}{lane}: did not run ({r['note']}); {outcome}"]
                + failed_check_lines(log, r.get("tail", []), m["id"], "--merge-check-log"))
    where = f"hansa merged it onto the base at {r.get('onto', '')[:10]}"
    if r.get("lane") and r["lane"] != m.get("lane"):
        where += f" in lane {r['lane']}'s worktree"
    if r.get("ok"):
        return [f"merge check: passed in {r.get('seconds', 0)} s ({where})"
                + ("" if r.get("steps") else f": {r.get('command', '')}")]
    if r.get("could_not_run"):
        return [f"{label}: {r['note']}; {outcome}", "  " + machine_hint(r)]
    how = (f"did not finish ({r['note']})" if interruption == "timeout" else
           f"FAILED ({r['note']})" if r.get("note") else f"FAILED (exit {r.get('exit')})")
    hint = machine_hint(r)
    if r.get("steps"):  # a --recheck's steps: named as the per-step lines below name them (merge_result_lines)
        ran = [s for s in r["steps"] if s.get("exit") is not None]
        failed = [s for s in ran if not s.get("ok")]
        not_run = [s for s in r["steps"] if s.get("exit") is None]
        did = (f" and ran the {', then the '.join(s['what'] for s in ran)} there" if ran else "") + (
            f"; the {' and the '.join(s['what'] for s in not_run)} did not run or was interrupted" if not_run else "")
        if failed and not r.get("note"):
            how = f"{failed[0]['what']} FAILED (exit {failed[0]['exit']})"
        return ([f"{label} ({where}{did}): {how} in {r.get('seconds', 0)} s; {outcome}"]
                + failed_check_lines(log, r.get("tail", []), m["id"], "--merge-check-log") + (["  " + hint] if hint else []))
    return ([f"{label} ({where} and ran the lane's {'merge check' if r.get('own') else 'check'} there): {how}"
             + (f" in {r['seconds']} s: {r['command']}" if r.get("command") else "") + f"; {outcome}"]
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
    """For an open to-do that may be done by now (ask_names): a hint saying so. hansa closes nothing itself."""
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


def ping_text(check) -> str:
    """A provider's last ping (hansa ping, the page's Ping) in a line: whether it answered, when in local time,
    what it said and how long it took."""
    if not isinstance(check, dict):
        return "never"
    at = check.get("at")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(at)) if isinstance(at, (int, float)) else "at an unknown time"
    secs = check.get("seconds")
    took = f", in {secs:g}s" if isinstance(secs, (int, float)) and not isinstance(secs, bool) else ""
    detail = str(check.get("detail") or "").strip()
    return (("answered" if check.get("ok") else "did not answer") + f" {when}" + (f": {detail}" if detail else "")
            + took)


def web_address(desk: Desk) -> tuple:
    """The address and port this desk's hansa serve last served the page on (the defaults before it ever ran)."""
    try:
        port = int((desk.state_dir / "web-port").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        port = 8765
    host = read_text(desk.state_dir / "web-host").strip() or "127.0.0.1"
    return host, port


def page_address(host: str, port) -> str:
    """The page's address without its key, as hansa status gives it: a server listening on every address
    (0.0.0.0, ::) at 127.0.0.1 (or ::1)."""
    every = host in ("0.0.0.0", "", "::")
    host = ("[::1]" if host == "::" else "127.0.0.1") if every else host
    return f"http://{host}:{port}/"


def page_text(page: dict) -> str:
    """Where the desk's web page is, and whether it is served now, for hansa status and hansa explain. A server
    listening on every address (0.0.0.0, ::) is given at 127.0.0.1 (or ::1), an address one can open."""
    every = page["host"] in ("0.0.0.0", "", "::")
    at = page_address(page["host"], page["port"]) + (" (it listens on every address)" if every else "")
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


def killed_last(m: dict) -> dict:
    """{"last_run": how its last run ended} for a queued task while it waits because the system killed that run and
    hansa queued it again (run.finish); {} otherwise. Its error is then the kill's: holding it (run.hold) or
    queuing it again for another cause since gives it another."""
    a = (m.get("attempts") or [{}])[-1]
    return {"last_run": m["error"]} if a.get("kind") == "killed" and a.get("error") == m.get("error") else {}


_TIMELINE_BOUNDARIES = {}  # Per log: only older starts (or missing starts) needed by ends still in the window.


def timeline_events(path: Path, since: float, *, tasks=(), releases=(), tries=None) -> list:
    """Read the window backwards; prefer recorded starts and bound/cache older lookups."""
    starts = {"task_started": "task", "check_started": "check", "merge_check_started": "merge",
              "lane_try_started": "try", "release_started": "release"}
    ends = {"task_finished": "task", "task_checked": "check", "check_killed": "check",
            "merge_checked": "merge", "merge_check_ended": "merge", "merge_check_killed": "merge",
            "lane_tried": "try", "release_ended": "release", "release_stopped": "release"}

    def identity(event, kind):
        return kind, event.get("id") if kind == "release" else event.get("lane") if kind == "try" else event.get("task")

    saved = {}
    meta = {m["id"]: m for m in tasks}
    def remember(name, started, ended=None, **fields):
        if started is not None:
            event = {"event": name, "ts": started, **fields}
            saved.setdefault(identity(event, starts[name]), []).append((event, ended))

    for m in tasks:
        fields = {"task": m["id"], "lane": m.get("lane")}
        remember("task_started", m.get("started"), m.get("finished"), **fields)
        for run in [*(m.get("attempts") or []), *(m.get("earlier_runs") or [])]:
            remember("task_started", run.get("ran_from"), run.get("ended") or run.get("at"),
                     **{**fields, "lane": run.get("lane", m.get("lane"))})
        check = m.get("check") or {}
        began = m.get("check_started")
        remember("check_started", began, began + check["seconds"] if began is not None and
                 check.get("seconds") is not None else None, **fields)
        mc = m.get("merge_check") or {}
        remember("merge_check_started", mc.get("started"),
                 **{**fields, "lane": mc.get("lane") or m.get("lane"), "members": mc.get("members") or [m["id"]]})
        result = m.get("merge_check_result") or {}
        if result.get("at") is not None and result.get("seconds") is not None and not result.get("waited"):
            remember("merge_check_started", result["at"] - result["seconds"], result["at"],
                     **{**fields, "lane": result.get("lane") or m.get("lane"), "members": result.get("batch") or [m["id"]]})
    for r in releases:
        remember("release_started", r.get("started"), r.get("ended"),
                 id=r["id"], lane=r.get("lane"), target=r.get("target"))
    for lane, trial in (tries or {}).items():
        remember("lane_try_started", trial.get("started"), trial.get("ended"), lane=lane)

    def never_started(event):
        # These end events explicitly describe work refused before launch: there is no older start.
        return (event.get("could_not_run") or event.get("waited") or event.get("ran") is False or
                event.get("event") == "task_finished" and "cancelled before it started" in event.get("error", "") or
                event.get("event") == "task_checked" and "could not run" in event.get("note", ""))

    recent, older = [], []
    try:
        stat = path.stat()
        file_id = (stat.st_dev, stat.st_ino)
        cached_id, cached_size, lookups = _TIMELINE_BOUNDARIES.get(str(path), (None, 0, {}))
        if cached_id != file_id or stat.st_size < cached_size:
            lookups = {}
        lookups = {signature: start for signature, start in lookups.items() if signature[1] >= since}
        with open(path, "rb") as source:
            def backwards():
                pos, pending = source.seek(0, 2), b""
                while pos:
                    size = min(pos, 65536)
                    pos -= size
                    source.seek(pos)
                    lines = (source.read(size) + pending).split(b"\n")
                    pending = lines.pop(0) if pos else b""
                    for line in reversed(lines):
                        event = event_record(line)
                        if event and isinstance(event.get("ts"), (int, float)):
                            yield event

            stream, boundary = backwards(), None
            for event in stream:
                if event["ts"] < since:
                    boundary = event
                    break
                recent.append(event)
            waiting, end_events = {}, {}
            for event in recent:  # newest first; only ends without their start need an older lookup
                name = event.get("event")
                if name in starts:
                    waiting.pop(identity(event, starts[name]), None)
                elif name in ends and not never_started(event):
                    key = identity(event, ends[name])
                    signature = waiting.setdefault(key, (key, event["ts"]))
                    end_events.setdefault(signature, event)
            for key, signature in list(waiting.items()):
                candidates = [start for start, end in saved.get(key, []) if start["ts"] <= signature[1]
                              and (end is None or signature[1] <= end + 2)]
                if candidates:
                    start = max(candidates, key=lambda e: e["ts"])
                    older.append(start)
                    lookups[signature] = start
                    del waiting[key]
                elif signature in lookups:
                    if lookups[signature]:
                        older.append(lookups[signature])
                    del waiting[key]
            if waiting:
                for event in itertools.chain([boundary] if boundary else [], stream):
                    if event["ts"] < since - 7 * 86400:
                        break
                    name = event.get("event")
                    if name in starts and (key := identity(event, starts[name])) in waiting:
                        lookups[waiting.pop(key)] = event
                        older.append(event)
                        if not waiting:
                            break
                    elif name in ends and (key := identity(event, ends[name])) in waiting:
                        # This older run already ended: a newer refused/aborted operation never started.
                        lookups[waiting.pop(key)] = None
                        if not waiting:
                            break
                for key, signature in waiting.items():
                    # The true start is unavailable within the bounded history. Draw a cut interval.
                    end = end_events[signature]
                    start = {**end, "event": next(name for name, kind in starts.items() if kind == key[0]),
                             "ts": since, "lane": end.get("lane") or meta.get(end.get("task"), {}).get("lane")}
                    lookups[signature] = start
                    older.append(start)
        _TIMELINE_BOUNDARIES[str(path)] = (file_id, stat.st_size, lookups)
    except FileNotFoundError:
        pass
    return sorted(older + recent, key=lambda e: e["ts"])


def timeline_data(lanes, tasks: list, events: list, releases: list, tries: dict, at: float) -> dict:
    """The page's rolling day. Records supply crossing/live work; event pairs retain every earlier run.

    Times are Unix seconds. started/ended retain the original interval; start/end are clipped for drawing.
    No status view or command uses this data.
    """
    since = at - 86400
    rows = {lane: [] for lane in lanes}
    meta = {m["id"]: m for m in tasks}
    records, paired = [], []

    def segment(kind, lane, started, ended=None, ids=(), result="running", failed=False,
                could_not_run=False, release_info=None):
        if lane not in rows or started is None or started > at or (ended is not None and ended < since):
            return None
        finish = min(at, ended if ended is not None else at)
        if finish < started:
            return None
        return {"kind": kind, "lane": lane, "started": started, "ended": ended,
                "start": max(since, started), "end": finish, "minutes": round((finish - started) / 60, 2),
                "running": ended is None, "failed": bool(failed), "could_not_run": bool(could_not_run),
                "result": result, "tasks": [{"id": tid, "title": meta.get(tid, {}).get("title", "")} for tid in ids],
                "release": release_info}

    def keep(*args, **kwargs):
        item = segment(*args, **kwargs)
        if item:
            records.append(item)

    for m in tasks:
        tid, lane = m["id"], m.get("lane")
        kind = "verification" if m.get("kind") == "verify" else "worker"
        for run in [*(m.get("attempts") or []), *(m.get("earlier_runs") or [])]:
            keep(kind, run.get("lane", lane), run.get("ran_from"), run.get("ended") or run.get("at"),
                 [tid], "failed", True)
        started, ended = m.get("started"), m.get("finished")
        if ended is not None or m.get("state") == "running":
            result = "running" if ended is None else (m.get("verdict") if kind == "verification" else "") or (
                "failed" if m.get("state") in ("failed", "queued", "cancelled") else "done")
            keep(kind, lane, started, ended, [tid], result, result in ("failed", "does not hold"))
        check = m.get("check") or {}
        check_start = m.get("check_started")
        if check_start:
            live = m.get("state") == "running" and m.get("phase") == "checking"
            keep("check", lane, check_start, None if live else check_start + check.get("seconds", 0), [tid],
                 "running" if live else "could_not_run" if check.get("could_not_run") else "passed" if check.get("ok") else "failed",
                 not live and check.get("ok") is False, check.get("could_not_run", False))
        mc = m.get("merge_check") or {}
        if mc.get("phase") in ("starting", "running"):
            keep("merge", mc.get("lane") or lane, mc.get("started"), ids=mc.get("members") or [tid])
        result = m.get("merge_check_result") or {}
        if result.get("at") and result.get("seconds") is not None and not result.get("waited"):
            keep("merge", result.get("lane") or lane, result["at"] - result["seconds"], result["at"],
                 result.get("batch") or [tid], "passed" if result.get("ok") else "failed", not result.get("ok"))
    for r in releases:
        keep("release", r.get("lane"), r.get("started"), r.get("ended"),
             result="running" if r.get("ended") is None else r.get("state", "done"),
             failed=r.get("state") in ("failed", "stopped"), release_info={"id": r["id"], "target": r.get("target") or "default"})
    for lane, trial in tries.items():
        keep("try", lane, trial.get("started"), trial.get("ended"),
             result="running" if trial.get("phase") == "running" else "passed" if trial.get("ok") else "failed",
             failed=trial.get("phase") != "running" and not trial.get("ok"))

    def key(item):
        return item["kind"], (item["tasks"][0]["id"] if item["tasks"] else
                              item["release"]["id"] if item["release"] else item["lane"])

    record_keys = {}
    for item in records:
        record_keys.setdefault(key(item), []).append(item)

    def recovered(item):
        # A server may save an end before it can append its event (or save a lost run in earlier_runs).
        saved = next((r for r in record_keys.get(key(item), []) if r["ended"] is not None
                      and (abs(r["started"] - item["started"]) < 2 or
                           r["started"] <= item["started"] < r["ended"])), None)
        if saved is None:
            return item
        return {**saved, "started": item["started"], "start": item["start"], "lane": item["lane"],
                "minutes": round((saved["end"] - item["started"]) / 60, 2)}

    active = {key(s): s for s in records if s["started"] < since}
    starts = {"task_started": "worker", "check_started": "check", "merge_check_started": "merge",
              "lane_try_started": "try", "release_started": "release"}
    ends = {"task_finished": "worker", "task_checked": "check", "check_killed": "check",
            "merge_checked": "merge", "merge_check_ended": "merge", "merge_check_killed": "merge",
            "lane_tried": "try", "release_ended": "release", "release_stopped": "release"}
    for event in sorted(events, key=lambda e: e["ts"]):
        ts, name = event["ts"], event.get("event")
        if ts > at or name not in starts and name not in ends:
            continue
        kind = starts.get(name) or ends[name]
        tid = event.get("task")
        m = meta.get(tid, {})
        if kind == "worker" and m.get("kind") == "verify":
            kind = "verification"
        identity = (kind, tid if tid else event.get("id") if kind == "release" else event.get("lane"))
        if name in starts:
            ids = event.get("members") or ([tid] if tid else [])
            item = segment(kind, event.get("lane") or m.get("lane"), ts, ids=ids,
                           release_info={"id": event["id"], "target": event.get("target") or "default"} if kind == "release" else None)
            if item:
                previous = active.pop(identity, None)
                if previous and ts >= since:
                    previous = recovered(previous)
                    if previous["running"]:
                        previous = {**previous, "ended": ts, "end": ts, "running": False, "failed": True,
                                    "result": "failed", "minutes": round((ts - previous["started"]) / 60, 2)}
                    paired.append(previous)
                active[identity] = item
            continue
        item = active.pop(identity, None)
        # A check refused before launch has no check_started. Keep its labelled, zero-duration marker.
        if item is None and kind == "check" and name == "task_checked":
            item = segment(kind, event.get("lane") or m.get("lane"), ts, ids=[tid])
        if item is None:
            continue
        result = event.get("state") or ("passed" if event.get("ok") else "failed")
        if kind in ("worker", "verification"):
            result = "done" if result == "checking" else result
            if kind == "verification" and abs((m.get("finished") or 0) - ts) < 2:
                result = m.get("verdict") or result
        cannot = bool(event.get("could_not_run")) or (kind == "check" and "could not" in event.get("note", ""))
        if cannot:
            result = "could_not_run"
        if name == "release_stopped":
            result = "stopped"
        step_failed = kind == "try" and any(isinstance(event.get(step), dict) and event[step].get("exit") not in (0, None)
                                            for step in ("setup", "check", "merge_check"))
        if step_failed:
            result = "failed"
        item = {**item, "ended": ts, "end": min(ts, at), "running": False,
                "minutes": round((ts - item["started"]) / 60, 2), "result": result,
                "could_not_run": cannot, "failed": event.get("ok") is False or
                result in ("failed", "cancelled", "queued", "stopped", "does not hold")}
        paired.append(item)
    paired.extend(recovered(item) for item in active.values())
    # Prefer event boundaries to records (whose finished/state may later describe cancellation or merging).
    by_key = {}
    for item in paired:
        by_key.setdefault(key(item), []).append(item)
    for item in records:
        if not any(abs(other["started"] - item["started"]) < 2 or
                   (other["started"] < (item["ended"] or at) and item["started"] < (other["ended"] or at)
                    and other["started"] != other["ended"])
                   for other in by_key.get(key(item), [])):
            paired.append(item)
    seen = set()
    for item in sorted(paired, key=lambda s: s["started"]):
        signature = (key(item), item["started"], item["ended"])
        if item["end"] >= since and signature not in seen:
            rows[item["lane"]].append(item)
            seen.add(signature)
    return {"start": since, "end": at, "lanes": [{"lane": lane, "segments": segments} for lane, segments in rows.items()]}


def timeline(desk: Desk, *, at: float | None = None) -> dict:
    at = time.time() if at is None else at
    lanes = desk.settings()["lanes"]
    trials = {lane: read_json(desk.state_dir / "tries" / f"{lane}.json", {}) or {} for lane in lanes}
    tasks, releases = desk.tasks(), release.releases(desk)
    events = timeline_events(desk.events_path, at - 86400, tasks=tasks, releases=releases, tries=trials)
    return timeline_data(lanes, tasks, events, releases, trials, at)


def desks() -> dict:
    """The account's desks, counts only; no status side effects or private text in the result."""
    problem = desks_list_problem()
    rows = []
    for root, _ in listed_desks(include_missing=True) if not problem else []:
        row = {"desk": str(root)}
        rows.append(row)
        try:
            if not root.exists():
                row["problem"] = "folder is gone"
                continue
            desk = Desk(root)
            desk.settings_path.read_text(encoding="utf-8")
            if not desk.tasks_dir.is_dir():
                raise OSError
            tasks, server = desk.tasks(), server_state(desk)
            host, port = web_address(desk)
            host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
            # none before its server first served the page: the default port may be another desk's
            served = (desk.state_dir / "web-port").exists()
            code = read_json(desk.state_dir / "serve-code", {}) or {}
            row.update(desk_name=alert.desk_name(desk), server=server["state"],
                       address=f"http://{'[' + host + ']' if ':' in host else host}:{port}/" if served else None,
                       version=code.get("build") if server["state"] != "gone" and code.get("pid") == server["pid"] else None,
                       running=sum(m.get("state") == "running" for m in tasks),
                       waiting=sum(m.get("state") == "queued" for m in tasks),
                       review=sum(m.get("state") == "done" and m.get("kind") != "verify" for m in tasks),
                       releases=sum(r.get("state") == "waiting" for r in release.releases(desk)),
                       todos=sum(not a.get("closed") for a in desk.asks()))
        except (OSError, UnicodeError, SettingsError):
            row.clear()
            row.update(desk=str(root), problem="folder cannot be read as a desk")
    return {"desks": rows, "problem": problem}


def desks_text(st: dict) -> str:
    blocks = []
    for row in st["desks"]:
        if row.get("problem"):
            blocks.append(f"desk {row['desk']}: {row['problem']}"
                          + (f"; to take it off the list: hansa desks --forget {shlex.quote(row['desk'])}"
                             if row["problem"] == "folder is gone" else ""))
            continue
        server = "not running" if row["server"] == "gone" else f"running ({row['server']})"
        blocks.append(f"desk {row['desk_name']} ({row['desk']})\n  server: {server}; hansa {row['version'] or 'unknown'}\n"
                      f"  page: {row['address'] or 'no address: not started'}\n"
                      f"  tasks: {row['running']} running, {row['waiting']} waiting, {row['review']} waiting for review\n"
                      f"  principal: {row['releases']} releases, {row['todos']} to-dos waiting")
    return st["problem"] or "\n\n".join(blocks) or "No desks listed. Make one with: hansa init DIR"


def status(desk: Desk) -> dict:
    s = desk.settings()
    tasks = desk.tasks()
    pst = desk.provider_state()
    ready = ready_now(desk, s)
    running = [m for m in tasks if m["state"] == "running"]
    merging = [m for m in tasks if m["state"] == "merging"]
    failed = [m for m in tasks if m["state"] == "failed" and not m.get("closed")]
    tried = tried_lanes(desk)
    busy = {occupied_lane(m) for m in tasks if lane_occupied(m)} | tried
    held = sandbox_held(desk)
    last = last_round(desk)
    server = server_state(desk)
    seconds_ago = server["quiet"]
    if seconds_ago is None and last:
        seconds_ago = int(time.time() - last)
    day = time.time() - 86400  # the last 24 h, each thing by when it happened: not reset at midnight
    rows = cost.worker_rows(desk, day, s, tasks)  # by when the run ended
    no_read = no_read_parts_shown(s, desk.root)
    queued = [m for m in tasks if m["state"] == "queued"]
    looks = waiting_looks(desk, s, tasks, server) if queued else None  # once for all of them
    from . import page_messages
    return page_messages.status(desk, s, {
        "desk": str(desk.root),
        "desk_name": alert.desk_name(desk),
        "build": build(),
        "installed": installed(),  # when the code this runs was put in place
        "serve_code": serve_code(desk, server),  # hansa serve on older code than is installed: what to do
        "serve_session": serve_session(desk, server),  # hansa serve started inside a Claude Code session: what it dropped
        "paused": desk.paused(),
        "server_up": server["state"] != "gone",  # alive, busy or not: run.server_state is the one rule
        "server": server,
        "page": page_of(desk, server),  # where the web page is, and whether it is served now
        "server_seconds_ago": seconds_ago,
        "srt_missing": srt_problem(desk, s),  # what stops setups and checks, as hansa serve last saw srt
        "srt": {**srt_record(desk), "needed": srt_needed(s)},
        "try_note": LINUX_TRY_NOTE if account.LINUX else "",  # what the sandbox try leaves to hansa's checks there
        "notifying": (hearing := notify.names(desk)),  # the sessions hansa posts to (hansa notify)
        # whether their turns' ends are recorded (hansa coordinator-turn), by the record of how each started
        "turns": [up.turn_line(desk, r.get("session") or {}, notify.who((r.get("session") or {}).get("pid"),
                                                                        r.get("name") or ""))
                  for r in notify.live_records(desk)] if hearing else [],
        "coordinator_start": "" if hearing else up.coordinator_line(desk),  # what hansa up does about the session
        "card_note": up.card_note(desk),  # the recorded session's folder does not load the card (the desk's CLAUDE.md)
        "unreached": alert.coordinator_lines(desk, s),  # the coordinator alert's cases that hold now
        "heard": notify.hears(desk),  # whether the session asking is one of them
        "mod": (mod := plugin.status(desk)),  # what hansa did at the coordinator's start: the mod loaded or not, and why
        "mod_line": plugin.status_line(mod) if mod else "",
        "clear_line": plugin.clear_line(mod) if mod else "",  # its daily clear: the window, and the clears recorded
        "compact_line": plugin.settings_line(s) if mod and mod.get("kind") != "notify" else "",
        "project": project_file(desk),  # notes/PROJECT.md, which the card imports
        "verification_notes": sum(not n.get("mark") for n in gaps.all_notes(desk, tasks)),
        "goals": goals.summaries(desk, tasks),
        "release_codes": alert.release_codes(desk, s),  # command or enabled encrypted Bark
        "alert_failed": alert.failure_line(desk),  # the principal's alert command's last failed run, if any
        "alert_held": alert.hold_status(desk),
        "alert_push": alert_push.public(desk, s),
        "alert_push_problem": alert_push.migration_problem(desk),
        "alert_push_duplicates": alert_push.duplicate_names(desk, s),
        "alert_minutes": alert.minutes_line(desk, s),  # the coordinator alert's minutes, while an alert can go out
        "bad_messages": bad_messages(s),  # message forms written by hand that hansa cannot use
        "merge_targets": [why for n, ln in sorted(s["lanes"].items()) if (why := merge_targets_problem(n, ln))],
        "shared_writes": shared_writes(desk.root, s),  # write-list folders another listed desk's lanes write too
        "desks_stop": desks_list_problem(),  # the account's list of desks cannot be read: no run starts
        # entries of what workers may not read that stop every run, and those left out of runs (desk.no_read_parts)
        "no_read_stop": stop_text(no_read[2]), "no_read_left_out": no_read[1],
        "concurrency": s["concurrency"],
        # free space: the desk's volume now, the floor, and the volumes for which hansa serve starts nothing new
        "disk": {"free": disk.desk_free(desk)[0], "floor": disk.floor(s), "line": disk.desk_line(desk, s),
                 "low": (low := disk.held(desk)),
                 "low_text": disk.held_text(desk, low, disk.floor(s)) if low else "",
                 "unknown": disk.unknown_lines(desk)},  # what its look could not measure, and what was known
        "memory": {"floor": memory.floor(s), "line": memory.desk_line(desk, s), "low": memory.held(desk),
                   "low_text": memory.held_text(desk) if memory.held(desk) else "",
                   "unknown": memory.unknown_lines(desk)},
        "lanes": [{"name": n, "providers": ln["providers"], "repo": ln["repo"],
                   "timeout_min": ln.get("timeout_min", 240),
                   "task": next((m["id"] for m in tasks if lane_occupied(m) and occupied_lane(m) == n), None),
                   **({"merge_lane": True, "merge_for": merge_served_lanes(s, n)}
                      if ln.get("merge_lane") else {}),
                   **({"verify_for": ln["verify_for"]} if ln.get("verify_for") else {}),
                   "trying": n in tried,  # a `hansa lane try` queued or running holds the lane
                   "held": held.get(n),  # its runs whose sandbox could not start, by group (run.sandbox_held)
                   # the lane's holder failed while hansa could not look for its worker: why it still waits
                   "waiting": next((lane_wait_text(m) for m in tasks
                                    if lane_occupied(m) and occupied_lane(m) == n), "")}
                  for n, ln in sorted(s["lanes"].items())],
        "providers": [{"name": n, "kind": p["kind"], "model": p["model"], "effort": p.get("effort", ""),
                       "rest_minutes": s.get("cool_minutes", 30),
                       "credit_since": pst.get(n, {}).get("credit_since", 0),
                       "model_since": pst.get(n, {}).get("model_since", 0),
                       "automatic_check": quota_probe_text(pst.get(n, {}), s),
                       "problem": provider_status_problem(desk, s, n, tasks), "check": pst.get(n, {}).get("check")}
                      for n, p in sorted(s["providers"].items())],
        "running": [{"id": m["id"], "title": m["title"], "lane": m.get("lane"), "provider": m.get("provider"),
                     "cancel": bool(m.get("cancel")),
                     "model": m.get("model_asked"), "minutes": _mins(m.get("started")),
                     "limit_min": s["lanes"].get(m.get("lane"), {}).get("timeout_min", 240),
                     "effort": m.get("effort_used", ""), "effort_from": m.get("effort_from"),
                     "effort_said": effort_text(m.get("effort_used", ""), m.get("effort_from"),
                                                s["providers"].get(m.get("provider") or "")),
                     "checking": m.get("phase") == "checking", "starting": m.get("phase") == "starting",
                     "check_minutes": _mins(m.get("check_started")) if m.get("phase") == "checking" else None,
                     "check_retry": bool(m.get("check_retry")),  # only retries reset the server's timeout start
                     **({"browser": m["browser"]} if "browser" in m else {}),
                     "progress": None if m.get("phase") in ("checking", "starting") else progress_of(desk, m)}
                    for m in running],
        "queued": [{"id": m["id"], "title": m["title"], "lane": m.get("lane") or "any",
                    **waiting_detail(desk, s, m, busy, looks), **killed_last(m)} for m in queued],
        "merging": [{"id": m["id"], "title": m["title"], "lane": merge_check_lane(s, m),
                     "members": (m.get("merge_check") or {}).get("members", []),
                     "check_task": (m.get("merge_check") or {}).get("leader", m["id"]),
                     **({"waiting_for": merge_lane_wait(desk, m, tasks)} if merge_check_lane(s, m) != m.get("lane") else {}),
                     "phase": (m.get("merge_check") or {}).get("phase", "queued"),
                     "minutes": _mins((m.get("merge_check") or {}).get("started")),
                     "limit_min": merge_minutes(s["lanes"].get(occupied_lane(m), {})),
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
        # failed work not put away (hansa close): the latest five, and how many there are in all
        "failed": [{"id": m["id"], "title": m["title"], "lane": m.get("lane"), "error": m.get("error", "")}
                   for m in failed][-5:],
        "failed_count": len(failed),
        "asks": [{**a, "note": ask_note(a, ready)} for a in desk.asks() if not a.get("closed")],
        # the open releases (hansa release) and the latest that ended: the page asks the principal about each waiting one
        "releases": release.page_rows(desk, s, tasks),
        "after_merge_waiting": release.after_merge_rows(desk, s, tasks),
        "last_24h": {"finished": sum((m.get("finished") or 0) >= day for m in tasks),
                     "merged": sum(m["state"] == "merged" and (m.get("merged") or 0) >= day for m in tasks),
                     "worker_tokens": sum(sum(r[k] for k in cost.TOKEN_FIELDS) for r in rows),
                     "worker_unpriced_tokens": sum(sum(r[k] for k in cost.TOKEN_FIELDS)
                                                   for r in rows if r["usd"] is None),
                     "worker_usd": round(sum(r["usd"] for r in rows if r["usd"] is not None), 2)},
    })


def todo_list(desk: Desk, page: int = 1, state: str = "") -> dict:
    """All to-dos, including old requests and their recorded answers; never trim history."""
    rows = sorted((a for a in desk.asks() if not state or bool(a.get("closed")) == (state == "closed")),
                  key=lambda a: (a.get("created") or 0, a["id"]), reverse=True)
    total = len(rows)
    pages = max(1, (total + 49) // 50)
    page = min(max(1, page), pages)
    return {"rows": rows[(page - 1) * 50:page * 50], "page": page, "pages": pages, "total": total}


ASKS_PAGE = 20  # closed to-dos hansa ask --closed shows at a time


def _ask_block(a: dict, goal_titles: dict) -> list:
    """One to-do in full, for hansa ask: its head line, its text and why, each line indented."""
    when = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"
    head = [f"#{a['id']}"]
    if a.get("closed"):
        head += ["work waited on it"] if a.get("holds") else []
        head.append(f"closed {when(a['closed'])}" + (" by the coordinator" if a.get("closed_by") == "coordinator" else ""))
    else:
        head.append("work waits on it" if a.get("holds") else "work goes on")
    head.append(f"asked {when(a.get('created'))}" + (f" by {a['by']}" if a.get("by") else ""))
    if a.get("goal"):
        title = goal_titles.get(a["goal"])
        head.append(f"goal {a['goal']}" + (f" ({title})" if title else ""))
    out = [" · ".join(head)]
    out += [f"  {line}" for line in (str(a.get("text") or "").splitlines() or [""])]
    if a.get("why"):
        out += [f"  why only the principal: {line}" if i == 0 else f"    {line}"
                for i, line in enumerate(str(a["why"]).splitlines())]
    if a.get("closed"):
        outcome = (a.get("reason") if a.get("closed_by") == "coordinator" else
                   f"answered on the page: {a['answer']}" if a.get("answer") else "none recorded")
        out += [f"  outcome: {line}" if i == 0 else f"    {line}" for i, line in enumerate(str(outcome).splitlines() or [""])]
    return out


def asks_text(desk: Desk, closed: bool = False, page: int = 1) -> str:
    """hansa ask with no text: the open to-dos in full, oldest first; with closed, the closed ones and their
    outcomes, newest first, ASKS_PAGE at a time."""
    try:
        goal_titles = {gid: g.get("title", "") for gid, g in goals.records(desk)[0].items()}
    except (OSError, KeyError, ValueError):
        goal_titles = {}
    asks = desk.asks()
    if not closed:
        rows = sorted((a for a in asks if not a.get("closed")), key=lambda a: (a.get("created") or 0, a["id"]))
        if not rows:
            return "no open to-dos for the principal (hansa ask --closed lists the closed ones)"
        out = [f"open to-dos for the principal ({len(rows)}), oldest first:"]
        for a in rows:
            out += _ask_block(a, goal_titles)
        out.append('close one with its outcome: hansa ask --close N "<outcome>"')
        return "\n".join(out)
    rows = sorted((a for a in asks if a.get("closed")), key=lambda a: (a.get("closed") or 0, a["id"]), reverse=True)
    if not rows:
        return "no closed to-dos yet (hansa ask lists the open ones)"
    pages = (len(rows) + ASKS_PAGE - 1) // ASKS_PAGE
    if not 1 <= page <= pages:
        raise SystemExit(f"hansa: there is no page {page} of closed to-dos; there {'is' if pages == 1 else 'are'} "
                         f"{pages} ({len(rows)} closed to-dos, {ASKS_PAGE} a page)")
    out = [f"closed to-dos, newest first: page {page} of {pages}, {len(rows)} in all"
           + (f" (next: hansa ask --closed --page {page + 1})" if page < pages else "") + ":"]
    for a in rows[(page - 1) * ASKS_PAGE:page * ASKS_PAGE]:
        out += _ask_block(a, goal_titles)
    return "\n".join(out)


def status_text(st: dict) -> str:
    out = [f"desk {st['desk_name']} ({st['desk']})",
           ("PAUSED: " + st["paused"]) if st["paused"] else f"dispatch on, {st['concurrency']} at a time",
           server_text(st.get("server") or {"state": "up" if st["server_up"] else "gone"})]
    out = [" · ".join(out + [f"hansa {st['build']}", installed_text(st.get("installed") or 0) or "installed ?",
                              time.strftime("now %H:%M")])]
    out += [st["serve_code"]] if st.get("serve_code") else []  # what asks for action first, then information
    out += [st["serve_session"]] if st.get("serve_session") else []
    out += [goals.summary_text(g) for g in st.get("goals", [])[:5]]
    if len(st.get("goals", [])) > 5:
        out += [f"and {len(st['goals']) - 5} more: hansa goal list"]
    out += [st["srt_missing"]] if st.get("srt_missing") else []
    out += [st["alert_failed"]] if st.get("alert_failed") else []
    if hold := st.get("alert_held"):
        out.append(f"{hold['count']} alert{'s' if hold['count'] != 1 else ''} held by the hourly limit until "
                   + time.strftime("%Y-%m-%d %H:%M", time.localtime(hold["until"])))
    out += [st["alert_push_problem"]] if st.get("alert_push_problem") else []
    for names in st.get("alert_push_duplicates", []):
        out.append("Push entries " + ", ".join(names) + " share a push address; remove one on the page (Settings, Alerts)")
    for entry in st.get("alert_push", []):
        last = entry["last"]
        encryption = alert_push.encryption_text(entry)
        out.append(f"Push {entry['service']} {entry['name'] or entry['id']}: {entry['address'] or 'no address'}"
                   f" · {'on' if entry['on'] else 'off'}"
                   + (" · " + encryption if encryption else "")
                   + (f" · last {'ok' if last['ok'] else 'failed'}: {last['why']} at "
                      + time.strftime("%Y-%m-%d %H:%M", time.localtime(last["at"]))
                      + (f" · code {last['code']}" if "code" in last else "") if last else " · no push yet"))
        if last and entry["service"] == "discord" and last["why"] == "http_4xx" and last.get("code") == 10015:
            out[-1] += " · this webhook address no longer exists; paste a new address on the page (Settings, Alerts)"
        if last and last["why"] == "bad_key" and not entry.get("encryption_invalid"):
            out[-1] += (" · this entry's encryption key is not valid; nothing was sent: set it again on the page "
                        "(Settings, Alerts)")
    out += [st["alert_minutes"]] if st.get("alert_minutes") else []
    out += [st["disk"]["low_text"] + ". More: hansa help free-space"] if (st.get("disk") or {}).get("low_text") else []
    out += (st.get("disk") or {}).get("unknown") or []
    out += [st["memory"]["low_text"]] if (st.get("memory") or {}).get("low_text") else []
    out += (st.get("memory") or {}).get("unknown") or []
    out += [f"WARNING: {x}" for x in st.get("bad_messages") or []]
    out += [f"WARNING: {x}" for x in st.get("merge_targets") or []]
    out += [f"WARNING: {x}" for x in st.get("shared_writes") or []]
    out += [f"STOPPED: {st['no_read_stop']}"] if st.get("no_read_stop") else []
    out += [f"STOPPED: {st['desks_stop']}"] if st.get("desks_stop") else []
    out += [f"what workers may not read, entry left out: {x.removeprefix(DENY_AT)}"
            for x in st.get("no_read_left_out") or []]
    out += [page_text(st["page"])] if st.get("page") else []
    out += [st["disk"]["line"]] if st.get("disk") else []
    out += [st["memory"]["line"]] if st.get("memory") else []
    out.append(("hansa posts to: " + ", ".join(st["notifying"]) if st.get("notifying") else
                "hansa posts to no session (hansa notify on, from the coordinator's session)")
               + (f" · {st['card_note']}" if st.get("card_note") else ""))
    out += [st["coordinator_start"]] if st.get("coordinator_start") and not st.get("notifying") else []
    out += st.get("turns") or []
    out += st.get("unreached") or []
    out += [x for x in (f"{st['mod_line']} {plugin.HELP}", st.get("clear_line"), st["compact_line"]) if x] \
        if st.get("mod_line") else []
    out += [project_text(st["project"])] if st.get("project") else []
    if st.get("verification_notes"):
        out.append(f"verification notes: {st['verification_notes']} not marked (hansa gaps)")
    out += [f"sandbox probe: {st['try_note']}"] if st.get("try_note") else []
    out.append("lanes:")
    lanes = st["lanes"] or [{"name": "(none)", "providers": [], "task": None}]
    wn, wp = max(len(ln["name"]) for ln in lanes), max(len(" > ".join(ln["providers"])) for ln in lanes)
    for ln in lanes:
        out.append(f"  {ln['name']:<{wn}}  {' > '.join(ln['providers']):<{wp}}  "
                   + (ln["task"] or ("held by a try (hansa lane try)" if ln.get("trying") else
                                     "UNABLE TO RUN" if {"work", "start"} & set(ln.get("held") or {}) else "free"))
                   + (f" · waiting: {ln['waiting']}" if ln.get("waiting") else "")
                   + (f" · merge lane for lanes {', '.join(ln['merge_for']) or '(none yet)'}; takes no work"
                      if ln.get("merge_lane") else "")
                   + (f" · verify lane for lanes {', '.join(ln['verify_for'])}; takes only verifications"
                      if ln.get("verify_for") else "")
                   + "".join(f"\n      {sandbox_held_text(h, short=True, lane=ln['name'])}" for h in (ln.get("held") or {}).values())
                   + ("\n      More: hansa help sandbox-probe" if "work" in (ln.get("held") or {}) else ""))
    if st["running"]:
        out.append("running:")
        out += [f"  {m['id']} {m['lane']} " + (f"worker done, hansa is running the lane's check, "
                                               f"{m['check_minutes']} min so far" if m["checking"]
                                               else "hansa is preparing it" if m.get("starting")
                                               else f"on {m['provider']} ({m['model']}"
                                               + (f", {m['effort_said']}" if m.get("effort_said") else "") + ")")
                + (" with a browser" if m.get("browser") else "")
                + (f" ({m['minutes']} min in all)" if m["checking"] else f", {m['minutes']} min") + f": {m['title']}"
                + (" · cancel requested" if m.get("cancel") else "")
                + (f"\n      so far: {_progress_line(m['progress'])}" if m.get("progress") else "")
                for m in st["running"]]
    if st["queued"]:
        out.append("queued:")
        out += [f"  {m['id']} lane {m['lane']}: {m['title']}\n      waiting: {m['waiting']}"
                + (f"\n      last run: {m['last_run']}" if m.get("last_run") else "") for m in st["queued"]]
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
        more = st.get("failed_count", 0) - len(st["failed"])
        if more > 0:
            out.append(f"  ... and {more} more failed, not closed (hansa close ID puts one away)")
    out.append("providers:")
    wn, wm = (max((len(p[k]) for p in st["providers"]), default=0) for k in ("name", "model"))
    rests = [rest_words(p) for p in st["providers"]]
    wr = max(map(len, rests), default=0)
    for p, rest in zip(st["providers"], rests):
        out.append(f"  {p['name']:<{wn}}  {p['kind']:<12}  {p['model']:<{wm}}  {rest:<{wr}}  {p['problem'] or 'ready'}"
                   + (f"; {p['automatic_check']}" if p.get("automatic_check") else ""))
    shown_releases = [r for r in st.get("releases") or [] if r["state"] in release.OPEN
                      or any(h.get("kind") == "pattern_limit" for h in r.get("hits") or [])]
    if shown_releases or st.get("after_merge_waiting"):
        out.append("releases (hansa release):")
        out += [f"  {r['what']}" for r in st.get("after_merge_waiting") or []]
        out += [f"  #{r['id']} {release.what(r)} at {(r['commit'] or '')[:10]}: {r['what']}" for r in shown_releases]
        waiting = [r for r in st["releases"] if r["state"] == "waiting"]
        if waiting:
            # Follow these releases, not the set-up: a code that failed to be made or was voided answers nothing.
            out.append("  the principal answers on the page, or says the one-time code of their alert in the "
                       "conversation: pass it with hansa release yes|no ID CODE"
                       if any(r.get("code_live") for r in waiting) else
                       "  the principal answers on the page: no waiting release has a live one-time code"
                       if st.get("release_codes") else
                       "  the principal answers on the page only: no alert command is set and no enabled Bark entry "
                       "encrypts, so no code is sent")
    if st["asks"]:
        out.append("open to-dos for the principal (hansa ask reads them whole):")
        out += [f"  #{a['id']}{' (work waits on it)' if a.get('holds') else ''}: {' '.join(a['text'].splitlines())[:100]}"
                + (f" · {a['note']}" if a.get("note") else "") for a in st["asks"]]
    t = st["last_24h"]
    unpriced = t.get("worker_unpriced_tokens", 0)
    worth = f"worth ${t['worker_usd']:.2f}; " if not unpriced or unpriced < t["worker_tokens"] else ""
    if unpriced:
        worth += f"{unpriced:,} tokens have no price yet; the principal sets prices on the page; "
    out.append(f"last 24 h: {t['finished']} finished, {t['merged']} merged; workers used {t['worker_tokens']:,} tokens"
               f" ({worth}hansa cost for everything)")
    return "\n".join(out)


def provider_status_problem(desk: Desk, s: dict, name: str, tasks: list) -> str:
    problem = provider_problem(desk, s, name)
    if not (desk.provider_state().get(name) or {}).get("unreachable_since"):
        return problem
    waiting = providers_waited_for(desk, s, tasks).get(name, 0)
    return problem + f"; {waiting} {'task waits' if waiting == 1 else 'tasks wait'} for it"


def rest_words(p: dict) -> str:
    """A provider's rest after a usage limit whose error gives no reset time, for hansa status."""
    if p.get("credit_since") or p.get("model_since") or p.get("rest_minutes") is None:
        return ""
    return f"rest {p['rest_minutes']} min" + " (desk)"


def limit_wait(desk: Desk, m: dict) -> str:
    """For hansa show: the line of a queued task whose last run ended on its provider's usage limit while that
    provider still rests; '' otherwise."""
    last = (m.get("attempts") or [{}])[-1]
    if m.get("state") != "queued" or last.get("kind") not in ("limit", "credit", "model"):
        return ""
    st = desk.provider_state().get(last["provider"]) or {}
    if st.get("model_since") and not st.get("credit_since"):
        return f"{last['provider']}: {model_text(st['model_unknown'])}; that return used no try"
    if st.get("credit_since"):
        return (f"{last['provider']}: {credit_text(st)}; it starts when a provider of its lane is ready. "
                "That return used no try. More: hansa help provider-limits")
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


def check_end_lines(record: dict, what: str) -> list:
    from .run import check_retry_reason, exit_text
    out = oom_lines(record, what)
    first = record.get("retried") or (record if record.get("rerun_started") else None)
    if first:
        out += [f"{what} ran again once: {check_retry_reason(first)} ({exit_text(first)}); "
                "its last lines:", *["  " + x for x in first.get("tail", [])]]
    else:
        first = record.get("previous") or record
        if first.get("oom_kills") and first.get("note") == check_retry_reason(first):
            out += [f"{what}'s first run: {check_retry_reason(first)} ({exit_text(first)}); its last lines:",
                    *["  " + x for x in first.get("tail", [])]]
    return out


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
                                                 else ""), desk.tasks_dir / r.get("output_task", m["id"]) / merge_log(m)))
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
        result = m.get("check" if what == "check" else "merge_check_result") or {}
        out += ["   " + x for x in check_end_lines(result, what)]
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


EFFORT_FROM = {"task": "the task's --effort", "provider": "the provider's default", "coordinator": "the coordinator's setting"}


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
    said = ", ".join(x for x in (m.get("model_asked", m.get("model", "")), e) if x)
    return f"{m['provider']}" + (f" ({said})" if said else "")  # none yet: a task that never started


def task_page(desk: Desk, tid: str) -> dict:
    from . import page_messages
    meta = page_messages.annotate(desk.meta(tid))
    for key in ("attempts", "earlier_runs"):
        meta[key] = [page_messages.annotate(r) for r in meta.get(key, [])]
    messages = {}
    text = show_text(desk, tid, full=True, page_messages=messages)
    return {"meta": meta, "text": text, "text_parts": messages["parts"]}


def show_text(desk: Desk, tid: str, full: bool = False, heard=None, page_messages=None) -> str:
    """hansa show. full: the whole text and report. heard: whether the session it is for hears from hansa
    (default: the session this command runs in)."""
    from .page_messages import describe, message
    m = as_closed_now(desk.meta(tid))
    m["markers"] = markers_of(desk, m)
    d = desk.tasks_dir / tid
    cancelling = m["state"] == "running" and m.get("cancel")
    head = f"{tid} · {m['state']} · lane {m.get('lane') or 'any'}"
    if cancelling:
        head += " · cancel requested"
    if m.get("kind") and m["kind"] not in ("task", "verify", "work"):
        head += " · " + str(m["kind"])
    if m.get("provider"):
        head += (f" · {run_text(m, desk.settings()['providers'].get(m['provider']))}"
                 + (f" · {m['turns']} turns" if m.get("turns") else ""))
    if m.get("started"):
        head += f" · {max(0, int(((m.get('finished') or time.time()) - m['started']) // 60))} min"
    if m.get("browser"):
        head += " · with a browser"
    waiting = {}
    if m["state"] == "queued":
        s, states = desk.settings(), desk.provider_state()
        tasks = desk.tasks()
        busy = {occupied_lane(t) for t in tasks if lane_occupied(t)} | tried_lanes(desk)
        waiting = waiting_detail(desk, s, m, busy, waiting_looks(desk, s, tasks))
    verifying = m.get("kind") == "verify"
    meaning = (f"a verification of {m.get('parent')}: a worker ran its change to see whether it holds; never merged"
               if verifying else STATES.get(m["state"], ""))
    if verifying:
        verified_task = read_json(desk.tasks_dir / str(m.get("parent")) / "meta.json", {}) or {}
        own = verified_task.get("lane") or ""
        on = "runs" if m["state"] in ("queued", "running") else "ran"
        meaning += (f"; it {on} on lane {m.get('lane') or 'any'}"
                    + (f", not {m['parent']}'s own lane {own}" if own and own != m.get("lane") else ""))
    elif waiting.get("waiting_code") == "after":
        meaning = waiting["waiting"]
    if m["state"] == "running" and m.get("setup_ran") and "setup_min" in (progress := progress_of(desk, m)):
        meaning = _progress_line(progress)
    if cancelling:
        meaning = "cancel requested; hansa serve will end it as cancelled within seconds"
    out = [head, f"title: {m['title']}", *task_text_lines(desk, m, full), f"meaning: {meaning}"]
    options = []
    if m.get("goal"):
        goal = goals.records(desk)[0].get(m["goal"], {})
        options.append(f"goal {m['goal']}" + (f" ({goals.one_line(goal['title'])})" if goal.get("title") else ""))
    options += [f"{label} {m[key]}" for key, label in (("max_turns", "max turns"), ("model", "model"), ("effort", "effort"))
                if m.get(key)]
    if options:
        out.append("task options: " + " · ".join(options))
    out += [f"attachment: {a['name']} ({a['size']} bytes)" for a in m.get("attachments", [])]
    if m.get("verify_after"):
        request = m["verify_after"]
        out.append("verification requested: after its check passes (or it has no check), if it has changes"
                   + (f"; lane {request['lane']}" if request.get("lane") else "")
                   + (f"; effort {request['effort']}" if request.get("effort") else "")
                   + (f"; look at: {request['note']}" if request.get("note") else ""))
    if m.get("verify_after_error"):
        out.append(f"verification could not be queued: {m['verify_after_error']}")
    out += [x for x in (rounds_line(desk, m), "" if verifying else rethink_text(desk, m)) if x]
    inherited = m.get("inherited_refusals", {})
    order = desk.settings()["lanes"].get(m.get("lane"), {}).get("providers", [])
    skipped = sorted(set(order) & refused_by({"inherited_refusals": inherited}, order))
    if skipped:
        out.append("providers left out: " + ", ".join(f"{p} (refused {inherited[p]})" for p in skipped)
                   + "; no follow-up try used")
    if m.get("error"):
        if page_messages is not None:
            page_messages[str(len(out))] = message("task_error", f"error: {m['error']}", reason=describe(m["error"]))
        out.append(f"error: {m['error']}")
    if m.get("failed_error") is not None and m["state"] == "cancelled":  # closed after it failed (hansa close)
        out.append(f"it had failed before it was closed: {m['failed_error'] or 'no error was recorded'}")
    out += [f"message: {m[k]}" for k in ("commit_message_note", "merge_message_note") if m.get(k)]
    out += [disk.trimmed_text(m)] if disk.trimmed_text(m) else []
    def git_words(lead, text):  # git's words, its first line after `lead`, the rest indented under it
        said = [x for x in desk.redact(text).splitlines() if x.strip()] or [""]
        return [f"{lead}{said[0]}", *("  " + x for x in said[1:])]

    if m.get("git_error"):  # what git said when hansa recorded the run's work (run.finish)
        out += git_words("git said: ", m["git_error"])
    if m.get("kept_ref"):
        out.append(f"the work hansa could not commit is kept: hansa git {m['lane']} show {m['kept_ref']}")
    elif m.get("kept_error"):
        out += git_words("the work hansa could not commit could not be kept aside either; it is still in the "
                         "worktree, and the lane's next task sets it aside in git stash: ", m["kept_error"])
    if verifying and m.get("scratch_ref"):
        out.append(f"left scratch changes: hansa git {m['lane']} show {m['scratch_ref']}")
    elif verifying and m.get("scratch_error"):
        out += git_words("its scratch changes could not be kept (the verdict stands): ", m["scratch_error"])
    left = m.get("leftovers") or {}
    if left.get("stash"):
        at = left["stash"][:12]
        out.append("set aside when it started: what the worktree held"
                   + (f", left after {left['after']} (the last task that ran there)," if left.get("after") else ",")
                   + f" is in git stash as {at} (hansa: left over before {tid}); its changes: "
                   + f"hansa git {m['lane']} diff {at}^ {at}"
                   + (f"; its new files: hansa git {m['lane']} show {at}^3" if left.get("untracked") else ""))
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

    for index, a in enumerate(m.get("attempts", [])):
        # Requeued attempts keep their end and numbers; the final attempt's numbers stay on the task.
        current = m["state"] == "failed" and index == len(m["attempts"]) - 1 and not a.get("ended")
        when = "last" if current else "earlier"
        on = a["provider"]  # with its model and effort when they were not those of the run above
        if a.get("model") and (a["provider"], a["model"], a.get("effort", ""), a.get("effort_from")) != (
                m.get("provider"), m.get("model_asked"), m.get("effort_used", ""), m.get("effort_from")):
            on = run_text(a, desk.settings()["providers"].get(a["provider"]))
        out.append(f"{when} run on {on}: {a['error']} ({a['kind']}; no try used)" if a.get("kind") in ("credit", "model") else
                   f"{when} try on {on}: {a['error']}" if a.get("kind") not in ("limit", "killed") else
                   f"{when} run on {on}: {a['error']}; what it reported, if anything, is not kept"
                   if a.get("kind") == "killed" else  # it used no try
                   f"{when} run on {on}: {a['error']} (its usage limit; "
                   + ("counted as a try)" if a.get("counted") else "no try used)"))
        if page_messages is not None:
            page_messages[str(len(out) - 1)] = message("task_prior", out[-1], provider=on, reason=describe(a["error"]),
                                                     kind=a.get("kind"), counted=bool(a.get("counted")))
        if a.get("kind") == "credit" and (m["state"] != "queued" or a != m["attempts"][-1]):
            if resting := credit_text(desk.provider_state().get(a["provider"]) or {}):
                out.append(f"  {a['provider']}: {resting}")
        if a.get("turns_unknown_after_resume"):  # run.finish: the system killed the run hansa resumed
            out.append("  its turns after hansa resumed it are unknown: the system killed it before it said")
        out += ["  " + x for x in oom_lines(a, f"{when} run")]
        start = a.get("run_start") or (m.get("run_start") if current else None)
        if (not start and a.get("kind") in ("network", "limit", "credit") and m.get("base")
                and m.get("kind") != "verify"):
            start = {"commit": m.get("start_from") or m["base"],
                     "source": "earlier task's commit" if m.get("start_from") else "base"}
        if start:
            out.append(f"  started from: {started_from(start)}")
    if m["state"] == "queued" and m.get("restart"):
        out.append(f"next run starts from: {started_from(m['restart'])}")
    elif m["state"] != "queued" and m.get("run_start"):
        out.append(f"run started from: {started_from(m['run_start'])}")
    out += oom_lines(m, {"verify": "verification"}.get(m.get("kind"), "worker"))
    if limit_wait(desk, m):
        out.append(limit_wait(desk, m))
    if m["state"] == "queued":
        if page_messages is not None:
            page_messages[str(len(out))] = message("task_wait", "waits: " + waiting["waiting"], waiting=waiting)
        out.append("waits: " + waiting["waiting"])
        names = {p for ln in ([m["lane"]] if m.get("lane") else s["lanes"]) if ln in s["lanes"]
                 for p in task_providers(s, ln, m)}
        out += [f"{p}: {provider_problem(desk, s, p)}" for p in sorted(names)
                if states.get(p, {}).get("shared_rest")]
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
    if m.get("preview"):
        out.append("preview: " + json.dumps(m["preview"], ensure_ascii=False))
    chk = m.get("check")
    if chk and chk.get("could_not_run"):
        out.append(f"check (run by hansa, not the worker): {chk['note']}: {chk['command']}")
        out.append("  " + machine_hint(chk))
    elif chk:
        how = ("passed" if chk["ok"] else "did not finish" if check_interruption(chk) == "timeout" else "FAILED")
        if not chk["ok"]:
            how += f" ({chk['note']})" if chk["note"] else f" (exit {chk['exit']})"
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
    out += check_end_lines(m.get("check") or {}, "check")
    batch_result = m.get("merge_check_result") or {}
    if batch_result.get("batch"):
        owner = batch_result["output_task"]
        out.append(f"checked with: {', '.join(x for x in batch_result['batch'] if x != tid)}; "
                   f"{owner}'s check; output: hansa show {owner} --merge-check-log "
                   f"({desk.tasks_dir / owner / merge_log(m)})")
    out += check_end_lines(m.get("merge_check_result") or {}, "merge check")
    if m["state"] == "merged" and m.get("unverified"):
        out.append(f"note: {m['unverified']}")
    if m["state"] == "running" and m.get("phase") == "starting":
        out.append("so far: hansa is preparing it; the worker starts in a moment")
    elif m["state"] == "running" and m.get("phase") != "checking":
        out.append(f"so far: {_progress_line(progress_of(desk, m))} (turns and tokens are recorded when it ends)")
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
    s = desk.settings()
    check_next = interruption_next(s, m, chk or {})
    review = (f"hansa merge {tid}" if v and v["verdict"] in HOLDING_VERDICTS
              else f"hansa diff {tid} --stat, then hansa verify {tid} (code or tests) or hansa merge {tid} (text only)")
    nxt = {"done": f"hansa retry {tid} \"what to change\" · hansa close {tid} to close it" if not m.get("commits")
           else f"{review} · hansa retry {tid} \"what to change\"" if not chk or chk["ok"]
           else check_next if check_next
           else (f"{recheck_step(chk, tid)} (runs the check "
                 f"again and merges if it passes) · hansa retry {tid} \"make the check pass: ...\" · "
                 f"hansa merge {tid} --despite-check") if machine_hint(chk)
           else f"hansa retry {tid} \"make the check pass: ...\" · hansa diff {tid} · hansa merge {tid} --despite-check",
           "failed": (f"read its notes, then split the task or make it clearer (hansa add ...); do not re-run it unchanged"
                      if str(m.get("error", "")).startswith("stopped at the ceiling")
                      else interruption_next(s, m, {"note": "the lane's setup failed"})
                      if m.get("setup_exit")
                      else f"once the machine is fixed (hansa explain server): hansa retry {tid} to run it again"
                      if m.get("machine_problem")
                      else (f"first fix what kept it from starting (the error above: the machine or the lane, or what "
                            f"the task was given; hansa explain server), then hansa retry {tid}")
                      if str(m.get("error", "")).startswith("could not start:")
                      else f"hansa retry {tid} \"what to change\" (or --fresh to start over)"),
           "queued": "hansa status shows what it is waiting for", "running": f"hansa cancel {tid} to stop it"}
    if m["state"] == "failed" and (m.get("attempts") or [{}])[-1].get("kind") == "refused":
        nxt["failed"] += f' · hansa retry {tid} "..." --lane L to choose another lane on the same repository and base'
    if v and v["verdict"] in ("does not hold", "unsure") and m["state"] == "done":
        nxt["done"] = _verification_next(m, desk.meta(v["task"]))
    newest = (v.get("latest") or v) if v else None
    if newest and newest["state"] in ("queued", "running") and m["state"] == "done":
        nxt["done"] = (f"wait for the verification: {notify.wait_step(desk, newest['task'], heard)} "
                       f"(hansa merge {tid} is held until it ends)")
    from .run import verification_due
    if verification_due(m):
        nxt["done"] = "wait for hansa serve to queue the requested verification"
    mr = m.get("merge_check_result") or {}
    merge_next = interruption_next(s, m, mr, merge=True)
    if m["state"] == "done" and merge_next:
        nxt["done"] = merge_next
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
        nxt = {"done": _verification_next(verified_task, m),
               "failed": f"hansa verify {m.get('parent')} to try again", "running": f"hansa cancel {tid} to stop it",
               "queued": "hansa status shows what it is waiting for"}
        if m.get("closed"):  # its task is closed: nothing is left to do with it
            nxt = {}
    if cancelling:
        nxt["running"] = f"wait for it to stop: {notify.wait_step(desk, tid, heard)}"
    if m["state"] in nxt:
        out.append("next: " + nxt[m["state"]])
    if page_messages is not None:
        page_messages["parts"] = [page_messages.get(str(i), line) for i, line in enumerate(out)]
    return "\n".join(out)


def merge_served_lanes(s: dict, name: str) -> list:
    """The work lanes whose merges a merge lane serves; verify lanes never merge."""
    return [n for n, ln in sorted(s["lanes"].items()) if n != name and not ln.get("verify_for")
            and merge_target(s, n) == merge_target(s, name)]


def merge_lane_text(s: dict, name: str) -> str:
    if s["lanes"][name].get("merge_lane"):
        served = merge_served_lanes(s, name)
        return f"  merge lane for lanes {', '.join(served) or '(none yet)'}; takes no work"
    selected = merge_lane(s, name)
    return f"  merge checks on lane {selected}" if selected != name else ""


def verify_lane_text(s: dict, name: str) -> str:
    served = s["lanes"][name].get("verify_for")
    if served:
        return f"  verify lane for lanes {', '.join(served)}; takes only verifications"
    verifiers = [n for n, ln in s["lanes"].items() if name in ln.get("verify_for", [])]
    return f"  verifications on lanes {', '.join(verifiers)}" if verifiers else ""


def _lane_command(entry, name: str) -> str:
    """The command that changed a lane, with the field or target it names, as one would type it again."""
    command = entry.get("command", "")
    field = entry["path"][len(f"lanes.{name}."):].split(".")
    if command == "hansa lane set":
        return f"with hansa lane set {name} {field[0]}"
    if command == "hansa lane target" and field[0] == "targets" and len(field) > 1:
        return f"with hansa lane target {name} {field[1]}"
    if command == "hansa lane brief":
        return f"with hansa lane brief {name}"
    return history.how(entry)


def lane_origin(desk: Desk, name: str) -> str:
    """Which command made the lane, and which last changed it: from the settings history, which names the command
    of every change; on a desk from before it, from the event log."""
    made, last = history.origin(desk, "lane", name, "repo")
    if made or last:
        return "; ".join(x for x in (made and "made " + history.how(made),
                                     last and "last changed " + _lane_command(last, name)) if x)
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
            last = (f"hansa lane target {name} {e.get('target', '')}" if e.get("field") == "targets"
                    else f"hansa lane set {name} {e.get('field', '')}").strip()
        elif e.get("event") == "lane_brief_changed" and not last:
            last = f"hansa lane brief {name}"
        elif e.get("event") in ("lane_added", "desk_created"):
            made = "hansa lane add" if e["event"] == "lane_added" else "hansa init"
            break
    if not made and not last:  # a desk from before hansa recorded either
        return "set by the coordinator with hansa lane add or hansa lane set"
    return "; ".join(x for x in (made and f"made with {made}", last and f"last changed with {last}") if x)


def provider_origin(desk: Desk, name: str) -> str:
    """How the provider was set, as the settings history records it."""
    made, last = history.origin(desk, "provider", name, "kind")
    if not made and not last:  # a desk from before the settings history
        return "set by hansa init or by the principal on the web page; no record says which"
    return "; ".join(x for x in (made and "set " + history.how(made, "by"),
                                 last and "last changed " + history.how(last, "by"))
                     if x)


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


def web_lines(allow: list) -> list:
    """What a lane's allowed commands give of Claude Code's web tools, which reach the network from its own
    process, outside the sandbox's network list."""
    sites, search = web_sites(allow)
    if not sites and not search:
        return ["  web tools (Claude Code's own, outside the sandbox's network list): none"]
    return ["  web tools (Claude Code's own, outside the sandbox's network list; workers and verifiers alike):",
            *(["    WebFetch reaches only: " + ", ".join(sites) + " (unlike the network list, it runs in Claude"
               " Code's own process, outside the sandbox; a *. wildcard opens every name under it; any listed name can be pointed at any address, this"
               " machine included: list only sites you know, and the principal sees every change)"] if sites else []),
            *(["    WebSearch: sends search queries out of the machine"] if search else [])]


def lane_check_times(tasks: list, ln: dict, merge_tasks: list) -> list:
    """Completed task checks and relevant merge checks; read only by explain lane."""
    out = []
    for label, field, records in (("task check", "check", tasks),
                                  ("merge check", "merge_check_result", merge_tasks)):
        if field == "check":
            if ln.get("merge_lane"):
                continue
            if not ln.get("check"):
                out.append("  task check times: no check")
                continue
        runs = []
        for m in records:
            r = m.get(field) or {}
            note = r.get("note") or ""
            if (r.get("ok") not in (True, False) or r.get("could_not_run") or r.get("waited")
                    or r.get("exit") is None or r["exit"] < 0 or r.get("seconds") is None
                    or any(x in note for x in ("stopped", "cancelled", "interrupted", "could not run",
                                               "check did not run"))):
                continue
            ended = r.get("at") if field == "merge_check_result" else (
                m.get("check_started", 0) + r["seconds"])
            runs.append((ended or 0, r["seconds"] / 60))
        minutes = [v for _, v in sorted(runs, key=lambda x: x[0], reverse=True)[:8]]
        if len(minutes) < 2:
            out.append(f"  {label} times: not enough runs yet")
            continue
        latest = minutes[:5]
        out.append(f"  {label} times (minutes, newest first): {', '.join(f'{v:.2f}' for v in latest)};"
                   f" median {statistics.median(latest):.2f}")
        if len(minutes) == 8:
            recent, before = statistics.mean(minutes[:3]), statistics.mean(minutes[3:8])
            if recent > before and recent >= 1.5 * before:
                increase = f"{recent / before:.1f} times" if before else f"up {recent:.2f} minutes from zero"
                out.append(f"  {label} is slower: last 3 average {recent:.2f} minutes, the 5 before"
                           f" {before:.2f} minutes ({increase}). Add a task to split the project's"
                           " tests or run them in parallel; use the merge check for what is slow"
                           " (hansa help merge-check).")
    return out


def lane_check_repetition(ln: dict, merge_command: str) -> list:
    check = " ".join((ln.get("check") or "").split())
    merge = " ".join(merge_command.split())
    if check and merge and check in merge:
        return ["  the merge check repeats the lane's check: every merge runs the lane's check again, in the"
                " merge check. Use the lane's check for the quick tests and the merge check for the whole suite."]
    return []


def lane_release_counts(tasks: list, ln: dict) -> list:
    if not ln.get("release_files"):
        return []
    from .cli import _release_files_touched  # cli imports views; use merge's check when explain is called
    since = time.time() - 14 * 86400
    recent = [m for m in tasks if m.get("state") == "merged" and m.get("merged", 0) >= since]
    try:
        touched = [_release_files_touched(Path(ln["repo"]).expanduser(), m, ln) for m in recent]
    except (OSError, RuntimeError) as error:
        reason = (error.strerror or "repository unavailable") if isinstance(error, OSError) else "git failed"
        return [f"  release_files counts could not be read: {reason}."]
    counts = [(p, sum(bool(release_files_in(files, [p])) for files in touched))
              for p in ln.get("release_files") or []]
    counts = sorted(((p, n) for p, n in counts if n), key=lambda x: (-x[1], x[0]))
    if not counts:
        return []
    return ["  release_files matches (tasks merged in the last 14 days, requiring verification): "
            + ", ".join(f"{p}: {n}" for p, n in counts),
            *(["  release_files lists only what a release runs outside the sandbox; an entry that 3 or more tasks"
               " touch may be wider than that (hansa help release-files)."] if counts[0][1] >= 3 else [])]


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
        served = merge_served_lanes(s, name) if ln.get("merge_lane") else []
        all_tasks = [m for m in desk.tasks() if m.get("kind") != "verify"]
        tasks = [m for m in all_tasks if m.get("lane") == name]
        merge_tasks = ([m for m in all_tasks if (m.get("merge_check_result") or {}).get("lane") == name]
                       if ln.get("merge_lane") else tasks)
        return "\n".join([
            f"lane {name} ({lane_origin(desk, name)}; not by the principal on the web page)",
            f"  repo: {ln['repo']} · base branch: {ln.get('base', 'main')} · worktree: {wt}",
            *([f"  WARNING: {repo_gone_text(name, ln['repo'])}"] if not Path(ln["repo"]).is_dir() else []),
            *([merge_lane_text(s, name)] if merge_lane_text(s, name) else []),
            *(["  WARNING: " + why] if (why := merge_targets_problem(name, ln)) else []),
            *([verify_lane_text(s, name)] if verify_lane_text(s, name) else []),
            f"  merge_batch: {check_lane.get('merge_batch', 4)} on check lane {check_name} "
            "(1 to 10; default 4; unsuccessful batches are checked alone)",
            f"  read them with hansa git {name} log|show|diff|grep ... (--worktree: the worktree), git commands that"
            " read only, run as the desk's account",
            f"  author and committer of every commit hansa makes for the lane (its tasks' commits, their merges into"
            f" the base, merges of the base into a task's branch,"
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
            *lane_check_times(tasks, ln, merge_tasks),
            *lane_check_repetition(ln, merge_command(check_lane) if separate or own_merge_check(ln) else ""),
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
            "  allowed commands (given to the lane's workers and verifiers; workers run any command"
            " in their sandbox and need no Bash rule):" + ("" if ln.get("allow") else " none"),
            *["    " + (r if isinstance(r, str) else json.dumps(r))
              + (f" (ignored: it {refused_rule(r)})" if refused_rule(r) else "") for r in ln.get("allow", [])],
            *(["    runs leave the ignored ones out; the coordinator removes them with: "
               + allow_fix(name, ln.get("allow", []))] if any(map(refused_rule, ln.get("allow", []))) else []),
            *release.lane_lines(desk, name, ln),
            *lane_release_counts(tasks, ln),
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
            f"provider {name} ({provider_origin(desk, name)})",
            f"  kind: {p['kind']} · model: {p['model']} · effort: {p.get('effort') or 'default'}",
            f"  rest after a usage limit: until the reset time the error gives, and a minute; else "
            f"{s.get('cool_minutes', 30)} minutes"
            + " (the desk's cool_minutes)",
            f"  {'login' if p['kind'] == 'subscription' else 'endpoint'}: {where}"
            + ("" if p["kind"] == "subscription" else f" · key: {'present' if desk.has_key(name) else 'missing'}"),
            f"  now: {provider_problem(desk, s, name) or 'ready'}",
            *([f"  {quota_probe_text(st, s)}"] if quota_probe_text(st, s) else []),
            f"  last ping: {ping_text(st.get('check'))}"])
    if what == "task":
        return show_text(desk, name)
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
            "`hansa up` starts it again as it last ran, detached in tmux when installed, or in the background, "
            "then the coordinator's session. It starts no server while one runs. More: hansa help bringing-up.",
            f"  now: {server_text(server)}",
            f"  {page_text(page_of(desk, server))}",
            "What it starts outside a worker's sandbox (a release's command, the alert command, "
            "git) runs with its environment, less what carries a Claude Code session's identity or "
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
            f"busy or stuck, with its pid. Time the machine sleeps does not count. "
            f"Stuck for good (a task it waits on that never ends, a terminal stopped "
            f"with Ctrl-Z): see what it is doing, e.g. with `ps -p PID` and its terminal, before stopping it; "
            f"stop it with Ctrl-C and start it again, and tasks running meanwhile are taken over.",
            "It runs each lane's setup and check in srt (Anthropic's sandbox runtime), with its own PATH, which can "
            "differ from the coordinator's. It tries srt (a run of `true` in a sandbox) when it starts and before "
            "each setup or check, and records what it saw; hansa status reports that record.",
            f"  now: {seen}",
            "When srt cannot run, the setup or check does not run either, and the task is not judged on it: its "
            "check reads \"could not run\". That is the machine's problem, not the task's. Before each setup or "
            "check, hansa looks for srt on the PATH hansa serve starts with, never on a lane's (a lane's env "
            "reaches only the command inside the sandbox). Node 22.12.0 or newer must be on that PATH "
            "too, since srt is a node script (an error such as `env: node: No such file or directory`, exit 127, "
            "means it is not). Install srt with `npm install -g @anthropic-ai/sandbox-runtime@0.0.79`, or fix the "
            "server's PATH, then restart hansa serve. Then, for a task whose "
            "check could not run or failed for the machine's sake: hansa merge ID --recheck runs the merge check "
            "again and merges if it passes; a task whose setup could not run: hansa retry ID.",
            "Before it starts a worker or a verifier, it probes that run's sandbox the same way: "
            "srt with the settings Claude Code is given, on /usr/bin/true, then a write of what git needs open "
            "there (the worktree's own index and git folder), and of what it must keep, to see that the deny patterns match (a new file atop the "
            "shared git folder, a name next to the worktree's own folder; what such a write makes is removed), once for each set of settings "
            "while it runs. When srt refuses them, keeps what git needs or lets through what it must keep (a "
            "sandbox that does not keep what it should runs no one), the run does not start: its task is held (queued, no try used) and its "
            "lane shows as unable to run in hansa status, with srt's first line and the part of the settings it "
            "refuses (found by trying each part alone, or the path written); the principal's alert is told once for each lane "
            "and reason. A refusal holds the lane's workers and verifiers; hansa status says which. Held tasks start by themselves once a probe "
            "passes: after a fix, restart hansa serve, which as it starts probes again the sandbox of each held lane "
            "(in the lane's worktree), clears the hold when "
            "the probe passes and keeps it with the new reason when it fails (so does a change of the settings, at "
            "the next task's start). Without srt the run starts as before, in Claude Code's own sandbox, "
            "which never runs a command outside it. Claude Code carries its own copy of the sandbox library, which "
            "may differ from the srt installed, so the probe is a guard, not proof: when a run's first Bash calls "
            "say the sandbox could not start (sandbox-exec: or bwrap: before any call succeeded), hansa stops the "
            "run and holds its task the same way.",
            *([f"  {LINUX_TRY_NOTE[0].upper()}{LINUX_TRY_NOTE[1:]}."] if account.LINUX else []),
            *[f"  held: lane {ln} cannot run, {sandbox_held_text(h, lane=ln)}" for ln, groups in sorted(sandbox_held(desk).items())
              for h in groups.values()],
            "  More: hansa help sandbox-probe",
            *disk.explain_lines(desk, desk.settings()), *memory.explain_lines(desk, desk.settings())])
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
            "could not start), a verification's verdict, a queued merge check that "
            "ended, the principal's answer to a release "
            "(hansa release), a release that ended (done, or failed with its exit code and last lines) or became void, "
            "a provider that an open request names "
            "becoming ready when it was not as the request was made (once; the request may be done; looked at when a "
            "key, the settings or a check change it, and when its rest after a usage limit runs out, which hansa "
            "serve sees), a provider's timed rest leaving queued tasks without a ready provider "
            "(provider, reason, rest end and up to five task IDs, then the count of any more; the same reason stays "
            "quiet until a run on that provider finishes instead of returning to wait; no principal alert), "
            "hansa serve stopping or "
            f"finishing no round for {STUCK_AFTER // 60} minutes (busy or stuck), and then running again, srt not "
            "running. Not what you did yourself and saw. Each message is bullet lines under one [hansa] header with "
            "the desk, only the latest line per task, request or kind of event kept; task lines carry next:, other "
            "lines only when one applies; events seconds apart come in one message.",
            "Who hears what: a task or request goes to the session that made it (hansa add, retry, verify, merge, "
            "and ask record it) while that session has a follower; otherwise to every follower of the desk, "
            "as do desk-wide events. A cancel is not posted to the session whose hansa cancel made it (hansa cancel "
            "records it); others hear it, as all do one hansa made itself. A follower that starts after one ended "
            "first posts what was missed meanwhile, as it is now, but leaves out a hansa serve stop it missed when "
            "serve runs again by then.",
            "A follower keeps refused posts, gathers new events and retries with the latest line per subject. "
            "A startup summary counts as caught up only after it posts. A lasting inbox refusal alerts the principal "
            "after their no-session minutes, once until a post succeeds. A session restarted in place (/restart) gets a "
            "new inbox: hansa notify on, run from it, moves posting there. More: hansa help inbox, hansa help alerts.",
            "These messages come from hansa, not from the principal, and approve nothing. A session whose settings "
            "hold messages from other sessions (crossSessionInbound) does not get them; only the principal can "
            "change that.",
            "hansa coordinator --clear-daily HH-HH hands a window of hours (local time; 22-03 runs past midnight) to "
            "hansa's mod, for that start only: once in each window it clears the session, when the session has been "
            "idle for 5 minutes and did not start inside that window. Inside the window, idle compaction waits for "
            "the clear. After any clear the session reads hansa status by itself, and hansa records when; hansa "
            "status says the last clear, and warns when windows passed without one. hansa notify on no longer clears.",
            "  now: " + (", ".join(who) if who else "no session"),
            *[f"  {x}" for x in up.card_lines(desk, up.read_coordinator(desk))]])
    return "\n".join([
        "What you can look up: hansa explain lane <name> | provider <name> | server | notify | history",
        f"lanes: {', '.join(sorted(s['lanes'])) or 'none'} · providers: {', '.join(sorted(s['providers']))}",
        f"coordinator: {s['coordinator']['provider']} ({s['coordinator']['model']})",
        "Providers, models, endpoints, keys, effort defaults and prices belong to the principal (web page). "
        "You change tasks, lanes (hansa lane), how many run at once (hansa concurrency), the floor of free space under "
        "which nothing new starts (hansa disk-floor), the available memory floor (hansa memory-floor), "
        "lanes' standing instructions (hansa lane brief) and "
        "pause/resume.",
        release_settings_line(s),
        alert.explain_line(s, desk)])


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
    own = session_private(desk)
    mac = (f"; on macOS also, under ~/Library: {', '.join(x.removeprefix('Library/') for x in MAC_SECRETS)}"
           if sys.platform == "darwin" else "")
    built_in = (f"{', '.join('~/' + x for x in HOME_SECRETS)}{mac}" if whole else
                f"{len(HOME_SECRETS)} names" + (f", and {len(MAC_SECRETS)} more on macOS under ~/Library"
                                                if sys.platform == "darwin" else "")
                + f" (the whole list: hansa explain lane {name} --all)")
    return ["  what its workers and verifiers, its setup and its check may not read (the sandbox refuses it to any"
            " command; a name that does not exist is refused all the same):",
            f"    the desk's keys and state: {desk.secrets_dir}, {desk.state_dir}",
            *([f"    the Claude Code files of the folder hansa notify on recorded for the coordinator's session (built "
               f"in): {', '.join(own)}"] if own else []),
            f"    every provider's Claude Code folder, whichever provider the run uses: {', '.join(homes)}; and ~/.claude",
            "    logins, keys, tokens, browsing data, messages and histories under the home folder (built in): "
            + built_in,
            "    folders and files the principal keeps from workers (the principal's, on the web page, Settings; you"
            " add to it with hansa hide, and only the principal removes an entry; Read is denied there to workers,"
            " verifiers and the session hansa coordinator starts, not one hansa notify on recorded; no lane's"
            " repository or write list may be one, inside one or above one): " + (", ".join(x + hidden_note(s, x) for x in kept) or "none"),
            *("      " + x for x in no_read_unusable(s, desk.root))]


def try_text(lane: str, t: dict) -> str:
    """What `hansa lane try` prints: for the setup and the check, the exit code, the time taken, the last lines."""
    if t.get("busy"):
        return f"hansa: {t['note']}. Try again when it is free: hansa wait {t['busy']}, then hansa lane try {lane}"
    if t.get("could_not_run"):  # srt did not run in the lane's sandbox: the machine's problem, not the commands'
        return (f"lane {lane}: {t['note']}\nnothing was run: the setup and check run in srt "
                f"(hansa explain server). Fix it, then hansa lane try {lane}")
    out = [f"lane {lane} tried at {(t.get('onto') or '')[:10]}: " + ("ok" if t.get("ok") else "FAILED")
           + (f" ({t['note']})" if t.get("note") else "")]
    out += oom_lines(t, "lane try")
    for step, x in t.get("steps", {}).items():
        if not x["ran"]:
            out.append(f"{step.replace('_', ' ')}: {x['command']}\n  did not run: the setup failed")
            continue
        how = "interrupted" if x["exit"] is None else f"exit {x['exit']}"
        out.append(f"{step.replace('_', ' ')}: {x['command']}\n  {how}" + (f" in {x['seconds']}s" if x["seconds"] is not None else "")
                   + (", last lines:" if x["tail"] else ", no output"))
        out += [f"    {plain(line, None)}" for line in x["tail"]]
        out += [f"  {h}" for h in x.get("hints", [])]
        out += ["  " + line for line in oom_lines(x, step.replace("_", " "))]
    if t.get("log"):
        out.append(f"each step's whole output, keys removed: {t['log']} (hansa lane try {lane}"
                   f"{' --merge-check' if t.get('merge') else ''} --log)")
    if t.get("clean"):
        out.append("the worktree is back clean at the base's tip, as the lane's next task expects it")
    return "\n".join(out)


def cost_text(rep: dict, by: str = "day") -> str:
    def dollars(usd):
        return "<$0.01" if 0 < usd < .01 else f"${usd:.2f}"

    rows = rep["by"].get(by, {})
    window = rep.get("window") or f"last {rep['days']} day(s)"
    unpriced = sum(v["unpriced_tokens"] for v in rows.values())
    total = rep["total"]
    worth = "no price" if unpriced and unpriced == total["tokens"] else f"worth {dollars(total['usd'])}"
    if unpriced and unpriced < total["tokens"]:
        worth += f" ({unpriced:,} tokens with no price)"
    out = [f"{window}, by {by}: {total['tokens']:,} tokens, {worth}"]
    for k, v in rows.items():
        all_unpriced = v["unpriced_tokens"] and v["unpriced_tokens"] == v["tokens"]
        worth = "no price" if all_unpriced else dollars(v["usd"])
        extra = f" ({v['unpriced_tokens']:,} tokens with no price)" if v["unpriced_tokens"] and not all_unpriced else ""
        if by == "task" and v.get("runs"):  # every run of the task: its earlier tries too
            extra += f"  {v['runs']} run(s), {v.get('turns', 0):,} turn(s)"
        out.append(f"  {k or '-':<22} {v['tokens']:>14,}  {worth}{extra}")
    w = rep.get("work") or {}
    if w.get("coordinator_turns"):
        out.append(f"the coordinator: {w['coordinator_turns']:,} turn(s), {w['coordinator_hours']:.2f} hour(s) of work "
                   f"in them, each from its first step to its last (hansa cost --turns)")
    if rep["unpriced_models"]:
        out.append("no price yet for: " + ", ".join(rep["unpriced_models"]) + " (the principal sets prices on the page)")
    if rep.get("coordinator_uncounted"):
        out.append(rep["coordinator_uncounted"])
    out.append(rep["note"])
    return "\n".join(out)


def cost_turns_text(rep: dict) -> str:
    """hansa cost --turns: one line per turn of the coordinator, then totals by what started them."""
    t = rep["total"]
    out = [f"the coordinator's turns, {rep['window']}: {t['turns']} turn(s), {t['steps']} step(s), worth ${t['usd']:.2f}, "
           f"{t.get('seconds', 0) / 3600:.2f} hour(s) of work, each turn from its first step to its last"]
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
    from .run import git, git_first_line, wgit
    m = desk.meta(tid)
    # git interprets paths from the checkout where it runs, which may be another lane's repository.
    # Reject absolute paths and traversal before git so these mistakes get the same useful explanation.
    if any(Path(p).is_absolute() or ".." in Path(p).parts for p in paths):
        return "hansa: diff paths are given relative to the repository, without '..'"
    if not m.get("head") or not m.get("worktree"):
        return f"{tid} has no changes recorded yet (state: {m['state']})"
    wt = Path(m["worktree"])
    since, head, notice = m["base"], "", ""
    read = lambda *args: wgit(desk, wt, *args)
    if not wt.exists():
        lanes = desk.settings()["lanes"]
        notice = (f"hansa: {tid}'s lane {m['lane']} was removed" if m["lane"] not in lanes else
                  f"hansa: {tid}'s lane {m['lane']}'s worktree {wt} was removed")
        # A branch at the recorded commit proves that a remaining lane can reach this task, even if
        # the repository moved. Never run git through the removed worktree or an unchecked .git file.
        for name, lane in lanes.items():
            if not branch_holds(lane["repo"], m):
                read = lambda *args: git(*args, cwd=lane["repo"])
                notice += f"; showing the diff from lane {name}'s repository {lane['repo']}\n"
                break
        else:
            branch = m.get("branch") or f"task/{tid}"
            return desk.redact(f"{notice}; no remaining lane can read its branch {branch} at the recorded commit; "
                               f"its branch is in {m.get('repo') or 'the repository where it ran'}")
    try:  # the worktree's .git is checked first; the task working there now fails for it when it ends
        mode, selected, parts = "--stat" if stat_only else "--patch", ["--", *paths], []
        if new:
            if not m.get("start_from") or m.get("kind") == "verify":
                return (f"hansa: {tid} did not continue from an earlier task, so all of it is new: "
                        f"hansa diff {tid}" + (" --stat" if stat_only else ""))
            since = m["start_from"]
            head = f"what {tid} added on top of {m.get('parent') or 'the earlier task'}'s last commit ({since[:10]})"
            if m.get("update"):
                rows = [x.split() for x in read("rev-list", "--first-parent", "--parents",
                                                f"{since}..{m['head']}").splitlines()]
                merge = next((x[0] for x in rows if len(x) > 2 and x[1] == since), "")
                if merge:
                    since = merge
                    if m.get("update_conflicts"):
                        # Compare the resolved merge with git's recreated conflicted tree, then show
                        # work after that merge. Comparing either parent would count the base's work.
                        parts.append(read("show", "--format=", "--remerge-diff", mode, merge, *selected))
                        head += f"; resolution of merge {merge[:10]} and later changes, without the base's own changes"
                    else:
                        head += f", without the base merged into it at {merge[:10]}"
        parts.append(read("diff", mode, since, m["head"], *selected))
        out = desk.redact("\n".join(x for x in parts if x))
    except (RuntimeError, OSError) as e:
        return desk.redact(f"{notice}hansa: {git_first_line(e)}; the diff is not shown")
    out = out or (f"(no changes in {', '.join(paths)})" if paths else "(no changes)")
    return desk.redact(notice) + (f"{head}:\n{out}" if head else out)
