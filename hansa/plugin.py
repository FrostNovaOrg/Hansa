"""The coordinator's mod: a Claude Code plugin hansa ships in hansa/mod/ and loads into the coordinator's session only.

`hansa coordinator` starts the session with `--plugin-dir <a copy of hansa/mod>` and `--autocompact <working_at>`,
both for that start only; hansa sets no environment variable but the one that names its thresholds file, and changes
no settings file, so no worker, design session or `claude` the session starts gets it from hansa. The mod compacts
the context once a turn has ended and the session is idle, at idle_at tokens after a minute and at long_idle_at after
long_idle_min minutes (hooks/register.js says when exactly); while it works, Claude Code's own auto-compact does, at
the window --autocompact gives. All are the principal's, on the page (desk.COMPACT_DEFAULTS).

It needs Claude Code NEEDS or later (mods, and --autocompact, on any endpoint). On an older one the session starts
without the mod and without --autocompact, and the start says so. Preparing the mod (the version probe, the copy,
its thresholds file, the start's record) never stops the session: a step that fails leaves the mod off, and
--autocompact with it, and the start says what failed. hansa says only what it knows at the start: whether the
session was started with the mod, and why not (status). It reads none of the mod's files, and the mod writes none.

Where things are kept (home):
    plugin-XXXXXX/   the copy --plugin-dir loads, made new at each start in a folder hansa creates, readable and
                     writable only by whoever runs the session; the earlier copies are removed
    settings.json    the thresholds (desk.compact_settings), which the mod reads at each decision
                     (HANSA_MOD_SETTINGS): the page's save writes them there, so a change holds without a restart
home is the desk's .hansa/coordinator-mod/, except for root on an account's desk (see account.py): root neither
reads nor writes in the desk, so its copy and thresholds are under root's home, in a folder of root's own, and a
change on the page reaches them at the session's next start. In the desk's .hansa/coordinator-mod/, whoever runs it:
    start.json       the last start: when, whether the mod is on and why not, by root or not, its thresholds
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from . import account
from .desk import COMPACT_DEFAULTS, Desk, compact_settings, now, write_json

MOD = Path(__file__).with_name("mod")
NEEDS = (2, 1, 290)  # Claude Code's version the mod and --autocompact need, on a compatible endpoint too
NOT_COPIED = ("tests",)  # in hansa/mod but not in the copy: the mod's own tests
# Written into a plugin's folder by Claude Code when it loads it: never copied from hansa/mod
CLAUDE_WRITES = (".claude-plugin/types", "tsconfig.json")
HELP = "More: hansa help inbox"
# The thresholds' names, the same in hansa status, the page and the docs
WORDS = {"idle_at": ("compact when idle at", "tokens"), "long_idle_at": ("compact after a long idle at", "tokens"),
         "long_idle_min": ("a long idle is", "minutes"), "working_at": ("compact while working at", "tokens")}


def _read(path, default=None):
    """One of hansa's own small JSON files: default when it is missing or cannot be read as JSON in UTF-8."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return default


def home(desk: Desk) -> Path:
    """Where this account keeps the copy and the thresholds for the desk's coordinator."""
    if account.desk_account(desk.root):
        return Path.home() / ".hansa" / "coordinator-mod" / hashlib.sha1(str(desk.root).encode()).hexdigest()[:12]
    return desk.mod_dir


def folder_problem(path: Path) -> str:
    """Why another account could change this folder or one above it; '' when none can: each is owned by this
    account or root, and not writable by others (unless sticky, as /tmp is)."""
    me = account.uid()
    real = Path(os.path.realpath(path))  # where its links lead: those folders are the ones written
    for d in [real, *real.parents]:
        try:
            st = os.stat(d)
        except OSError as e:
            return f"{d} cannot be looked at ({e.strerror})"
        owner = account.owner(d)
        if owner not in (me, 0):
            return f"{d} belongs to {account.name_of(owner)}"
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not st.st_mode & stat.S_ISVTX:
            return f"others may write in {d}"
    return ""


