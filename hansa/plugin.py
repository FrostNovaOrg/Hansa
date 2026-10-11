"""The coordinator's mod: a Claude Code plugin hansa ships in hansa/mod/ and loads into the coordinator's session only.

`hansa coordinator` starts the session with `--plugin-dir <a copy of hansa/mod>` and `--autocompact <working_at>`,
both for that start only; hansa sets no environment variable but HANSA_MOD_SETTINGS, the path of the desk's own
settings file, and HANSA_MOD_CLEAR_DAILY, the window of `--clear-daily HH-HH` when the start gives one, and changes no
settings file, so no worker, design session or `claude` the session starts gets it from hansa. The mod compacts the context once a turn has ended and the session is idle, at idle_at tokens after a minute
and at long_idle_at after long_idle_min minutes (hooks/register.js says when exactly): it reads those three in the
desk's settings file, the one the page saves, at each decision, so a change there needs nothing kept in step. While
the session works, Claude Code's own auto-compact compacts it, at the window --autocompact gives (working_at, read
when the session starts). All are the principal's, on the page (desk.COMPACT_DEFAULTS). With a window, the mod also
clears the session once in each window, when it has been idle for 5 minutes (hooks/register.js says when exactly).

It needs Claude Code NEEDS or later (mods, and --autocompact, on any endpoint). On an older one the session starts
without the mod and without --autocompact, and the start says so. --autocompact is given whenever Claude Code is new
enough, whether or not the mod loads. Preparing the mod (the version probe, the copy, the start's record) never stops
the session: a step that fails leaves the mod off, and the start says what failed. hansa reads none of the mod's
files, and the mod writes none.

Where things are kept:
    plugin-XXXXXX/   the copy --plugin-dir loads (home), made new at each start in a folder hansa creates, readable and
                     writable only by whoever runs the session; the earlier copies are removed. home is the desk's
                     .hansa/coordinator-mod/, except for root on an account's desk (see account.py): root writes
                     nothing in the desk, so its copy is under root's home, in a folder of root's own
    start.json       in the desk's .hansa/coordinator-mod/, written by the desk's owner: what hansa did at this start
                     (whether it loaded the mod, and why not) and the --autocompact options of the command line, as
                     given, and the daily clear's window handed over. Removed before the mod is prepared, and tied
                     to this start (coordinator.json's at): an earlier start's is never shown
    cleared.json     beside it, written by the desk's owner from the session's SessionStart hook for /clear: when the
                     session was last cleared (the daily clear or anyone's), tied to the start as start.json is, and
                     removed with it. Root's session on an account's desk records none

Of a running session, status and the page say only what those records hold, and nothing of what Claude Code or the
mod make of it; the thresholds, as the desk's settings.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from . import account
from .desk import Desk, compact_settings, now, write_json

MOD = Path(__file__).with_name("mod")
NEEDS = (2, 1, 290)  # Claude Code's version the mod and --autocompact need, on a compatible endpoint too
NOT_COPIED = ("tests",)  # in hansa/mod but not in the copy: the mod's own tests
# Written into a plugin's folder by Claude Code when it loads it: never copied from hansa/mod
CLAUDE_WRITES = (".claude-plugin/types", "tsconfig.json")
HELP = "More: hansa help inbox"
WINDOW = re.compile(r"^([0-9]{1,2})-([0-9]{1,2})$")
MISSED = 2  # this many windows ended without a clear: the clear's line is a warning
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


def parse_window(text) -> tuple:
    """--clear-daily's window, HH-HH in local time (22-03 runs past midnight): (first hour, hour it ends). The mod
    reads it by the same rule (hooks/register.js: window)."""
    m = WINDOW.fullmatch(text) if isinstance(text, str) else None  # as given: no spaces around it
    a, b = (int(m[1]), int(m[2]) or 24) if m else (-1, -1)
    if not (0 <= a <= 23 and 1 <= b <= 24 and a != b):
        raise ValueError(f"--clear-daily takes a window of hours in local time, such as 04-09; not {text!r}")
    return a, b


def _window(x):
    """A recorded window as given; '' for none; None when it is not one."""
    if x == "":
        return ""
    try:
        parse_window(x)
    except ValueError:
        return None
    return x if isinstance(x, str) else None


def windows_ended(window: str, since: float, t: float) -> int:
    """How many of the window's days opened at or after since and ended by t, in local time: a window that had
    opened before since is not counted (the mod skips the window a session started in)."""
    a, b = parse_window(window)
    lt = time.localtime(since - 86400)
    n = 0
    for i in range(400):  # the days from the one before since, up to t
        y, mo, d = lt.tm_year, lt.tm_mon, lt.tm_mday + i
        opened = time.mktime((y, mo, d, a, 0, 0, 0, 0, -1))
        if opened > t:
            break
        ended = time.mktime((y, mo, d + (b <= a), b, 0, 0, 0, 0, -1))
        n += opened >= since and ended <= t
    return n


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
        return "hansa could not tell which Claude Code this is from claude --version"
    if tuple(int(x) for x in v.split(".")) < NEEDS:
        return f"Claude Code {v} is older than {_needed()}"
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


def options(cmd: list, option: str) -> list:
    """The words of a command line that give option, as they are there: each word that is option with the word after
    it, and each that starts with option=. hansa reads nothing into them."""
    out, i = [], 0
    while i < len(cmd):
        if cmd[i] == option:
            out += cmd[i:i + 2]
            i += 2
            continue
        if cmd[i].startswith(option + "="):
            out.append(cmd[i])
        i += 1
    return out


def start_record(p: dict, cmd: list, start, clear: str = "") -> dict:
    """What the desk keeps of this start: whether hansa loaded the mod, and why not; the --autocompact options of
    cmd, the whole command line Claude Code is started with; and the window of --clear-daily ('' for none), handed to
    the mod when it loads. start: coordinator.json's at, which coordinator_desk wrote for this start."""
    return {"start": start, "loaded": p["on"], "why": p.get("why", ""),
            "autocompact_options": options(cmd, "--autocompact"), "version": p.get("version", ""), "clear_daily": clear}


