"""Goals and product decisions: immutable entries in the desk's private state."""
from __future__ import annotations

import re
import time

from . import cost, notify, release
from .desk import Desk, event_record, now, read_json, write_json


def entries(desk: Desk) -> list:
    folder = desk.state_dir / "intent"
    return [read_json(p) for p in sorted(folder.glob("*.json"), key=lambda p: int(p.stem))]


def records(desk: Desk) -> tuple[dict, dict]:
    goals, decisions = {}, {}
    for e in entries(desk):
        if e["event"] == "goal_added":
            goals[e["id"]] = {**e, "state": "open", "history": []}
        elif e["event"] == "goal_ended":
            goals[e["id"]].update(state=e["state"], outcome=e["outcome"], ended=e["created"],
                                   ended_session=e.get("session"))
            goals[e["id"]]["history"].append({"event": "ended", "state": e["state"], "outcome": e["outcome"],
                                              "created": e["created"], "session": e.get("session")})
        elif e["event"] == "goal_reopened":
            g = goals[e["id"]]
            g["history"].append({"event": "reopened", "was": g["state"], "why": e["why"],
                                 "created": e["created"], "session": e.get("session")})
            g["state"] = "open"
            for k in ("outcome", "ended", "ended_session"):
                g.pop(k, None)
        elif e["event"] == "goal_tied":
            items = e["tasks"] + [f"#{n}" for n in e["todos"]]
            goals[e["id"]]["history"].append({"event": "tied", "items": items, "from": e["from"],
                                              "created": e["created"], "session": e.get("session")})
            moved = {}
            for item in items:
                if e["from"].get(item):
                    moved.setdefault(e["from"][item], []).append(item)
            for old, gone in moved.items():
                if old in goals:
                    goals[old]["history"].append({"event": "moved", "items": gone, "to": e["id"],
                                                  "created": e["created"], "session": e.get("session")})
        elif e["event"] == "decision_added":
            decisions[e["id"]] = dict(e)
            if e.get("replaces"):
                decisions[e["replaces"]]["replaced_by"] = e["id"]
    return goals, decisions


def _append(desk: Desk, **entry) -> None:
    """Called with the desk lock: atomic publication of one new file, never rewriting an entry."""
    folder = desk.state_dir / "intent"
    folder.mkdir(exist_ok=True)
    n = max((int(p.stem) for p in folder.glob("*.json")), default=0) + 1
    write_json(folder / f"{n}.json", {**entry, "created": now(), **notify.recorded(notify.session_here())})


def _text(desk: Desk, text: str, what: str) -> str:
    if not text.strip():
        raise SystemExit(f"hansa: give {what}; nothing was recorded")
    keys = desk.keys_in(text)
    if keys:
        raise SystemExit(f"hansa: {what} holds a key hansa knows ({', '.join(keys)}); nothing was recorded")
    return text


def goal(desk: Desk, gid: str, *, open_only: bool = False) -> dict:
    g = records(desk)[0].get(gid)
    if not g:
        raise SystemExit(f"hansa: no goal {desk.redact(gid)}; nothing was changed")
    if open_only and g["state"] != "open":
        raise SystemExit(f"hansa: goal {gid} is {g['state']}; use an open goal; nothing was changed")
    return g


def add(desk: Desk, title: str, why: str = "") -> str:
    with desk.lock():
        _text(desk, title, "the goal's title")
        if why:
            _text(desk, why, "the goal's reason")
        gid = f"g{len(records(desk)[0]) + 1}"
        _append(desk, event="goal_added", id=gid, title=title, why=why)
    return gid


def still_open(desk: Desk, gid: str, tasks: list | None = None) -> list:
    """The goal's work not settled yet: latest tasks queued, running or waiting for review, and open to-dos."""
    tasks = desk.tasks() if tasks is None else tasks
    out = [{"id": m["id"], "state": m["state"], "title": m.get("title", "")}
           for m in latest_tasks(tasks, gid) if m["state"] in OPEN_STATES and not m.get("closed")]
    return out + [{"id": f"#{a['id']}", "state": "open to-do", "title": a.get("text", "")}
                  for a in desk.asks() if a.get("goal") == gid and not a.get("closed")]


