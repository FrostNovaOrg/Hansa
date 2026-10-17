"""The shim's Cursor backend: a worker run on the Cursor provider (ANTHROPIC_BASE_URL http://cursor.invalid) runs
Cursor's headless CLI (`cursor-agent -p`) in a copy of the worktree, and the copy's changes come back to the
worktree as a patch. hansa's provider check on it runs Cursor once in an empty folder of the run's own.

On macOS Cursor cannot run inside srt: it needs the login keychain when it starts, and srt refuses it. So it runs
outside srt, and its commands run in Cursor's own sandbox ($HOME/.cursor/sandbox.json). That sandbox opens the
workspace's git folder for writing, and follows it where a command points it, so Cursor is never given hansa's
worktree. It works in ws/, a copy with a repository of its own; the shim brings back the difference between two
snapshots of the copy, taken in a repository Cursor is never given. A commit made in the copy is kept off a
repository outside it, however the copy's .git is pointed: for the run's length the shim itself keeps the git
folders outside the copy read-only (the modes recorded in a lock folder there, in any of the layouts git_dirs
resolves). The hold is shared by the runs on one repository — each run an owner of the lock, so several runs
go at once — and the last to end puts the modes back, on every end the shim controls and by the watchdog when
the shim is killed outright; a restore that cannot finish fails the run and keeps its record, for a later run
to finish, and a holder that cannot be proven dead is never taken over. After the run the shim compares the
folders' refs and objects and fails the run, bringing nothing back, when they moved (README, "The copy's git
boundary").

Cursor leaves processes its own group does not hold: the cursorsandbox each command runs in, in a group of its own,
and a detached worker-server. The shim finds them by what they run and where (the run's HOME in the sandbox's
--policy, the run's ws/ as the worker-server's working folder) and stops them with Cursor: on a signal, past
--max-turns and after every run; a watchdog does the same when the shim is killed outright. Python 3.9, standard
library only; hansa_codex_shim routes the call here while the settings turn the backend on (HANSA_CURSOR_ENABLE=on;
off, the call is the real claude's), and its parse, answer, emit, classify, process table and given_env are this
module's too.
"""
import contextlib
import json
import os
import pwd
import re
import select
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import hansa_codex_shim as codex
from hansa_codex_shim import answer, emit, one_line

GROK = "grok-4.7"  # HANSA_CURSOR_MODEL's default; hansa's --effort picks its form
DEFAULT_MODEL = GROK
EFFORT_FORMS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}
NO_EFFORT_FORM = "high"  # no --effort
VERSION_TIMEOUT = 30  # seconds `cursor-agent -v` may take
CHECK_PROMPT = "Reply with the single word: ok"  # hansa's provider check, as Cursor is asked it
# Cursor's settings for each run, in its CURSOR_CONFIG_DIR (Cursor adds its own keys and keeps these): no MCP tools
# and no WebFetch, its sandbox on with the network of sandbox.json alone, commands only as the allowlist says
CLI_CONFIG = {"version": 1, "permissions": {"allow": [], "deny": ["Mcp(*:*)", "WebFetch(*)"]},
              "sandbox": {"mode": "enabled", "networkAccess": "user_config_only"}, "approvalMode": "allowlist",
              "channel": "static", "autoAcceptWebSearch": False}
LOCAL_DENY = ["127.0.0.0/8", "::1/128", "localhost"]  # the sandbox's network never reaches this machine
# What Cursor gives the model from the folders above its workspace: a run refuses while one is there
ABOVE_FILES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".cursorrules")
# In the worktree, anywhere: what Cursor would obey (its project folder; Claude Code's project settings, whose hooks
# it runs outside its sandbox), and a repository below the top, whose git folder its sandbox would open for writing
WORKTREE_DENY = ".cursor"
CLAUDE_SETTINGS = ("settings.json", "settings.local.json")
# A patch touching a path with one of these components does not come back: the project settings, hooks, rules and
# skills of Cursor, Claude Code and Codex, which a later run on the branch would obey once hansa has committed them
PATCH_DENY = (".cursor", ".claude", ".codex", ".agents")
SUBMODULE = "160000"
# The copy's git boundary, held by the shim itself, not left to Cursor's sandbox (README, "The copy's git
# boundary"): the git folders of the worktree (its own and the main repository's common one, in any of the
# layouts git_dirs resolves), the repositories outside the copy that a commit made in the copy must never
# reach, however the copy's .git is pointed. For the run's length hold_boundary takes every write permission
# off them; the last run to end puts it back (release_boundary on every end the shim controls, the watchdog
# when the shim is killed outright); a restore that cannot finish is reported, the run fails, and the lock is
# kept with what is left to restore, for a later run to finish. The hold is shared: HOLD, a lock folder in the
# common git folder, records the modes once (before any is taken off), and each run on the repository adds an
# owner file of its own (named with its token, its path and token in <mine>/GIT_BOUNDARY for its watchdog), so
# several runs on one repository go at once, each protected alike; the modes go back when no owner is left
# alive. A release touches the run's own owner file alone, so a late cleanup of an old run cannot strip a new
# run's protection, and every transition of an established hold runs under a short-lived mkdir lock inside it
# (hold_mux), so a join and the last leaver's restore never cross. A lock half set up (a holder died making
# it) is refused while its holder is alive or cannot be proven dead, else repaired: what it stripped put
# back, then held again. After the run, boundary_state compared across it is the net for a command that put
# the write bits back: the run fails and nothing is brought back.
HOLD = "hansa-cursor-hold"
# Seconds a run waits for a hold being set up or for another's short transition of it before giving up
# (HANSA_CURSOR_TEST_MUTEX_WAIT: the tests set it small, as they do HANSA_CURSOR_TEST_FAIL_CHMOD)
MUTEX_WAIT = int(os.environ.get("HANSA_CURSOR_TEST_MUTEX_WAIT") or 60)
GIT_BOUNDARY = "git-boundary"
# Every git command the shim runs: no hooks, no fsmonitor, no garbage collection left running in the background;
# with git_env, no global or system config and no terminal prompt
GIT_OFF = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "gc.auto=0",
           "-c", "maintenance.auto=false", "-c", "user.name=hansa-cursor", "-c", "user.email=hansa-cursor@localhost")
# The snapshot repository's info/attributes, which outranks every .gitattributes: no conversion in the snapshots,
# the patch or its application (eol, filters, ident, encodings), so the bytes that come back are the copy's.
# (GIT_ATTR_SOURCE would do it too, but git apply 2.50 crashes on it, as on any attributes not from a work tree.)
NO_CONVERSION = b"* -text -eol -crlf -filter -ident -working-tree-encoding\n"
# What git add says, exiting 0, of a folder it cannot read: the snapshot would leave it out, so it is an error
UNREAD = re.compile(r"could not open|Permission denied", re.I)
# Run outside any sandbox, before Cursor: the lane's env and sandbox prelude (hansa's CLAUDE_ENV_FILE), so that
# Cursor's commands get it, then the run's HOME, CURSOR_CONFIG_DIR, TMPDIR and TMPPREFIX, whatever the lane's env said.
INNER = ('hansa_cursor_f=$1; hansa_cursor_h=$2; hansa_cursor_c=$3; hansa_cursor_t=$4; hansa_cursor_z=$5; shift 5; '
         'if [ -n "$hansa_cursor_f" ]; then . "$hansa_cursor_f"; fi; '
         'HOME=$hansa_cursor_h; CURSOR_CONFIG_DIR=$hansa_cursor_c; TMPDIR=$hansa_cursor_t; TMPPREFIX=$hansa_cursor_z; '
         'export HOME CURSOR_CONFIG_DIR TMPDIR TMPPREFIX; '
         'unset hansa_cursor_f hansa_cursor_h hansa_cursor_c hansa_cursor_t hansa_cursor_z; exec "$@"')
