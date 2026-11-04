"""Goals and product decisions: immutable entries in the desk's private state."""
from __future__ import annotations

import time

from . import notify
from .desk import Desk, now, read_json, write_json


def entries(desk: Desk) -> list:
    folder = desk.state_dir / "intent"
    return [read_json(p) for p in sorted(folder.glob("*.json"), key=lambda p: int(p.stem))]


def records(desk: Desk) -> tuple[dict, dict]:
    goals, decisions = {}, {}
    for e in entries(desk):
        if e["event"] == "goal_added":
            goals[e["id"]] = {**e, "state": "open"}
        elif e["event"] == "goal_ended":
            goals[e["id"]].update(state=e["state"], outcome=e["outcome"], ended=e["created"],
                                   ended_session=e.get("session"))
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


def end(desk: Desk, gid: str, state: str, outcome: str) -> None:
    with desk.lock():
        goal(desk, gid, open_only=True)
        _text(desk, outcome, "the goal's outcome" if state == "done" else "why the goal was dropped")
        _append(desk, event="goal_ended", id=gid, state=state, outcome=outcome)


def decide(desk: Desk, text: str, why: str, *, gid: str = "", by: str = "coordinator",
           task: str = "", replaces: str = "") -> str:
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
        did = f"d{len(decisions) + 1}"
        _append(desk, event="decision_added", id=did, text=text, why=why, goal=gid, by=by,
                task=task, replaces=replaces)
    return did


def latest_tasks(tasks: list, gid: str) -> list:
    linked = [m for m in tasks if m.get("goal") == gid]
    parents = {m.get("parent") for m in linked if m.get("kind") != "verify"}
    return [m for m in linked if m.get("kind") == "verify" or m["id"] not in parents]


def summaries(desk: Desk, tasks: list | None = None, *, all_: bool = False) -> list:
    tasks = desk.tasks() if tasks is None else tasks
    asks = desk.asks()
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
    return out


def one_line(text: str) -> str:
    return " ".join(text.split())


def summary_text(g: dict) -> str:
    c = g["counts"]
    return (f"{g['id']} {one_line(g['title'])}: {c['active']} queued/running, {c['review']} waiting for review, "
            f"{c['merged']} merged, {c['failed']} failed, {c['todos']} open to-dos"
            + (f"; {g['state']}: {one_line(g['outcome'])}" if g["state"] != "open" else ""))


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))


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


def show_text(desk: Desk, gid: str) -> str:
    from .run import rounds_of
    g = goal(desk, gid)
    decisions = [d for d in reversed(list(records(desk)[1].values())) if d["goal"] == gid]
    out = [f"{gid} {g['title']} [{g['state']}]", f"created: {_when(g['created'])}",
           f"session: {g.get('session') or '(none)'}", f"why: {g['why'] or '(none)'}"]
    if g["state"] != "open":
        out += [f"outcome: {g['outcome']}", f"ended: {_when(g['ended'])}"]
    out += ["current decisions:"]
    out += [decision_text(d) for d in decisions if not d.get("replaced_by")]
    out += ["replaced decisions:"]
    out += [f"{d['id']} {one_line(d['text'])} · replaced by {d['replaced_by']}"
            for d in decisions if d.get("replaced_by")]
    out += ["tasks (latest in each retry chain):"]
    for m in latest_tasks(desk.tasks(), gid):
        rounds = len(rounds_of(desk, m)) if m.get("kind") != "verify" else 1
        out += [f"{m['id']} {m['state']} · {rounds} round{'s' if rounds != 1 else ''} · {one_line(m['title'])}"
                + (f" · verification of {m['parent']}" if m.get("kind") == "verify" else "")]
    out += ["open to-dos:"]
    out += [f"#{a['id']} {a['text']}" for a in desk.asks() if a.get("goal") == gid and not a.get("closed")]
    return "\n".join(out)