OPEN_STATES = {"queued": "queued", "running": "running", "done": "waiting for review", "merging": "waiting for review"}


def end(desk: Desk, gid: str, state: str, outcome: str) -> list:
    """Ends the goal even with work still open under it, and returns that work (still_open)."""
    with desk.lock():
        goal(desk, gid, open_only=True)
        _text(desk, outcome, "the goal's outcome" if state == "done" else "why the goal was dropped")
        _append(desk, event="goal_ended", id=gid, state=state, outcome=outcome)
        return still_open(desk, gid)


def ended_text(gid: str, state: str, left: list) -> str:
    """What goal done or drop did; the id and state stay the first words."""
    if not left:
        return f"{gid} {state}"
    return "\n".join([f"{gid} {state}; still open under it ({len(left)}):"]
                      + [f"  {w['id']} {OPEN_STATES.get(w['state'], w['state'])} · {one_line(w['title'])}"
                         for w in left])


def reopen(desk: Desk, gid: str, why: str) -> dict:
    """Open a done or dropped goal again; the end stays on record and the reopening is a new entry."""
    with desk.lock():
        g = goal(desk, gid)
        if g["state"] == "open":
            raise SystemExit(f"hansa: goal {gid} is open; only a done or dropped goal can be reopened; "
                             "nothing was recorded")
        _text(desk, why, "why the goal is reopened")
        _append(desk, event="goal_reopened", id=gid, why=why)
    return g


def reopened_text(g: dict, why: str) -> str:
    return (f"{g['id']} reopened: {one_line(g['title'])}\n  was {g['state']}: {one_line(g['outcome'])}"
            f"\n  why: {one_line(why)}")


def _chain(tasks: list, tid: str) -> list:
    """The task with every task linked to it by retries, follow-ups and verifications, in id order."""
    near = {}
    for m in tasks:
        if m.get("parent"):
            near.setdefault(m["id"], set()).add(m["parent"])
            near.setdefault(m["parent"], set()).add(m["id"])
    seen, todo = {tid}, [tid]
    while todo:
        for n in near.get(todo.pop(), ()):
            if n not in seen:
                seen.add(n)
                todo.append(n)
    return sorted(seen)


def tie(desk: Desk, gid: str, refs: list) -> dict:
    """Tie tasks (tN, with their retry chain) and to-dos (#N or N) to an open goal, moving them from any
    other goal. One new entry records what moved and from where; nothing earlier is rewritten. Returns
    {"tasks": [...], "todos": [...], "from": {item: old goal or ""}, "asked": [...], "already": [...]}."""
    with desk.lock():
        goal(desk, gid, open_only=True)
        tasks = {m["id"]: m for m in desk.tasks()}
        asks = desk.asks()
        ask_ids = {a["id"] for a in asks}
        wanted_tasks, wanted_todos = [], []
        for ref in refs:
            r = ref.strip()
            number = r[1:] if r.startswith("#") else r
            if number.isdigit():
                if int(number) not in ask_ids:
                    raise SystemExit(f"hansa: no to-do #{int(number)}; nothing was tied")
                wanted_todos.append(int(number))
            elif re.fullmatch(r"t\d+", r):
                if r not in tasks:
                    raise SystemExit(f"hansa: no task {r}; nothing was tied")
                wanted_tasks.append(r)
            else:
                raise SystemExit(f"hansa: {desk.redact(r)!r} is neither a task (t0001) nor a to-do (#1 or 1); "
                                 "nothing was tied")
        all_tasks = sorted({t for r in wanted_tasks for t in _chain(list(tasks.values()), r)})
        todos = sorted(set(wanted_todos))
        before = {t: tasks[t].get("goal") or "" for t in all_tasks}
        before.update({f"#{a['id']}": a.get("goal") or "" for a in asks if a["id"] in todos})
        moving = [t for t in all_tasks if before[t] != gid]
        moving_todos = [n for n in todos if before[f"#{n}"] != gid]
        already = [i for i, g in before.items() if g == gid]
        if moving or moving_todos:
            _append(desk, event="goal_tied", id=gid, tasks=moving, todos=moving_todos,
                    **{"from": {i: before[i] for i in moving + [f"#{n}" for n in moving_todos]}})
            for t in moving:
                desk.change(t, lambda m: {"goal": gid})
            if moving_todos:
                for a in asks:
                    if a["id"] in moving_todos:
                        a["goal"] = gid
                write_json(desk.asks_path, asks)
    return {"tasks": moving, "todos": moving_todos, "from": {i: before[i] for i in before if before[i] != gid},
            "asked": wanted_tasks + [f"#{n}" for n in wanted_todos], "already": already}


