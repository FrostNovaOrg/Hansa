"""Free space: a floor under which hansa serve starts nothing new, and the trimming of what old tasks kept.

The floor (settings `disk_floor`, in GB; the coordinator's, with hansa disk-floor; default DEFAULT_FLOOR; 0 turns it
off). Each round hansa serve looks at the free space (shutil.disk_usage) of each volume it and its runs write on
(places: the desk's folder, each lane's repository and worktree, the folder worker runs get their temporary folders
in and hansa serve's own TMPDIR, where the setups', checks' and tries' are made; on Linux also the desk's .hansa/tmp,
where those commands' own TMPDIR is, run.run_tmp: on the desk's volume, but named). While any of them has less free than
its floor, it starts no new task, verification, try, merge or merge check, design session or release; what already
runs goes on (a worker's resume and its task's check are part of a task that runs). It starts again once every
volume has its floor and its margin more free (MARGIN GB), so that it does not flap. A volume's floor is the
setting, but a volume smaller than SMALL times it (a tmpfs /tmp of 3.9 GB, say) gets 1/SMALL of its size instead
(vol_floor: about 1 GB there), and its margin is scaled with it, to no more than 1/SMALL of that floor
(vol_margin); a volume whose total size is not known keeps the setting. The setting stays one number. What it saw is in .hansa/disk.json (look),
which hansa status, the page and hansa explain server read; going under and coming back over are events (disk_low,
disk_ok), which the coordinator's session hears once each (notify), and disk_low runs the principal's alert once
(alert.from_event). Each place is looked at on its own (measure): one that cannot be (a folder above it the account
cannot read) is left out, and only it, and said so; the free space of its volume is not known. A look that fails
as a whole holds nothing back while it fails, and its record says so (look_failed). Either is an event when it
changes (disk_look_failed, disk_look_ok), not each round: the event log is never trimmed.

What old tasks kept (trim): a task's folder keeps its text, record (meta.json), report (result.md) and result
(run.json), which stay whole, and the output of its processes, of which only the preview's is capped (PREVIEW_KEEP).
Once a day hansa serve cuts merged tasks' passed setup, check and merge check logs to their first TRIM_HEAD and
last TRIM_KEEP bytes, with a line counting the bytes cut. Other kept logs (TRIMMED) of tasks that ended (ENDED)
or stayed done more than TRIM_AFTER ago, and ended releases' logs that old, keep their last TRIM_KEEP bytes.
The task's record says so too (`trimmed`), and hansa show. The event log is not touched.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from . import account
from .desk import Desk, now, read_json, write_bytes, write_json

GB = 10 ** 9
DEFAULT_FLOOR = 10  # GB
MOST_FLOOR = 100000  # GB: a floor past this is a typo
MARGIN = 1  # GB over the floor every volume needs before hansa serve starts things again
SMALL = 4  # a volume smaller than this many times the floor gets 1/SMALL of its size as its floor
STARTS = "no new task, verification, try, merge or merge check, design session or release"
TRIM_AFTER = 30 * 86400  # a task that ended this long ago has its kept logs trimmed
TRIM_KEEP = 256 * 1024  # bytes of a trimmed log kept, its end
TRIM_HEAD = 32 * 1024  # also kept at the start of a merged task's passed log
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


def _total(v: dict):
    t = v.get("total")
    return t if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0 else None


def vol_floor(fl, v: dict) -> float:
    """The floor in GB that holds for volume v (as measure gives it) under the setting fl: fl, or for a volume
    smaller than SMALL times fl, 1/SMALL of its size. fl where its total size is not known, and 0 (off) for 0."""
    t = _total(v)
    return fl if not fl or t is None or t >= SMALL * fl * GB else t / SMALL / GB


def vol_margin(fl, v: dict) -> float:
    """The GB over its floor volume v needs before hansa serve starts again: MARGIN, scaled with a volume's own
    floor where that is not the setting, and then no more than 1/SMALL of it."""
    vf = vol_floor(fl, v)
    return MARGIN if vf == fl else min(MARGIN * vf / fl, vf / SMALL)


def vol_bar(fl, v: dict) -> float:
    """The free bytes volume v needs for hansa serve to start again once it held back."""
    return (vol_floor(fl, v) + vol_margin(fl, v)) * GB


def own_floor(fl, v: dict) -> str:
    """'floor 1.0 GB for this 3.9 GB volume' for a volume whose floor is not the setting; '' for one whose is."""
    vf = vol_floor(fl, v)
    return "" if vf == fl else f"floor {gb(vf * GB)} for this {gb(_total(v))} volume"


def places(desk: Desk, s: dict) -> list:
    """What hansa serve and its runs write on, (what, path): the desk's folder first."""
    from .run import RUN_TMP_DIR, WORKER_TMP_ROOT
    out = [("the desk's folder", desk.root)]
    for name, ln in sorted((s.get("lanes") or {}).items()):
        if isinstance(ln, dict) and isinstance(ln.get("repo"), str) and ln["repo"].startswith("/"):
            out.append((f"lane {name}'s repository", Path(ln["repo"])))
        out.append((f"lane {name}'s worktree", desk.worktrees_dir / name))
    out.append(("the runs' temporary folders", Path(WORKER_TMP_ROOT)))
    out.append(("hansa serve's TMPDIR", Path(tempfile.gettempdir())))
    if account.LINUX and not re.search(r"\s", str(desk.state_dir)):  # where lane commands' TMPDIR is (run.run_tmp)
        out.append(("lane commands' TMPDIR", desk.state_dir / RUN_TMP_DIR))
    return out


