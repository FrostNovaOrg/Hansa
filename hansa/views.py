"""Views: the facts behind `hansa status`, `hansa show`, `hansa explain` and the web page.
Each view returns plain data; the *_text helpers turn it into short lines."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from . import build, cost, installed, installed_text, notify, release
from .desk import (HOME_SECRETS, MAC_SECRETS, Desk, claude_home, is_git_rule, kept_from_workers, listed_lanes, read_json,
                   read_split, write_json)
from .run import (ROUND_QUIET, STUCK_AFTER, is_local, join_command, lane_occupied, last_round, markers_in_change,
                  markers_text, merge_check_ahead, not_looking, progress_of, provider_problem, serve_code, server_state,
                  lane_env, server_text, srt_needed, srt_problem, srt_record, srt_when, tried_lanes, waiting_reason, write_list)

STATES = {
    "queued": "waiting for a free lane and a ready provider",
    "running": "a worker is on it, or hansa is running the lane's check on its work",
    "done": "the worker finished; waiting for your review (hansa show / hansa merge / hansa retry)",
    "merging": ("hansa merge is checking it together with what the base gained since it was made: merged onto the "
                "base as it is now, with the lane's check run there; it merges if that passes. Or it needs no check "
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
    return next((x["id"] for x in (tasks if tasks is not None else desk.tasks())
                 if x.get("lane") == m.get("lane") and lane_occupied(x) and x["id"] != m["id"]), None)


def merge_check_waits_for(desk: Desk, m: dict, tasks: list | None = None) -> str:
    """The merge check on the same repository and base branch that a queued one waits for, in words ("t0123's
    merge check on lane a"); '' when it is first in line."""
    s = desk.settings()
    x = merge_check_ahead(s, m, tasks if tasks is not None else desk.tasks())
    what = "merge" if ((x or {}).get("merge_check") or {}).get("plain") else "merge check"
    if x:
        return f"{x['id']}'s {what} on lane {x.get('lane')}"
    lane = s["lanes"].get(m.get("lane"))
    r = release.running_on(desk, lane["repo"]) if lane and m["state"] == "merging" and \
        (m.get("merge_check") or {}).get("phase", "queued") == "queued" else None
    return release.running_text(r) if r else ""


def merge_check_text(desk: Desk, m: dict, tasks: list | None = None) -> str:
    """Where a merging task's merge check is, in one line."""
    mc = m.get("merge_check") or {}
    lane = m.get("lane")
    base = desk.settings()["lanes"].get(lane, {}).get("base") or "main"
    again = f" (again: {base} moved while it was checked)" if mc.get("moves") else ""
    if mc.get("phase") in ("starting", "running"):
        return (f"merge check running for {_mins(mc.get('started'))} min{again}: {m['id']} merged onto {base} at "
                f"{mc.get('onto', '')[:10]}, then " + ("the lane's setup and " if mc.get("setup") else "")
                + f"check: {mc.get('command', '')}")
    ahead = merge_check_waits_for(desk, m, tasks)
    if mc.get("plain"):
        how = "without a check" + ("" if mc.get("despite") else f" if {base} has still not moved")
        return (f"merge queued, to go in {how}: " + (f"waits for {ahead}" if ahead else not_looking(desk)
                                                      or "merges when `hansa serve` next looks (within seconds)"))
    other = merge_check_busy_with(desk, m, tasks)
    return (f"merge check queued{again}: " + (f"waits for {ahead}" if ahead else
                                              f"waits for lane {lane}, busy with {other}" if other else
                                              not_looking(desk)
                                              or "starts when `hansa serve` next looks (within seconds)"))


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


def merge_result_lines(m: dict) -> list:
    """What the last merge check found, for hansa show."""
    r = m.get("merge_check_result")
    if not r:
        return []
    if r.get("waited"):  # it needed no check: it waited its turn behind other merges, and no check ran
        return ["merge: " + (f"{WAITED}: " + r["conflict"].removeprefix("hansa: ") if r.get("conflict")
                             else waited_text(r))]
    if r.get("conflict"):
        return ["merge check: nothing was merged; " + r["conflict"].removeprefix("hansa: ")]
    where = f"hansa merged it onto the base at {r.get('onto', '')[:10]}"
    if r.get("ok"):
        return [f"merge check: passed in {r.get('seconds', 0)} s ({where}): {r.get('command', '')}"]
    if r.get("could_not_run"):
        return [f"merge check: {r['note']}; nothing was merged", "  " + machine_hint(r)]
    how = f"FAILED ({r['note']})" if r.get("note") else f"FAILED (exit {r.get('exit')})"
    hint = machine_hint(r)
    return ([f"merge check ({where} and ran the lane's check there): {how}"
             + (f" in {r['seconds']} s: {r['command']}" if r.get("command") else "") + "; nothing was merged"]
            + ["  " + plain(x) for x in r.get("tail", [])] + (["  " + hint] if hint else []))


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


def status(desk: Desk) -> dict:
    s = desk.settings()
    tasks = desk.tasks()
    pst = desk.provider_state()
    ready = ready_now(desk, s)
    designing = [m for m in tasks if m["state"] == "running" and m.get("kind") == "design"]
    running = [m for m in tasks if m["state"] == "running" and m.get("kind") != "design"]
    merging = [m for m in tasks if m["state"] == "merging"]
    tried = tried_lanes(desk)
    busy = {m.get("lane") for m in tasks if lane_occupied(m)} | tried
    last = last_round(desk)
    server = server_state(desk)
    day = time.time() - 86400  # the last 24 h, each thing by when it happened: not reset at midnight
    rows = cost.worker_rows(desk, day)  # by when the run ended
    return {
        "desk": str(desk.root),
        "build": build(),
        "installed": installed(),  # when the code this runs was put in place
        "serve_code": serve_code(desk, server),  # hansa serve on older code than is installed: what to do
        "audits_cost": bool(s["coordinator"].get("audits_cost")),
        "paused": desk.paused(),
        "server_up": server["state"] != "gone",  # alive, busy or not: run.server_state is the one rule
        "server": server,
        "server_seconds_ago": int(time.time() - last) if last else None,
        "srt_missing": srt_problem(desk, s),  # what stops setups and checks, as hansa serve last saw srt
        "srt": {**srt_record(desk), "needed": srt_needed(s)},
        "notifying": notify.names(desk),  # the sessions hansa posts to (hansa notify)
        "heard": notify.hears(desk),  # whether the session asking is one of them
        "concurrency": s["concurrency"],
        "lanes": [{"name": n, "providers": ln["providers"], "repo": ln["repo"],
                   "task": next((m["id"] for m in tasks if lane_occupied(m) and m.get("lane") == n), None),
                   "trying": n in tried}  # a `hansa lane try` queued or running holds the lane
                  for n, ln in sorted(s["lanes"].items())],
        "providers": [{"name": n, "kind": p["kind"], "model": p["model"], "effort": p.get("effort", ""),
                       "problem": provider_problem(desk, s, n), "check": pst.get(n, {}).get("check")}
                      for n, p in sorted(s["providers"].items())],
        "running": [{"id": m["id"], "title": m["title"], "lane": m.get("lane"), "provider": m.get("provider"),
                     "model": m.get("model_asked"), "minutes": _mins(m.get("started")),
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
                    "waiting": waiting_reason(desk, s, m, busy)} for m in tasks if m["state"] == "queued"],
        "merging": [{"id": m["id"], "title": m["title"], "lane": m.get("lane"),
                     "phase": (m.get("merge_check") or {}).get("phase", "queued"),
                     "minutes": _mins((m.get("merge_check") or {}).get("started")),
                     "busy_with": merge_check_busy_with(desk, m, tasks),
                     "waits_for": {k: x.get(k) for k in ("id", "lane")}
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
    out += [st["serve_code"]] if st.get("serve_code") else []
    out += [st["srt_missing"]] if st.get("srt_missing") else []
    out.append("hansa posts to: " + ", ".join(st["notifying"]) if st.get("notifying") else
               "hansa posts to no session (hansa notify on, from the coordinator's session)")
    out.append("lanes:")
    lanes = st["lanes"] or [{"name": "(none)", "providers": [], "task": None}]
    wn, wp = max(len(ln["name"]) for ln in lanes), max(len(" > ".join(ln["providers"])) for ln in lanes)
    for ln in lanes:
        out.append(f"  {ln['name']:<{wn}}  {' > '.join(ln['providers']):<{wp}}  "
                   + (ln["task"] or ("held by a try (hansa lane try)" if ln.get("trying") else "free")))
    if st["running"]:
        out.append("running:")
        out += [f"  {m['id']} {m['lane']} " + (f"worker done, hansa is running the lane's check, "
                                               f"{m['check_minutes']} min so far" if m["checking"]
                                               else "hansa is preparing it" if m.get("starting")
                                               else f"on {m['provider']} ({m['model']})")
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
    for p in st["providers"]:
        out.append(f"  {p['name']:<{wn}}  {p['kind']:<12}  {p['model']:<{wm}}  {p['problem'] or 'ready'}")
    if [r for r in st.get("releases") or [] if r["state"] in release.OPEN]:
        out.append("releases (hansa release):")
        out += [f"  #{r['id']} lane {r['lane']} at {(r['commit'] or '')[:10]}: {r['what']}" for r in st["releases"]
                if r["state"] in release.OPEN]
    if st["asks"]:
        out.append("open asks to the principal:")
        out += [f"  #{a['id']}: {a['text'][:100]}" + (f" · {a['note']}" if a.get("note") else "") for a in st["asks"]]
    t = st["last_24h"]
    out.append(f"last 24 h: {t['finished']} finished, {t['merged']} merged; workers used {t['worker_tokens']:,} tokens"
               f" (worth ${t['worker_usd']:.2f}; hansa cost for everything)")
    return "\n".join(out)


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


def report_text(desk: Desk, tid: str) -> str:
    """hansa show ID --report: the report alone, whole; for a verification, its verdict first."""
    m = desk.meta(tid)
    report = report_of(desk, tid)
    out = []
    if m.get("kind") == "verify" and (m.get("state") == "done" or m.get("verdict")):
        out.append(f"verdict: {m.get('verdict') or 'none given'}")
    out.append(report or f"(no report: {tid} is {m['state']})")
    return "\n\n".join(out)


def show_text(desk: Desk, tid: str, full: bool = False, heard=None) -> str:
    """hansa show. full: the whole text and report. heard: whether the session it is for hears from hansa
    (default: the session this command runs in)."""
    m = as_closed_now(desk.meta(tid))
    m["markers"] = markers_of(desk, m)
    d = desk.tasks_dir / tid
    head = f"{tid} · {m['state']} · lane {m.get('lane') or 'any'}"
    if m.get("provider"):
        head += f" · {m['provider']} ({m.get('model_asked')})" + (f" · {m['turns']} turns" if m.get("turns") else "")
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
    if m.get("error"):
        out.append(f"error: {m['error']}")
    if m.get("closed"):  # a verification that had ended when its task closed: kept as it ended
        out.append(m["closed"])
    if m.get("machine_problem") and m["state"] == "failed":
        out.append("  this is the machine's problem, not the task's: srt, which runs the lane's setup, could not run "
                   "on the server, so the worker never started (hansa explain server)")
    out += [f"write list: {x}" for x in m.get("write_left_out") or []]
    out += [f"env: {x}" for x in m.get("env_left_out") or []]
    out += [f"what workers may not read: {x}" for x in m.get("no_read_left_out") or []]
    for a in m.get("attempts", []):
        out.append(f"earlier try on {a['provider']}: {a['error']}")
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
                   + ("; nothing to merge, so the lane's check was not run" if m.get("state") == "done"
                      and not m.get("commits") else ""))
        out += ["  " + f for f in m.get("files", [])[:30]]
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
                out.append("  " + "\n    ".join([first, *more]) + (f"\n    Claude Code said: {plain(why)}" if why else ""))
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
    chk = m.get("check")
    if chk and chk.get("could_not_run"):
        out.append(f"check (run by hansa, not the worker): {chk['note']}: {chk['command']}")
        out.append("  " + machine_hint(chk))
    elif chk:
        how = "passed" if chk["ok"] else "FAILED" + (f" ({chk['note']})" if chk["note"] else f" (exit {chk['exit']})")
        out.append(f"check (run by hansa, not the worker): {how} in {chk['seconds']} s: {chk['command']}")
        if not chk["ok"]:
            out += ["  " + plain(x) for x in chk["tail"]]
            if machine_hint(chk):
                out.append("  " + machine_hint(chk))
    elif m.get("phase") == "checking":
        out.append(f"check: running now: {m.get('check_command', '')}")
    elif m["state"] == "done" and m.get("commits") and not verifying:
        ln = desk.settings()["lanes"].get(m.get("lane"))
        if ln is not None and not ln.get("check"):
            out.append(f"check: none. Lane {m['lane']} has no check, so this task will be merged untested; set one "
                       f"with: hansa lane set {m['lane']} check \"<the command that runs the tests>\"")
    if m["state"] == "merging":
        out.append(merge_check_text(desk, m))
    elif m["state"] in ("done", "merged"):
        out += merge_result_lines(m)
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
    for model, u in (m.get("usage") or {}).items():
        out.append(f"usage {model}: in {u['in']:,} · out {u['out']:,} · cache read {u['cache_read']:,}"
                   f" · cache write {u['cache_write']:,}")
    review = (f"hansa merge {tid}" if v and v["verdict"] == "holds"
              else f"hansa diff {tid} --stat, then hansa verify {tid} (code or tests) or hansa merge {tid} (text only)")
    nxt = {"done": f"hansa retry {tid} \"what to change\" · hansa cancel {tid} to close it" if not m.get("commits")
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
                       f"hansa ask (they may want to change that key) · hansa cancel {tid}")
    if verifying:
        nxt = {"done": f"read the report above, then merge or send back {m.get('parent')}",
               "failed": f"hansa verify {m.get('parent')} to try again", "running": f"hansa cancel {tid} to stop it",
               "queued": "hansa status shows what it is waiting for"}
        if m.get("closed"):  # its task is closed: nothing is left to do with it
            nxt = {}
    if m["state"] in nxt:
        out.append("next: " + nxt[m["state"]])
    return "\n".join(out)


def lane_origin(desk: Desk, name: str) -> str:
    """Which command made the lane, and which last changed it, as the event log has them."""
    made, last = "", ""
    try:
        lines = desk.events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in reversed(lines):
        if f'"{name}"' not in line:  # most lines are about something else: not parsed
            continue
        try:
            e = json.loads(line)
        except ValueError:
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


def explain_text(desk: Desk, what: str = "", name: str = "") -> str:
    s = desk.settings()
    if what == "lane":
        ln = s["lanes"].get(name)
        if not ln:
            return f"no lane {name}; lanes: {', '.join(sorted(s['lanes'])) or 'none'}"
        wt = desk.worktrees_dir / name
        brief = desk.lanes_dir / name / "brief.md"
        may_write, left_out = write_list(desk, ln)  # as a run is given it: a fence may have come since it was saved
        env, env_left = lane_env(desk, ln)
        return "\n".join([
            f"lane {name} ({lane_origin(desk, name)}; not by the principal on the web page)",
            f"  repo: {ln['repo']} · base branch: {ln.get('base', 'main')} · worktree: {wt}",
            f"  read them with hansa git {name} log|show|diff|grep ... (--worktree: the worktree), git commands that"
            " read only, run as the desk's account",
            f"  providers, in order: {' > '.join(ln['providers'])} (the next one takes over when one hits its limit)",
            f"  per task: stopped at {ln.get('max_turns', 300)} turns or {ln.get('timeout_min', 240)} minutes"
            " (check included); set high, so reaching it means something went wrong",
            "  check after each task: " + (f"{ln['check']} (hansa runs it in the worktree after the worker ends,"
                                            " in the lane's sandbox;"
                                            " hansa merge refuses a failed check; when the base has moved since a task"
                                            " was made, hansa merge runs it on the two merged together first;"
                                            " hansa merge ID --recheck runs it again that way in any case, for a"
                                            " check that failed or could not run for the machine's sake)"
                                            if ln.get("check") else "none, so its tasks are merged untested"),
            "  setup before each task: " + (f"{ln['setup']} (hansa runs it in the worktree before the worker starts,"
                                             " in the lane's sandbox, so one that downloads needs its registry on"
                                             " the network list;"
                                             " what it changes in the repository is set aside; if it fails, the"
                                             " task fails and hansa show has its last lines)" if ln.get("setup")
                                             else "none"),
            *(["  the setup and check run in srt with the lane's sandbox: the network and write lists below"
               " hold for them as for workers",
               f"  to see that they work where tasks run, with no worker: hansa lane try {name} (after changing the"
               " setup, check, network or write list, or moving the desk to another machine)"]
              if ln.get("setup") or ln.get("check") else []),
            "  environment set for workers: " + (", ".join(f"{k}={v}" for k, v in env.items()) or "none"),
            *["    a run leaves out " + x.removeprefix("left out of this run: ") for x in env_left],
            "  what a worker may use (it runs in Claude Code's sandbox, which the operating system enforces;"
            " what it refuses shows up in hansa show):",
            "    any command inside the sandbox, but it changes files only in the lane's worktree, its own"
            " temporary folder (made for each run and removed when it ends) and the folders listed below;"
            " it cannot read the desk's keys and state or what is listed below; it reaches no network but the hosts below; git history is left to hansa (no commit, merge,"
            " reset, branch and the like), which commits its changes",
            *no_read_lines(desk, s),
            "  network during a task: " + (", ".join(ln.get("network", [])) or "none"),
            "    " + local_servers_text(sys.platform == "darwin", any(is_local(h) for h in ln.get("network", []))),
            "  folders a worker may also write (a build or test tool's cache): "
            + (", ".join(may_write) or "none"),
            *["    a run leaves out " + x.removeprefix("left out of this run: ") for x in left_out],
            "  allowed commands (for design sessions only, which run outside the sandbox and may run these plus"
            " their own git add, commit and merge; workers run any command in theirs):"
            + ("" if ln.get("allow") else " none"),
            *["    " + r + (" (ignored: hansa handles git)" if is_git_rule(r) else "")
              for r in ln.get("allow", [])],
            *release.lane_lines(desk, name, ln),
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
            f"  {'login' if p['kind'] == 'subscription' else 'endpoint'}: {where}"
            + ("" if p["kind"] == "subscription" else f" · key: {'present' if desk.has_key(name) else 'missing'}"),
            f"  now: {provider_problem(desk, s, name) or 'ready'}",
            f"  last check: {st.get('check') or 'never'}"])
    if what == "task":
        return show_text(desk, name)
    if what == "states":
        return "\n".join(f"{k:<10} {v}" for k, v in STATES.items())
    if what == "server":
        rec = srt_record(desk)
        seen = ("hansa serve has not tried srt yet" if not rec else
                "hansa serve tried srt" + (f" at {srt_when(rec)}" if srt_when(rec) else "") + ": "
                + ("it ran" + (f" ({rec.get('path')})" if rec.get("path") else "") if rec.get("ran") else
                   f"found at {rec.get('path')}, but it could not run: {rec.get('said')}" if rec.get("found")
                   else "not found on its PATH"))
        return "\n".join([
            "`hansa serve` starts queued tasks, finishes ended ones and serves the web page. It runs on the "
            "desk's machine, usually in a terminal multiplexer. Without it, tasks stay queued. One runs per "
            "desk: a second one refuses to start.",
            f"  now: {server_text(server_state(desk))}",
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
            "check could not run or failed for the machine's sake: hansa merge ID --recheck runs the lane's check "
            "again and merges if it passes; a task whose setup could not run: hansa retry ID."])
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
            "key, the settings or a check change it, so one ready again only because its rest ran out gets the note "
            "in hansa status but no post), hansa serve stopping or "
            f"finishing no round for {STUCK_AFTER // 60} minutes (busy or stuck), and then running again, srt not "
            "running. Not what you did yourself and saw. Each message starts with [hansa] and the desk, with one "
            "line per event and the next command; events seconds apart come in one message. While the session is idle "
            "(claude agents --json), each post would start a turn that rereads its whole conversation, so hansa holds "
            "the lines until 2 minutes pass with nothing new, or 5 minutes since the first, and posts them as one "
            "message (HANSA_NOTIFY_IDLE_GATHER and HANSA_NOTIFY_IDLE_MOST, in seconds; 0 holds nothing); a session "
            "that is busy, or stops being idle, or whose state cannot be read, gets them at once, and lines about "
            "hansa serve itself are never held. hansa notify and hansa status show lines held, and until when.",
            "Who hears what: a task or request goes to the session that made it (hansa add, retry, verify, merge, "
            "design and ask record it) while that session has a follower; otherwise to every follower of the desk, "
            "as do desk-wide events. A cancel is not posted to the session whose hansa cancel made it (hansa cancel "
            "records it); others hear it, as all do one hansa made itself. A follower that starts after one ended "
            "first posts what was missed meanwhile, as it is now.",
            "These messages come from hansa, not from the principal, and approve nothing. A session whose settings "
            "hold messages from other sessions (crossSessionInbound) does not get them; only the principal can "
            "change that.",
            "--clear-daily HH-HH (hansa coordinator --clear-daily 04-09, or hansa notify on --clear-daily 04-09): "
            "once a day in that window of hours, local time, the follower types /clear into the session's tmux "
            "pane. Off by default; it needs the session to run in tmux. It types only when claude agents --json "
            "shows the session idle on two readings a minute apart, hansa posted nothing to it for 5 minutes, no "
            "key was pressed in a terminal on that tmux session for 90 s, and the input box is empty; it presses "
            "Enter only when the box then reads exactly /clear and the session is still idle. Otherwise it sends six "
            "backspaces only when the box ends in /clear, and tries again later; when the box cannot be read, holds "
            "something else, or tmux fails, it leaves the box as it is and counts today as tried. When the session's sessionId does not "
            "change within 15 s of Enter, today counts as tried too: no other try that day. A session that "
            "started inside today's window, after it opened, counts as new already: its next clear is tomorrow. "
            "Anything it "
            "cannot read counts as busy. A session started with hansa coordinator then reads hansa status by "
            "itself. hansa notify shows the window and today's state; the follower's log has each reason it held.",
            "  now: " + (", ".join(who) if who else "no session")])
    return "\n".join([
        "What you can look up: hansa explain lane <name> | provider <name> | task <id> | states | server | notify",
        f"lanes: {', '.join(sorted(s['lanes'])) or 'none'} · providers: {', '.join(sorted(s['providers']))}",
        f"coordinator: {s['coordinator']['provider']} ({s['coordinator']['model']}) · "
        f"cost audit by the coordinator: {'on' if s['coordinator'].get('audits_cost') else 'off'}",
        *read_lines(desk, s),
        "Providers, models, endpoints, keys, effort defaults and prices belong to the principal (web page). "
        "You change tasks, lanes (hansa lane), how many run at once (hansa concurrency), lane briefs (hansa lane brief) and "
        "pause/resume.",
        release_settings_line(s)])


def release_settings_line(s: dict) -> str:
    """For hansa explain: the principal's release settings."""
    listed = [n for n in listed_lanes(s) if release.lane_listed(s, n)]
    changed = [n for n in listed_lanes(s) if n not in listed]
    words = s.get("release_forbidden") or []
    return ("Releases (hansa release L) go out only after the principal's yes on the page"
            + (f", but for lanes {', '.join(listed)}, which the principal lets go out without asking while the desk is "
               "not paused" if listed else "")
            + (f" (lanes {', '.join(changed)} are on that list, but their repository, base branch or release command "
               "is no longer what it was when the principal saved it, so theirs wait for the yes)" if changed else "")
            + f"; {len(words)} word(s) the principal keeps from going out are checked first. Both lists are the "
            "principal's, on the web page; you cannot set them (hansa ask). Do not push or deploy yourself.")


def no_read_lines(desk: Desk, s: dict) -> list:
    """For hansa explain lane: what the lane's workers and verifiers, its setup and its check may not read."""
    homes = dict.fromkeys(str(claude_home(desk.state_dir, n, p)) for n, p in s["providers"].items() if isinstance(p, dict))
    kept, left = kept_from_workers(s, desk.root)
    mac = (f"; on macOS also, under ~/Library: {', '.join(x.removeprefix('Library/') for x in MAC_SECRETS)}"
           if sys.platform == "darwin" else "")
    return ["  what its workers and verifiers, its setup and its check may not read (the sandbox refuses it to any"
            " command; a name that does not exist is refused all the same):",
            f"    the desk's keys and state: {desk.secrets_dir}, {desk.state_dir}",
            f"    every provider's Claude Code folder, whichever provider the run uses: {', '.join(homes)}; and ~/.claude",
            "    logins, keys, tokens, browsing data, messages and histories under the home folder (built in): "
            f"{', '.join('~/' + x for x in HOME_SECRETS)}{mac}",
            "    folders and files the principal keeps from workers (set on the web page, Settings; you cannot set"
            " it: hansa ask; Read is denied there to you, workers, verifiers and design sessions as well; no"
            " lane's repository or write list may be one, inside one or above one): " + (", ".join(kept) or "none"),
            *(f"      not applied: {x.removeprefix('workers cannot be kept from reading ')}" for x in left)]


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
            out.append(f"{step}: {x['command']}\n  did not run: the setup failed")
            continue
        how = "interrupted" if x["exit"] is None else f"exit {x['exit']}"
        out.append(f"{step}: {x['command']}\n  {how}" + (f" in {x['seconds']}s" if x["seconds"] is not None else "")
                   + (", last lines:" if x["tail"] else ", no output"))
        out += [f"    {plain(line, None)}" for line in x["tail"]]
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
    session = None
    for r in rep["turns"]:
        if r["session"] != session:
            session = r["session"]
            out.append(f"session {session[:8]}")
        tools = ", ".join(f"{k} {n}" for k, n in r["tools"].items())
        out.append(f"  {time.strftime('%m-%d %H:%M', time.localtime(r['start']))}  {r['kind']:<13} "
                   f"{r['steps']:>4} step(s)  length {r['length']:>9,}  {'$%.2f' % r['usd']:>8}" + (f"  {tools}" if tools else ""))
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