def tied_text(desk: Desk, gid: str, done: dict) -> str:
    g = records(desk)[0][gid]
    items = done["tasks"] + [f"#{n}" for n in done["todos"]]
    if not items:
        return f"{gid} {one_line(g['title'])}: nothing tied; {', '.join(done['already'])} already tied to it; nothing was recorded"
    out = [f"{gid} {one_line(g['title'])}: tied {', '.join(items)}"]
    for item in items:
        old = done["from"].get(item)
        how = f"moved from {old}" if old else "had no goal"
        if item.startswith("t") and item not in done["asked"]:
            how += ", in the retry chain of " + ", ".join(t for t in done["asked"] if t.startswith("t"))
        out.append(f"  {item}: {how}")
    if done["already"]:
        out.append(f"  already tied: {', '.join(done['already'])}")
    return "\n".join(out)


def decide(desk: Desk, text: str, why: str, *, gid: str | None = None, by: str = "coordinator",
           task: str = "", replaces: str = "") -> str:
    """gid None: a replacement keeps the goal of the decision it replaces (also an ended one); "" is no goal."""
    with desk.lock():
        _text(desk, text, "the decision")
        _text(desk, why, "the decision's reason")
        if gid:
            goal(desk, gid, open_only=True)
        if task and task not in {m["id"] for m in desk.tasks()}:
            raise SystemExit(f"hansa: no task {desk.redact(task)}; nothing was recorded")
        decisions = records(desk)[1]
        if replaces:
            old = decisions.get(replaces)
            if not old:
                raise SystemExit(f"hansa: no decision {desk.redact(replaces)}; nothing was recorded")
            if old.get("replaced_by"):
                raise SystemExit(f"hansa: {replaces} was already replaced by {old['replaced_by']}; nothing was recorded")
            if gid is None:
                gid = old.get("goal") or ""
        gid = gid or ""
        did = f"d{len(decisions) + 1}"
        _append(desk, event="decision_added", id=did, text=text, why=why, goal=gid, by=by,
                task=task, replaces=replaces)
    return did


def latest_tasks(tasks: list, gid: str) -> list:
    linked = [m for m in tasks if m.get("goal") == gid]
    parents = {m.get("parent") for m in linked if m.get("kind") != "verify" and not m.get("restored_parent")}
    return [m for m in linked if m.get("kind") == "verify" or m["state"] == "merged" or m["id"] not in parents]