def _volume_of(p: Path) -> tuple:
    """(device, path) of the volume path p is on: the path's, or where it is not there yet the nearest folder above
    it that is (a worktree not made yet is on its parent's volume), and that folder. Raises OSError when it cannot
    be looked at (a folder above it the account cannot read, say)."""
    p = Path(os.path.abspath(p))
    while True:
        try:
            return os.stat(p).st_dev, p
        except (FileNotFoundError, NotADirectoryError):
            if p == p.parent:
                raise
            p = p.parent


MOUNTS = {}  # (device, folder): its mount, as _mount found it; looked up once in a process


def _mount_of(dev, p: Path) -> str:
    if (dev, p) not in MOUNTS:
        MOUNTS[(dev, p)] = _mount(p)
    return MOUNTS[(dev, p)]


def _mount(p: Path) -> str:
    p = Path(os.path.realpath(p))
    while not os.path.ismount(p) and p != p.parent:
        p = p.parent
    return str(p)


def measure(desk: Desk, s: dict, was: dict | None = None) -> tuple:
    """Each place looked at on its own: (volumes, failed). volumes: each volume of places once, {mount, what (the
    places on it), free, total}, the desk's first. failed: the places that cannot be looked at, {what, path,
    error, since} (since: from was, the last look's record, for a place that failed then too), left out of volumes
    and only they; their volumes' free space is not known."""
    before = {(f.get("what"), f.get("path")): f.get("since") for f in failed_places(was or {})}
    vols, failed = {}, []
    for what, p in places(desk, s):
        try:
            dev, at = _volume_of(p)
            u = shutil.disk_usage(at)
            v = vols.get(dev) or {"mount": _mount_of(dev, at), "what": [], "free": u.free, "total": u.total}
        except Exception as e:  # noqa: BLE001 - this place is left out; the rest are measured
            failed.append({"what": what, "path": str(p), "error": desk.redact(f"{type(e).__name__}: {e}")[:300],
                           "since": before.get((what, str(p))) or now()})
            continue
        vols[dev] = v
        if what not in v["what"]:
            v["what"].append(what)
        v["free"] = min(v["free"], u.free)
    return list(vols.values()), failed


def failed_places(rec: dict) -> list:
    f = rec.get("failed")
    return [x for x in f if isinstance(x, dict)] if isinstance(f, list) else []


def record_file(desk: Desk) -> Path:
    return desk.state_dir / "disk.json"


def record(desk: Desk) -> dict:
    r = read_json(record_file(desk), {})
    return r if isinstance(r, dict) else {}


def volume_text(v: dict, fl=None) -> str:
    """The volume, the places on it and its free space; with fl, the setting, also its own floor where that is not
    fl (own_floor)."""
    own = own_floor(fl, v) if fl else ""
    return (f"{v.get('mount')} ({', '.join(v.get('what') or [])}) has {gb(v.get('free') or 0)} free"
            + (f" ({own})" if own else ""))


def again_text(fl, low: list) -> str:
    """How much free each volume needs before hansa serve starts again: 'every volume has 11.0 GB free', or where
    a volume of low has a floor of its own, each low one's amount (the others have theirs already)."""
    if all(vol_floor(fl, v) == fl for v in low):
        return f"every volume has {gb((fl + MARGIN) * GB)} free"
    return ("every volume has its own floor and margin free (" + "; ".join(
        f"{v.get('mount')} {gb(vol_bar(fl, v))}" for v in low) + ")")


def place_text(f: dict) -> str:
    return f"{f.get('what')} ({f.get('path')})"