# What Cursor leaves running (measured on 2026.10.01-e373342): `<the program's folder>/cursorsandbox --policy
# <HOME>/.cursor/sandbox-policies/<file> -- /bin/zsh -c ...` for each command, in a group of its own, its parent
# launchd once Cursor ends; and `<the program's folder>/node <the program's folder>/index.js worker-server`, detached,
# with the workspace as its working folder, for some minutes after a run
SANDBOX_PROGRAM = "cursorsandbox"
WORKER_SERVER = "worker-server"
# Cursor's own words for a missing login, seen in its output; its other failures are classed as Codex's are
LOGIN_TEXT = re.compile(r"Authentication required|Not logged in", re.I)


class Problem(Exception):
    """Why the run cannot start, or its changes cannot come back: said in the error answer."""


class Live:
    proc = None  # Cursor, while it runs
    seen: dict = {}  # what ran below it, as last looked up
    mine = ""  # the run's folder, removed however the run ends
    find = None  # the run's leftovers in a process table (leftovers, for this run's program, HOME and ws/)
    dog = None  # the watchdog


def main(kind: str, argv: list, env: dict, conf: dict) -> int:
    """A call on the Cursor provider (hansa_codex_shim's call_kind): "cursor", "cursor-check" or "cursor-resume"."""
    a = codex.parse(argv)
    if kind == "cursor-resume":
        return emit(answer("error_during_execution", "hansa-cursor: resume is not supported on the Cursor provider",
                           a.get("--resume") or ""))
    return run(a, env, conf, check=kind == "cursor-check")


# ---- paths --------------------------------------------------------------------------------------------------

def within(path: str, folder: str) -> bool:
    """Whether `path` is `folder` or inside it, as this volume compares paths (macOS's match names without regard
    to case): their spellings, as given and with links resolved, compared case-folded; and, where the folder
    exists, the folder itself (os.path.samefile) among the path and the folders above it."""
    if not path or not folder:
        return False
    if any(codex.under(x.casefold(), y.casefold()) for x in codex.both([path]) for y in codex.both([folder])):
        return True
    try:
        f = os.stat(folder)
    except OSError:
        return False
    p = os.path.abspath(path)
    while True:
        try:
            if os.path.samestat(os.stat(p), f):
                return True
        except OSError:
            pass
        if os.path.dirname(p) == p:
            return False
        p = os.path.dirname(p)


def git_dirs(wt: str):
    """(the worktree's own git folder, the repository's common one), read as files (no git runs here); (None,
    None) without a .git, or when it leads nowhere readable. Three layouts: a plain .git folder (own is the
    common one); a .git file naming a folder that holds a commondir file (a linked worktree); and a .git file
    naming a folder that holds none (--separate-git-dir: the folder it names is the whole repository, so own
    is the common one)."""
    dot = os.path.join(wt, ".git")
    if os.path.isdir(dot):
        return dot, dot
    try:
        text = Path(dot).read_text(encoding="utf-8").strip()
    except OSError:
        return None, None
    if not text.startswith("gitdir:"):
        return None, None
    own = os.path.normpath(os.path.join(wt, text[len("gitdir:"):].strip()))
    if not os.path.isdir(own):
        return None, None
    try:
        common = Path(own, "commondir").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return own, own  # no commondir: the folder the .git file names is the whole repository
    except OSError:
        return None, None
    return own, os.path.normpath(os.path.join(own, common))


def guarded(wt: str, tmp: str) -> list:
    """What Cursor's commands are never given: the worktree, its git folders (its own and the main repository's)
    and the worker's TMPDIR, which holds the run's folder (the snapshot, Cursor's config)."""
    own, common = git_dirs(wt)
    return [x for x in (wt, own, common, tmp) if x]


def boundary_folders(wt: str) -> list:
    """The git folders of the worktree, its own and the main repository's common one (git_dirs: any of the
    layouts), links resolved, each once: the repositories outside the copy that a commit made in the copy must
    never reach, however the copy's .git is pointed. [] when the worktree's .git cannot be read; a main
    checkout's own and common are one."""
    own, common = git_dirs(wt)
    return list(dict.fromkeys(os.path.realpath(x) for x in (own, common) if x))


def pid_alive(pid: int, start: str) -> bool:
    """Whether a process with this pid and start time runs, against the process table. What cannot be checked
    is taken as alive, never as dead: a process table that cannot be read at all (it would call a live holder
    dead, and a restore with a live holder would strip a held boundary), and a line without a start (its
    writer could not learn its own: its pid alone, in a readable table). The tests' fault injection:
    HANSA_CURSOR_TEST_NO_TABLE makes the table unreadable here, as HANSA_CURSOR_TEST_FAIL_CHMOD does a chmod,
    so a holder that cannot be checked is exercised in the shim's own process."""
    table = {} if os.environ.get("HANSA_CURSOR_TEST_NO_TABLE") else codex.process_table()
    if os.getpid() not in table:
        return True  # the process table cannot be read: the holder may be alive
    return table.get(pid, (0, ""))[1] == start if start else pid in table


def owner_alive(lock: str) -> bool:
    """Whether the run whose lock this is still runs: its owner file's pid and start (pid_alive). A lock
    without its owner file yet (a holder killed in the first instant, or writing it now) is taken as alive
    for its first seconds."""
    try:
        pid, _, start = Path(lock, "owner").read_text(encoding="utf-8").strip().partition(" ")
    except OSError:
        pid = ""
    if pid.isdigit():
        return pid_alive(int(pid), start)
    try:
        return time.time() - os.path.getmtime(lock) < 5
    except OSError:
        return False


def holders(lock: str) -> list:
    """The hold's owner files, [(path, alive)]: one per run sharing the hold (`owner-<token>`, `<pid> <start>`
    inside; `owner` is an old lock's). An owner file without a usable line is alive for its first seconds, as
    owner_alive's lock is; an unreadable one says nothing — the folder went with it."""
    out = []
    try:
        names = sorted(os.listdir(lock))
    except OSError:
        return out
    for name in names:
        if name != "owner" and not name.startswith("owner-"):
            continue
        p = os.path.join(lock, name)
        try:
            pid, _, start = Path(p).read_text(encoding="utf-8").strip().partition(" ")
        except OSError:
            continue
        if pid.isdigit():
            alive = pid_alive(int(pid), start)
        else:
            try:
                alive = time.time() - os.path.getmtime(p) < 5
            except OSError:
                alive = False
        out.append((p, alive))
    return out


def my_start() -> str:
    """This process's start time as the process table gives it ('' when it cannot be read)."""
    return codex.process_table().get(os.getpid(), (0, ""))[1]


class _HoldGone(Exception):
    """The hold's folder went with another run's transition while this one waited for its turn; the caller
    looks again from the top."""


@contextlib.contextmanager
def hold_mux(lock: str):
    """One transition of an established hold at a time — a join, a release, a repair: a mkdir lock inside the
    hold's folder (the folder keeps its write bit, so this can be made while the boundary is read-only), kept
    for the short length of one transition, so a join and the last leaver's restore never cross. A run that
    finds it waits — a transition is quick — up to MUTEX_WAIT seconds, then refuses; one whose owner is
    provably dead removes it (its own release removes it, so the folder cannot be taken from under a live
    holder); one it cannot check it waits out, never removed: a live holder would look dead to a removal.
    _HoldGone when the hold's folder itself went meanwhile. Problem after the wait."""
    mux = os.path.join(lock, "mux")
    end = time.time() + MUTEX_WAIT
    while True:
        try:
            os.mkdir(mux)
            break
        except FileExistsError:
            if not owner_alive(mux):
                shutil.rmtree(mux, ignore_errors=True)
                continue
            if time.time() > end:
                raise Problem(f"another Cursor run's transition of this repository's git boundary did not "
                              f"clear; the run is refused, not queued: try again when it ends")
            time.sleep(0.05)
        except FileNotFoundError:
            raise _HoldGone()
        except OSError as e:
            raise Problem(f"the repository's git boundary could not be held: {e}")
    try:
        try:
            Path(mux, "owner").write_text(f"{os.getpid()} {my_start()}\n", encoding="utf-8")
        except OSError as e:
            raise Problem(f"the repository's git boundary could not be held: {e}")
        yield
    finally:
        shutil.rmtree(mux, ignore_errors=True)


