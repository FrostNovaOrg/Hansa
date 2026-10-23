"""Collect workers' release notes from the commits on a lane's base branch."""
from __future__ import annotations

import re
import subprocess

from .desk import Desk, lane_targets
from .release import _git_bytes, last_out, repo_key


MARKER = re.compile(r"^\s*(?:-\s+)?(?:\*\*)?changelog:(?:\*\*)?\s*(.*?)\s*$", re.I)


def report_line(desk: Desk, task: dict) -> str | None:
    """The last marker's sentence, empty for none, or None for an older report."""
    path = desk.tasks_dir / task["id"] / "result.md"
    try:
        report = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    found = [m.group(1) for line in report.splitlines() if (m := MARKER.match(line))]
    if not found:
        return None
    return "" if found[-1].casefold() in ("none", "-") else found[-1]


def lines(desk: Desk, lane: str, target: str | None = None):
    """Release range, one entry per merged task or direct commit, then the tasks saying none.

    Unlike the release word scan, this range includes remote-tracking history: these are the changes since
    this target's last successful release, wherever else they have already been pushed.
    """
    settings = desk.settings()
    ln = settings["lanes"].get(lane)
    if not ln:
        raise SystemExit(f"hansa: there is no lane {lane}; lanes: {', '.join(sorted(settings['lanes'])) or 'none'}")
    targets = lane_targets(ln)
    if not targets:
        raise SystemExit(f"hansa: lane {lane} has no release target; set one with hansa lane target {lane}")
    if target is None:
        if len(targets) > 1:
            raise SystemExit(f"hansa: lane {lane} has several release targets ({', '.join(sorted(targets))}); "
                             f"say which: hansa changelog {lane} TARGET")
        target = next(iter(targets))
    if target not in targets:
        raise SystemExit(f"hansa: lane {lane} has no release target {target}; its targets: {', '.join(sorted(targets))}")
    repo, base = ln["repo"], ln.get("base") or "main"
    tip = _git_bytes(repo, "rev-parse", "--verify", f"refs/heads/{base}^{{commit}}").strip()
    previous = last_out(desk, lane, repo, base, target)
    since = previous["commit"] if previous else ""
    rng = f"{since}..{tip}" if since else tip
    counted = f"from release #{previous['id']}" if previous else "no done release; all commits"
    if since and subprocess.run(["git", "merge-base", "--is-ancestor", since, tip], cwd=repo,
                                capture_output=True, stdin=subprocess.DEVNULL).returncode:
        counted += f"; release commit {since} is not in {base}'s history"
    yield f"Changelog for lane {lane}, target {target}: {counted}; range {rng}"

    rows = _git_bytes(repo, "log", "--reverse", "--topo-order", "--format=%H%x1f%P%x1f%s", rng, "--")
    commits = [row.split("\x1f", 2) for row in rows.splitlines()]
    parents = {sha: ps.split() for sha, ps, _ in commits}
    entries, owned, unknown = {}, set(), []
    for task in desk.tasks():
        if task.get("kind") == "verify" or not (task.get("merged") or task.get("state") == "merged"):
            continue
        own_lane = settings["lanes"].get(task.get("lane"), {})
        own_repo = task.get("merge_repo") or task.get("repo") or own_lane.get("repo")
        own_base = task.get("merge_base") or own_lane.get("base") or "main"
        if not own_repo or repo_key(own_repo) != repo_key(repo) or own_base != base:
            continue
        landing, head = task.get("merge_commit"), task.get("head")
        marker = landing or head
        if not marker:
            unknown.append(task["id"])
            continue
        if marker not in parents:
            continue
        ps = parents[marker]
        if landing and len(ps) > 1:
            # What this recorded landing brought in, including an unmerged earlier attempt of a follow-up.
            introduced = _git_bytes(repo, "rev-list", landing, f"^{ps[0]}", "--").splitlines()
        elif head:
            introduced = (_git_bytes(repo, "rev-list", head, f"^{task['base']}", "--").splitlines()
                          if task.get("base") else [head])
        else:
            unknown.append(task["id"])
            continue
        owned.update(introduced)
        entries.setdefault(marker, []).append(task)

    none = []
    for sha, ps, subject in commits:
        for task in entries.get(sha, []):
            sentence = report_line(desk, task)
            if sentence == "":
                none.append(task["id"])
            else:
                if sentence is None:
                    sentence = f"[no changelog line] {task['title']}"
                yield f"- {sentence} ({task['id']})"
        if sha not in owned and len(ps.split()) < 2:
            yield f"- [no task] {subject} ({sha[:7]})"
    for tid in unknown:
        yield f"Warning: {tid}: cannot identify the task's commit from its record"
    if none:
        yield f"Tasks saying none: {', '.join(none)}"
