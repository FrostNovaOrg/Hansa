"""Views: the facts behind `hansa status`, `hansa show`, `hansa explain` and the web page.
Each view returns plain data; the *_text helpers turn it into short lines."""
from __future__ import annotations

import time
from pathlib import Path

from . import build, cost
from .desk import Desk, is_git_rule, read_json
from .run import (join_command, lane_occupied, markers_text, progress_of, provider_problem, server_up, srt_needed,
                  srt_problem, srt_record, tried_lanes, waiting_reason)

STATES = {
    "queued": "waiting for a free lane and a ready provider",
    "running": "a worker is on it, or hansa is running the lane's check on its work",
    "done": "the worker finished; waiting for your review (hansa show / hansa merge / hansa retry)",
    "merging": ("hansa merge is checking it together with what the base gained since it was made: merged onto the "
                "base as it is now, with the lane's check run there; it merges if that passes"),
    "failed": "the run ended with an error; hansa show says why",
    "merged": "merged into the lane's base branch",
    "cancelled": "stopped or closed on request",
    "replaced": "a follow-up task took its place",
}


def verification(desk: Desk, m: dict):
    """The latest verification of a task: {task, state, verdict}, or None."""
    vid = m.get("verify")
    if not vid:
        return None
    v = desk.meta(vid)
    return {"task": vid, "state": v["state"], "verdict": v.get("verdict", "")}


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
    other = merge_check_busy_with(desk, m, tasks)
    return (f"merge check queued{again}: " + (f"waits for lane {lane}, busy with {other}" if other
                                              else "starts when `hansa serve` next looks (within seconds)"
                                              if server_up(desk) else "`hansa serve` is not running"))


def machine_hint(r: dict) -> str:
    """Why a check or merge check that did not pass may be the machine's doing and not the task's; '' when
    nothing says so."""
    if r.get("could_not_run"):
        return ("it never ran, so the task is not judged on it: this is the machine's problem, not the task's "
                "(hansa explain server)")
    if r.get("ok") or r.get("note"):  # stopped, over the time ceiling, its setup failed: said in the note
        return ""
    if not r.get("tail"):
        return ("nothing came out: it printed nothing at all, which usually means the command could not start, "
                "the machine's problem rather than the task's (hansa explain server)")
    if r.get("exit") in (126, 127):
        return (f"exit {r['exit']} usually means a command was not found or could not start, which may be the "
                f"machine's problem rather than the task's (hansa explain server)")
    return ""


def merge_result_lines(m: dict) -> list:
    """What the last merge check found, for hansa show."""
    r = m.get("merge_check_result")
    if not r:
        return []
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
            + ["  " + x[:200] for x in r.get("tail", [])] + (["  " + hint] if hint else []))


def _midnight() -> float:
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def status(desk: Desk) -> dict:
    s = desk.settings()
    tasks = desk.tasks()
    pst = desk.provider_state()
    designing = [m for m in tasks if m["state"] == "running" and m.get("kind") == "design"]
    running = [m for m in tasks if m["state"] == "running" and m.get("kind") != "design"]
    merging = [m for m in tasks if m["state"] == "merging"]
    tried = tried_lanes(desk)
    busy = {m.get("lane") for m in tasks if lane_occupied(m)} | tried
    hb = read_json(desk.state_dir / "heartbeat", 0) or 0
    today = [m for m in tasks if (m.get("finished") or 0) >= _midnight()]
    rows = cost.worker_rows(desk, _midnight())
    return {
        "desk": str(desk.root),
        "build": build(),
        "audits_cost": bool(s["coordinator"].get("audits_cost")),
        "paused": desk.paused(),
        "server_up": server_up(desk),
        "server_seconds_ago": int(time.time() - hb) if hb else None,
        "srt_missing": srt_problem(desk, s),  # what stops setups and checks, as hansa serve last saw srt
        "srt": {**srt_record(desk), "needed": srt_needed(s)},
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
                     "browser": bool(m.get("browser")),
                     "progress": None if m.get("phase") in ("checking", "starting") else progress_of(desk, m)}
                    for m in running],
        "design": [{"id": m["id"], "title": m["title"], "lane": m.get("lane"), "minutes": _mins(m.get("started")),
                    "join": join_command(m), "checking": m.get("phase") == "checking",
                    "progress": None if m.get("phase") == "checking" else progress_of(desk, m)} for m in designing],
        "queued": [{"id": m["id"], "title": m["title"], "lane": m.get("lane") or "any",
                    "waiting": waiting_reason(desk, s, m, busy)} for m in tasks if m["state"] == "queued"],
        "merging": [{"id": m["id"], "title": m["title"], "lane": m.get("lane"),
                     "phase": (m.get("merge_check") or {}).get("phase", "queued"),
                     "minutes": _mins((m.get("merge_check") or {}).get("started")),
                     "busy_with": merge_check_busy_with(desk, m, tasks),
                     "again": bool((m.get("merge_check") or {}).get("moves")),
                     "where": merge_check_text(desk, m, tasks)} for m in merging],
        "review": [{"id": m["id"], "title": m["title"], "lane": m.get("lane"), "stat": m.get("stat", ""),
                    "commits": m.get("commits", 0), "denied": len(m.get("denials", [])), "leaked": m.get("leaked", []),
                    "markers": sorted(m.get("markers") or {}),
                    "check": None if not m.get("check") else m["check"]["ok"],
                    "check_could_not_run": bool((m.get("check") or {}).get("could_not_run")), "verify": verification(desk, m),
                    "merge_check": (m.get("merge_check_result") or {}).get("ok"),
                    "merge_note": (m.get("merge_check_result") or {}).get("note")
                    or f"exit {(m.get('merge_check_result') or {}).get('exit')}"}
                   for m in tasks if m["state"] == "done" and m.get("kind") != "verify"],
        "failed": [{"id": m["id"], "title": m["title"], "lane": m.get("lane"), "error": m.get("error", "")}
                   for m in tasks if m["state"] == "failed"][-5:],
        "asks": [a for a in desk.asks() if not a.get("closed")],
        "today": {"finished": len(today), "merged": sum(m["state"] == "merged" for m in today),
                  "worker_tokens": sum(sum(r[k] for k in cost.TOKEN_FIELDS) for r in rows),
                  "worker_usd": round(sum(r["usd"] for r in rows if r["usd"] is not None), 2)},
    }


