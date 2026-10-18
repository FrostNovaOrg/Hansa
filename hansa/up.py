"""Remember how a desk's server and coordinator's session started, and bring them back up after they have stopped."""
from __future__ import annotations

import fcntl
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import SESSION, SESSION_PREFIXES, account, notify, without_session
from .desk import Desk, now, program, read_json, write_json
from .run import alive, proc_args, proc_cwd, proc_start, server_state

DEFAULTS = {"host": "127.0.0.1", "port": 8765, "every": 3.0}
START_ENV = ("PATH", "LANG", "HANSA_CLAUDE", "HANSA_TMUX", "CLAUDE_CONFIG_DIR", "TMPDIR",
             "XDG_RUNTIME_DIR", "XDG_STATE_HOME", "TMUX_TMPDIR")
LOG_LIMIT = 20 * 1024 * 1024
START_WAIT = 8.0
COORDINATOR_START = "coordinator-start.json"
COORDINATOR_WAIT = 3.0  # seconds a started coordinator's pane must last to count as started
LAUNCHER = "notify-launch"  # hansa's hidden command that starts a session hansa notify on recorded (cli.py)
# claude's words that pick a conversation, or give a session's inbox, and whether each takes a value (None: one
# that does not start with -, if any). A start again drops them and continues the folder's latest conversation
PICKS = {"-c": False, "--continue": False, "--fork-session": False, "-r": None, "--resume": None,
         "--session-id": True, "--messaging-socket-path": True}
PRINTS = ("-p", "--print")  # claude's words for a run that is not an interactive session
# With no record on a desk that has tasks or followers, its coordinator ran before hansa recorded starts: maybe a
# session in another folder, so a fresh hansa coordinator would be a second one, in the wrong folder
NOT_YET = ("its start is not recorded yet: this desk had a coordinator before hansa recorded its start, maybe a "
           "session in another folder")
TO_RECORD = ("Start it as before and run hansa notify on in it, or start it with hansa coordinator; from then on "
             "hansa up starts it again")


def hansa_command() -> str:
    # Keep installation links, as bin/hansa does, so switching one changes future starts.
    return str(Path(__file__).absolute().parent.parent / "bin" / "hansa")


def desks_file() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "hansa" / "desks.json"


def start_env(desk: Desk, env) -> dict:
    """Only launch settings, never session identity or a desk key (even under an allowed name)."""
    keys = list(desk.keys().values())
    return {k: v for k, v in without_session(env).items()
            if (k in START_ENV or k.startswith("LC_")) and isinstance(v, str)
            and not any(key in v for key in keys)}


def tmux_server(socket: str) -> list:
    """tmux recreates its own socket directory after reboot; custom sockets keep their full path."""
    path = Path(socket)
    roots = [Path("/tmp")]
    if os.environ.get("TMUX_TMPDIR"):
        roots.append(Path(os.environ["TMUX_TMPDIR"]))
    if any(path.parent.resolve() == (root / f"tmux-{os.getuid()}").resolve() for root in roots):
        return ["-L", path.name]
    return ["-S", socket]


