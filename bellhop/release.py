"""A release run that can be resumed from the step it stopped at.

A release is a fixed sequence whose contents differ per desk: preflight, a full
check, a tag, a package, a publish, a deploy and its verification. Run by hand it is
done differently each time, and the step it failed at is remembered rather than
recorded - so the second attempt starts from the beginning, and a step that is not
safe to repeat (a tag, a publish) is repeated.

Four rules, all of them from a production release script:

1. **The steps live in the desk's config, never in a skill.** `release_steps` is an
   ordered list of `{name, cmd}`. The framework owns the order, the stop and the
   record; the desk owns what the steps actually are. A step written into a skill is
   a step a model can paraphrase.
2. **Stop at the first non-zero exit, and say where.** The last line is
   `🛑 stopped at: <step>`, on its own, in that shape - it is what a person greps
   for and what the resume line is built from. A run that stopped at step 3 and a
   run that stopped at step 7 look the same in "the release failed".
3. **Record progress as it happens.** `state/.release-<version>.json` gains a row per
   step as the step ends, so a crash between two steps still leaves the record that
   `--from` reads. A record written only at the end is a record that is missing
   exactly when it is needed.
4. **A stop-dispatch order stops a release too.** The desk paused dispatch for a
   reason, and a release is the largest thing it dispatches. `release_ignores_stop`
   turns that off for desks whose stop order means something narrower.

`{version}` in a command is replaced with the version, by `str.replace` and never by
`str.format`: a command containing `awk '{print $1}'` is ordinary, and `format` would
raise on it.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from . import stopdispatch as stop_mod
from .config import Desk
from .util import BellhopError, read_json, ts, write_json

# The version becomes part of a file name and is substituted into a shell command.
# Anything outside this set is refused rather than quoted: a version that is also a
# shell fragment and a version that is a version look the same in `1.2.3`.
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")

STOP_LINE = "🛑 stopped at: %s"

OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"
PENDING = "pending"


def progress_path(desk: Desk, version: str) -> Path:
    return desk.state / (".release-%s.json" % version)


def steps(desk: Desk) -> List[Dict[str, str]]:
    """The desk's release steps, validated. An empty list is an error, not a no-op."""
    raw = desk.cfg.get("release_steps") or []
    if not isinstance(raw, list) or not raw:
        raise BellhopError(
            "no release_steps in bellhop.json; a release with no steps would exit 0 "
            "having released nothing"
        )
    out: List[Dict[str, str]] = []
    seen = set()
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise BellhopError("release_steps[%d] is not an object with `name` and `cmd`" % i)
        name = str(item.get("name") or "").strip()
        cmd = str(item.get("cmd") or "").strip()
        if not name or not cmd:
            raise BellhopError("release_steps[%d] needs both `name` and `cmd`" % i)
        if name in seen:
            raise BellhopError(
                "release_steps has two steps named %r; `--from %s` could not say which one"
                % (name, name)
            )
        seen.add(name)
        out.append({"name": name, "cmd": cmd})
    return out


def check_version(version: str) -> str:
    version = (version or "").strip()
    if not VERSION_RE.match(version):
        raise BellhopError(
            "version %r is not in [A-Za-z0-9][A-Za-z0-9._+-]*; it is substituted into a "
            "shell command and used as a file name, so it is refused rather than quoted"
            % version
        )
    return version


def check_stop(desk: Desk) -> None:
    if desk.cfg.get("release_ignores_stop"):
        return
    allowed, reason = stop_mod.dispatch_allowed(desk)
    if not allowed:
        raise BellhopError(
            reason + " A release is refused while the order stands; set "
            "`release_ignores_stop` to true if this desk's stop order is narrower than that."
        )


def _start_index(step_names: List[str], from_step: Optional[str]) -> int:
    if not from_step:
        return 0
    if from_step not in step_names:
        raise BellhopError(
            "no release step named %r (the steps are: %s)" % (from_step, ", ".join(step_names))
        )
    return step_names.index(from_step)