def copy(d: Path, src: Path = MOD) -> tuple:
    """A new copy of the mod in a new folder in d, readable and writable only by this account; the earlier copies
    there are removed. Returns the new folder, and what could not be removed, each with why."""
    dest = Path(tempfile.mkdtemp(prefix="plugin-", dir=d))  # new, and 0700
    for f in sorted(src.rglob("*")):
        rel = f.relative_to(src).as_posix()
        if f.is_file() and rel.split("/")[0] not in NOT_COPIED and not any(
                rel == x or rel.startswith(x + "/") for x in CLAUDE_WRITES):
            (dest / rel).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            (dest / rel).write_bytes(f.read_bytes())
            (dest / rel).chmod(0o600)
    left = []
    for old in d.glob("plugin*"):
        if old == dest:
            continue
        try:
            if old.is_dir() and not old.is_symlink():
                shutil.rmtree(old)
            else:
                old.unlink()
        except OSError as e:
            left.append(f"{old} ({e.strerror or e})")
    return dest, left


# -- preparing it for a start --------------------------------------------------------------------------------------
def version(claude: str) -> str:
    """The Claude Code version `claude --version` says (2.1.296), or '' when it says none."""
    try:
        r = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, ValueError, subprocess.TimeoutExpired):  # ValueError: what it printed is not UTF-8
        return ""
    m = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", r.stdout or "")
    return ".".join(m.groups()) if m and r.returncode == 0 else ""


def _needed() -> str:
    return ".".join(map(str, NEEDS))


def version_problem(v: str) -> str:
    """Why this Claude Code cannot run the mod or take --autocompact; '' when it can."""
    if not v:
        return f"hansa could not tell which Claude Code this is (claude --version), and the mod needs {_needed()}"
    if tuple(int(x) for x in v.split(".")) < NEEDS:
        return f"Claude Code {v} is older than {_needed()}, which the mod needs"
    return ""


def failed(mod: dict, step: str, e) -> dict:
    """A step of preparing the mod failed: the session starts without the mod and without --autocompact."""
    said = f"{type(e).__name__}: {e}" if isinstance(e, BaseException) else str(e)
    return {"on": False, "autocompact": False, "version": mod.get("version", ""), "failed": True,
            "left": mod.get("left", []), "why": f"{step} failed ({' '.join(said.split())[:300]})"}


def plan(desk: Desk, claude: str, values: dict) -> dict:
    """Whether this start loads the mod and passes --autocompact, and why not: {on, autocompact, why, version, left}
    (left: earlier copies that could not be removed). With the mod on, its copy is made and its thresholds file
    written with these values: {copy, settings}. Never raises: a step that fails leaves both off (failed)."""
    out = {"on": False, "autocompact": False, "version": "", "left": []}
    step = "asking Claude Code its version (claude --version)"
    try:
        out["version"] = v = version(claude)
        old = version_problem(v)
        if old:
            return {**out, "why": old}
        d = home(desk)
        step = f"making the mod's folder {d}"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        bad = folder_problem(d)
        if bad:
            return {**out, "autocompact": True,
                    "why": f"its copy would be in a folder another account could change: {bad}"}
        step = f"copying the mod into {d}"
        dest, out["left"] = copy(d)
        step = f"writing the mod's settings file {d / 'settings.json'}"
        write_json(d / "settings.json", values)
        return {**out, "on": True, "autocompact": True, "why": "", "copy": str(dest),
                "settings": str(d / "settings.json")}
    except Exception as e:  # noqa: BLE001 - the session starts all the same, without it
        return failed(out, step, e)


def start_record(p: dict) -> dict:
    """What the desk keeps of this start (start.json), made by whoever runs the session."""
    return {"at": now(), "on": p["on"], "autocompact": p["autocompact"], "why": p.get("why", ""),
            "version": p.get("version", "")}


def record_start(desk: Desk, rec: dict, root: bool = False) -> None:
    """Keep this start in the desk, as the desk's owner. root: root's session on the account's desk."""
    clean = {k: rec.get(k) for k in ("at", "on", "autocompact", "why", "version")}
    desk.mod_dir.mkdir(parents=True, exist_ok=True)
    write_json(desk.mod_dir / "start.json", {**clean, "root": root, **compact_settings(desk.settings())})