def record_tmux() -> dict:
    if not (os.environ.get("TMUX") and os.environ.get("TMUX_PANE")):
        return {}
    tmux = program("tmux")
    if not tmux:
        return {}
    socket = os.environ["TMUX"].rsplit(",", 2)[0]
    try:
        r = subprocess.run([tmux, "-S", socket, "display-message", "-p", "-t", os.environ["TMUX_PANE"],
                            "#{socket_path}|#{session_name}"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {}
    if r.returncode or "|" not in r.stdout:
        return {}
    socket, session = r.stdout.strip().split("|", 1)
    return {"server": tmux_server(socket), "session": session}


def record_start(desk: Desk, options) -> None:
    """Called after serve removes session identity, while it holds serve.lock."""
    rec = {"hansa": hansa_command(), "options": {k: getattr(options, k) for k in DEFAULTS},
           "env": start_env(desk, os.environ), "tmux": record_tmux(), "at": now()}
    write_json(desk.state_dir / "serve-start.json", rec)
    listed = desks_file()
    try:
        listed.parent.mkdir(parents=True, exist_ok=True)
        # Different desks may start together. Lock the stable inode, replace just the JSON.
        with open(listed.with_suffix(".lock"), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            entries = read_json(listed, [])
            entries = [x for x in entries if isinstance(x, dict) and x.get("desk") != str(desk.root)] \
                if isinstance(entries, list) else []
            entries.append({"desk": str(desk.root), "hansa": rec["hansa"], "at": rec["at"]})
            write_json(listed, entries)
    except OSError as e:
        print(desk.redact(f"hansa serve: could not update account's desk list: {e}"), file=sys.stderr, flush=True)


def serve_command(desk: Desk, rec: dict) -> list:
    opts = {**DEFAULTS, **rec.get("options", {})}
    return [rec.get("hansa") or hansa_command(), "--desk", str(desk.root), "serve",
            *[str(x) for k in DEFAULTS for x in ("--" + k, opts[k])]]


def clean_env(desk: Desk, recorded: dict) -> dict:
    pw = pwd.getpwuid(os.getuid())
    env = {"HOME": os.environ.get("HOME", pw.pw_dir), "USER": pw.pw_name,
           "LOGNAME": pw.pw_name, "SHELL": os.environ.get("SHELL", pw.pw_shell),
           "TERM": os.environ.get("TERM", "dumb")}
    env.update(start_env(desk, recorded))
    return env


def rotate_log(desk: Desk) -> Path:
    log = desk.state_dir / "serve.log"
    if log.exists() and log.stat().st_size > LOG_LIMIT:
        log.replace(log.with_name("serve.log.1"))
    return log


def log_tail(log: Path) -> str:
    try:
        with open(log, "rb") as f:
            f.seek(max(0, f.seek(0, os.SEEK_END) - 8192))
            return "\n".join(f.read().decode("utf-8", errors="replace").splitlines()[-12:]) or "(no server output)"
    except OSError:
        return "(no server output)"


def desk_server(desk: Desk) -> list:
    """The tmux server hansa up uses for a desk when none is recorded."""
    return ["-L", "hansa-" + (re.sub(r"[^A-Za-z0-9_-]", "-", desk.root.name) or "desk")]


def pane_command(env: dict, cmd: list, log: Path = None) -> str:
    """What a pane runs: the command in this environment alone, its output also appended to log when one is given."""
    # An existing tmux server has its own environment. Clear that too, keeping only tmux's pane identity.
    pane_env = shlex.join(["/usr/bin/env", "-i", *[f"{k}={v}" for k, v in env.items()]])
    pane_env += ' TMUX="$TMUX" TMUX_PANE="$TMUX_PANE" TERM="$TERM"'
    pane_env += ' TERM_PROGRAM="${TERM_PROGRAM-}" COLORTERM="${COLORTERM-}"'
    if log is None:
        return "exec " + pane_env + " " + shlex.join(cmd)
    script = shlex.join(cmd) + " 2>&1 | /usr/bin/tee -a " + shlex.quote(str(log))
    return "exec " + pane_env + " /bin/sh -c " + shlex.quote(script)


def new_pane(prefix: list, session: str, window: str, folder: str, pane: str, env: dict):
    """A detached pane running `pane` in folder: a window in the session when it exists, else the session. What
    tmux said: the pane's id on its output. tmux exits 0 without one when it cannot start its server."""
    exists = subprocess.run([*prefix, "has-session", "-t", "=" + session], env=env, cwd=folder,
                            capture_output=True, text=True, timeout=5)
    create = ["new-window", "-d", "-t", "=" + session, "-n", window] if exists.returncode == 0 else \
        ["new-session", "-d", "-s", session]
    return subprocess.run([*prefix, *create, "-P", "-F", "#{pane_id}", "-c", folder, "/bin/sh", "-c", pane], env=env,
                          cwd=folder, capture_output=True, text=True, timeout=10, check=True)


def launch_tmux(desk: Desk, rec: dict, cmd: list, env: dict, tmux: str, log: Path) -> str:
    saved = rec.get("tmux") or {}
    server = saved.get("server") or desk_server(desk)
    session = saved.get("session") or "serve"
    prefix = [tmux, *server]
    new_pane(prefix, session, "serve", str(desk.root), pane_command(env, cmd, log), env)
    attach = [*prefix, "attach-session", "-t", "=" + session]
    if env.get("TMUX_TMPDIR") and server[0] == "-L":
        attach = ["env", "TMUX_TMPDIR=" + env["TMUX_TMPDIR"], *attach]
    return shlex.join(attach)


def server_up(desk: Desk) -> bool:
    st = server_state(desk)
    if st["state"] != "gone":
        print(f"server already running (pid {st['pid']})")
        return True
    rec = read_json(desk.state_dir / "serve-start.json", {}) or {}
    if not rec:
        print("No recorded server start; using the defaults and this shell's launch settings.")
    cmd = serve_command(desk, rec)
    env = clean_env(desk, rec.get("env", {}) if rec else os.environ)
    desk.state_dir.mkdir(exist_ok=True)
    log = rotate_log(desk)
    watch, child = "", None
    try:
        tmux = program("tmux", env=env)
        if tmux:
            watch = launch_tmux(desk, rec, cmd, env, tmux, log)
        else:
            print("tmux gives a pane to watch the server in; starting in the background without it.")
            with open(log, "ab") as output:
                child = subprocess.Popen(cmd, cwd=desk.root, env=env, stdin=subprocess.DEVNULL,
                                         stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        end = time.monotonic() + START_WAIT
        while True:
            st = server_state(desk)
            # The pulse follows binding the page and recording its port. A held lock alone is too early
            # to report success: the page may fail to bind and the server immediately exit.
            if st["state"] != "gone" and st.get("answers"):
                port = (desk.state_dir / "web-port").read_text().strip()
                print(f"server started (pid {st['pid']}), page on port {port}")
                print("watch: " + (watch or shlex.join(["tail", "-f", str(log)])))
                return True
            if (child is not None and child.poll() is not None) or time.monotonic() >= end:
                break
            time.sleep(0.1)
    except (OSError, subprocess.SubprocessError) as e:
        detail = e.stderr if isinstance(e, subprocess.CalledProcessError) else str(e)
        print(desk.redact(f"server failed: {detail}"))
    else:
        print("server failed to start")
    print(desk.redact(log_tail(log)))
    return False


def unpicked(words: list) -> list:
    """claude's words without those that pick a conversation or give an inbox (PICKS), with their values. Words
    after `--` are claude's own text, kept as they are."""
    out, i = [], 0
    while i < len(words):
        x = words[i]
        if x == "--":
            return out + words[i:]
        name, eq, _ = x.partition("=") if x.startswith("--") else (x, "", "")
        if name not in PICKS:
            out.append(x)
        elif not eq and PICKS[name] is not False and i + 1 < len(words) and (
                PICKS[name] or not words[i + 1].startswith("-")):
            i += 1  # its value
        i += 1
    return out


def resumed(words: list) -> list:
    """The words to start claude again with: --continue in place of the words that picked a conversation."""
    words = unpicked(words)
    at = words.index("--") if "--" in words else len(words)
    return words[:at] + ["--continue"] + words[at:]


def prints(words: list) -> bool:
    """Whether these words start claude with --print: a run that ends, not an interactive session."""
    return any(x in PRINTS for x in words[:words.index("--") if "--" in words else len(words)])


def unsafe(desk: Desk, words: list) -> str:
    """Why these words are not kept: one holds a desk's key, or the session's identity or inbox; '' when none does."""
    if any(k in w for w in words for k in desk.keys().values()):
        return "a word of its command holds one of the desk's keys"
    ids = [v for k, v in {**SESSION, **os.environ}.items() if k.startswith(SESSION_PREFIXES) and len(v) >= 16]
    if any(v in w for w in words for v in ids):
        return "a word of its command holds the session's identity"
    return ""


def read_coordinator(desk: Desk) -> dict:
    rec = read_json(desk.state_dir / COORDINATOR_START, {})
    return rec if isinstance(rec, dict) else {}


def _keep(desk: Desk, rec: dict, why: str = "", unread: bool = False) -> str:
    """Write the coordinator's start record ('' then), or, when why says it cannot be kept, a record of only that
    and the session, so hansa up starts no older one. A session that could be read before keeps its record when
    it cannot be read now. Returns why it was not recorded."""
    old = read_coordinator(desk)
    if why and unread and old.get("kind") == "notify" and notify.same(old.get("session"), rec["session"]):
        return why
    rec = {"kind": "unrecorded", "why": why, "session": rec["session"]} if why else rec
    try:
        desk.state_dir.mkdir(exist_ok=True)
        write_json(desk.state_dir / COORDINATOR_START, {**rec, "at": now()})
    except OSError as e:
        return why or f"{COORDINATOR_START} could not be written: {e}"
    return why


def record_coordinator(desk: Desk, clear: str, words: list) -> str:
    """How hansa coordinator started this session (this process, which becomes it), for hansa up: '' when
    recorded, else why not. Its own option, the words it gave claude, the desk, tmux and the launch settings."""
    rec = {"kind": "coordinator", "hansa": hansa_command(), "clear_daily": clear, "words": unpicked(words),
           "folder": str(desk.root), "tmux": record_tmux(), "env": start_env(desk, os.environ),
           "session": {"pid": os.getpid(), "start": proc_start(os.getpid())}}
    # the words kept: those --continue replaces (as --resume <its id>) are not recorded, so they refuse nothing
    return _keep(desk, rec, unsafe(desk, [clear, *rec["words"], str(desk.root)]))


def record_session(desk: Desk, sess: dict, argv: list = None, folder: str = None) -> str:
    """How a session hansa did not start began, for hansa up: its claude's exact arguments and folder, read from
    its process unless given (notify-launch gives its own), tmux, and the launch settings of this command's
    environment, which it has from the session. '' when recorded, else why not."""
    argv = proc_args(sess["pid"]) if argv is None else argv
    folder = proc_cwd(sess["pid"]) if folder is None else folder
    rec = {"kind": "notify", "hansa": hansa_command(), "argv": argv and [argv[0], *unpicked(argv[1:])],
           "folder": folder, "tmux": record_tmux(), "env": start_env(desk, os.environ), "session": sess}
    if not argv:
        return _keep(desk, rec, "the arguments of its claude process cannot be read", unread=True)
    if not folder or not os.path.isdir(folder):
        return _keep(desk, rec, "the folder of its claude process cannot be read", unread=True)
    # the words kept, as record_coordinator: not --resume <its id>, nor the inbox hansa up's launcher gave it
    return _keep(desk, rec, unsafe(desk, [*rec["argv"], folder]))


def restart_plan(desk: Desk, rec: dict) -> dict:
    """What hansa up runs to start the coordinator's session again, from its record: the command, the folder, the
    words claude was given, and the command to run by hand."""
    words = [x for x in (rec.get("words") if isinstance(rec.get("words"), list) else []) if isinstance(x, str)]
    argv = [x for x in (rec.get("argv") if isinstance(rec.get("argv"), list) else []) if isinstance(x, str)]
    hansa = rec.get("hansa") if isinstance(rec.get("hansa"), str) and rec.get("hansa") else hansa_command()
    desk_words = ["--desk", str(desk.root)]
    if rec.get("kind") == "coordinator":
        clear = ["--clear-daily", rec["clear_daily"]] if isinstance(rec.get("clear_daily"), str) and \
            rec.get("clear_daily") else []
        cmd = [hansa, *desk_words, "coordinator", *clear, *resumed(words)]
        return {"cmd": cmd, "hand": cmd, "folder": str(desk.root), "words": words, "kind": "coordinator"}
    if rec.get("kind") == "notify" and argv and isinstance(rec.get("folder"), str):
        claude = [argv[0], *resumed(argv[1:])]
        return {"cmd": [hansa, *desk_words, LAUNCHER, *claude], "hand": claude, "folder": rec["folder"],
                "words": argv[1:], "kind": "notify"}
    cmd = [hansa_command(), *desk_words, "coordinator"]  # a new desk's first start, so nothing to continue
    return {"cmd": cmd, "hand": cmd, "folder": str(desk.root), "words": [], "kind": ""}


def new_desk(desk: Desk) -> bool:
    """A desk that never had a coordinator, as after hansa init: no task, and nothing under .hansa/followers/."""
    def has(d: Path, what) -> bool:
        try:
            return any(what(x) for x in d.iterdir())
        except FileNotFoundError:
            return False
        except OSError:
            return True  # cannot tell: not new
    return not has(desk.tasks_dir, Path.is_dir) and not has(notify.register_dir(desk), lambda x: True)


def by_hand(plan: dict) -> str:
    then = ", then hansa notify on in it" if plan["kind"] == "notify" else ""
    return shlex.join(["cd", plan["folder"]]) + " && " + shlex.join(plan["hand"]) + then


def still_runs(rec: dict):
    """The recorded session's pid when that process still runs (the same pid, started at the same time)."""
    sess = rec.get("session") if isinstance(rec.get("session"), dict) else {}
    pid = sess.get("pid")
    return pid if isinstance(pid, int) and alive(pid, str(sess.get("start") or "")) else None


def restart_line(desk: Desk, rec: dict) -> str:
    """'in FOLDER with COMMAND': what hansa up starts after a restart, from this record."""
    plan = restart_plan(desk, rec)
    return f"in {plan['folder']} with {shlex.join(plan['hand'])}"


def notify_on_line(desk: Desk) -> str:
    """The line hansa notify on adds: how hansa up starts this session again, recorded now; or why it does not."""
    sess = notify.session_here()
    theirs = account.desk_account(desk.root)
    if theirs:  # root's session on the account's desk: root writes nothing there, and hansa up runs as the account
        return f"hansa up does not start this session again after a restart: it is root's, on {theirs}'s desk"
    if os.environ.get("HANSA_ROLE") == "coordinator":  # hansa coordinator started it, and recorded that itself
        rec = read_coordinator(desk)
        same = rec.get("kind") == "coordinator" and notify.same(rec.get("session"), sess)
        return f"After a restart, hansa up starts this session again {restart_line(desk, rec)}." if same else ""
    why = record_session(desk, sess)
    rec = read_coordinator(desk)
    kept = rec.get("kind") == "notify" and notify.same(rec.get("session"), sess)
    if why and not kept:
        return f"hansa up will not start this session again after a restart: {why}; nothing was recorded."
    then = f"{why}, so nothing new was recorded. " if why else ""
    return f"{then}After a restart, hansa up starts this session again {restart_line(desk, rec)}."


def coordinator_line(desk: Desk) -> str:
    """For hansa status while no session hears from hansa: what hansa up does about the coordinator's session."""
    rec = read_coordinator(desk)
    pid = still_runs(rec)
    if pid:
        return (f"the coordinator's session pid {pid} still runs and does not hear from hansa: hansa notify on in it "
                "(hansa up starts no second one)")
    if rec.get("kind") not in ("coordinator", "notify", "unrecorded"):
        return "" if new_desk(desk) else f"hansa up starts no coordinator's session: {NOT_YET}. {TO_RECORD}"
    if rec.get("kind") == "unrecorded":
        return f"hansa up cannot start the coordinator's session again: its last start was not recorded ({rec.get('why')})"
    plan = restart_plan(desk, rec)
    if prints(plan["words"]):
        return "hansa up starts no coordinator's session: its last start ran claude with --print, not interactive"
    if not os.path.isdir(plan["folder"]):  # as hansa up says it
        return f"hansa up starts no coordinator's session: its folder {plan['folder']} is not there"
    return f"hansa up starts the coordinator's session again, in tmux, {restart_line(desk, rec)}"


def attach_command(tmux: str, server: list, session: str, env: dict) -> str:
    name = os.path.basename(tmux) if shutil.which(os.path.basename(tmux)) == tmux else tmux
    attach = [name, *([] if server == ["-L", "default"] else server), "attach", "-t", session]
    if env.get("TMUX_TMPDIR") and server[0] == "-L":
        attach = ["env", "TMUX_TMPDIR=" + env["TMUX_TMPDIR"], *attach]
    return shlex.join(attach)


def pane_ended(prefix: list, pane: str, env: dict) -> bool:
    r = subprocess.run([*prefix, "display-message", "-p", "-t", pane, "#{pane_dead}"], env=env,
                       capture_output=True, text=True, timeout=5)
    return r.returncode != 0 or r.stdout.strip() == "1"


def coordinator_up(desk: Desk) -> bool:
    """Start the coordinator's session again as it last started, in tmux, unless one hears from hansa or the
    recorded one still runs."""
    hearing = notify.names(desk)
    if hearing:
        print(f"coordinator: hansa posts to {', '.join(hearing)}; nothing started")
        return True
    rec = read_coordinator(desk)
    pid = still_runs(rec)
    if pid:  # even one that does not hear from hansa: a second session would continue the same conversation
        print(f"coordinator: its session pid {pid} still runs but does not hear from hansa; nothing started. "
              "Run hansa notify on in it")
        return True
    if rec.get("kind") == "unrecorded":
        print(f"coordinator: its last start was not recorded ({rec.get('why')}); nothing started. Start it yourself, "
              "then run hansa notify on in it")
        return False
    rec = rec if rec.get("kind") in ("coordinator", "notify") else {}
    if not rec and not new_desk(desk):
        print(f"coordinator: {NOT_YET}; nothing started. {TO_RECORD}")
        return False
    plan = restart_plan(desk, rec)
    if prints(plan["words"]):
        print("coordinator: its last start ran claude with --print, which is not an interactive session; "
              "nothing started")
        return False
    if not os.path.isdir(plan["folder"]):
        print(f"coordinator: its folder {plan['folder']} is not there; nothing started")
        return False
    saved_env = rec.get("env") if rec else dict(os.environ)  # as the server part: none recorded, this shell's
    env = clean_env(desk, saved_env if isinstance(saved_env, dict) else {})
    tmux = program("tmux", env=env)
    if not tmux:
        print("coordinator: tmux gives its session a window to run in, and there is none here; nothing started. "
              f"In a terminal of your own: {by_hand(plan)}")
        return True
    if not plan["kind"]:
        print("No recorded coordinator start, and a new desk; starting hansa coordinator in the desk. The first start "
              "in a folder asks whether to trust it: attach and answer.")
    saved = rec.get("tmux") if isinstance(rec.get("tmux"), dict) else {}
    serve = (read_json(desk.state_dir / "serve-start.json", {}) or {}).get("tmux") or {}
    server = saved.get("server") or (serve.get("server") if isinstance(serve, dict) else None) or desk_server(desk)
    session = saved.get("session") or "coordinator"
    prefix = [tmux, *server]
    try:
        made = new_pane(prefix, session, "coordinator", plan["folder"], pane_command(env, plan["cmd"]), env)
        pane = (made.stdout or "").strip()
        if not pane:  # no pane: tmux did not start (it exits 0 when it cannot make its server's socket)
            raise subprocess.CalledProcessError(made.returncode, made.args, made.stdout,
                                                made.stderr or "tmux started no pane")
        end = time.monotonic() + COORDINATOR_WAIT
        while not pane_ended(prefix, pane, env):
            if time.monotonic() >= end:
                print(f"coordinator started in tmux, in {plan['folder']}: {shlex.join(plan['hand'])}")
                print("attach: " + attach_command(tmux, server, session, env))
                return True
            time.sleep(0.25)
        print(f"coordinator failed to start: its pane ended. Run it by hand: {by_hand(plan)}")
    except (OSError, subprocess.SubprocessError) as e:
        detail = e.stderr if isinstance(e, subprocess.CalledProcessError) else str(e)
        print(desk.redact(f"coordinator failed to start: {str(detail).strip()}. Run it by hand: {by_hand(plan)}"))
    return False


def desk_up(desk: Desk) -> bool:
    """The desk's startup sequence: the server, then the coordinator's session."""
    theirs = account.desk_account(desk.root)
    if theirs:
        command = shlex.join([hansa_command(), "--desk", str(desk.root), "up"])
        print(f"This desk belongs to {theirs}; run as {theirs}: {command}")
        return False
    ok = server_up(desk)
    return coordinator_up(desk) and ok


def all_desks_up() -> bool:
    entries = read_json(desks_file(), []) or []
    found, ok = False, True
    for rec in entries:
        if not isinstance(rec, dict) or not rec.get("desk"):
            continue
        root = Path(rec["desk"])
        if not ((root / "settings.json").is_file() and (root / "tasks").is_dir()):
            continue
        if found:
            print()
        found = True
        print(f"Desk {root}:", flush=True)
        desk = Desk(root)
        last = read_json(desk.state_dir / "serve-start.json", {}) or {}
        last["hansa"] = rec.get("hansa", "")
        command = rec.get("hansa") or ""
        try:
            supported = bool(command and os.path.isfile(command) and os.access(command, os.X_OK))
            if supported:
                supported = subprocess.run([command, "up", "--help"], capture_output=True,
                                           text=True, timeout=15).returncode == 0
            if not supported:
                print("Its hansa is missing or too old for up; nothing started.")
                print("Last server command: " + shlex.join(serve_command(desk, last)))
                ok = False
                continue
            r = subprocess.run([command, "--desk", str(root), "up"], capture_output=True, text=True, timeout=30)
            print(desk.redact((r.stdout + r.stderr).rstrip()))
            ok = ok and r.returncode == 0
        except (OSError, subprocess.SubprocessError) as e:
            print(desk.redact(f"Could not run its hansa: {e}"))
            print("Last server command: " + shlex.join(serve_command(desk, last)))
            ok = False
    if not found:
        print("No desks on this account's list. A desk gets on it when its server starts once (hansa serve).")
    return ok
