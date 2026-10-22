"""Available memory: the coordinator's floor, and serve's last reading.

Like the disk floor, this holds new starts while existing runs finish. A small
machine gets a quarter of its memory as its floor; recovery always needs 1 GB
more. Failed readings hold nothing back. Transitions reach the coordinator
through notify, without a principal alert.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from . import disk
from .desk import Desk, now, read_json, write_json

GB = disk.GB
DEFAULT_FLOOR = 2
MARGIN = 1


def floor(s: dict) -> float:
    v = s.get("memory_floor", DEFAULT_FLOOR)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= disk.MOST_FLOOR else DEFAULT_FLOOR


def measure() -> dict:
    """Available and total bytes. Errors propagate to serve, which records them."""
    if sys.platform == "darwin":
        percent = int(subprocess.check_output(["sysctl", "-n", "kern.memorystatus_level"],
                                              text=True, stderr=subprocess.PIPE, timeout=5))
        total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"],
                                            text=True, stderr=subprocess.PIPE, timeout=5))
        if not 0 <= percent <= 100:
            raise ValueError("kern.memorystatus_level is not a percentage")
        available = total * percent / 100
    elif sys.platform.startswith("linux"):
        fields = dict(re.findall(r"^(MemAvailable|MemTotal):\s+(\d+)\s+kB\s*$",
                                 Path("/proc/meminfo").read_text(), re.MULTILINE))
        available, total = (int(fields[k]) * 1024 for k in ("MemAvailable", "MemTotal"))
    else:
        raise OSError(f"available memory is not supported on {sys.platform}")
    if total <= 0 or not 0 <= available <= total:
        raise ValueError("invalid available or total memory")
    return {"available": available, "total": total}


def machine_floor(fl, reading: dict) -> float:
    return disk.vol_floor(fl, reading)


def record_file(desk: Desk) -> Path:
    return desk.state_dir / "memory.json"


def record(desk: Desk) -> dict:
    r = read_json(record_file(desk), {})
    return r if isinstance(r, dict) else {}


def look(desk: Desk, s: dict) -> dict:
    was = record(desk)
    reading = measure()
    fl = floor(s)
    effective = machine_floor(fl, reading)
    low = bool(fl and reading["available"] < (effective + (MARGIN if was.get("low") else 0)) * GB)
    rec = {**reading, "measured": now(), "floor": fl, "low": low}
    if not disk.written(was, rec):
        write_json(record_file(desk), rec)
    if low and not was.get("low"):
        desk.event("memory_low", floor=effective, available=reading["available"], again=(effective + MARGIN) * GB)
    elif was.get("low") and not low:
        desk.event("memory_ok", floor=effective, available=reading["available"])
    if was.get("error"):
        desk.event("memory_look_ok", error=was["error"].get("text"))
    return rec


def look_failed(desk: Desk, error: str) -> None:
    was = record(desk)
    old = was.get("error") or {}
    same = old.get("text") == error
    if not same:
        desk.event("memory_look_failed", error=error)
    write_json(record_file(desk), {**was, "low": False,
                                 "error": {"text": error, "since": old.get("since") if same else now()}})


def held(desk: Desk) -> bool:
    rec = record(desk)
    return bool(rec.get("low") and not rec.get("error"))


def held_text(desk: Desk) -> str:
    rec = record(desk)
    fl = machine_floor(floor(desk.settings()), rec)
    return (f"MEMORY LOW: {disk.gb(rec['available'])} available (floor {disk.floor_text(fl)}): "
            f"hansa serve starts {disk.STARTS} until {disk.gb((fl + MARGIN) * GB)} is available; "
            "what runs goes on · hansa explain server")


def short_text(desk: Desk) -> str:
    rec = record(desk)
    fl = machine_floor(floor(desk.settings()), rec)
    return ("hansa serve starts nothing new while available memory is under its floor "
            f"({disk.floor_text(fl)}): {disk.gb(rec['available'])} available; "
            f"waits for memory ({disk.gb((fl + MARGIN) * GB)})")


def unknown_lines(desk: Desk) -> list:
    rec = record(desk)
    err = rec.get("error")
    if not isinstance(err, dict):
        return []
    known = (f"last measured {disk._when(rec.get('measured'))}: {disk.gb(rec['available'])} available"
             if "available" in rec else "available memory never measured")
    return [f"memory: hansa serve could not look at available memory since {disk._when(err.get('since'))} "
            f"({err.get('text')}), so it holds nothing back until it can; {known} · hansa explain server"]


def current() -> dict:
    """A fresh reading for status and the command; unknown when the reader fails."""
    try:
        return measure()
    except Exception:  # noqa: BLE001 - serve records the failure; status can still show the setting
        return {}


def desk_line(desk: Desk, s: dict) -> str:
    reading = current()
    fl = floor(s)
    effective = machine_floor(fl, reading)
    own = (f" (floor {disk.gb(effective * GB)} for this {disk.gb(reading['total'])} machine)"
           if fl != effective else "")
    return (f"memory: {disk.gb(reading['available']) if 'available' in reading else '? GB'} available "
            f"· floor {disk.floor_text(fl)}{own} (hansa memory-floor)")


def explain_lines(desk: Desk, s: dict) -> list:
    return ["Memory: each round hansa serve reads available memory (macOS: kern.memorystatus_level percent "
            "of hw.memsize; Linux: MemAvailable). Below its floor it starts " + disk.STARTS + "; what runs goes on. "
            "It starts again at the floor plus 1 GB. A machine smaller than four times the floor uses a quarter "
            "of its memory instead. The coordinator hears each transition once; no principal alert runs. "
            "hansa memory-floor GB sets it (default 2; 0 turns it off).",
            "  " + desk_line(desk, s), *["  " + x for x in unknown_lines(desk)],
            *(["  now: " + held_text(desk)] if held(desk) else []), "  More: hansa help free-space"]
