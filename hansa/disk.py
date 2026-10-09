"""Free space: a floor under which hansa serve starts nothing new, and the trimming of what old tasks kept.

The floor (settings `disk_floor`, in GB; the coordinator's, with hansa disk-floor; default DEFAULT_FLOOR; 0 turns it
off). Each round hansa serve looks at the free space (shutil.disk_usage) of each volume it and its runs write on
(places: the desk's folder, each lane's repository and worktree, the folder worker runs get their temporary folders
in and hansa serve's own TMPDIR, where the setups', checks' and tries' are made). While any of them has less free than
the floor, it starts no new task, verification, try, merge or merge check, design session or release; what already
runs goes on (a worker's resume and its task's check are part of a task that runs). It starts again once every
volume has the floor and MARGIN GB more free, so that it does not flap. What it saw is in .hansa/disk.json (look),
which hansa status, the page and hansa explain server read; going under and coming back over are events (disk_low,
disk_ok), which the coordinator's session hears once each (notify), and disk_low runs the principal's alert once
(alert.from_event).

What old tasks kept (trim): a task's folder keeps its text, record (meta.json), report (result.md) and result
(run.json), which stay whole, and the output of its processes, of which only the preview's is capped (PREVIEW_KEEP).
A check that prints a long build log keeps all of it. So once a day hansa serve cuts each kept log (TRIMMED) of a
task that ended (ENDED) more than TRIM_AFTER ago to its last TRIM_KEEP bytes, under a first line saying it was
trimmed, when, and how big it was; the task's record says so too (`trimmed`), and hansa show. The event log is not
touched.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

from .desk import Desk, now, read_json, write_json

GB = 10 ** 9
DEFAULT_FLOOR = 10  # GB
MOST_FLOOR = 100000  # GB: a floor past this is a typo
MARGIN = 1  # GB over the floor every volume needs before hansa serve starts things again
STARTS = "no new task, verification, try, merge or merge check, design session or release"
TRIM_AFTER = 30 * 86400  # a task that ended this long ago has its kept logs trimmed
TRIM_KEEP = 256 * 1024  # bytes of a trimmed log kept, its end
TRIM_EVERY = 86400  # seconds between hansa serve's trims
# what a task's processes wrote that hansa keeps in its folder: the worker's errors, the setup's, check's and merge
# check's output, the preview's (capped already, but long)
TRIMMED = ("run.err", "setup.log", "check.log", "merge-check.log", "merge-setup.log", "merge-check.err", "preview.log",
           "preview-before-resume.log")
ENDED = ("merged", "cancelled", "failed", "replaced")


def floor(s: dict) -> float:
    """The floor in GB; 0: off."""
    v = s.get("disk_floor", DEFAULT_FLOOR)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else DEFAULT_FLOOR


def gb(n) -> str:
    return f"{n / GB:.1f} GB"


def floor_text(fl) -> str:
    return "off" if not fl else f"{fl:g} GB"


def places(desk: Desk, s: dict) -> list:
    """What hansa serve and its runs write on, (what, path): the desk's folder first."""
    from .run import WORKER_TMP_ROOT
    out = [("the desk's folder", desk.root)]
    for name, ln in sorted((s.get("lanes") or {}).items()):
        if isinstance(ln, dict) and isinstance(ln.get("repo"), str) and ln["repo"].startswith("/"):
            out.append((f"lane {name}'s repository", Path(ln["repo"])))
        out.append((f"lane {name}'s worktree", desk.worktrees_dir / name))
    out.append(("the runs' temporary folders", Path(WORKER_TMP_ROOT)))
    out.append(("hansa serve's TMPDIR", Path(tempfile.gettempdir())))
    return out


def _there(p: Path):
    """The path, or the nearest folder above it that exists (a worktree not made yet is on its parent's volume)."""
    p = Path(os.path.abspath(p))
    while not p.exists():
        if p == p.parent:
            return None
        p = p.parent
    return p


def _mount(p: Path) -> str:
    p = Path(os.path.realpath(p))
    while not os.path.ismount(p) and p != p.parent:
        p = p.parent
    return str(p)


