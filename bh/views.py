"""Views: the facts behind `bh status`, `bh show`, `bh explain` and the web page.
Each view returns plain data; the *_text helpers turn it into short lines."""
from __future__ import annotations

import time
from pathlib import Path

from . import cost
from .desk import Desk, read_json
from .run import progress_of, provider_problem, waiting_reason

STATES = {
    "queued": "waiting for a free lane and a ready provider",
    "running": "a worker is on it, or bh is running the lane's check on its work",
    "done": "the worker finished; waiting for your review (bh show / bh merge / bh retry)",
    "failed": "the run ended with an error; bh show says why",
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
    return "checking now" if m.get("phase") == "checking" else _progress_line(progress_of(desk, m))


def _mins(since) -> int:
    return int((time.time() - (since or time.time())) // 60)


def _midnight() -> float:
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def status(desk: Desk) -> dict:
    s = desk.settings()
    tasks = desk.tasks()
    pst = desk.provider_state()
    running = [m for m in tasks if m["state"] == "running"]
    busy = {m.get("lane") for m in running}
    hb = read_json(desk.state_dir / "heartbeat", 0) or 0
    today = [m for m in tasks if (m.get("finished") or 0) >= _midnight()]
    rows = cost.worker_rows(desk, _midnight())
    return {
        "desk": str(desk.root),
        "paused": desk.paused(),
        "server_up": time.time() - hb < 30,
        "server_seconds_ago": int(time.time() - hb) if hb else None,
        "concurrency": s["concurrency"],
        "lanes": [{"name": n, "providers": ln["providers"], "repo": ln["repo"],
                   "task": next((m["id"] for m in running if m.get("lane") == n), None)}
                  for n, ln in sorted(s["lanes"].items())],
        "providers": [{"name": n, "kind": p["kind"], "model": p["model"], "effort": p.get("effort", ""),
                       "problem": provider_problem(desk, s, n), "check": pst.get(n, {}).get("check")}
                      for n, p in sorted(s["providers"].items())],
        "running": [{"id": m["id"], "title": m["title"], "lane": m.get("lane"), "provider": m.get("provider"),
                     "model": m.get("model_asked"), "minutes": _mins(m.get("started")),
                     "checking": m.get("phase") == "checking", "browser": bool(m.get("browser")),
                     "progress": None if m.get("phase") == "checking" else progress_of(desk, m)} for m in running],
        "queued": [{"id": m["id"], "title": m["title"], "lane": m.get("lane") or "any",
                    "waiting": waiting_reason(desk, s, m, busy)} for m in tasks if m["state"] == "queued"],
        "review": [{"id": m["id"], "title": m["title"], "lane": m.get("lane"), "stat": m.get("stat", ""),
                    "commits": m.get("commits", 0), "denied": len(m.get("denials", [])),
                    "check": None if not m.get("check") else m["check"]["ok"], "verify": verification(desk, m)}
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
           "server up" if st["server_up"] else "server NOT running: start it with `bh serve` (see bh explain server)"]
    out = [" · ".join(out + [time.strftime("now %H:%M")])]
    out.append("lanes:")
    for ln in st["lanes"] or [{"name": "(none)", "providers": [], "task": None}]:
        out.append(f"  {ln['name']:<8} {' > '.join(ln['providers']):<24} {ln['task'] or 'free'}")
    if st["running"]:
        out.append("running:")
        out += [f"  {m['id']} {m['lane']} " + ("worker done, bh is running the lane's check" if m["checking"]
                                               else f"on {m['provider']} ({m['model']})")
                + (" with a browser" if m["browser"] else "") + f", {m['minutes']} min: {m['title']}"
                + (f"\n      so far: {_progress_line(m['progress'])}" if m.get("progress") else "")
                for m in st["running"]]
    if st["queued"]:
        out.append("queued:")
        out += [f"  {m['id']} lane {m['lane']}: {m['title']}\n      waiting: {m['waiting']}" for m in st["queued"]]
    if st["review"]:
        out.append("waiting for review (bh show <id>):")
        out += [f"  {m['id']} {m['lane']}: {m['title']} [{m['stat'] or 'no changes'}"
                + {True: "; check passed", False: "; check FAILED", None: ""}[m["check"]]
                + (f"; verification {_verification_line(m['verify'])}" if m.get("verify") else "")
                + (f"; {m['denied']} command(s) refused" if m["denied"] else "") + "]" for m in st["review"]]
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
               f" (worth ${t['worker_usd']:.2f}; bh cost for everything)")
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
    out = [head, f"title: {m['title']}", f"meaning: {meaning}"]
    if m.get("error"):
        out.append(f"error: {m['error']}")
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
    if chk:
        how = "passed" if chk["ok"] else "FAILED" + (f" ({chk['note']})" if chk["note"] else f" (exit {chk['exit']})")
        out.append(f"check (run by bh, not the worker): {how} in {chk['seconds']} s: {chk['command']}")
        if not chk["ok"]:
            out += ["  " + x[:200] for x in chk["tail"]]
    elif m.get("phase") == "checking":
        out.append(f"check: running now: {m.get('check_command', '')}")
    if m["state"] == "running" and m.get("phase") != "checking":
        out.append(f"so far: {_progress_line(progress_of(desk, m))} (turns and tokens are recorded when it ends)")
    v = verification(desk, m)
    if v:
        out.append(f"verification (a worker ran the change): {_verification_line(v)}"
                   + ("" if v["state"] in ("queued", "running") else f"; bh show {v['task']} says what it ran"))
    if verifying and m.get("state") == "done":
        out.append(f"verdict: {m.get('verdict') or 'none given'}")
    for model, u in (m.get("usage") or {}).items():
        out.append(f"usage {model}: in {u['in']:,} · out {u['out']:,} · cache read {u['cache_read']:,}"
                   f" · cache write {u['cache_write']:,}")
    nxt = {"done": f"bh retry {tid} \"what to change\" · bh cancel {tid} to close it" if not m.get("commits")
           else f"bh diff {tid} · bh merge {tid} · bh retry {tid} \"what to change\"" if not chk or chk["ok"]
           else f"bh retry {tid} \"make the check pass: ...\" · bh diff {tid} · bh merge {tid} --despite-check",
           "failed": (f"read its notes, then split the task or make it clearer (bh add ...); do not re-run it unchanged"
                      if str(m.get("error", "")).startswith("stopped at the ceiling")
                      else f"bh retry {tid} \"what to change\" (or --fresh to start over)"),
           "queued": "bh status shows what it is waiting for", "running": f"bh cancel {tid} to stop it"}
    if v and v["verdict"] == "does not hold" and m["state"] == "done":
        nxt["done"] = f"bh retry {tid} \"fix what {v['task']} found: ...\" · bh merge {tid} --despite-check"
    if v and v["state"] in ("queued", "running") and m["state"] == "done":
        nxt["done"] = f"wait for the verification: bh wait {v['task']} (bh merge {tid} is held until it ends)"
    if verifying:
        nxt = {"done": f"read the report above, then merge or send back {m.get('parent')}",
               "failed": f"bh verify {m.get('parent')} to try again", "running": f"bh cancel {tid} to stop it",
               "queued": "bh status shows what it is waiting for"}
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
            f"lane {name} (set by the principal on the web page; ask with bh ask to change it)",
            f"  repo: {ln['repo']} · base branch: {ln.get('base', 'main')} · worktree: {wt}",
            f"  providers, in order: {' > '.join(ln['providers'])} (the next one takes over when one hits its limit)",
            f"  per task: stopped at {ln.get('max_turns', 300)} turns or {ln.get('timeout_min', 240)} minutes"
            " (check included); set high, so reaching it means something went wrong",
            "  check after each task: " + (f"{ln['check']} (bh runs it in the worktree after the worker ends;"
                                            " bh merge refuses a failed check)" if ln.get("check") else "none"),
            "  environment set for workers: " + (", ".join(f"{k}={v}" for k, v in ln.get("env", {}).items()) or "none"),
            "  what a worker may use (anything else is refused and shows up in bh show):",
            "    Read, Edit and Write inside the lane's worktree only, and read-only commands there"
            " such as grep, find and sed -n",
            *["    " + r for r in ln.get("allow", [])],
            f"  standing instructions: {brief} ({'set' if brief.is_file() and brief.read_text().strip() else 'empty'};"
            " you may edit this file)"])
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
        return ("`bh serve` starts queued tasks, finishes ended ones and serves the web page. It runs on the "
                "desk's machine, usually in a terminal multiplexer. Without it, tasks stay queued.")
    return "\n".join([
        "What you can look up: bh explain lane <name> | provider <name> | task <id> | states | server",
        f"lanes: {', '.join(sorted(s['lanes'])) or 'none'} · providers: {', '.join(sorted(s['providers']))}",
        f"coordinator: {s['coordinator']['provider']} ({s['coordinator']['model']}) · "
        f"cost audit by the coordinator: {'on' if s['coordinator'].get('audits_cost') else 'off'}",
        "Settings, keys, prices and allowed commands belong to the principal (web page). "
        "You change tasks, lane briefs and pause/resume."])


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
    out.append(rep["note"])
    return "\n".join(out)


def diff_text(desk: Desk, tid: str, stat_only: bool = False) -> str:
    from .run import git
    m = desk.meta(tid)
    if not m.get("head") or not m.get("worktree"):
        return f"{tid} has no changes recorded yet (state: {m['state']})"
    args = ["diff", "--stat" if stat_only else "--patch", m["base"], m["head"]]
    return git(*args, cwd=Path(m["worktree"])) or "(no changes)"