def status_text(st: dict) -> str:
    out = [f"desk {st['desk']}",
           ("PAUSED: " + st["paused"]) if st["paused"] else f"dispatch on, {st['concurrency']} at a time",
           "server up" if st["server_up"] else "server NOT running: start it with `hansa serve` (see hansa explain server)"]
    out += ["cost audit on"] if st.get("audits_cost") else []
    out = [" · ".join(out + [f"hansa {st['build']}", time.strftime("now %H:%M")])]
    out += [st["srt_missing"]] if st.get("srt_missing") else []
    out.append("lanes:")
    for ln in st["lanes"] or [{"name": "(none)", "providers": [], "task": None}]:
        out.append(f"  {ln['name']:<8} {' > '.join(ln['providers']):<24} {ln['task'] or ('held by a try (hansa lane try)' if ln.get('trying') else 'free')}")
    if st["running"]:
        out.append("running:")
        out += [f"  {m['id']} {m['lane']} " + ("worker done, hansa is running the lane's check" if m["checking"]
                                               else "hansa is preparing it" if m.get("starting")
                                               else f"on {m['provider']} ({m['model']})")
                + (" with a browser" if m["browser"] else "") + f", {m['minutes']} min: {m['title']}"
                + (f"\n      so far: {_progress_line(m['progress'])}" if m.get("progress") else "")
                for m in st["running"]]
    if st.get("design"):
        out.append("design sessions (the principal works in them directly; each comes back for review when they end it):")
        out += [f"  {m['id']} lane {m['lane']}, {m['minutes']} min: {m['title']}"
                + ("\n      ended; hansa is running the lane's check" if m["checking"] else
                   f"\n      so far: {_progress_line(m['progress'])}" if m.get("progress") else "")
                for m in st["design"]]
    if st["queued"]:
        out.append("queued:")
        out += [f"  {m['id']} lane {m['lane']}: {m['title']}\n      waiting: {m['waiting']}" for m in st["queued"]]
    if st.get("merging"):
        out.append("merging (each merges by itself if its check passes on the base as it is now; hansa wait <id>):")
        out += [f"  {m['id']} {m['lane']}: {m['title']}\n      {m['where']}" for m in st["merging"]]
    if st["review"]:
        out.append("waiting for review (hansa show <id>):")
        out += [f"  {m['id']} {m['lane']}: {m['title']} [{m['stat'] or 'no changes'}"
                + ("; check could not run (the machine's problem, not the task's)" if m.get("check_could_not_run")
                   else {True: "; check passed", False: "; check FAILED", None: ""}[m["check"]])
                + (f"; merge check FAILED ({m['merge_note']}), nothing merged"
                   if m.get("merge_check") is False else "")
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
    for p in st["providers"]:
        out.append(f"  {p['name']:<8} {p['kind']:<12} {p['model']:<22} {p['problem'] or 'ready'}")
    if st["asks"]:
        out.append("open asks to the principal:")
        out += [f"  #{a['id']}: {a['text'][:100]}" for a in st["asks"]]
    t = st["today"]
    out.append(f"today: {t['finished']} finished, {t['merged']} merged; workers used {t['worker_tokens']:,} tokens"
               f" (worth ${t['worker_usd']:.2f}; hansa cost for everything)")
    return "\n".join(out)


def show_text(desk: Desk, tid: str) -> str:
    m = desk.meta(tid)
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
    if m.get("kind") == "design":
        meaning = ("a design session: the principal shapes this feature with it directly, on the real code; "
                   + ("it is open now" if m["state"] == "running" and m.get("phase") != "checking"
                      else "it comes back for review when they end it" if m["state"] == "queued" else meaning))
    out = [head, f"title: {m['title']}", f"meaning: {meaning}"]
    if m.get("error"):
        out.append(f"error: {m['error']}")
    if m.get("machine_problem") and m["state"] == "failed":
        out.append("  this is the machine's problem, not the task's: srt, which runs the lane's setup, could not run "
                   "on the server, so the worker never started (hansa explain server)")
    for a in m.get("attempts", []):
        out.append(f"earlier try on {a['provider']}: {a['error']}")
    report = (d / "result.md").read_text(encoding="utf-8").strip() if (d / "result.md").exists() else ""
    if report:
        lines = report.splitlines()
        out += ["report:"] + ["  " + x for x in lines[:60]]
        if len(lines) > 60:
            out.append(f"  ... {len(lines) - 60} more lines; the whole report is {d / 'result.md'}")
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
                "its last lines:"] + ["  " + x[:200] for x in tail]
    if m.get("denials"):
        out.append(f"refused by the lane's allowed commands ({len(m['denials'])}):")
        for x in m["denials"][:8]:
            first, *more = x.splitlines() or [""]
            out.append(f"  {first[:160]}" + (f"  (+{len(more)} more lines)" if more else ""))
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
            out += ["  " + x[:200] for x in chk["tail"]]
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
    if m["state"] == "running" and m.get("phase") == "starting":
        out.append("so far: hansa is preparing it; the worker starts in a moment")
    elif m["state"] == "running" and m.get("phase") != "checking":
        out.append(f"so far: {_progress_line(progress_of(desk, m))} (turns and tokens are recorded when it ends)")
        if m.get("kind") == "design":
            out.append(f"the principal joins it with: {join_command(m)}")
    v = verification(desk, m)
    if v:
        out.append(f"verification (a worker ran the change): {_verification_line(v)}"
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
           else (f"once the machine is fixed (hansa explain server): hansa merge {tid} --recheck (runs the check "
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
        nxt["done"] = f"wait for the verification: hansa wait {v['task']} (hansa merge {tid} is held until it ends)"
    mr = m.get("merge_check_result") or {}
    if mr and not mr.get("ok") and m["state"] == "done" and machine_hint(mr):
        nxt["done"] = (f"once the machine is fixed (hansa explain server): hansa merge {tid} --recheck (runs the check "
                       f"again and merges if it passes) · hansa retry {tid} \"...\" · hansa merge {tid} --despite-check")
    elif mr and not mr.get("ok") and m["state"] == "done":
        nxt["done"] = (f"hansa retry {tid} --update \"make it work with what the base has now: ...\" · "
                       f"hansa merge {tid} again (it checks the merge again) · hansa merge {tid} --despite-check")
    nxt["merging"] = (f"hansa wait {tid}: it merges by itself if the check passes, and comes back for review if not "
                      f"· hansa cancel {tid} stops it")
    if m.get("markers") and m["state"] == "done":
        nxt["done"] = f"hansa retry {tid} --update \"settle the markers in {', '.join(m['markers'])}\""
    if m.get("leaked") and m["state"] == "done":
        nxt["done"] = (f"hansa retry {tid} --fresh \"...\" (the same work, without the key) · tell the principal with "
                       f"hansa ask (they may want to change that key) · hansa cancel {tid}")
    if verifying:
        nxt = {"done": f"read the report above, then merge or send back {m.get('parent')}",
               "failed": f"hansa verify {m.get('parent')} to try again", "running": f"hansa cancel {tid} to stop it",
               "queued": "hansa status shows what it is waiting for"}
    if m["state"] in nxt:
        out.append("next: " + nxt[m["state"]])
    return "\n".join(out)


def explain_text(desk: Desk, what: str = "", name: str = "") -> str:
    s = desk.settings()
    if what == "lane":
        ln = s["lanes"].get(name)
        if not ln:
            return f"no lane {name}; lanes: {', '.join(sorted(s['lanes'])) or 'none'}"
        wt = desk.worktrees_dir / name
        brief = desk.lanes_dir / name / "brief.md"
        return "\n".join([
            f"lane {name} (set by the coordinator with hansa lane set, not by the principal on the web page)",
            f"  repo: {ln['repo']} · base branch: {ln.get('base', 'main')} · worktree: {wt}",
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
            "  environment set for workers: " + (", ".join(f"{k}={v}" for k, v in ln.get("env", {}).items()) or "none"),
            "  what a worker may use (it runs in Claude Code's sandbox, which the operating system enforces;"
            " what it refuses shows up in hansa show):",
            "    any command, but it changes files only in the lane's worktree (and its temporary folder and the"
            " folders listed below);"
            " it cannot read the desk's keys and state or common credential files (.ssh, .aws, .netrc and the"
            " like); it reaches no network but the hosts below; git history is left to hansa (no commit, merge,"
            " reset, branch and the like), which commits its changes",
            "  network during a task: " + (", ".join(ln.get("network", [])) or "none"),
            "  folders a worker may also write (a build or test tool's cache): "
            + (", ".join(ln.get("write", [])) or "none"),
            "  allowed commands (a worker in the sandbox needs none; a design session runs outside the sandbox"
            " and may run these plus its own git add, commit and merge):"
            + ("" if ln.get("allow") else " none"),
            *["    " + r + (" (ignored: hansa handles git)" if is_git_rule(r) else "")
              for r in ln.get("allow", [])],
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
                f"hansa serve tried srt at {time.strftime('%m-%d %H:%M', time.localtime(rec.get('at') or 0))}: "
                + ("it ran" + (f" ({rec.get('path')})" if rec.get("path") else "") if rec.get("ran") else
                   f"found at {rec.get('path')}, but it could not run: {rec.get('said')}" if rec.get("found")
                   else "not found on its PATH"))
        return "\n".join([
            "`hansa serve` starts queued tasks, finishes ended ones and serves the web page. It runs on the "
            "desk's machine, usually in a terminal multiplexer. Without it, tasks stay queued. One runs per "
            "desk: a second one refuses to start.",
            "It runs each lane's setup and check in srt (Anthropic's sandbox runtime), with its own PATH, which can "
            "differ from the coordinator's. It tries srt (a run of `true` in a sandbox) when it starts and before "
            "each setup or check, and records what it saw; hansa status reports that record.",
            f"  now: {seen}",
            "When srt cannot run, the setup or check does not run either, and the task is not judged on it: its "
            "check reads \"could not run\". That is the machine's problem, not the task's. srt must be on the PATH "
            "hansa serve starts with, and so must node, since srt is a node script (an error such as `env: node: "
            "No such file or directory`, exit 127, means node is not). Install it with `npm install -g "
            "@anthropic-ai/sandbox-runtime`, or fix the PATH, then restart hansa serve. Then, for a task whose "
            "check could not run or failed for the machine's sake: hansa merge ID --recheck runs the lane's check "
            "again and merges if it passes; a task whose setup could not run: hansa retry ID."])
    return "\n".join([
        "What you can look up: hansa explain lane <name> | provider <name> | task <id> | states | server",
        f"lanes: {', '.join(sorted(s['lanes'])) or 'none'} · providers: {', '.join(sorted(s['providers']))}",
        f"coordinator: {s['coordinator']['provider']} ({s['coordinator']['model']}) · "
        f"cost audit by the coordinator: {'on' if s['coordinator'].get('audits_cost') else 'off'}",
        "Providers, models, endpoints, keys, effort defaults and prices belong to the principal (web page). "
        "You change tasks, lanes (hansa lane), how many run at once (hansa concurrency), lane briefs (hansa lane brief) and "
        "pause/resume."])


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
        out += [f"    {line}" for line in x["tail"]]
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


def diff_text(desk: Desk, tid: str, stat_only: bool = False) -> str:
    from .run import GitFileChanged, wgit
    m = desk.meta(tid)
    if not m.get("head") or not m.get("worktree"):
        return f"{tid} has no changes recorded yet (state: {m['state']})"
    args = ["diff", "--stat" if stat_only else "--patch", m["base"], m["head"]]
    try:  # the worktree's .git is checked first; the task working there now fails for it when it ends
        return desk.redact(wgit(desk, Path(m["worktree"]), *args)) or "(no changes)"
    except GitFileChanged as e:
        return f"hansa: {e}; the diff is not shown"
