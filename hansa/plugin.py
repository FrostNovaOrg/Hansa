"""The coordinator's mod: a Claude Code plugin hansa ships in hansa/mod/ and loads into the coordinator's session only.

`hansa coordinator` starts the session with `--plugin-dir <a copy of hansa/mod>` and `--autocompact <working_at>`,
both for that start only; hansa sets no environment variable but HANSA_MOD_SETTINGS, the path of the desk's own
settings file, and changes no settings file, so no worker, design session or `claude` the session starts gets it from
hansa. The mod compacts the context once a turn has ended and the session is idle, at idle_at tokens after a minute
and at long_idle_at after long_idle_min minutes (hooks/register.js says when exactly): it reads those three in the
desk's settings file, the one the page saves, at each decision, so a change there needs nothing kept in step. While
the session works, Claude Code's own auto-compact compacts it, at the window --autocompact gives (working_at, read
when the session starts). All are the principal's, on the page (desk.COMPACT_DEFAULTS).

It needs Claude Code NEEDS or later (mods, and --autocompact, on any endpoint). On an older one the session starts
without the mod and without --autocompact, and the start says so. --autocompact is given whenever Claude Code is new
enough, whether or not the mod loads. Preparing the mod (the version probe, the copy, the start's record) never stops
the session: a step that fails leaves the mod off, and the start says what failed. hansa says only what it knows at
the start: whether the session was started with the mod, and why not (status). It reads none of the mod's files, and
the mod writes none.

Where things are kept:
    plugin-XXXXXX/   the copy --plugin-dir loads (home), made new at each start in a folder hansa creates, readable and
                     writable only by whoever runs the session; the earlier copies are removed. home is the desk's
                     .hansa/coordinator-mod/, except for root on an account's desk (see account.py): root writes
                     nothing in the desk, so its copy is under root's home, in a folder of root's own
    start.json       in the desk's .hansa/coordinator-mod/, written by the desk's owner: this start, as passed to
                     Claude Code (the mod loaded or not and why, and the window: the last --autocompact of its command
                     line, the caller's options included). Removed before the mod is prepared, and tied to this start
                     (coordinator.json's at): an earlier start's is never shown

What status and the page say of a running session comes from that record, and the thresholds from the desk's settings
file, read as the mod reads it (thresholds).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from . import account
from .desk import COMPACT_DEFAULTS, COMPACT_MOST, Desk, now, write_json

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
    """Where this account keeps the copy of the mod for the desk's coordinator."""
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
        r = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
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
    """A step of preparing the mod failed: the session starts without the mod (--autocompact as it was)."""
    said = f"{type(e).__name__}: {e}" if isinstance(e, BaseException) else str(e)
    return {**mod, "on": False, "failed": True, "why": f"{step} failed ({' '.join(said.split())[:300]})"}


def plan(desk: Desk, claude: str) -> dict:
    """Whether this start loads the mod and passes --autocompact, and why not: {on, autocompact, why, version, left}
    (left: earlier copies that could not be removed); with the mod on, its copy: {copy}. --autocompact whenever this
    Claude Code is new enough. Never raises: a step that fails leaves the mod off (failed)."""
    out = {"on": False, "autocompact": False, "version": "", "left": []}
    step = "asking Claude Code its version (claude --version)"
    try:
        out["version"] = v = version(claude)
        old = version_problem(v)
        if old:
            return {**out, "why": old}
        out["autocompact"] = True
        step = "finding the folder for the mod's copy (the home folder)"
        d = home(desk)
        step = f"making the mod's folder {d}"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        bad = folder_problem(d)
        if bad:
            return {**out, "why": f"its copy would be in a folder another account could change: {bad}"}
        step = f"copying the mod into {d}"
        dest, out["left"] = copy(d)
        return {**out, "on": True, "why": "", "copy": str(dest)}
    except Exception as e:  # noqa: BLE001 - the session starts all the same, without it
        return failed(out, step, e)


def _number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def given(cmd: list, option: str) -> list:
    """The values of option in a command line, in order, in either spelling (--option N, --option=N); none after a
    bare --, where the options end."""
    out = []
    for i, x in enumerate(cmd[1:], 1):
        if x == "--":
            break
        if x == option:
            out.append(cmd[i + 1] if i + 1 < len(cmd) else "")
        elif x.startswith(option + "="):
            out.append(x[len(option) + 1:])
    return out


def start_record(p: dict, cmd: list, start) -> dict:
    """What the desk keeps of this start: what Claude Code is started with, cmd its whole command line. The window is
    the last --autocompact there, whoever put it there: Claude Code takes the last. start: coordinator.json's at, which
    coordinator_desk wrote for this start."""
    windows = given(cmd, "--autocompact")
    return {"start": start, "plugin": p["on"] and p.get("copy") in given(cmd, "--plugin-dir"),
            "autocompact": windows[-1] if windows else None, "why": p.get("why", ""), "version": p.get("version", "")}


def forget(desk: Desk) -> None:
    """Remove the earlier start's record, before the mod is prepared; as the desk's owner. Never raises: one left
    there is not this start's, and status does not show it."""
    try:
        (desk.mod_dir / "start.json").unlink()
    except OSError:
        pass