def volumes(desk: Desk, s: dict) -> list:
    """Each volume of places once: {mount, what (the places on it), free, total}, the desk's first. A place that
    cannot be looked at is left out."""
    vols = {}
    for what, p in places(desk, s):
        at = _there(p)
        if at is None:
            continue
        try:
            dev = os.stat(at).st_dev
            u = shutil.disk_usage(at)
        except OSError:
            continue
        v = vols.setdefault(dev, {"mount": _mount(at), "what": [], "free": u.free, "total": u.total})
        if what not in v["what"]:
            v["what"].append(what)
        v["free"] = min(v["free"], u.free)
    return list(vols.values())


def record_file(desk: Desk) -> Path:
    return desk.state_dir / "disk.json"


def record(desk: Desk) -> dict:
    r = read_json(record_file(desk), {})
    return r if isinstance(r, dict) else {}


def volume_text(v: dict) -> str:
    return f"{v.get('mount')} ({', '.join(v.get('what') or [])}) has {gb(v.get('free') or 0)} free"


def look(desk: Desk, s: dict) -> dict:
    """hansa serve's look, each round: the volumes, and which hold it back (low). Under the floor it holds back; once
    holding back, it goes on until every volume has the floor and MARGIN more. Says each change once, as an event.
    Returns the record it wrote."""
    fl = floor(s)
    was = record(desk)
    was_low = bool(was.get("low"))
    vols = volumes(desk, s)
    bar = (fl + (MARGIN if was_low else 0)) * GB
    low = [v for v in vols if v["free"] < bar] if fl else []
    rec = {"pid": os.getpid(), "at": now(), "floor": fl, "volumes": vols, "low": low,
           "since": (was.get("since") if was_low else now()) if low else None}
    desk.state_dir.mkdir(exist_ok=True)
    write_json(record_file(desk), rec)
    if low and not was_low:
        desk.event("disk_low", floor=fl, volumes=[volume_text(v) for v in low])
    elif was_low and not low:
        desk.event("disk_ok", floor=fl, volumes=[volume_text(v) for v in vols])
    return rec


def held(desk: Desk) -> list:
    """The volumes for which hansa serve starts nothing new, as it last looked (a server that starts again looks
    again in its first round); [] when none."""
    low = record(desk).get("low")
    return low if isinstance(low, list) else []


def held_text(desk: Desk, low: list, fl=None) -> str:
    """Why hansa serve starts nothing new, in a line."""
    fl = floor(desk.settings()) if fl is None else fl
    under = [v for v in low if (v.get("free") or 0) < fl * GB]
    said = "; ".join(volume_text(v) + ("" if v in under else f", not yet {gb((fl + MARGIN) * GB)}") for v in low)
    return (f"DISK LOW: {said} (floor {floor_text(fl)}): hansa serve starts {STARTS} until every volume has "
            f"{gb((fl + MARGIN) * GB)} free; what runs goes on · hansa explain server")


def short_text(desk: Desk, low: list) -> str:
    """For a queued task, merge or release: why it waits, in a clause."""
    return (f"hansa serve starts nothing new while free space is under the floor ({floor_text(floor(desk.settings()))}"
            f"): {'; '.join(volume_text(v) for v in low)}")


def desk_free(desk: Desk):
    """The free and total bytes of the desk's volume now; (None, None) when it cannot be looked at."""
    try:
        u = shutil.disk_usage(desk.root)
    except OSError:
        return None, None
    return u.free, u.total


def desk_line(desk: Desk, s: dict) -> str:
    """For hansa status: the free space of the desk's volume, and the floor."""
    free, _ = desk_free(desk)
    fl = floor(s)
    return (f"disk: {gb(free) if free is not None else '? GB'} free on the desk's volume · floor {floor_text(fl)}"
            + ("" if fl else " (hansa serve starts things however little is free)") + " (hansa disk-floor)")