def look(desk: Desk, s: dict) -> dict:
    """hansa serve's look, each round: the volumes, and which hold it back (low). Under its floor (vol_floor) a volume
    holds back; once holding back, it goes on until every volume has its floor and its margin (vol_margin) more. Says each change once, as an event:
    of the floor (disk_low, disk_ok), and of the places that cannot be looked at (disk_look_failed, disk_look_ok:
    which, and when they can be looked at again), as the event log is never trimmed. Returns its record, which it
    writes only when what its readers use changes (written)."""
    fl = floor(s)
    was = record(desk)
    was_low = bool(was.get("low"))
    vols, failed = measure(desk, s, was)
    bar = lambda v: vol_bar(fl, v) if was_low else vol_floor(fl, v) * GB
    low = [v for v in vols if v["free"] < bar(v)] if fl else []
    rec = {"measured": now(), "floor": fl, "low": low, "failed": failed}
    if not written(was, rec):
        desk.state_dir.mkdir(exist_ok=True)
        write_json(record_file(desk), rec)
    if low and not was_low:
        desk.event("disk_low", floor=fl, volumes=[volume_text(v, fl) for v in low], again=again_text(fl, low))
    elif was_low and not low:
        desk.event("disk_ok", floor=fl, volumes=[volume_text(v, fl) for v in vols])
    key = lambda f: (f.get("what"), f.get("path"))
    old = failed_places(was)
    new = [f for f in failed if key(f) not in {key(x) for x in old}]
    back = [f for f in old if key(f) not in {key(x) for x in failed}]
    if new:
        desk.event("disk_look_failed", places=[f"{place_text(f)}: {f['error']}" for f in new])
    if back:
        desk.event("disk_look_ok", places=[place_text(f) for f in back])
    if isinstance(was.get("error"), dict):
        desk.event("disk_look_ok", error=was["error"].get("text"))
    return rec


def written(was: dict, rec: dict) -> bool:
    """Whether the record on file (was) already says what rec does to its readers (held, unknown_lines, and
    look_failed, which keeps it): the same fields and values, measured in the same minute (as _when shows it),
    and no failed look on it."""
    minute = lambda t: time.localtime(t)[:5] if isinstance(t, (int, float)) else None
    return set(was) == set(rec) and all(was[k] == rec[k] for k in rec if k != "measured") \
        and minute(was["measured"]) == minute(rec["measured"])


def look_failed(desk: Desk, error: str) -> None:
    """hansa serve's look failed as a whole (not one place: a fault in it, or its record cannot be written): while
    it fails nothing is held back, and its record says so, with what the last look that worked measured, so that
    hansa status does not go on saying what that look saw as if it held. An event when the error is new, not each
    round."""
    was = record(desk)
    old = was.get("error") if isinstance(was.get("error"), dict) else {}
    same = old.get("text") == error
    if not same:
        desk.event("disk_look_failed", error=error)
    write_json(record_file(desk), {
        **was, "low": [],
        "error": {"text": error, "since": old.get("since") if same else now()},
        "last_low": was.get("last_low") if old else (was.get("low") or [])})


def held(desk: Desk) -> list:
    """The volumes for which hansa serve starts nothing new, as it last looked (a server that starts again looks
    again in its first round); [] when none, or when its look fails (look_failed)."""
    rec = record(desk)
    low = rec.get("low")
    return low if isinstance(low, list) and not rec.get("error") else []


def _when(t) -> str:
    if not isinstance(t, (int, float)) or isinstance(t, bool):
        return "?"
    return time.strftime("%H:%M" if time.time() - t < 86400 else "%Y-%m-%d %H:%M", time.localtime(t))


def unknown_lines(desk: Desk, rec: dict | None = None) -> list:
    """For hansa status, explain server and the page: what hansa serve's look could not measure, from its record
    (rec, or the one on file): a look that failed as a whole, with what was last known and when it was measured,
    and each place that cannot be looked at, whose volume's free space is not known. [] when it measured all."""
    rec = record(desk) if rec is None else rec
    out = []
    err = rec.get("error")
    if isinstance(err, dict):
        last = rec.get("last_low") if isinstance(rec.get("last_low"), list) else []
        fl = rec.get("floor") if isinstance(rec.get("floor"), (int, float)) else None
        known = (f"last measured {_when(rec['measured'])}: " + ("; ".join(volume_text(v, fl) for v in last)
                 + ", under the floor then" if last else "every volume over the floor then")
                 if isinstance(rec.get("measured"), (int, float)) else "free space never measured")
        out.append(f"disk: hansa serve could not look at free space since {_when(err.get('since'))} ({err.get('text')})"
                   f", so it holds nothing back until it can; {known} · hansa explain server")
    for f in failed_places(rec):
        out.append(f"disk: hansa serve cannot look at {place_text(f)} since {_when(f.get('since'))} ({f.get('error')}):"
                   " the free space of its volume is not known, and holds nothing back · hansa explain server")
    return out