def record_start(desk: Desk, rec: dict, root: bool = False) -> None:
    """Keep this start in the desk, as the desk's owner, with only what a record holds (rec may be root's, given to
    the account's hansa). root: root's session on the account's desk."""
    ac, why, v = rec.get("autocompact"), rec.get("why"), rec.get("version")
    clean = {"start": rec.get("start") if _number(rec.get("start")) else None, "at": now(),
             "plugin": rec.get("plugin") is True,
             "autocompact": "".join(c for c in ac if c.isprintable())[:40] if isinstance(ac, str) else None,
             "why": " ".join(why.split())[:300] if isinstance(why, str) else "",
             "version": v if isinstance(v, str) and re.fullmatch(r"\d{1,6}\.\d{1,6}\.\d{1,6}", v) else "",
             "root": root}
    desk.mod_dir.mkdir(parents=True, exist_ok=True)
    write_json(desk.mod_dir / "start.json", clean)


def unrecorded_text(where, e) -> str:
    """What the start says when its record could not be written."""
    said = f"{type(e).__name__}: {e}" if isinstance(e, BaseException) else str(e)
    return (f"hansa could not record this start in {where} ({' '.join(said.split())[:300]}): hansa status and the "
            "page say they do not know how this session was started")


def record(desk: Desk, rec: dict) -> str:
    """Keep this start in the desk; '' when it was, else what the start says. Never raises."""
    try:
        record_start(desk, rec)
        return ""
    except Exception as e:  # noqa: BLE001 - the session starts all the same
        return unrecorded_text(desk.mod_dir / "start.json", e)


def window_text(w) -> str:
    """The window Claude Code was given (the last --autocompact, as given), in words."""
    if w is None:
        return "its own window"
    if isinstance(w, int) or re.fullmatch(r"\d{1,12}", w):
        return f"{int(w):,} tokens"
    return f"{w} (as given to --autocompact)"


def start_text(p: dict, window) -> str:
    """What the start says of the mod when it is off, in one line; '' when it is on. window: the record's."""
    if p["on"]:
        return ""
    return (f"the coordinator's mod is off{'' if p['autocompact'] else ', and so is --autocompact'}: {p['why']}"
            + ("" if p["autocompact"] or p.get("failed") else " (claude update)") + ". The session does not compact "
            f"when idle; Claude Code compacts at {window_text(window)}. " + HELP)


def left_text(p: dict) -> str:
    """What the start says, in one line, of earlier copies it could not remove; '' when there were none."""
    return ("hansa could not remove an earlier copy of the coordinator's mod: " + "; ".join(p["left"])
            if p.get("left") else "")


# -- what hansa says of it -----------------------------------------------------------------------------------------
def _whole(x) -> bool:
    """A whole number as the mod's Number.isInteger takes one: 400000, and 400000.0 or 4e5 too."""
    return _number(x) and x == int(x)


def thresholds(path) -> dict | None:
    """The three the mod reads at each decision, read as it reads them (hooks/register.js: thresholds): one absent or
    out of range is its default; None when the file cannot be read or parsed, and then it compacts nothing while
    idle."""
    def no(x):
        raise ValueError(x)  # NaN, Infinity: not JSON to the mod
    try:
        x = json.loads(Path(path).read_bytes().decode("utf-8"), parse_constant=no)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(x, dict):
        return None
    cc = x.get("coordinator_compact") if isinstance(x.get("coordinator_compact"), dict) else {}
    return {k: int(cc[k]) if _whole(cc.get(k)) and COMPACT_MOST[k][0] <= cc[k] <= COMPACT_MOST[k][1] else v
            for k, v in COMPACT_DEFAULTS.items() if k != "working_at"}


def values_text(v: dict) -> str:
    """The thresholds in their words: compact when idle at 400,000 tokens, ..."""
    window = f"{WORDS['working_at'][0]} {window_text(v['working_at'])}"
    if v["idle_at"] is None:
        return "it cannot read the desk's settings file now, so it does not compact when idle; " + window
    return ", ".join([*(f"{WORDS[k][0]} {v[k]:,} {WORDS[k][1]}" for k in WORDS if k != "working_at"), window])


def status(desk: Desk) -> dict | None:
    """The coordinator's mod as hansa status and the page show it: what hansa knew when the session started
    ({known: False} when that start left no record of its own). None before a coordinator was started."""
    who = _read(desk.state_dir / "coordinator.json")
    start = who.get("at") if isinstance(who, dict) else None
    if not _number(start):
        return None
    try:
        started = time.strftime("%m-%d %H:%M", time.localtime(start))
    except (OverflowError, OSError, ValueError):
        return None
    r = _read(desk.mod_dir / "start.json")
    if not isinstance(r, dict) or r.get("start") != start:  # none, or an earlier start's that was left there
        return {"known": False, "started": started}
    ac = r.get("autocompact") if isinstance(r.get("autocompact"), str) else None
    # the first three: what the mod reads at its next decision, from the file it reads (None: it cannot read it); the
    # fourth: the window Claude Code was given, a number of tokens when it was one
    read = thresholds(desk.settings_path) or {k: None for k in WORDS if k != "working_at"}
    values = {**read, "working_at": int(ac) if ac and re.fullmatch(r"\d{1,12}", ac) else ac}
    return {"known": True, "on": r.get("plugin") is True, "root": r.get("root") is True,
            "started": started, "why": " ".join(str(r.get("why") or "").split())[:300], "autocompact": ac,
            "values": values}


def status_line(st: dict) -> str:
    """hansa status's line on the coordinator's mod: the session was started with it, or without it and why, or
    hansa does not know."""
    head = f"coordinator's mod: the session started {st['started']} was started "
    if not st["known"]:
        return (f"coordinator's mod: hansa does not know how the session started {st['started']} was started (that "
                f"start left no record). {HELP}")
    if not st["on"]:
        return (head + f"without it: {st['why']}"
                + (f"; Claude Code compacts at {window_text(st['autocompact'])}" if st["autocompact"] is not None
                   else "")
                + f". {HELP}")
    return head + "with it; " + values_text(st["values"])