def explain_lines(desk: Desk, s: dict) -> list:
    fl = floor(s)
    low = held(desk)
    lines = [
        f"Free space: each round hansa serve looks at the free space of each volume it and its runs write on (the "
        f"desk's folder, each lane's repository and worktree, the folder worker runs get their temporary folders in, "
        f"and its own TMPDIR, where the setups', checks' and tries' are made). While any has less free than the "
        f"floor, it starts {STARTS}, and says so here, in hansa status and on the page; what already runs goes on. "
        f"It tells your session once when a volume goes under the floor and once when all are back, and runs the "
        f"principal's alert command once. It starts again by itself once every volume has the floor and {MARGIN} GB "
        f"more free. Free space by removing what builds left (a lane whose builds leave large outputs in its "
        f"worktree, or fill a cache of its own, should clean them in its setup), or set the floor with hansa "
        f"disk-floor GB (default {DEFAULT_FLOOR}; 0 turns it off).",
        f"  floor: {floor_text(fl)}"]
    lines += [f"  {volume_text(v)}" for v in volumes(desk, s)]
    lines += [f"  now: {held_text(desk, low, fl)}"] if low else []
    return lines


# -- what old tasks kept ---------------------------------------------------------------------------------------------
def ended_at(m: dict, tasks: dict):
    """When a task ended, for trimming; None when it has not or it cannot be told."""
    if m.get("state") not in ENDED:
        return None
    if m["state"] == "merged" and m.get("merged"):
        return m["merged"]
    if m["state"] == "replaced":
        by = tasks.get(m.get("replaced_by") or "") or {}
        return by.get("created") or m.get("finished")
    return m.get("finished")


def _trim_one(f: Path, at: float) -> int | None:
    """Cut a log to its last TRIM_KEEP bytes (from the start of a line, when one starts in its first KB) under a
    line saying so; its size before, or None when it is small enough or cannot be read."""
    try:
        size = f.stat().st_size
        if size <= TRIM_KEEP + 1024:  # small, or trimmed already
            return None
        with open(f, "rb") as fh:
            fh.seek(size - TRIM_KEEP)
            tail = fh.read()
    except OSError:
        return None
    nl = tail.find(b"\n", 0, 1024)
    tail = tail[nl + 1:] if nl >= 0 else tail
    while tail and tail[0] & 0xC0 == 0x80:  # not in the middle of a character
        tail = tail[1:]
    head = (f"[hansa trimmed this log on {time.strftime('%Y-%m-%d %H:%M', time.localtime(at))}: it was {size:,} "
            f"bytes; its last {len(tail):,} bytes follow]\n").encode()
    tmp = f.with_name(f"{f.name}.{os.getpid()}.trim")
    try:
        tmp.write_bytes(head + tail)
        os.replace(tmp, f)
    except OSError:
        tmp.unlink(missing_ok=True)
        return None
    return size


def trim(desk: Desk, at: float | None = None) -> list:
    """Trim the kept logs of the tasks that ended more than TRIM_AFTER ago (once a day, from hansa serve); what was
    trimmed, as (task, file, size before)."""
    at = at or time.time()
    tasks = {m["id"]: m for m in desk.tasks() if isinstance(m.get("id"), str)}
    done = []
    for tid, m in tasks.items():
        end = ended_at(m, tasks)
        if not isinstance(end, (int, float)) or at - end < TRIM_AFTER:
            continue
        cut = {}
        with desk.lock():  # as a key's scrub of the same files takes it (Desk.scrub)
            for name in TRIMMED:
                size = _trim_one(desk.tasks_dir / tid / name, at)
                if size is not None:
                    cut[name] = {"at": round(at, 3), "bytes": size}
                    done.append((tid, name, size))
            if cut:
                desk.change(tid, lambda x: {"trimmed": {**(x.get("trimmed") or {}), **cut}})
    if done:
        desk.event("logs_trimmed", tasks=sorted({t for t, _, _ in done}), files=len(done),
                   bytes=sum(n for _, _, n in done))
    return done


def trimmed_text(m: dict) -> str:
    """For hansa show: which of the task's logs were trimmed, when, and how big they were; '' when none."""
    t = m.get("trimmed") or {}
    if not isinstance(t, dict) or not t:
        return ""
    return (f"logs trimmed (the task ended over {TRIM_AFTER // 86400} days ago; each keeps its last "
            f"{TRIM_KEEP // 1024} KB): "
            + ", ".join(f"{n} on {time.strftime('%Y-%m-%d', time.localtime(x.get('at') or 0))} (was "
                        f"{(x.get('bytes') or 0) / 2 ** 20:.1f} MB)" for n, x in sorted(t.items()) if isinstance(x, dict)))