def summaries(desk: Desk, tasks: list | None = None, *, all_: bool = False, evidence: bool = False,
              context: dict | None = None) -> list:
    tasks = desk.tasks() if tasks is None else tasks
    asks = desk.asks()
    if evidence and context is None:
        context = evidence_records(desk)
    out = []
    for g in records(desk)[0].values():
        if not all_ and g["state"] != "open":
            continue
        # Verifications are shown under the goal, but do not count as product work twice.
        work = [m for m in latest_tasks(tasks, g["id"]) if m.get("kind") != "verify"]
        out.append({**g, "counts": {
            "active": sum(m["state"] in ("queued", "running") for m in work),
            "review": sum(m["state"] in ("done", "merging") for m in work),
            "merged": sum(m["state"] == "merged" for m in work),
            "failed": sum(m["state"] == "failed" for m in work),
            "todos": sum(a.get("goal") == g["id"] and not a.get("closed") for a in asks)}})
        if evidence:
            chains = [task_evidence(desk, m, context) for m in work]
            out[-1]["counts"].update(
                missing=sum(bool(c["missing"]) for c in chains if c["state"] == "merged"),
                released=sum(any(r["state"] == "done" for r in c["releases"]) for c in chains))
    return out


def one_line(text: str) -> str:
    return " ".join(text.split())


def summary_text(g: dict) -> str:
    c = g["counts"]
    return (f"{g['id']} {one_line(g['title'])}: {c['active']} queued/running, {c['review']} waiting for review, "
            f"{c['merged']} merged, "
            + (f"{c['missing']} merged missing evidence, {c['released']} released, " if "missing" in c else "")
            + f"{c['failed']} failed, {c['todos']} open to-dos"
            + (f"; {g['state']}: {one_line(g['outcome'])}" if g["state"] != "open" else ""))