def record(desk: Desk, mod: dict) -> dict:
    """Keep this start in the desk; the mod as it is then (off, when that failed, and that start kept if it can be).
    A mod already off is kept off for its own reason."""
    try:
        record_start(desk, start_record(mod))
        return mod
    except Exception as e:  # noqa: BLE001
        if not mod["on"]:
            return mod
        off = failed(mod, f"recording the start in {desk.mod_dir / 'start.json'}", e)
    with contextlib.suppress(Exception):
        record_start(desk, start_record(off))
    return off


def start_text(p: dict, working_at: int) -> str:
    """What the start says of the mod when it is off, in one line; '' when it is on."""
    if p["on"]:
        return ""
    if not p["autocompact"]:
        return (f"the coordinator's mod is off, and so is --autocompact: {p['why']}"
                + ("" if p.get("failed") else " (claude update)") + ". The session does not compact when idle, and "
                "Claude Code compacts at its own window. " + HELP)
    return (f"the coordinator's mod is off: {p['why']}. The session does not compact when idle; Claude Code "
            f"compacts at {working_at:,} tokens. " + HELP)


def left_text(p: dict) -> str:
    """What the start says, in one line, of earlier copies it could not remove; '' when there were none."""
    return ("hansa could not remove an earlier copy of the coordinator's mod: " + "; ".join(p["left"])
            if p.get("left") else "")


# -- what hansa says of it -----------------------------------------------------------------------------------------
def values_text(v: dict) -> str:
    """The thresholds in their words: compact when idle at 400,000 tokens, ..."""
    return ", ".join(f"{WORDS[k][0]} {v[k]:,} {WORDS[k][1]}" for k in WORDS)


def status(desk: Desk) -> dict | None:
    """The coordinator's mod as hansa status and the page show it: what hansa knew when the session started. None
    before a session was started with it in mind, or when that start's record cannot be read."""
    r = _read(desk.mod_dir / "start.json")
    if not isinstance(r, dict) or not isinstance(r.get("at"), (int, float)) or isinstance(r.get("at"), bool):
        return None
    try:
        started = time.strftime("%m-%d %H:%M", time.localtime(r["at"]))
    except (OverflowError, OSError, ValueError):
        return None
    on, root = r.get("on") is True, r.get("root") is True
    values = compact_settings({"coordinator_compact": r})
    held = _read(desk.mod_dir / "settings.json") if on and not root else None
    if isinstance(held, dict):  # the page's save reaches this session's file: the thresholds it decides by now
        values.update({k: v for k, v in compact_settings({"coordinator_compact": held}).items() if k != "working_at"})
    return {"on": on, "root": root, "started": started, "why": " ".join(str(r.get("why") or "").split())[:300],
            "values": values}


def status_line(st: dict) -> str:
    """hansa status's line on the coordinator's mod: the session was started with it, or without it and why."""
    head = f"coordinator's mod: the session started {st['started']} was started "
    if not st["on"]:
        return head + f"without it: {st['why']}. {HELP}"
    return (head + "with it; " + values_text(st["values"])
            + ("; root started it, so a change on the page reaches it at its next start" if st["root"] else ""))


def kept_text(desk: Desk) -> str:
    """What the page says when the page's thresholds did not reach the mod's thresholds file of the last start;
    '' when they did."""
    r = _read(desk.mod_dir / "start.json")
    path = desk.mod_dir / "settings.json"
    if not isinstance(r, dict) or r.get("on") is not True or r.get("root") is True or not path.exists():
        return ""
    held = _read(path)
    want = compact_settings(desk.settings())
    if isinstance(held, dict) and all(held.get(k) == want[k] for k in COMPACT_DEFAULTS if k != "working_at"):
        return ""
    return (f"hansa could not write the new thresholds into {path}: the coordinator's mod keeps its earlier ones "
            "until the session's next start")