def forget(desk: Desk) -> None:
    """Remove the earlier start's record and its clears, before the mod is prepared; as the desk's owner. Never raises:
    one left there is not this start's, and status does not show it."""
    for name in ("start.json", "cleared.json"):
        try:
            (desk.mod_dir / name).unlink()
        except OSError:
            pass


def _words(x) -> bool:
    return isinstance(x, list) and all(isinstance(w, str) for w in x)


def record_start(desk: Desk, rec: dict, root: bool = False) -> None:
    """Keep this start in the desk, as the desk's owner, with only what a record holds (rec may be root's, given to
    the account's hansa); a field that is not of its kind is kept as not known (None). root: root's session on the
    account's desk."""
    why, v, opts = rec.get("why"), rec.get("version"), rec.get("autocompact_options")
    clean = {"start": rec.get("start") if _number(rec.get("start")) else None, "at": now(),
             "loaded": rec.get("loaded") if isinstance(rec.get("loaded"), bool) else None,
             "why": " ".join(why.split())[:300] if isinstance(why, str) else "",
             "autocompact_options": opts if _words(opts) else None,
             "version": v if isinstance(v, str) and re.fullmatch(r"\d{1,6}\.\d{1,6}\.\d{1,6}", v) else "",
             "clear_daily": _window(rec.get("clear_daily")), "root": root}
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


def start_lines(p: dict, clear: str = "") -> list:
    """What the start says hansa did not do, and why: a line each for the mod, --autocompact and the daily clear."""
    old = not p["autocompact"] and not p.get("failed")  # a Claude Code to update
    return ([f"hansa did not load the coordinator's mod, because {p['why']}{' (claude update)' if old else ''}. {HELP}"]
            if not p["on"] else []) + ([f"hansa did not pass --autocompact, because {p['why']}."]
                                       if not p["autocompact"] else []) + (
        [f"hansa did not hand over the daily clear {clear}, because hansa did not load the coordinator's mod "
         f"({p['why']}): the session is not cleared."] if clear and not p["on"] else [])


# -- the clears, recorded by the SessionStart hook for /clear -------------------------------------------------------
def record_clear(desk: Desk) -> str:
    """Record that the coordinator's session was cleared now, tied to the start coordinator.json names; '' when it
    was recorded or is not this account's to record (only the desk's owner records: root's session on an account's
    desk records none), else what the hook says. Never raises."""
    path = desk.mod_dir / "cleared.json"
    try:
        if account.owner(desk.root) != account.uid():
            return ""
        who = _read(desk.state_dir / "coordinator.json")
        write_json(path, {"start": who.get("at") if isinstance(who, dict) else None, "at": now()})
        return ""
    except Exception as e:  # noqa: BLE001 - the fresh context gets the desk's state all the same
        said = f"{type(e).__name__}: {e}"
        return (f"hansa could not record this clear in {path} ({' '.join(said.split())[:300]}): hansa status and the "
                "page do not count it")


def left_text(p: dict) -> str:
    """What the start says, in one line, of earlier copies it could not remove; '' when there were none."""
    return ("hansa could not remove an earlier copy of the coordinator's mod: " + "; ".join(p["left"])
            if p.get("left") else "")


# -- what hansa says of it -----------------------------------------------------------------------------------------
def _quoted(words: list) -> str:
    """Words as a shell would take them, on one line."""
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode() for c in shlex.join(words))