def _when(ts: float) -> str:
    """Local time, in the form hansa status uses."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def added_text(g: dict) -> str:
    """What goal add recorded; the id stays the first word for scripts."""
    return f"{g['id']} recorded: {one_line(g['title'])}\n  why: {one_line(g['why']) if g.get('why') else '(none)'}"


def decided_text(desk: Desk, d: dict) -> str:
    """What decide recorded; the id stays the first word for scripts."""
    goal_ = records(desk)[0].get(d["goal"]) if d.get("goal") else None
    tied = (f"tied to goal {d['goal']} {one_line(goal_['title'])}"
            + (f" [{goal_['state']}]" if goal_["state"] != "open" else "") if goal_ else "tied to no goal")
    return (f"{d['id']} recorded: {one_line(d['text'])}\n  by: {d['by']} · {tied}"
            + (f" · task: {d['task']}" if d.get("task") else "")
            + (f" · replaces {d['replaces']}" if d.get("replaces") else ""))


def decision_text(d: dict) -> str:
    return (f"{d['id']} {d['text']}\n  why: {d['why']}\n  by: {d['by']} · {_when(d['created'])}"
            f" · task: {d['task'] or '(none)'} · goal: {d['goal'] or '(none)'}"
            + (f" · replaces {d['replaces']}" if d.get("replaces") else "")
            + (f" · replaced by {d['replaced_by']}" if d.get("replaced_by") else ""))


def decisions_text(desk: Desk, gid: str = "", grep: str = "", *, all_: bool = False) -> str:
    if gid:
        goal(desk, gid)
    needle = grep.casefold()
    return "\n".join(decision_text(d) for d in reversed(list(records(desk)[1].values()))
                     if (all_ or not d.get("replaced_by")) and (not gid or d["goal"] == gid)
                     and (needle in d["text"].casefold() or needle in d["why"].casefold()))


def evidence_records(desk: Desk) -> dict:
    """Read merge marks once. Release commit lists can be capped; cache any history read per release."""
    merges = {}
    try:
        with desk.events_path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                e = event_record(line)
                if e and e.get("event") == "task_merged":
                    merges[e.get("task")] = e
    except FileNotFoundError:
        pass
    return {"merges": merges, "releases": release.releases(desk), "commits": {}}


def task_releases(m: dict, context: dict) -> list:
    from .run import git
    sha = m.get("merge_commit")
    if m["state"] != "merged" or not sha:
        return []
    found = []
    for r in context["releases"]:
        # The exact outgoing commit proves inclusion, even from another release lane or base.
        commits = {c["sha"] for c in r.get("commits", [])}
        if sha not in commits and r.get("count", 0) > len(commits):
            if r["id"] not in context["commits"]:
                try:
                    context["commits"][r["id"]] = set(git(
                        "rev-list", r["commit"], *(["^" + r["since"]] if r.get("since") else []), "--",
                        cwd=r["repo"]).splitlines())
                except (RuntimeError, OSError):
                    context["commits"][r["id"]] = set()
            commits = context["commits"][r["id"]]
        if sha in commits:
            found.append({k: r.get(k) for k in ("id", "lane", "target", "state", "commit", "asked", "ended")})
    return found


def task_evidence(desk: Desk, m: dict, context: dict) -> dict:
    from .run import rounds_of, standing_verification
    check = m.get("check") or {}
    if check:
        checked = "could not run" if check.get("could_not_run") else "passed" if check.get("ok") else "failed"
    elif m["state"] in ("queued", "running"):
        checked = "not run yet"
    else:
        if "lanes" not in context:
            context["lanes"] = desk.settings().get("lanes") or {}
        lane = context["lanes"].get(m.get("lane")) or {}
        # "none" says the lane has no check; a lane with one whose check never ran on this task says so.
        checked = "not run" if lane.get("check") and m.get("kind") != "verify" else "none"
    v = standing_verification(desk, m)
    verdict = ("running" if v and v["state"] in ("queued", "running") else
               (v.get("verdict") or "none") if v else "none")
    merge, missing = "not merged", None
    marks = context["merges"].get(m["id"], {})
    merge_checked = bool(marks.get("merge_checked") if marks else (m.get("merge_check_result") or {}).get("ok"))
    if m["state"] == "merged":
        from .run import unverified_note
        # The saved note is what the merge said, before it closed a pending verification.
        note = m["unverified"] if "unverified" in m else unverified_note(desk, m)
        missing = [note] if note else []
        despite = marks.get("despite_check") and (not marks.get("merge_not_checked") or
                  (check and not check.get("ok")) or verdict == "does not hold")
        if despite:
            merge = "merged despite a failed check or verdict"
            missing.append(merge)
        elif merge_checked:
            merge = "merged after its merge check"
        else:
            merge = "merged without one"
        if marks.get("merge_not_checked"):
            missing.append("merge not checked")
    rounds = rounds_of(desk, m)
    # Merged work stays a chain of its own. Count only rounds after its merge in a follow-up's chain,
    # and count an abandoned, never-started follow-up separately from the parent it restored.
    boundary = max((i + 1 for i, r in enumerate(rounds[:-1]) if r["state"] == "merged"), default=0)
    count = 1 if m.get("restored_parent") else len(rounds) - boundary
    return {"id": m["id"], "title": m["title"], "state": m["state"], "lane": m.get("lane"),
            "closed": bool(m.get("closed")),
            "rounds": count, "check": checked, "verification": verdict,
            "verification_task": v["id"] if v else None, "merge": merge, "merge_commit": m.get("merge_commit"),
            "merge_checked": merge_checked, "despite_check": bool(marks.get("despite_check")),
            "merge_not_checked": bool(marks.get("merge_not_checked")), "missing": missing,
            "releases": task_releases(m, context)}


def show(desk: Desk, gid: str, *, tasks: list | None = None, context: dict | None = None) -> dict:
    """The goal's recorded intent, latest chains and lifetime cost; verifications are not extra chains."""
    g = goal(desk, gid)
    tasks = desk.tasks() if tasks is None else tasks
    linked = [m for m in tasks if m.get("goal") == gid]
    latest = latest_tasks(linked, gid)
    context = evidence_records(desk) if context is None else context
    chains = [task_evidence(desk, m, context) for m in latest if m.get("kind") != "verify"]
    totals = cost.grouped(cost.worker_rows(desk, 0, tasks=linked), "goal").get(
        gid, {"tokens": 0, "usd": 0.0, "unpriced_tokens": 0, "turns": 0, "runs": 0})
    return {**g, "decisions": [d for d in reversed(list(records(desk)[1].values())) if d["goal"] == gid],
            "chains": chains, "verifications": [{k: m.get(k) for k in ("id", "title", "state", "parent", "verdict")}
                                                for m in latest if m.get("kind") == "verify"],
            "todos": [a for a in desk.asks() if a.get("goal") == gid and not a.get("closed")],
            "cost": totals, "rounds": sum(c["rounds"] for c in chains)}