def _save(desk: Desk, version: str, rows: List[Dict[str, object]]) -> None:
    write_json(progress_path(desk, version), {
        "version": version,
        "updated": ts(),
        "steps": rows,
    })


def previous_stop(desk: Desk, version: str) -> Optional[str]:
    """The step a previous run of this version stopped at, if there was one."""
    data = read_json(progress_path(desk, version), None) or {}
    for row in data.get("steps", []):
        if row.get("status") == FAILED:
            return str(row.get("name"))
    return None


def _initial_rows(desk: Desk, version: str, plan: List[Dict[str, str]],
                  start: int) -> List[Dict[str, object]]:
    """The record a run starts from, carrying forward what an earlier one proved.

    A step that ran and passed on an earlier attempt, and a step that `--from` jumped
    over without ever running, must not both read `skipped`: the first is a step whose
    work is done, the second is a step nobody has evidence for.
    """
    prior = {r.get("name"): r
             for r in (read_json(progress_path(desk, version), None) or {}).get("steps", [])}
    rows: List[Dict[str, object]] = []
    for i, step in enumerate(plan):
        if i >= start:
            rows.append({"name": step["name"], "status": PENDING, "exit": None, "at": None})
        elif (prior.get(step["name"]) or {}).get("status") == OK:
            rows.append(dict(prior[step["name"]]))
        else:
            rows.append({"name": step["name"], "status": SKIPPED, "exit": None, "at": ts()})
    return rows


def release(desk: Desk, version: str, from_step: Optional[str] = None,
            dry_run: bool = False) -> int:
    """Run the desk's release steps. Returns the process exit code."""
    version = check_version(version)
    plan = steps(desk)
    names = [s["name"] for s in plan]
    start = _start_index(names, from_step)
    if not dry_run:
        # Dry run only prints, so it is allowed to read a stopped desk - the same
        # rule the sentinel's dry run follows for its checks and its busy probe.
        check_stop(desk)

    if dry_run:
        allowed, reason = stop_mod.dispatch_allowed(desk)
        if not allowed and not desk.cfg.get("release_ignores_stop"):
            print("note: a real run would be refused - " + reason)
        print("release %s, %d step(s), dry run - nothing is run and nothing is written:" % (
            version, len(plan) - start))
        for i, step in enumerate(plan):
            mark = "  skipped (--from %s)" % from_step if i < start else ""
            print("  %d. %s: %s%s" % (i + 1, step["name"], step["cmd"].replace("{version}", version), mark))
        return 0

    stopped_before = previous_stop(desk, version)
    if stopped_before and not from_step:
        print("note: an earlier run of %s stopped at %r; `--from %s` resumes there"
              % (version, stopped_before, stopped_before))

    rows = _initial_rows(desk, version, plan, start)
    _save(desk, version, rows)

    for i in range(start, len(plan)):
        step = plan[i]
        cmd = step["cmd"].replace("{version}", version)
        # Flushed, because the step writes to the same file descriptor directly:
        # unflushed, the header lands after the output of the step it announces.
        print("== %s (%d/%d): %s" % (step["name"], i + 1, len(plan), cmd), flush=True)
        proc = subprocess.run(cmd, shell=True, cwd=str(desk.root))
        rows[i] = {"name": step["name"], "status": OK if proc.returncode == 0 else FAILED,
                   "exit": proc.returncode, "at": ts()}
        _save(desk, version, rows)
        if proc.returncode != 0:
            print(STOP_LINE % step["name"])
            print("exit %d; after fixing it, resume with `bellhop release %s --from %s`"
                  % (proc.returncode, version, step["name"]))
            print("progress: %s" % progress_path(desk, version))
            return 1

    print("released %s (%d step(s) run, %d skipped)" % (version, len(plan) - start, start))
    print("progress: %s" % progress_path(desk, version))
    return 0
