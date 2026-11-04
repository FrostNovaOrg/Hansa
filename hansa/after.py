"""Tasks queued after other tasks' merges. Waiting never claims a lane or a concurrency slot."""
from __future__ import annotations

import re

from .desk import event_record, work_lane_problem


def validate(desk, opts: dict) -> None:
    """Before creating any task files, pin dependencies to one repository and base branch.
    Unassigned work may use any ordinary lane on that target; it must never drift to another one."""
    from .run import merge_target
    ids = list(dict.fromkeys(opts.get("after") or []))
    if not ids:
        return
    s = desk.settings()
    target = merge_target(s, opts.get("lane"))
    if opts.get("lane") and target is None:
        raise SystemExit(f"hansa: --after needs an existing lane {opts['lane']}; nothing was queued")
    for tid in ids:
        if not re.fullmatch(r"t[0-9]+", tid):
            raise SystemExit(f"hansa: no task {tid}; nothing was queued")
        try:
            m = desk.meta(tid)
        except SystemExit:
            raise SystemExit(f"hansa: no task {tid}; nothing was queued") from None
        at = merge_target(s, m.get("lane"))
        if not m.get("lane"):
            known = m.get("after_target")
            targets = {(known["repo"], known["base"])} if known else {
                merge_target(s, ln) for ln in s["lanes"] if not work_lane_problem(s, ln)}
            at = next(iter(targets)) if len(targets) == 1 else None
        if at is None or m.get("kind") == "verify":
            raise SystemExit(f"hansa: --after {tid} needs a work task with a known repository and base branch; "
                             "nothing was queued")
        target = target or at
        if at != target:
            raise SystemExit(f"hansa: --after {tid} is on another repository or base branch; "
                             "dependencies must share the new task's repository and base; nothing was queued")
    saved = {"repo": target[0], "base": target[1]}
    if opts.get("after_target") and opts["after_target"] != saved:
        raise SystemExit("hansa: the dependency repository or base branch changed; nothing was queued")
    opts.update(after=ids, after_target=saved)


def lane_matches(s: dict, m: dict, lane: str) -> bool:
    from .run import merge_target
    target = m.get("after_target")
    return not target or merge_target(s, lane) == (target["repo"], target["base"])


def pending(m: dict, tasks: list) -> list:
    """Resolve each original id through retries, including retries of closed/cancelled tasks and a retry
    saved just before its parent's replaced_by pointer. Verification children do not replace work."""
    if not m.get("after"):
        return []
    by_id = {t["id"]: t for t in tasks}
    children = {}
    for t in sorted(tasks, key=lambda t: (t.get("created", 0), len(t["id"]), t["id"])):
        if t.get("parent") and t.get("kind") != "verify":
            children[t["parent"]] = t["id"]
    out = []
    for tid in m["after"]:
        latest, seen = tid, set()
        while latest not in seen:
            seen.add(latest)
            t = by_id.get(latest, {})
            successor = children.get(latest) or t.get("replaced_by")
            if not successor:
                break
            latest = successor
        else:
            t = {}  # malformed chain: hold rather than start without the prerequisite
        state = t.get("state", "missing")
        if state == "merged":
            target = m.get("after_target") or {}
            if ((not t.get("merge_repo") or t["merge_repo"] == target.get("repo"))
                    and (not t.get("merge_base") or t["merge_base"] == target.get("base"))):
                continue
            state = "elsewhere"
        why = ("closed without merging" if t.get("error") == "closed without merging" else
               {"failed": "failed without merging", "cancelled": "cancelled without merging",
                "missing": "task or retry record is missing", "replaced": "replacement is missing",
                "elsewhere": "merged into another repository or base branch"}.get(state, ""))
        out.append({"task": tid, "latest": latest, "state": state, "why": why, "held": bool(why)})
    return out


def waiting(m: dict, dependencies: list) -> str:
    parts = []
    for dep in dependencies:
        name = dep["latest"] + (f" (latest follow-up to {dep['task']})" if dep["latest"] != dep["task"] else "")
        parts.append(f"HELD: {name} {dep['why']}; hansa cancel {m['id']}, or hansa retry {dep['latest']}"
                     if dep["held"] else f"waiting for {name} to be merged into {m['after_target']['base']}")
    return "; ".join(parts)


def announce(desk, tid: str) -> None:
    """One coordinator event per held dependency/latest attempt, also across restart or a failed save.
    Append before marking it told; on a save failure the existing event prevents a second post."""
    with desk.lock():
        m = desk.meta(tid)
        if m["state"] != "queued":
            return
        told = m.get("after_notified") or []
        held = [d for d in pending(m, desk.tasks()) if d["held"] and [d["task"], d["latest"]] not in told]
        if not held:
            return
        recorded = set()
        if desk.events_path.exists():
            with desk.events_path.open(encoding="utf-8", errors="replace") as source:
                for line in source:
                    e = event_record(line)
                    if e and e.get("event") == "task_after_held" and e.get("task") == tid:
                        recorded.add((e.get("after"), e.get("latest")))
        for dep in held:
            key = [dep["task"], dep["latest"]]
            if tuple(key) not in recorded:
                desk.event("task_after_held", task=tid, after=dep["task"], latest=dep["latest"])
            told = [*told, key]
        desk.update(tid, after_notified=told)