def page_list(desk: Desk) -> list:
    """The command reports together, sharing the full event-log read across every goal."""
    tasks, context = desk.tasks(), evidence_records(desk)
    return [{**g, **show(desk, g["id"], tasks=tasks, context=context)}
            for g in summaries(desk, tasks, all_=True, evidence=True, context=context)]


SHOWN_TASKS = 20


def _settled(m: dict) -> bool:
    return m["state"] in ("merged", "cancelled") or bool(m.get("closed"))


def history_text(h: dict) -> str:
    if h["event"] == "ended":
        return f"{h['state']}: {one_line(h['outcome'])}"
    if h["event"] == "reopened":
        return f"reopened (was {h['was']}): {one_line(h['why'])}"
    if h["event"] == "tied":
        return "tied " + ", ".join(i + (f" (from {h['from'][i]})" if h["from"].get(i) else "") for i in h["items"])
    return f"moved to {h['to']}: {', '.join(h['items'])}"


def show_text(desk: Desk, gid: str, *, all_: bool = False) -> str:
    g = show(desk, gid)
    decisions = g["decisions"]
    out = [f"{gid} {g['title']} [{g['state']}]", f"created: {_when(g['created'])}",
           f"session: {g.get('session') or '(none)'}", f"why: {g['why'] or '(none)'}"]
    if g["state"] != "open":
        out += [f"outcome: {g['outcome']}", f"ended: {_when(g['ended'])}"]
    if any(h["event"] != "ended" for h in g.get("history", [])):  # a goal only ended once shows that above
        out += ["history:"] + [f"  {_when(h['created'])} {history_text(h)}" for h in g["history"]]
    out += ["current decisions:"]
    out += [decision_text(d) for d in decisions if not d.get("replaced_by")]
    out += ["replaced decisions:"]
    out += [f"{d['id']} {one_line(d['text'])} · replaced by {d['replaced_by']}"
            for d in decisions if d.get("replaced_by")]
    out += ["tasks (latest in each retry chain, newest first):"]
    chains = list(reversed(g["chains"]))
    folded = [] if all_ else [m for m in chains[SHOWN_TASKS:] if _settled(m)]
    for m in chains:
        if any(m is f for f in folded):
            continue
        rounds = m["rounds"]
        out += [f"{m['id']} {m['state']} · {rounds} round{'s' if rounds != 1 else ''} · {one_line(m['title'])}",
                f"  check: {m['check']}", f"  verification: {m['verification']}"
                + (f" ({m['verification_task']})" if m["verification_task"] else ""), f"  merge: {m['merge']}"]
        out += [f"  release #{r['id']}: {r['target']} · {r['state']}" for r in m["releases"]] or ["  releases: none"]
        if m["missing"] is not None:
            out += ["  missing: " + ("; ".join(m["missing"]) or "nothing")]
    if folded:
        out += [f"{len(folded)} older merged or closed task{'s' if len(folded) != 1 else ''} not listed "
                f"(hansa goal show {gid} --all lists them)"]
    out += [f"{v['id']} {v['state']} · 1 round · {one_line(v['title'])} · verification of {v['parent']}"
            for v in reversed(g["verifications"])]
    c = g["cost"]
    out += [f"cost: {c['tokens']:,} tokens · ${c['usd']:.6f} worth"
            + (f" · {c['unpriced_tokens']:,} tokens unpriced" if c["unpriced_tokens"] else ""),
            f"rounds: {g['rounds']}"]
    out += ["open to-dos:"]
    out += [f"#{a['id']} {a['text']}" for a in g["todos"]]
    return "\n".join(out)