def held_text(desk: Desk, low: list, fl=None) -> str:
    """Why hansa serve starts nothing new, in a line."""
    fl = floor(desk.settings()) if fl is None else fl
    own = any(vol_floor(fl, v) != fl for v in low)
    said = "; ".join(volume_text(v, fl) + (f" (floor {floor_text(fl)})" if own and vol_floor(fl, v) == fl else "")
                     + ("" if (v.get("free") or 0) < vol_floor(fl, v) * GB else f", not yet {gb(vol_bar(fl, v))}")
                     for v in low)
    return (f"DISK LOW: {said}" + ("" if own else f" (floor {floor_text(fl)})") + f": hansa serve starts {STARTS} "
            f"until {again_text(fl, low)}; what runs goes on · hansa explain server")


def short_text(desk: Desk, low: list) -> str:
    """For a queued task, merge or release: why it waits, in a clause."""
    fl = floor(desk.settings())
    if any(vol_floor(fl, v) != fl for v in low):
        return ("hansa serve starts nothing new while free space is under its floor: " + "; ".join(
            volume_text(v, fl) + ("" if own_floor(fl, v) else f" (floor {floor_text(fl)})") for v in low))
    return (f"hansa serve starts nothing new while free space is under the floor ({floor_text(fl)}"
            f"): {'; '.join(volume_text(v) for v in low)}")


def floor_zh(fl, low: list) -> str:
    """The floor for the Chinese line of what waits: the setting, or each low volume's own."""
    if all(vol_floor(fl, v) == fl for v in low):
        return floor_text(fl)
    return "；".join(f"{v.get('mount')} {gb(vol_floor(fl, v) * GB)}" for v in low)


def desk_free(desk: Desk):
    """The free and total bytes of the desk's volume now; (None, None) when it cannot be looked at."""
    try:
        u = shutil.disk_usage(desk.root)
    except OSError:
        return None, None
    return u.free, u.total


def desk_line(desk: Desk, s: dict) -> str:
    """For hansa status: the free space of the desk's volume, and the floor."""
    free, total = desk_free(desk)
    fl = floor(s)
    own = own_floor(fl, {"total": total}) if fl else ""
    return (f"disk: {gb(free) if free is not None else '? GB'} free on the desk's volume · floor {floor_text(fl)}"
            + (" (" + own.replace("this", "the desk's", 1) + ")" if own else "")
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
        f"more free. A volume smaller than {SMALL} times the floor (a tmpfs /tmp, say) has a quarter of its size as "
        f"its floor instead, and a margin scaled with it, no more than a quarter of that floor; a volume whose size "
        f"is not known keeps the floor as set. Free space by removing what builds left (a lane whose builds leave large outputs in its "
        f"worktree, or fill a cache of its own, should clean them in its setup), or set the floor with hansa "
        f"disk-floor GB (default {DEFAULT_FLOOR}; 0 turns it off).",
        f"  floor: {floor_text(fl)}"]
    rec = record(desk)
    try:
        vols, failed = measure(desk, s, rec)
        rec = {**rec, "failed": failed}
    except Exception:  # noqa: BLE001 - what hansa serve's last look recorded, which says it failed
        vols = []
    lines += [f"  {volume_text(v, fl)}" for v in vols]
    lines += [f"  {x}" for x in unknown_lines(desk, rec)]
    lines += [f"  now: {held_text(desk, low, fl)}"] if low else []
    lines += ["  More: hansa help free-space"]
    return lines


# -- what old tasks kept ---------------------------------------------------------------------------------------------
def ended_at(m: dict, tasks: dict):
    """When a task ended, for trimming; None when it has not or it cannot be told."""
    if m.get("state") not in (*ENDED, "done"):
        return None
    if m["state"] == "merged" and m.get("merged"):
        return m["merged"]
    if m["state"] == "replaced":
        by = tasks.get(m.get("replaced_by") or "") or {}
        return by.get("created") or m.get("finished")
    return m.get("finished")