def status(desk: Desk) -> dict | None:
    """The coordinator's session as hansa status and the page show it: when it started; whether hansa loaded the mod
    at that start (loaded), and why not; the --autocompact options of its command line, quoted ('' when none); and its
    daily clear (_clear). None for what that start's record does not tell (none, an earlier start's, or one that does
    not hold it); None in all before a coordinator was started."""
    who = _read(desk.state_dir / "coordinator.json")
    start = who.get("at") if isinstance(who, dict) else None
    if not _number(start):
        return None
    try:
        started = time.strftime("%m-%d %H:%M", time.localtime(start))
    except (OverflowError, OSError, ValueError):
        return None
    r = _read(desk.mod_dir / "start.json")
    r = r if isinstance(r, dict) and r.get("start") == start else {}
    loaded = r.get("loaded") if isinstance(r.get("loaded"), bool) else None
    why = r.get("why") if loaded is False and isinstance(r.get("why"), str) else ""
    opts = r.get("autocompact_options")
    return {"started": started, "loaded": loaded, "why": " ".join(why.split())[:300] or None,
            "autocompact": _quoted(opts) if _words(opts) else None, "clear": _clear(desk, r, start, loaded)}


def _clear(desk: Desk, r: dict, start: float, loaded) -> dict:
    """The daily clear, from the start's record and the clears its hook recorded: the window handed over ('' none,
    None not known: an older record, or none); root, root's session on an account's desk, whose clears are not
    recorded; known, whether the last clear is known; last, when ('MM-DD HH:MM', None for none since the start); and
    missed, how many windows ended since the later of the start and the last clear (warn: MISSED or more)."""
    window = _window(r.get("clear_daily")) if "clear_daily" in r else None
    out = {"window": window, "root": r.get("root") is True, "known": False, "last": None, "missed": 0, "warn": False}
    if not window or loaded is not True or out["root"]:
        return out
    path = desk.mod_dir / "cleared.json"
    c = _read(path) if path.exists() or path.is_symlink() else {}
    mine = isinstance(c, dict) and c.get("start") == start  # an earlier start's (forget left it): none since
    if not isinstance(c, dict) or (mine and not _number(c.get("at"))):
        return out
    at = c["at"] if mine else None
    try:
        out.update(known=True, last=time.strftime("%m-%d %H:%M", time.localtime(at)) if at else None,
                   missed=windows_ended(window, max(start, at or 0), time.time()))
    except (OverflowError, OSError, ValueError):
        out.update(known=False, last=None, missed=0)
    out["warn"] = out["missed"] >= MISSED
    return out


def status_line(st: dict) -> str:
    """The line on the coordinator's session, in hansa status and on the page: what hansa did at its start, and
    nothing else."""
    if st["loaded"] is None:
        mod = "whether hansa loaded the coordinator's mod: not known"
    elif st["loaded"]:
        mod = "hansa loaded the coordinator's mod"
    else:
        mod = f"hansa did not load the coordinator's mod, because {st['why'] or 'of what is not known'}"
    opts = "not known" if st["autocompact"] is None else st["autocompact"] or "none"
    return f"coordinator's session started {st['started']}: {mod}; --autocompact options of its command line: {opts}."


def clear_line(st: dict) -> str:
    """The line on the coordinator's daily clear, in hansa status and on the page: the window hansa handed over at the
    start and the clears its hook recorded; a warning once MISSED windows or more ended without one."""
    c, w = st["clear"], st["clear"]["window"]
    if w is None or (w and st["loaded"] is None):
        return "daily clear: not known."
    if not w:
        return "no daily clear, because the coordinator was started without --clear-daily."
    if not st["loaded"]:
        return f"no daily clear {w}, because hansa did not load the coordinator's mod ({st['why'] or 'why is not known'})."
    if c["root"]:
        return f"daily clear {w}: the last clear is not known (hansa records no clear of root's session on this desk)."
    if not c["known"]:
        return f"daily clear {w}: the last clear is not known."
    said = f"last cleared {c['last']}" if c["last"] else f"not cleared since the start at {st['started']}"
    if c["warn"]:
        return f"WARNING: daily clear {w}: {said}; {c['missed']} windows ended since without a clear."
    return f"daily clear {w}: {said}."


def settings_line(s: dict) -> str:
    """hansa status's line on the desk's compaction settings, in their words."""
    v = compact_settings(s)
    said = [f"{WORDS[k][0]} {v[k]:,} {WORDS[k][1]}" for k in WORDS]
    return ("coordinator's compaction, the desk's settings: " + ", ".join(said[:3]) + " (the mod reads these); "
            + said[3] + " (hansa passes it as --autocompact at a start)")