def restore_chmod(path: str, mode: int) -> None:
    """os.chmod on a path the hold is giving back, with the tests' fault injection: when
    HANSA_CURSOR_TEST_FAIL_CHMOD is set, every path with that ending refuses, so a restore that cannot finish
    is exercised for real."""
    fail = os.environ.get("HANSA_CURSOR_TEST_FAIL_CHMOD")
    if fail and str(path).endswith(fail):
        raise PermissionError(f"injected failure: {path} (the tests' HANSA_CURSOR_TEST_FAIL_CHMOD)")
    os.chmod(path, mode)


def restore_record(lock: str) -> list:
    """Put back the modes the hold's lock recorded (parents before their children: a child's path resolves
    through them); [] returned. What refuses is reported, [(path, why)], and the record narrowed to only what
    is left to restore, so the failure is not lost and a later run can finish it."""
    try:
        held = json.loads(Path(lock, "modes").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        held = []
    left, failed = [], []
    for path, mode in sorted(held, key=lambda pm: pm[0].count(os.sep)):
        try:
            restore_chmod(path, mode)
        except (OSError, TypeError) as e:
            failed.append((path, f"{e}"))
            left.append((path, mode))
    if failed:
        try:  # only what is left to restore, for a later run to finish (the file is 0o444 from the hold)
            record = os.path.join(lock, "modes")
            os.chmod(record, 0o644)
            Path(record).write_text(json.dumps(left), encoding="utf-8")
            os.chmod(record, 0o444)
        except OSError:
            pass
    return failed


def put_back_why(lock: str, failed: list) -> str:
    """The restore failure as the run's error says it."""
    what = ", ".join(f"{p} ({e})" for p, e in failed[:3])
    return (f"the copy's git boundary could not be put back: {what}{' ...' if len(failed) > 3 else ''}; the "
            f"repository's git folders are still read-only, what is left to restore recorded in {lock}")


def release_lock(lock: str, token: str) -> list:
    """The run's share of the hold given back: its owner file (the one its token names) removed, and, when no
    owner of the hold is left alive after that, the boundary restored — the recorded modes put back and the
    lock removed — under the boundary's mutex. [] returned. A token whose owner file is not there (already
    given back, or another run's instance) does nothing, so a late cleanup of an old run cannot strip a new
    run's protection, and with owners left whose liveness cannot be checked the restore waits for them rather
    than risk a live one. What refuses of the restore is reported, [(path, why)]; the lock is kept, the
    record narrowed to what is left and the run's owner file put back with it, so a later release or run can
    finish it."""
    try:
        with hold_mux(lock):
            mine = os.path.join(lock, f"owner-{token}")
            if not os.path.exists(mine):
                return []  # not this run's share (given back already, or never taken): nothing to do
            try:
                os.remove(mine)
            except OSError:
                pass
            if any(alive for _, alive in holders(lock)):
                return []  # other runs still hold the boundary: the last of their releases restores it
            if not os.path.isdir(lock):
                return []
            failed = restore_record(lock)
            if failed:
                try:  # the record stays, and this run's owner file with it: still this instance's work
                    Path(mine).write_text(f"{os.getpid()} {my_start()}\n", encoding="utf-8")
                except OSError:
                    pass
                return failed
            shutil.rmtree(lock, ignore_errors=True)
            return []
    except _HoldGone:
        return []  # the hold went with another transition, this run's share with it
    except Problem as e:
        return [(lock, str(e))]


def release_boundary(mine: str) -> str:
    """The run's share of the hold given back: the owner file the run's folder names — the lock's path on the
    note's first line, its token on the next — released. None named, or none of this run's there (a late
    cleanup of an old run, its watchdog firing after it ended): nothing to do. '' returned, or why the
    boundary's modes could not all be put back."""
    try:
        lines = Path(mine, GIT_BOUNDARY).read_text(encoding="utf-8").split("\n")
    except OSError:
        return ""
    lock, token = lines[0].strip(), lines[1].strip() if len(lines) > 1 else ""
    if not lock or not token:
        return ""  # nothing named, or a note without an identity: it can name no share of this run's
    failed = release_lock(lock, token)
    return put_back_why(lock, failed) if failed else ""


def hold_boundary(mine: str, wt: str) -> None:
    """Hold the copy's git boundary read-only for the run's length, sharing the hold the runs on this
    repository take: every write permission off every file and folder in the worktree's git folders, so that
    a command in the copy cannot make a commit land in a repository outside it, however it points the copy's
    .git. The first of the runs sharing the hold records the modes in the lock folder (before any is taken
    off) and takes the write permissions; each run, the first and every joiner, adds an owner file of its own
    (its token's name, its pid and start inside), after naming the lock and token in the run's folder, so its
    watchdog can give the share back when the shim is killed outright; release_boundary does it on every
    other end. The last owner left puts the modes back; a restore that cannot finish is reported and its
    record kept. Every transition of an established hold runs under its transition lock (hold_mux), so a join
    and a restore never cross. Problem — the run refused, not queued — when the lock is half set up and its
    maker is alive or cannot be proven dead within MUTEX_WAIT seconds (it is repaired otherwise: what the
    dead run stripped put back, then held again), or when a half set-up lock's record cannot be put back."""
    own, common = git_dirs(wt)
    folders = list(dict.fromkeys(os.path.realpath(x) for x in (own, common) if x))
    if not folders:
        return
    lock = os.path.join(os.path.realpath(common) if common else folders[0], HOLD)
    token = uuid.uuid4().hex  # this run's share of the hold: the owner file it alone may add and remove
    Path(mine, GIT_BOUNDARY).write_text(f"{lock}\n{token}\n", encoding="utf-8")  # the watchdog's, first
    owner = os.path.join(lock, f"owner-{token}")
    ready = os.path.join(lock, "ready")
    deadline = time.time() + MUTEX_WAIT  # one budget for the waits below (a setup, a transition)
    while True:
        try:
            os.mkdir(lock)  # the hold's folder mine to make: this run sets the hold up (below the loop)
            break
        except FileExistsError:
            pass
        except OSError as e:
            raise Problem(f"the repository's git boundary could not be held: {e}")
        if os.path.exists(ready):
            # an established hold: join it (its first run recorded the modes; the last leaver's restore uses
            # that record, and the boundary is already read-only). The restore of a hold whose owners are all
            # gone ends before its folder goes, so a join under the transition lock either lands in the
            # established hold or finds it gone and looks again — it never crosses a restore.
            try:
                with hold_mux(lock):
                    if os.path.exists(ready):  # still the established hold, its restore not begun or done
                        try:
                            Path(owner).write_text(f"{os.getpid()} {my_start()}\n", encoding="utf-8")
                        except OSError as e:
                            raise Problem(f"the repository's git boundary could not be held: {e}")
                        return
            except _HoldGone:
                pass
            continue  # the hold went with another run's transition; look again
        # half set up: its maker is still at it (wait for the ready marker), or died making it (repair)
        own_files = holders(lock)
        fresh = False
        if not own_files:
            try:
                fresh = time.time() - os.path.getmtime(lock) < 5
            except OSError:
                continue  # went meanwhile; look again
        if fresh or any(alive for _, alive in own_files):
            if time.time() > deadline:
                raise Problem(f"another Cursor run holds this repository's git boundary; the run is refused, "
                              f"not queued: try again when it ends")
            time.sleep(0.05)
            continue
        try:  # the instance seen dead: a repair acts on it alone, never on a hold made again at its path
            ino = os.stat(lock).st_ino
        except OSError:
            continue
        try:
            with hold_mux(lock):
                try:
                    if os.stat(lock).st_ino != ino or os.path.exists(ready):
                        continue  # another instance meanwhile, or set up after all: look again
                except OSError:
                    continue
                failed = restore_record(lock)  # what the dead run stripped, put back before held again
                if failed:
                    raise Problem(put_back_why(lock, failed))
                try:  # an old lock's folder may itself be read-only from a crash mid-strip
                    os.chmod(lock, 0o755)
                except OSError:
                    pass
                shutil.rmtree(lock, ignore_errors=True)
        except _HoldGone:
            pass
    # set the hold up (this run made the folder): the owner file, the modes recorded, the write permissions
    # off, then the ready marker joiners wait for. The hold's own folder is not in the record: it is shim
    # metadata — owner files join into it and the transition lock lives in it, for the hold's length — and
    # the teardown removes it rather than putting a mode back.
    try:
        Path(owner).write_text(f"{os.getpid()} {my_start()}\n", encoding="utf-8")
        held = []
        for top in folders:
            for folder, dirs, files in os.walk(top):
                if folder == lock:  # the walk's tops are links-resolved, so the hold's folder compares equal
                    dirs[:] = []
                    continue
                for p in (folder, *(os.path.join(folder, n) for n in files)):
                    try:
                        st = os.lstat(p)
                    except OSError:
                        continue
                    if not stat.S_ISLNK(st.st_mode):  # a link's own mode does not guard its target
                        held.append((p, stat.S_IMODE(st.st_mode)))
        Path(lock, "modes").write_text(json.dumps(held), encoding="utf-8")
        os.chmod(os.path.join(lock, "modes"), 0o444)  # made after the walk, so not in it
        for p, mode in held:
            if mode & 0o222:
                os.chmod(p, mode & ~0o222)
        Path(ready).write_text(f"{token}\n", encoding="utf-8")
    except OSError as e:
        restore_record(lock)  # put back what was stripped (the record may be partial or not there)
        try:
            os.chmod(lock, 0o755)
        except OSError:
            pass
        shutil.rmtree(lock, ignore_errors=True)
        raise Problem(f"the repository's git boundary could not be held: {e}")


def boundary_state(genv: dict, wt: str) -> tuple:
    """The boundary's refs, HEADs and objects (names and sizes): what a commit made in the copy would change in a
    repository outside it. Recorded before the run and compared after it, while the hold still keeps every other
    writer out; a difference fails the run and nothing is brought back (the hold is the same account's modes, so
    a command that nothing else stops can put the write bits back — this is the net then)."""
    folders = boundary_folders(wt)
    refs = git(genv, wt, "for-each-ref", "--format=%(refname) %(objectname)") if folders else b""
    heads, objects = [], []
    for folder in folders:
        try:
            heads.append(Path(folder, "HEAD").read_bytes())
        except OSError:
            heads.append(b"")
        found = []
        for base, _, files in os.walk(os.path.join(folder, "objects")):
            for n in files:
                p = os.path.join(base, n)
                try:
                    found.append((os.path.relpath(p, folder), os.lstat(p).st_size))
                except OSError:
                    pass
        objects.append(sorted(found))
    return refs, heads, objects


# ---- before the run ---------------------------------------------------------------------------------------

def model_name(model: str, effort: str) -> str:
    """Cursor's --model: for grok-4.7, the form hansa's --effort names (max as xhigh, none as high); any other
    model as it is."""
    return f"{GROK}-{EFFORT_FORMS.get(effort, NO_EFFORT_FORM)}" if model == GROK else model


def lane_writes(allow, wt: str, tmp: str):
    """(the lane's write list for Cursor's commands, the entries left out as above what they may never write):
    the --settings sandbox's allowWrite less the worktree, its git folders, the worker's TMPDIR and what is
    inside them (within: as the volume compares paths). An entry above one of them would give Cursor's commands
    the worktree, the main repository or the shim's snapshot, so it is left out too."""
    mine = guarded(wt, tmp)
    kept, above = [], []
    for w in dict.fromkeys(str(x) for x in allow or [] if x):
        if any(within(w, m) for m in mine):
            continue
        (above if any(within(m, w) for m in mine) else kept).append(w)
    return kept, above


def lane_reads(reads, wt: str, tmp: str):
    """(HANSA_CURSOR_READ's folders for this run, those left out): less each that is, is inside or is above the
    worktree, its git folders or the worker's TMPDIR, as lane_writes leaves them out of the write list: one of them
    would open the worktree, the main repository or the run's own folder (snap/, cfg/) to Cursor's commands."""
    mine = guarded(wt, tmp)
    kept, left = [], []
    for r in dict.fromkeys(x for x in reads if x):
        (left if any(within(r, m) or within(m, r) for m in mine) else kept).append(r)
    return kept, left


def worktree_problem(wt: str) -> str:
    """What in the worktree Cursor would obey or open: a .cursor, a .claude holding settings.json or
    settings.local.json, or a .git below the top (a submodule), anywhere; names compared without regard to case,
    as macOS's volumes match them. '' when none."""
    for folder, dirs, files in os.walk(wt):
        top = folder == wt
        for name in dirs + files:
            low, p = name.lower(), os.path.join(folder, name)
            if low == ".git" and not top:
                return (f"the worktree holds {p}, a repository below its top (a submodule), whose git folder "
                        f"Cursor's sandbox would open for writing")
            if low == WORKTREE_DENY:
                return f"the worktree holds {p}, which Cursor would obey"
            if low == ".claude":
                for s in CLAUDE_SETTINGS:
                    if os.path.lexists(os.path.join(p, s)):
                        return (f"the worktree holds {os.path.join(p, s)}, whose hooks Cursor would run outside its "
                                f"sandbox")
        if top:
            dirs[:] = [d for d in dirs if d.lower() != ".git"]
    return ""


def read_problem(reads: list, writes: list, deny_read) -> str:
    """A folder of HANSA_CURSOR_READ or the lane's write list that is, is inside or is above an entry of the
    --settings sandbox's denyRead, as the volume compares paths (within). '' when none."""
    for what, folders in (("HANSA_CURSOR_READ", reads), ("the lane's write list", writes)):
        for f in folders:
            for d in deny_read or []:
                if within(f, str(d)) or within(str(d), f):
                    return (f"{f} ({what}) is, is inside or is above {d}, which the sandbox may not read; Cursor's "
                            f"commands would read it")
    return ""


def above_problem(paths) -> str:
    """An instructions file Cursor would give the model, in a folder above the workspace (`paths`, its
    spellings), as given or with links resolved. '' when none."""
    for p in codex.both(paths):
        for d in Path(p).parents:
            for n in ABOVE_FILES:
                if os.path.lexists(d / n):
                    return (f"{d / n} is in a folder above Cursor's workspace {p}, and Cursor would give it to the "
                            f"model; move it, or the worker's temporary folder")
    return ""


def version_problem(program: str, want: str, env: dict) -> str:
    """'' when `program -v` says `want`: the version whose sandbox was checked."""
    try:
        p = subprocess.run([program, "-v"], stdin=subprocess.DEVNULL, capture_output=True, env=env,
                           timeout=VERSION_TIMEOUT, start_new_session=True)
    except subprocess.TimeoutExpired:
        return f"`{program} -v` did not answer within {VERSION_TIMEOUT} s"
    except OSError as e:
        return f"`{program} -v` could not run: {e}"
    got = p.stdout.decode("utf-8", "replace").strip()
    if p.returncode == 0 and got == want:
        return ""
    return (f"Cursor's version changed: `{program} -v` says {one_line(got, 80) or '(nothing)'} (exit "
            f"{p.returncode}), HANSA_CURSOR_VERSION is {want}. Check Cursor's sandbox again (README, \"The Cursor "
            f"backend\") before changing the setting")


def sandbox_json(tmp: str, writes: list, reads: list, hosts: list) -> dict:
    """$HOME/.cursor/sandbox.json: commands read and write the workspace, write the run's tmp/ and the lane's write
    list, read HANSA_CURSOR_READ, write no /tmp, and reach the lane's domains alone, never this machine."""
    return {"type": "workspace_readwrite", "readBoundary": "workspace", "additionalReadPaths": reads,
            "additionalReadwritePaths": [tmp, *writes], "disableTmpWrite": True,
            "networkPolicy": {"version": 1, "default": "deny", "allow": hosts, "deny": LOCAL_DENY}}


def prompt(system: str, task: str, wt: str, ws: str, tmp: str = "", rtmp: str = "") -> str:
    """hansa's system prompt, then the task, marked apart. Where either names the worktree's path, the copy's;
    where either names the worker's TMPDIR (`tmp`), or the folder in it hansa tells the worker's commands of
    (claude-<uid>), the run's tmp/ (`rtmp`), which Cursor's commands may write. Each as given or with links
    resolved."""
    places = {s: ws for s in codex.both([wt])}
    if tmp and rtmp:
        for t in codex.both([tmp]):
            places.update({os.path.join(t, f"claude-{os.getuid()}"): rtmp, t: rtmp})
    where = re.compile("|".join(re.escape(s) for s in sorted(places, key=len, reverse=True)) + r"(?![\w-])")

    def mapped(text):
        return where.sub(lambda m: places[m.group(0)], text)
    return (f"Your instructions as hansa's worker (they stand where a system prompt would):\n"
            f"<instructions>\n{mapped(system)}\n</instructions>\n\n"
            f"Your task:\n<task>\n{mapped(task)}\n</task>\n")


def cursor_argv(program: str, ws: str, model: str, text: str) -> list:
    """Cursor's command line: its sandbox on, the workspace trusted, nothing run without it (never --force,
    --yolo, --approve-mcps or --sandbox disabled)."""
    return [program, "-p", "--output-format", "stream-json", "--sandbox", "enabled", "--trust", "--workspace", ws,
            "--model", model, text]


# ---- git ----------------------------------------------------------------------------------------------------

def git_env(env: dict) -> dict:
    """The environment of the shim's git commands: none of the caller's GIT_*, no global or system config, no
    terminal prompt."""
    out = {k: v for k, v in env.items() if not k.startswith("GIT_")}
    out.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"})
    return out


def git(genv: dict, cwd: str, *args, data=None, ok=(0,), unread=None) -> bytes:
    """git's stdout; Problem when it exits with a code not in `ok`, or says what `unread` matches on stderr."""
    feed = {"input": data} if data is not None else {"stdin": subprocess.DEVNULL}
    p = subprocess.run(["git", *GIT_OFF, *args], cwd=cwd, env=genv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       **feed)
    said = p.stderr.decode("utf-8", "replace")
    name = next((x for x in args if x[:1] not in ("-", "/")), "")  # the command, past --git-dir and the like
    if p.returncode not in ok:
        raise Problem(f"git {name} failed (exit {p.returncode}): {one_line(said)}")
    if unread is not None and unread.search(said):
        raise Problem(f"git {name} could not read all of the copy: {one_line(said)}")
    return p.stdout


def copy_worktree(wt: str, ws: str) -> None:
    """Everything in the worktree but its .git into ws/, with APFS clones (cp -c), else copied whole."""
    names = [os.path.join(wt, n) for n in sorted(os.listdir(wt)) if n != ".git"]
    if not names:
        return
    for flags in ("-cRp", "-Rp"):
        p = subprocess.run(["cp", flags, "--", *names, ws], stdin=subprocess.DEVNULL, capture_output=True)
        if p.returncode == 0:
            return
        shutil.rmtree(ws, ignore_errors=True)
        os.mkdir(ws)
    raise Problem(f"the worktree could not be copied: {one_line(p.stderr.decode('utf-8', 'replace'))}")


def own_repository(genv: dict, wt: str, ws: str, exclude: bytes) -> None:
    """ws/.git: a shallow repository holding the worktree's HEAD commit alone, HEAD on a branch of the same name
    (detached when the worktree's is), its index the commit's, info/exclude the main repository's. Nothing in it
    points at the main repository (no remote, no FETCH_HEAD)."""
    head = git(genv, wt, "rev-parse", "--verify", "-q", "HEAD^{commit}", ok=(0, 1)).decode().strip()
    ref = git(genv, wt, "symbolic-ref", "-q", "HEAD", ok=(0, 1)).decode().strip()
    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ""
    git(genv, ws, "init", "-q", "--template=", *(("-b", branch) if branch else ()), ws)
    if head:
        git(genv, ws, "fetch", "-q", "--depth=1", "--no-tags", "--no-write-fetch-head", wt, head)
        git(genv, ws, "update-ref", *((ref,) if branch else ("--no-deref", "HEAD")), head)
        git(genv, ws, "read-tree", head)
        git(genv, ws, "update-index", "-q", "--refresh", ok=(0, 1))
    write_info(os.path.join(ws, ".git"), exclude=exclude)


def write_info(gitdir: str, **files: bytes) -> None:
    """info/<name> in `gitdir`, for each name given."""
    os.makedirs(os.path.join(gitdir, "info"), exist_ok=True)
    for name, data in files.items():
        Path(gitdir, "info", name).write_bytes(data)


def snapshot(genv: dict, snap: str, ws: str, first: bool = False) -> str:
    """The tree of every file in the copy that git does not ignore, recorded in the snapshot repository `snap`
    (its own index, the main repository's info/exclude, NO_CONVERSION), with the copy as its work tree. `first`
    (before the run): with the files the copy's commit tracks that its ignore rules match; after the run, what
    the index tracks stays tracked, so they are recorded again. A file or folder git cannot read is a Problem."""
    tree = ("--git-dir", snap, "--work-tree", ws)
    git(genv, ws, *tree, "add", "-A", unread=UNREAD)
    if first:
        tracked = git(genv, ws, "ls-files", "-z", "-c", "-i", "--exclude-standard").split(b"\0")
        kept = [p for p in tracked if p and os.path.lexists(os.path.join(ws, os.fsdecode(p)))]
        if kept:
            git({**genv, "GIT_LITERAL_PATHSPECS": "1"}, ws, *tree, "add", "-f", "--pathspec-from-file=-",
                "--pathspec-file-nul", data=b"\0".join(kept) + b"\0", unread=UNREAD)
    return git(genv, ws, *tree, "write-tree").decode().strip()


def refused_paths(raw: bytes) -> list:
    """From `git diff-tree -r -z --no-renames`: the paths a patch may not touch (PATCH_DENY, without regard to case),
    and its submodule entries."""
    parts, out = raw.split(b"\0"), []
    for meta, path in zip(parts[0::2], parts[1::2]):
        p = os.fsdecode(path)
        if SUBMODULE in meta.decode("ascii", "replace").lstrip(":").split()[:2]:
            out.append(f"{p} (a submodule entry)")
        elif any(c.lower() in PATCH_DENY for c in p.split("/")):
            out.append(p)
    return out


def bring_back(genv: dict, snap: str, before: str, after: str, wt: str, kept: Path) -> None:
    """The binary diff between the two snapshots, kept in `kept`, applied to the worktree (hansa commits it).
    Problem, and nothing applied, when it touches what may not come back or does not apply."""
    tree = ("--git-dir", snap)
    patch = git(genv, wt, *tree, "diff-tree", "-p", "--binary", "--no-renames", "--no-color", "--no-ext-diff",
                "--no-textconv", before, after)
    kept.write_bytes(patch)
    bad = refused_paths(git(genv, wt, *tree, "diff-tree", "-r", "-z", "--no-renames", before, after))
    if bad:
        raise Problem(f"the run changed what may not come back (the project settings, hooks, rules or skills of "
                      f"Cursor, Claude Code or Codex, or a submodule): {', '.join(bad)}; nothing was brought back "
                      f"(the patch is kept in {kept})")
    if patch:
        try:
            git(genv, wt, *tree, "--work-tree", wt, "apply", "--whitespace=nowarn", str(kept))
        except Problem as e:
            raise Problem(f"the run's changes did not apply to the worktree, and nothing was brought back (the "
                          f"patch is kept in {kept}): {e}")


# ---- processes ------------------------------------------------------------------------------------------------

def proc_args(pid: int) -> list:
    """[the program's path, *argv] of process `pid`; [] when they cannot be read (another user's, gone). On macOS
    sysctl KERN_PROCARGS2 (argc, the path, NUL padding, argv), as the Codex module reads the process table;
    /proc on Linux."""
    if os.path.isdir("/proc"):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")[:-1]
        except OSError:
            return []
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            exe = ""
        return [exe, *map(os.fsdecode, argv)]
    try:
        data = codex._sysctl(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    except (OSError, AttributeError, ImportError, TypeError, ValueError):
        return []
    if len(data) < 5:
        return []
    argc = struct.unpack_from("@i", data, 0)[0]
    exe, _, rest = data[4:].partition(b"\0")
    return [os.fsdecode(exe), *map(os.fsdecode, rest.lstrip(b"\0").split(b"\0")[:max(argc, 0)])]


_LIBC = []


def proc_cwd(pid: int) -> str:
    """The working folder of process `pid`; '' when it cannot be read. On macOS proc_pidinfo's
    PROC_PIDVNODEPATHINFO (two vnode_info_path, 152 bytes of vnode_info then MAXPATHLEN of path: the working
    folder's first); /proc on Linux."""
    if os.path.isdir("/proc"):
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return ""
    try:
        import ctypes
        import ctypes.util
        if not _LIBC:
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            libc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
            _LIBC.append(libc)
        buf = ctypes.create_string_buffer(2352)
        if _LIBC[0].proc_pidinfo(pid, 9, 0, buf, 2352) != 2352:  # PROC_PIDVNODEPATHINFO
            return ""
    except (OSError, AttributeError, ImportError, TypeError, ValueError):
        return ""
    return os.fsdecode(buf.raw[152:152 + 1024].split(b"\0")[0])


def policy_of(argv: list) -> str:
    """The file a cursorsandbox's --policy names; '' when none."""
    for i, x in enumerate(argv):
        if x == "--policy" and i + 1 < len(argv):
            return argv[i + 1]
        if x.startswith("--policy="):
            return x[len("--policy="):]
    return ""


def leftovers(table: dict, program: str, home: str, ws: str) -> dict:
    """{pid: start} of what Cursor leaves running for this run, in `table` (the process table): every
    cursorsandbox whose --policy is under the run's HOME, and every worker-server of the program (its node or
    index.js in the program's folder) whose working folder is the run's ws/. Another HOME's sandbox and another
    folder's worker-server are not this run's. The name is looked for in the program's path, argv[0] and argv[1]
    (a script, after its interpreter)."""
    folders = [os.path.dirname(x) for x in codex.both([program])] if program else []
    out = {}
    for pid, (_, start) in table.items():
        if pid in (os.getpid(), os.getppid()):
            continue
        args = proc_args(pid)
        named, argv = [x for x in args[:3] if x], args[1:]
        if any(os.path.basename(x) == SANDBOX_PROGRAM for x in named):
            if within(policy_of(argv), home):
                out[pid] = start
        elif WORKER_SERVER in argv[1:] and any(within(x, f) for x in named if os.path.isabs(x) for f in folders):
            if within(proc_cwd(pid), ws):
                out[pid] = start
    return out


def pgid(pid: int):
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def stop(found: dict, find=None, grace: float = codex.STOP_GRACE, groups=()) -> None:
    """Stop the processes in `found` ({pid: start}) and what `find` finds in the process table (the run's
    leftovers), with every process below one of them, every process in a group one of them leads and every
    process in `groups` (whose leader may be gone): SIGTERM, up to `grace` seconds, then SIGKILL, after looking
    again (codex.stop_tree's way, for several groups at once). A process is signalled only while it runs with the
    start recorded; never this process or its parent, and no group is taken whole that one of them is in."""
    spared = {os.getpid(), os.getppid()}
    spared_groups = {pgid(p) for p in spared} - {None}
    seen, groups = dict(found), set(groups) - spared_groups

    def look(finding: bool) -> dict:
        table = codex.process_table()
        if finding and find is not None:
            seen.update(find(table))
        group_of = {}

        def gid(p):
            if p not in group_of:
                group_of[p] = pgid(p)
            return group_of[p]
        while True:
            groups.update(p for p, s in seen.items()
                          if table.get(p, (0, None))[1] == s and gid(p) == p and p not in spared_groups)
            more = {p: s for p, (pp, s) in table.items() if p not in seen and p not in spared and (
                (pp in seen and table.get(pp, (0, None))[1] == seen[pp]) or (groups and gid(p) in groups))}
            if not more:
                return table
            seen.update(more)

    def running(table):
        return [p for p, s in seen.items() if p not in spared and (not table or table.get(p, (0, None))[1] == s)]

    def send(sig, table):
        for p in running(table):
            try:
                os.kill(p, sig)
            except OSError:
                pass
    send(signal.SIGTERM, look(True))
    end = time.time() + grace
    while time.time() < end:
        table = look(False)
        if table and not running(table):
            break
        time.sleep(0.1)
    send(signal.SIGKILL, look(True))


def stop_run(grace: float = codex.STOP_GRACE) -> None:
    """Stop Cursor and everything below it (and whatever else the shim started that still runs, but the watchdog),
    and the run's leftovers. While Cursor runs, its group (it leads one) gets SIGTERM at once, before the process
    table is looked through, so that it takes no further step meanwhile; the group is then stopped with the rest.
    (Cursor is not yet waited for, so its group's number is not another's.)"""
    proc, group = Live.proc, ()
    if proc is not None and proc.returncode is None:
        group = (proc.pid,)
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
    table = codex.process_table()
    dog = Live.dog.pid if Live.dog is not None else None
    kids = {p: s for p, (pp, s) in table.items() if pp == os.getpid() and p != dog}
    stop({**Live.seen, **kids}, Live.find, grace, group)


def give_back(folder: str) -> None:
    """Read, write and search permission back to the owner on every folder and file in `folder`, top down, never
    through a link (the run's commands may have taken them: chmod a-w, chmod 000), so that it can be removed."""
    nofollow = {"follow_symlinks": False} if os.chmod in os.supports_follow_symlinks else {}
    todo = [folder]
    while todo:
        p = todo.pop()
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            continue
        want = 0o700 if stat.S_ISDIR(st.st_mode) else 0o600
        if st.st_mode & want != want:
            try:
                os.chmod(p, stat.S_IMODE(st.st_mode) | want, **nofollow)
            except (OSError, NotImplementedError):
                pass
        if stat.S_ISDIR(st.st_mode):
            try:
                todo += [os.path.join(p, n) for n in os.listdir(p)]
            except OSError:
                pass


def remove(folder: str) -> None:
    """`folder` removed, never following a link (the keychain stays); where the run's commands took permissions
    away, they are given back first."""
    shutil.rmtree(folder, ignore_errors=True)
    if os.path.lexists(folder):
        give_back(folder)
        shutil.rmtree(folder, ignore_errors=True)


def drop() -> None:
    if Live.mine:
        remove(Live.mine)
        Live.mine = ""


def end() -> str:
    """The run is over, however it ended: the copy's git boundary given back, the run's folder removed, the
    watchdog ended. '' returned, or why the boundary's modes could not all be put back (the caller says it:
    a run whose boundary stays read-only is not a success)."""
    why = release_boundary(Live.mine) if Live.mine else ""
    drop()
    if Live.dog is not None:
        Live.dog.kill()
        Live.dog.wait()
        Live.dog = None
    return why


def on_signal(sig, _frame):
    """hansa's ceiling, or a cancel: Cursor, what runs below it and what it leaves running stop, the run's folder
    goes, and no answer is written: the exit is a killed process's."""
    stop_run()
    end()
    os._exit(128 + sig)


def watchdog(shim: int, program: str, home: str, ws: str, mine: str) -> None:
    """Watches the shim from a session of its own, keeping what runs below it, looked up every half second, and
    Cursor itself, whose pid and start the shim writes on this one's stdin (`<pid> <start>` lines) as soon as it
    starts. The shim's end closes that pipe: when the shim dies without cleaning up (SIGKILL), this stops what it
    kept and the run's leftovers, puts the copy's git boundary back (the modes its lock folder recorded), and
    removes the run's folder. (The shim ends this one first when it ends well.)"""
    seen, given = {}, b""
    while True:
        codex.descendants(shim, seen)
        if not select.select([0], [], [], 0.5)[0]:
            if os.getppid() != shim:  # gone, though its pipe is not closed
                break
            continue
        data = os.read(0, 4096)
        if not data:
            break
        *lines, given = (given + data).split(b"\n")
        for line in lines:
            pid, _, start = line.decode("ascii", "replace").partition(" ")
            if pid.isdigit() and start:
                seen[int(pid)] = start
    for p in (shim, os.getpid()):
        seen.pop(p, None)
    stop(seen, lambda table: leftovers(table, program, home, ws))
    release_boundary(mine)  # the copy's git boundary, back as it was
    remove(mine)


def tell_watchdog(pid: int) -> None:
    """Cursor's pid and start to the watchdog, which then stops it even if the shim dies before a look."""
    start = codex.process_table().get(pid, (0, ""))[1]
    if Live.dog is not None and start:
        try:
            Live.dog.stdin.write(f"{pid} {start}\n".encode())
            Live.dog.stdin.flush()
        except OSError:
            pass


# ---- the run ------------------------------------------------------------------------------------------------

def usage_of(u: dict) -> dict:
    """The result line's usage as Claude Code's modelUsage."""
    def n(k):
        try:
            return max(int(u.get(k) or 0), 0)
        except (TypeError, ValueError):
            return 0
    return {"inputTokens": n("inputTokens"), "outputTokens": n("outputTokens"),
            "cacheReadInputTokens": n("cacheReadTokens"), "cacheCreationInputTokens": n("cacheWriteTokens")}


class Stream:
    """What the shim reads of Cursor's stream-json, a line at a time: the result line, the tool calls started,
    Cursor's session id, the last assistant text, and the last lines that are not JSON."""

    def __init__(self):
        self.result, self.tools, self.csid, self.said, self.stray = None, 0, "", "", []

    def take(self, line: bytes) -> None:
        try:
            ev = json.loads(line)
        except ValueError:
            if line.strip():
                self.stray = [*self.stray[-9:], line.decode("utf-8", "replace").strip()]
            return
        if not isinstance(ev, dict):
            return
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init" and ev.get("session_id"):
            self.csid = str(ev["session_id"])
        elif t == "tool_call" and ev.get("subtype") == "started":
            self.tools += 1
        elif t == "assistant" and isinstance(ev.get("message"), dict):
            content = ev["message"].get("content")
            text = "".join(c.get("text") or "" for c in content if isinstance(c, dict) and c.get("type") == "text") \
                if isinstance(content, list) else str(content or "")
            self.said = text or self.said
        elif t == "result":
            self.result = ev
            self.csid = str(ev.get("session_id") or self.csid)


def follow(proc, path: Path, max_turns: int):
    """(the Stream, whether its tool calls passed max_turns) of Cursor's stdout, the log file itself, read as it
    is written, as the Codex run reads its log: whole lines, and a last one without its newline once Cursor has
    ended. Returns when Cursor ends, or at the call past max_turns. What runs below Cursor is looked up every 2 s
    (Live.seen), for a stop."""
    s, partial, ended, looked = Stream(), b"", False, 0.0
    with open(path, "rb", buffering=0) as f:
        while not ended:
            if time.time() - looked > 2:
                looked = time.time()
                Live.seen = codex.descendants(proc.pid, dict(Live.seen))
            ended = proc.poll() is not None  # before the read: what Cursor wrote is then all in the file
            chunk = f.read()
            if not chunk and not ended:
                time.sleep(codex.POLL)
                continue
            lines = (partial + chunk).split(b"\n")
            partial = lines.pop()
            if ended and partial:
                lines.append(partial)
            for line in lines:
                s.take(line)
                if s.tools > max_turns:
                    return s, True
    return s, False


def classify(text: str) -> dict:
    """Cursor's failure as hansa reads Claude Code's: its own words for a missing login ("Authentication
    required", "Not logged in") as a login problem; the rest as the Codex module classes Codex's (a usage limit, a
    dropped connection, a server error...)."""
    whole = " ".join(str(text).split())
    if LOGIN_TEXT.search(whole):
        return {"result": f"Not logged in · Please run /login (Cursor: {one_line(whole)})"}
    return codex.classify(whole, "Cursor")


def run(a: dict, env: dict, conf: dict, check: bool = False) -> int:
    """A worker run on the Cursor provider, or (`check`) hansa's provider check on it: Cursor once, answered as
    Claude Code answers. A worker run works in a copy of the worktree, whose changes come back as a patch, however
    the run ends. A check works in a new empty folder, with no lane write list, reads or hosts, and is asked
    CHECK_PROMPT. The run's folder, in the worker's TMPDIR, is removed however the run ends, and what Cursor left
    running is stopped first."""
    sid = str(uuid.uuid4()) if check else a["--session-id"]
    name = f"check-{sid}" if check else sid  # the log files'
    model = model_name(conf.get("HANSA_CURSOR_MODEL") or DEFAULT_MODEL, a.get("--effort") or "")
    program, version = conf.get("HANSA_CURSOR_BIN") or "", conf.get("HANSA_CURSOR_VERSION") or ""
    logs = Path(conf.get("HANSA_CURSOR_LOGS") or codex.REPO / "logs")
    try:
        max_turns = int(a.get("--max-turns") or codex.DEFAULT_MAX_TURNS)
    except ValueError:
        max_turns = codex.DEFAULT_MAX_TURNS
    task = sys.stdin.buffer.read().decode("utf-8", "replace")
    tmp, genv = env.get("TMPDIR") or tempfile.gettempdir(), git_env(env)
    wt = "" if check else os.getcwd()

    def failed(why):
        back = end()
        why = f"{why} · {back}" if back else why
        return emit(answer("error_during_execution", f"hansa-cursor: {why}", sid, model=model))
    for what, v in (("HANSA_CURSOR_BIN", program), ("HANSA_CURSOR_VERSION", version)):
        if not v:
            return failed(f"{what} is not set")
    if model.startswith("-"):
        return failed(f"HANSA_CURSOR_MODEL ({model}) is not a model name")
    writes, reads, hosts = [], [], []  # a check's: hansa gives it no sandbox, so none of a lane's
    if not check:
        try:
            box = json.loads(a.get("--settings") or "{}")
        except ValueError:
            box = {}
        sb = box.get("sandbox") if isinstance(box, dict) else None
        if not (isinstance(sb, dict) and sb.get("enabled")):
            return failed("no sandbox in the --settings hansa gave; Cursor runs only in one")
        fs, net = sb.get("filesystem") or {}, sb.get("network") or {}
        writes, above = lane_writes(fs.get("allowWrite"), wt, tmp)
        given_reads = [os.path.expanduser(x) for x in (conf.get("HANSA_CURSOR_READ") or "").split(":") if x]
        why = worktree_problem(wt) or read_problem(codex.both(given_reads), writes, fs.get("denyRead"))
        if why:
            return failed(why)
        reads, unread = lane_reads(given_reads, wt, tmp)
        reads, hosts = codex.both(reads), [str(h) for h in net.get("allowedDomains") or []]
        if above:
            sys.stderr.write(f"hansa-cursor: left out of the lane's write list for this Cursor run, as above the "
                             f"worktree, its git folders or the worker's TMPDIR: {', '.join(above)}\n")
        if unread:
            sys.stderr.write(f"hansa-cursor: left out of HANSA_CURSOR_READ for this Cursor run, as the worktree, its "
                             f"git folders or the worker's TMPDIR, or inside or above one: {', '.join(unread)}\n")
    for s in codex.STOP_SIGNALS:
        signal.signal(s, on_signal)
    try:
        given = tempfile.mkdtemp(prefix="cursor-check-" if check else "cursor-", dir=tmp)
    except OSError as e:
        return failed(f"the run's folder could not be made in {tmp}: {e}")
    Live.mine = mine = os.path.realpath(given)
    home, cfg, rtmp, ws, snap = (os.path.join(mine, n) for n in ("home", "cfg", "tmp", "ws", "snap"))
    Live.find = lambda table: leftovers(table, program, home, ws)
    if not env.get(codex.NO_WATCHDOG):
        try:  # in a session of its own, so that hansa's kill of this one's group leaves it to clean up
            Live.dog = subprocess.Popen([sys.executable, "-I", os.path.abspath(codex.__file__), codex.CURSOR_WATCHDOG,
                                         str(os.getpid()), program, home, ws, mine], stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass
    why = above_problem([os.path.join(given, "ws"), ws])
    if why:
        return failed(why)
    sandbox = sandbox_json(rtmp, writes, reads, hosts)
    try:
        for d in (home, cfg, rtmp, ws, os.path.join(home, "Library"), os.path.join(home, ".cursor")):
            os.mkdir(d)
        # the login, in the account's keychain: from the password database, not $HOME
        os.symlink(os.path.join(pwd.getpwuid(os.getuid()).pw_dir, "Library", "Keychains"),
                   os.path.join(home, "Library", "Keychains"))
        Path(home, ".cursor", "sandbox.json").write_text(json.dumps(sandbox, indent=1), encoding="utf-8")
        Path(cfg, "cli-config.json").write_text(json.dumps(CLI_CONFIG, indent=1), encoding="utf-8")
        logs.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(os.path.join(home, ".cursor", "sandbox.json"), logs / f"{name}.sandbox.json")
        shutil.copyfile(os.path.join(cfg, "cli-config.json"), logs / f"{name}.cli-config.json")
    except (OSError, KeyError) as e:
        return failed(f"the run's folder could not be made: {e}")
    zsh_prefix = os.path.join(rtmp, "zsh")  # zsh's here-documents: /tmp/zsh by default, which sandbox.json denies
    cenv = {k: v for k, v in env.items() if k not in codex.DROP_VARS}
    cenv.update({"HOME": home, "CURSOR_CONFIG_DIR": cfg, "TMPDIR": rtmp, codex.ZSH_PREFIX: zsh_prefix})
    why = version_problem(program, version, cenv)
    if why:
        return failed(why)
    if check:
        text, lane_env = CHECK_PROMPT, ""
    else:
        _, common = git_dirs(wt)
        try:
            exclude = Path(common, "info", "exclude").read_bytes() if common else b""
        except OSError:
            exclude = b""
        try:
            copy_worktree(wt, ws)
            own_repository(genv, wt, ws, exclude)
            git(genv, mine, "init", "-q", "--bare", "--template=", snap)
            write_info(snap, exclude=exclude, attributes=NO_CONVERSION)
            before = snapshot(genv, snap, ws, first=True)
        except (Problem, OSError) as e:
            return failed(f"the copy of the worktree could not be made: {e}")
        try:
            state0 = boundary_state(genv, wt)
            hold_boundary(mine, wt)  # the copy's git boundary read-only for the run's length
        except (Problem, OSError) as e:
            return failed(str(e))
        text = prompt(a.get("--append-system-prompt") or "", task, wt, ws, tmp, rtmp)
        lane_env = env.get("CLAUDE_ENV_FILE") or ""
    command = ["/bin/sh", "-c", INNER, "hansa-cursor", lane_env, home, cfg, rtmp, zsh_prefix,
               *cursor_argv(program, ws, model, text)]
    jsonl, err = logs / f"{name}.jsonl", logs / f"{name}.err"
    with open(jsonl, "wb") as out, open(err, "wb") as errs:
        try:
            Live.proc = proc = subprocess.Popen(command, cwd=ws, env=cenv, stdin=subprocess.DEVNULL, stdout=out,
                                                stderr=errs, start_new_session=True)
        except OSError as e:
            return failed(f"Cursor could not start: {e}")
    tell_watchdog(proc.pid)
    sys.stderr.write(f"hansa-cursor: Cursor's output is kept in {logs}/{name}.jsonl and .err\n")
    stream, over = follow(proc, jsonl, max_turns)
    if over:  # the turn ceiling: stopped as for a signal
        stop_run()
    code = proc.wait()
    Live.proc = None
    stop_run()  # what Cursor left running, before its changes are read
    problem = ""
    if not check:
        try:  # the boundary compared while the hold still keeps every other writer out
            untouched = boundary_state(genv, wt) == state0
        except (Problem, OSError):
            untouched = False
        if not untouched:
            problem = ("the run reached a repository outside the copy (its refs or its objects changed); nothing "
                       "was brought back")
        else:
            try:
                after = snapshot(genv, snap, ws)
            except (Problem, OSError) as e:
                problem = f"the run's changes could not be read, and nothing was brought back: {e}"
            else:
                try:
                    bring_back(genv, snap, before, after, wt, logs / f"{name}.patch")
                except (Problem, OSError) as e:
                    problem = str(e)
    put_back = end()
    result = stream.result
    extra = {"cursor_session_id": stream.csid} if stream.csid else {}
    usage = usage_of(result["usage"]) if result and isinstance(result.get("usage"), dict) else None
    ours = f"hansa-cursor: {problem}" if problem else ""
    if put_back:  # the boundary still read-only is a failure, however the run itself went
        ours = f"{ours} · hansa-cursor: {put_back}" if ours else f"hansa-cursor: {put_back}"
    if over:
        return emit(answer("error_max_turns", " · ".join(x for x in (stream.said, ours) if x), sid, stream.tools,
                           model, usage, **extra))
    if result and result.get("subtype") == "success" and not result.get("is_error") and code == 0:
        return emit(answer("error_during_execution", ours, sid, stream.tools, model, usage, **extra) if ours else
                    answer("success", str(result.get("result") or ""), sid, stream.tools, model, usage, **extra))
    try:
        said = err.read_text(encoding="utf-8", errors="replace")[-4000:].strip()
    except OSError:
        said = ""
    said = " · ".join(x for x in (said, str((result or {}).get("result") or "").strip(), *stream.stray) if x)
    fields = classify(f"{said} (exit {code})") if said else {"result": f"Cursor ended (exit {code}) without a result"}
    text = " · ".join(x for x in (fields.pop("result"), ours) if x)
    return emit(answer("error_during_execution", text, sid, stream.tools, model, usage, **fields, **extra))