def _trim_one(f: Path, at: float, keep_head: bool = False) -> int | None:
    """Keep a log's tail, optionally its first TRIM_HEAD bytes too, with a trim line; return its size before,
    or None if small, already cut or unreadable. Cuts follow nearby lines, or UTF-8 character boundaries."""
    try:
        size = f.stat().st_size
        if size <= TRIM_KEEP + (TRIM_HEAD if keep_head else 0) + 1024:
            return None
        with open(f, "rb") as fh:
            start = fh.read(TRIM_HEAD + 128)
            marker = b"[hansa trimmed this log on "
            if start.startswith(marker) or marker in start[TRIM_HEAD - 1024:]:
                return None  # including a head-and-tail cut now eligible for a tail-only cut
            fh.seek(size - TRIM_KEEP)
            tail = fh.read()
    except OSError:
        return None
    when = time.strftime('%Y-%m-%d %H:%M', time.localtime(at))
    nl = tail.find(b"\n", 0, 1024)
    tail = tail[nl + 1:] if nl >= 0 else tail
    while tail and tail[0] & 0xC0 == 0x80:  # not in the middle of a character
        tail = tail[1:]
    if keep_head:
        nl = start.rfind(b"\n", TRIM_HEAD - 1024, TRIM_HEAD)
        end = nl + 1 if nl >= 0 else TRIM_HEAD
        floor = end - 3  # UTF-8 has at most three continuation bytes.
        while end > floor and start[end] & 0xC0 == 0x80:
            end -= 1
        head = start[:end]
        head += (b"" if head.endswith(b"\n") else b"\n") + (
            f"[hansa trimmed this log on {when}: cut {size - len(head) - len(tail):,} bytes]\n").encode()
    else:
        head = (f"[hansa trimmed this log on {when}: it was {size:,} "
                f"bytes; its last {len(tail):,} bytes follow]\n").encode()
    try:
        write_bytes(f, head + tail, follow_symlinks=False)
    except OSError:
        return None
    return size


def trim(desk: Desk, at: float | None = None) -> list:
    """Daily trim: merged tasks' passed logs, old ended/done tasks and old ended releases. Return
    (task or 'release #ID', file, size before) for each cut."""
    from . import release
    at = at or time.time()
    tasks = {m["id"]: m for m in desk.tasks() if isinstance(m.get("id"), str)}
    done = []
    for tid, m in tasks.items():
        cut = {}
        with desk.lock():  # as a key's scrub of the same files takes it (Desk.scrub)
            m = desk.meta(tid)  # a done task may have queued a merge since tasks() was read
            end = ended_at(m, tasks)
            old = isinstance(end, (int, float)) and at - end >= TRIM_AFTER
            passed = ({"setup.log": m.get("setup_exit") in (None, 0),
                       "check.log": (m.get("check") or {}).get("ok"),
                       "merge-check.log": (m.get("merge_check_result") or {}).get("ok"),
                       "merge-setup.log": (m.get("merge_check_result") or {}).get("ok")}
                      if m.get("state") == "merged" else {})
            for name in TRIMMED:
                if not old and not passed.get(name):
                    continue
                size = _trim_one(desk.tasks_dir / tid / name, at, keep_head=bool(passed.get(name)))
                if size is not None:
                    cut[name] = {"at": round(at, 3), "bytes": size}
                    done.append((tid, name, size))
            if cut:
                desk.change(tid, lambda x: {"trimmed": {**(x.get("trimmed") or {}), **cut}})
    releases = []
    with desk.lock():
        for r in release.releases(desk):
            end = r.get("ended")
            if r.get("state") in release.OPEN or not isinstance(end, (int, float)) or at - end < TRIM_AFTER:
                continue
            name = f"{r['id']}.log"
            size = _trim_one(release.log_dir(desk) / name, at)
            if size is not None:
                releases.append((r["id"], name, size))
    if done or releases:
        desk.event("logs_trimmed", tasks=sorted({t for t, _, _ in done}), files=len(done) + len(releases),
                   bytes=sum(n for _, _, n in done) + sum(n for _, _, n in releases),
                   **({"releases": [rid for rid, _, _ in releases]}
                      if releases else {}))
    return done + [(f"release #{rid}", name, size) for rid, name, size in releases]


def trimmed_text(m: dict) -> str:
    """For hansa show: which of the task's logs were trimmed, when, and how big they were; '' when none."""
    t = m.get("trimmed") or {}
    if not isinstance(t, dict) or not t:
        return ""
    return (f"logs trimmed (merged passed: first {TRIM_HEAD // 1024} + last {TRIM_KEEP // 1024} KB; "
            f"other ended/done over {TRIM_AFTER // 86400} days: last {TRIM_KEEP // 1024} KB): "
            + ", ".join(f"{n} on {time.strftime('%Y-%m-%d', time.localtime(x.get('at') or 0))} (was "
                        f"{(x.get('bytes') or 0) / 2 ** 20:.1f} MB)" for n, x in sorted(t.items()) if isinstance(x, dict)))
