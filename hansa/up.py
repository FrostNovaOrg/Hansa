"""Remember how a desk's server and coordinator's session started, and bring them back up after they have stopped."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import SESSION, SESSION_PREFIXES, account, notify, session_value, without_session
from .desk import Desk, _desks_entries, desks_file, desks_list_problem, listed_desks, now, program, read_json, write_json, write_text
from .run import alive, proc_args, proc_cwd, proc_start, server_state

DEFAULTS = {"host": "127.0.0.1", "port": 8765, "every": 3.0}
START_ENV = ("PATH", "LANG", "HANSA_CLAUDE", "HANSA_TMUX", "CLAUDE_CONFIG_DIR", "TMPDIR",
             "XDG_RUNTIME_DIR", "XDG_STATE_HOME", "TMUX_TMPDIR")
LOG_LIMIT = 20 * 1024 * 1024
START_WAIT = 8.0
COORDINATOR_START = "coordinator-start.json"
COORDINATOR_WAIT = 3.0  # seconds a started coordinator's pane must last to count as started
# in .hansa/: what the coordinator's pane showed last as it ended, written by the pane itself, so the reason a start
# failed is not lost with the pane
PANE_OUTPUT = "coordinator-pane.txt"
# seconds: a pane whose command ended within these of its start keeps its screen in PANE_OUTPUT (a start that
# failed); one that ran longer, a session that was used, keeps none
PANE_KEPT_WITHIN = 60
UP_LOCK = "up.lock"  # in .hansa/: held while hansa up starts the desk (desk_up)
LAUNCHER = "notify-launch"  # hansa's hidden command that starts a session hansa notify on recorded (cli.py)
# claude's words that pick a conversation, or give a session's inbox, and whether each takes a value (None: one
# that does not start with -, if any). A start again drops them and picks the conversation its record names
# (restart_plan)
PICKS = {"-c": False, "--continue": False, "--fork-session": False, "-r": None, "--resume": None,
         "--session-id": True, "--messaging-socket-path": True}
# A Claude Code session's id, as its hooks, --session-id and CLAUDE_CODE_SESSION_ID give it: a UUID
CONVERSATION_ID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
PRINTS = ("-p", "--print")  # claude's words for a run that is not an interactive session
# With no record on a desk that had a coordinator (new_desk), it ran before hansa recorded starts: maybe a session
# in another folder, so a fresh hansa coordinator would be a second one, in the wrong folder
NOT_YET = ("its start is not recorded yet: this desk had a coordinator before hansa recorded its start, maybe a "
           "session in another folder")
# With no record on a desk hansa saw new (STARTS_RECORDED) whose only sign of a coordinator is a task added from a
# Claude Code session: no coordinator's start was ever left unrecorded there, so none ran before; that session is
# not known to be the coordinator's
NONE_YET = "no coordinator's session is recorded for this desk yet"
TO_RECORD_FIRST = ("To record one: start it with hansa coordinator, or run hansa notify on in the session that "
                   "coordinates; from then on hansa up starts it again")
# In .hansa/: written while the desk is new (new_desk), at hansa init or as the first task from a session is added:
# from then on hansa records each coordinator's start. An older desk has none, so hansa can tell the two apart
STARTS_RECORDED = "starts-recorded.json"
EXTERNAL_ASK = "Allow external CLAUDE.md file imports?"  # Claude Code's question for imports from outside the folder
TO_RECORD = ("Start it as before and run hansa notify on in it, or start it with hansa coordinator; from then on "
             "hansa up starts it again")
CARD = Path(__file__).with_name("card.md")  # the card hansa puts in a desk (cli.py), and the one its copy holds
# Where hansa notify on copies the card for a session that does not read it: in the session's folder, which Claude
# Code reads rules from at each start, for every session started there or below
COPY = os.path.join(".claude", "rules", "hansa-card.md")
COPY_MARK = "<!-- hansa: copy of the coordinator's card for the desk at "  # its header's first line, then the desk
COPIES = "card-copies.json"  # in .hansa/: the copies hansa wrote, so notify off finds them after the record changes
COPY_ENABLED = "card-copy.json"  # in .hansa/: the desk's choice, independent of the session's start record
COPIES_WRITTEN = "card-copies-written.json"  # in .hansa/: each copy's path and the sha256 of what hansa last wrote


def card_copy_enabled(desk: Desk) -> bool:
    return read_json(desk.state_dir / COPY_ENABLED, True) is not False


def hansa_command() -> str:
    # Keep installation links, as bin/hansa does, so switching one changes future starts.
    return str(Path(__file__).absolute().parent.parent / "bin" / "hansa")


def start_env(desk: Desk, env) -> dict:
    """Only launch settings, never session identity or a desk key (even under an allowed name)."""
    keys = list(desk.keys().values())
    return {k: v for k, v in without_session(env).items()
            if (k in START_ENV or k.startswith("LC_")) and isinstance(v, str)
            and not any(key in v for key in keys)}


def recorded_program(desk: Desk, name: str = "claude"):
    """The program the last recorded `hansa serve` start runs `name` with, or None when no start is recorded or its
    record keeps no environment (an older start's, say)."""
    rec = read_json(desk.state_dir / "serve-start.json", {})
    env = rec.get("env") if isinstance(rec, dict) else None
    return program(name, env=env) if isinstance(env, dict) else None


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


def list_desk(desk: Desk, at=None) -> str:
    """Add or update this desk's entry on the account's list of desks, as each hansa serve start does. Returns
    what kept it from being written, worded for the caller to report, or '' when it was written."""
    listed = desks_file()
    try:
        listed.parent.mkdir(parents=True, exist_ok=True)
        # Different desks may start together. Lock the stable inode, replace just the JSON.
        with open(listed.with_suffix(".lock"), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            entries, problem = _desks_entries()
            if problem:  # rewriting it would drop the other desks and let runs start with nothing of them hidden
                return f"left the account's list of desks as it is: {problem}"
            entries = [x for x in entries if isinstance(x, dict) and x.get("desk") != str(desk.root)]
            entries.append({"desk": str(desk.root), "hansa": hansa_command(), "at": now() if at is None else at})
            write_json(listed, entries)
    except OSError as e:
        return f"could not update account's desk list: {e}"
    return ""


def _same_desk(spelled: str, root: Path) -> bool:
    """Whether an entry's desk folder, as it was written, is root: the same spelling, or else the same folder with
    its links followed."""
    return spelled == str(root) or os.path.realpath(spelled) == os.path.realpath(str(root))


def forget_desk(folder) -> tuple:
    """Take one desk off the account's list of desks, for hansa desks --forget; its folder and files stay, and no
    other entry changes. Returns (the desk's folder as taken off, '') or (None, why it was not): its server runs, it
    is not on the list, or the list cannot be read. An entry whose folder is gone can still be taken off."""
    root = Desk(folder).root
    listed = desks_file()
    try:
        listed.parent.mkdir(parents=True, exist_ok=True)
        # The same lock list_desk takes, so a start and a forget one after the other leave one list, not two.
        with open(listed.with_suffix(".lock"), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            entries, problem = _desks_entries()
            if problem:  # rewriting it would drop the other desks, and hansa cannot tell whether root is listed
                return None, problem
            found = [x for x in entries if isinstance(x, dict) and isinstance(x.get("desk"), str)
                     and _same_desk(x["desk"], root)]
            if not found:
                return None, f"{root} is not on the account's list of desks; nothing was changed"
            state = server_state(Desk(found[0]["desk"]))
            if state["state"] != "gone":
                return None, (f"the server for {found[0]['desk']} is running; stop it first (press x on its row in "
                              "the hansa menu), then run hansa desks --forget again. Nothing was changed")
            write_json(listed, [x for x in entries
                                if not (isinstance(x, dict) and isinstance(x.get("desk"), str)
                                        and _same_desk(x["desk"], root))])
            return str(root), ""
    except OSError as e:
        return None, f"could not update the account's list of desks: {e}"


def record_start(desk: Desk, options) -> None:
    """Called after serve removes session identity and binds the page, while it holds serve.lock: a start that
    fails to bind (a port in use, a mistyped --host) leaves the last good start's record."""
    rec = {"hansa": hansa_command(), "options": {k: getattr(options, k) for k in DEFAULTS},
           "port_explicit": options.port is not None,
           "env": start_env(desk, os.environ), "tmux": record_tmux(), "at": now()}
    write_json(desk.state_dir / "serve-start.json", rec)
    problem = list_desk(desk, rec["at"])
    if problem:
        print(desk.redact(f"hansa serve: {problem}"), file=sys.stderr, flush=True)


def serve_command(desk: Desk, rec: dict) -> list:
    opts = {**DEFAULTS, "port": None, **rec.get("options", {})}
    if "port_explicit" not in rec and opts["port"] == DEFAULTS["port"]:
        opts["port"] = None  # older records saved 8765 even when no --port was given
    return [rec.get("hansa") or hansa_command(), "--desk", str(desk.root), "serve",
            *[str(x) for k in DEFAULTS if opts[k] is not None for x in ("--" + k, opts[k])]]


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
    root = desk.root.resolve()
    name = re.sub(r"[^A-Za-z0-9_-]", "-", root.name)[:32] or "desk"
    digest = hashlib.sha256(os.fsencode(root)).hexdigest()[:12]
    return ["-L", f"hansa-{name}-{digest}"]


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


def kept_pane(env: dict, cmd: list, tmux: str, out: Path) -> str:
    """What the coordinator's pane runs: the command as pane_command gives it, on the terminal (an interactive
    session), and, when it ends within PANE_KEPT_WITHIN seconds of its start (a start that failed: hansa up looks
    after COORDINATOR_WAIT), the pane's last screen written to out by tmux, before the pane closes. A session that
    ran longer and ended leaves no copy of its screen on disk."""
    keep = (shlex.quote(tmux) + ' -S "${TMUX%%,*}" capture-pane -p -J -S -100 -t "$TMUX_PANE" > '
            + shlex.quote(str(out)) + " 2>/dev/null")
    # POSIX sh, which has no $SECONDS: the time by which it must end, from date and expr. A date that gives no number
    # leaves t empty or not a number, and test then fails, so nothing is kept
    began = f't=$(expr "$(date +%s)" + {PANE_KEPT_WITHIN} 2>/dev/null) ; '
    soon = '[ "$(date +%s)" -lt "$t" ] 2>/dev/null'
    return (began + pane_command(env, cmd)[len("exec "):] + " ; s=$? ; if " + soon + " ; then " + keep
            + " ; fi ; exit $s")


def pane_output(out: Path, lines: int = 12) -> list:
    """The last lines a coordinator's pane showed as it ended (kept_pane): none when it wrote none."""
    try:
        text = out.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [x.rstrip() for x in text.splitlines() if x.strip()][-lines:]


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
    desk.state_dir.mkdir(mode=0o700, exist_ok=True)
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


DENY_FLAGS = ("--disallowedTools", "--disallowed-tools")  # claude's option for deny rules, in both its spellings


def with_rules(words: list, rules: list) -> list:
    """claude's words with these deny rules: in the first --disallowedTools (or --disallowed-tools) list the words
    hold, right after its name, the words' own entries kept after them (but one that is one of these); else as a
    list of their own, at the end of the options (before --), so no word after it is read as a rule."""
    end = words.index("--") if "--" in words else len(words)
    at = next((i for i, x in enumerate(words[:end]) if x.partition("=")[0] in DENY_FLAGS), None)
    if at is None:
        return words[:end] + ["--disallowedTools", *rules] + words[end:]
    name, eq, value = words[at].partition("=")
    own = [value] if eq and value not in rules else []
    i = at + 1
    if not eq:  # a list of values, up to the next option
        while i < end and not words[i].startswith("-"):
            own += [] if words[i] in rules else [words[i]]
            i += 1
    return words[:at] + [name, *rules, *own] + words[i:]


def without_rules(words: list, rules: list) -> list:
    """claude's words without the deny rules with_rules added (a list left with no entry goes too): what a
    session hansa notify-launch started is recorded with when it runs hansa notify on."""
    out, i = [], 0
    while i < len(words):
        x = words[i]
        if x == "--":
            return out + words[i:]
        if x not in DENY_FLAGS:
            out.append(x)
            i += 1
            continue
        own, i = [], i + 1
        while i < len(words) and not words[i].startswith("-"):
            own += [] if words[i] in rules else [words[i]]
            i += 1
        out += [x, *own] if own else []
    return out


SETTINGS_FLAG = "--settings"


def has_settings(words: list) -> bool:
    """Whether claude's words (before --) give a --settings option of their own."""
    end = words.index("--") if "--" in words else len(words)
    return any(x == SETTINGS_FLAG or x.startswith(SETTINGS_FLAG + "=") for x in words[:end])


def with_settings(words: list, settings: str) -> list:
    """claude's words with --settings SETTINGS at the end of the options (before --)."""
    end = words.index("--") if "--" in words else len(words)
    return words[:end] + [SETTINGS_FLAG, settings] + words[end:]


def without_settings(words: list, settings) -> list:
    """claude's words without the --settings SETTINGS with_settings added (that exact value only): what a session
    hansa notify-launch started is recorded with when it runs hansa notify on, as without_rules."""
    if not isinstance(settings, str):
        return words
    out, i = [], 0
    while i < len(words):
        x = words[i]
        if x == "--":
            return out + words[i:]
        if x == SETTINGS_FLAG and i + 1 < len(words) and words[i + 1] == settings:
            i += 2
            continue
        if x == f"{SETTINGS_FLAG}={settings}":
            i += 1
            continue
        out.append(x)
        i += 1
    return out


# -- the coordinator's turns: how each ended, written by its Stop and StopFailure hooks (hansa coordinator-turn) --
TURNS_DIR = "coordinator-turns"
# Claude Code's StopFailure error types; any other is kept as "unknown"
TURN_ERRORS = ("rate_limit", "overloaded", "authentication_failed", "oauth_org_not_allowed", "account_on_hold",
               "verification_required", "billing_error", "invalid_request", "model_not_found", "server_error",
               "max_output_tokens", "cloud_credential_error", "unknown")
TURN_EVENTS = ("Stop", "StopFailure")


def turns_dir(desk: Desk) -> Path:
    return desk.state_dir / TURNS_DIR


def turn_input(raw: str) -> dict | None:
    """Of a Stop or StopFailure hook's input, only what the record keeps: the event and the error type. None when
    the input is not such a hook's."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("hook_event_name") not in TURN_EVENTS:
        return None
    err = data.get("error")
    return {"hook_event_name": data["hook_event_name"],
            **({"error": err if err in TURN_ERRORS else "unknown"} if data["hook_event_name"] == "StopFailure" else {})}


def record_turn(desk: Desk, pid: int, given: dict) -> None:
    """Write .hansa/coordinator-turns/<pid>.json for one turn's end (given: turn_input's), whole, in one rename."""
    path = turns_dir(desk) / f"{int(pid)}.json"
    old = read_json(path, {}) if path.is_file() and not path.is_symlink() else {}
    old = old if isinstance(old, dict) else {}
    at = now()
    fails = old.get("fails") if isinstance(old.get("fails"), int) and not isinstance(old.get("fails"), bool) else 0
    last_ok = old.get("last_ok") if isinstance(old.get("last_ok"), (int, float)) else None
    if given["hook_event_name"] == "Stop":
        rec = {"at": at, "ok": True, "error": None, "fails": 0, "failed_since": None, "last_ok": at}
    else:
        since = old.get("failed_since") if fails > 0 and isinstance(old.get("failed_since"), (int, float)) else at
        rec = {"at": at, "ok": False, "error": given.get("error") or "unknown", "fails": max(fails, 0) + 1,
               "failed_since": since, "last_ok": last_ok}
    desk.state_dir.mkdir(mode=0o700, exist_ok=True)
    turns_dir(desk).mkdir(exist_ok=True)
    write_json(path, rec)


PID_MAX = 1 << 22  # the most a pid can be (Linux's PID_MAX_LIMIT; macOS's is lower)


def turn_pid(name: str) -> int | None:
    """The pid a file in coordinator-turns/ is the record of: '<pid>.json', the pid plain ASCII digits in the range a
    pid can have; None for any other name."""
    m = re.fullmatch(r"([1-9][0-9]{0,6})\.json", name)
    return int(m.group(1)) if m and int(m.group(1)) <= PID_MAX else None


def sweep_turns(desk: Desk) -> list:
    """Remove the turn records of sessions whose process is gone (hansa serve's start); returns the pids removed.
    Leaves alone any file whose name is not a pid's record (turn_pid), and never raises."""
    gone = []
    try:
        entries = list(turns_dir(desk).iterdir())
    except OSError:
        return gone
    for f in entries:
        try:
            pid = turn_pid(f.name)
            if pid is None or alive(pid):
                continue
            f.unlink()
            gone.append(pid)
        except Exception:  # noqa: BLE001 - one record left behind is never worth hansa serve's start
            pass
    return gone


def forget_turns(desk: Desk, pid: int) -> None:
    """Remove the turn record of this pid, before a new session with it starts: a dead session's record, whose pid
    the new one got, is not the new one's. Never raises."""
    try:
        if turn_pid(f"{int(pid)}.json") is not None:
            (turns_dir(desk) / f"{int(pid)}.json").unlink()
    except (Exception, SystemExit):  # noqa: BLE001 - none there, mostly
        pass


# a session hansa started before its starts gave the turn hooks: its start record does not say it has them
OLDER_START = ("not recorded: started before hansa gave its sessions turn hooks; they come with its next start by "
               "hansa coordinator or hansa up")


def turn_line(desk: Desk, sess: dict, name: str) -> str:
    """For hansa status: whether the turns of this session hansa posts to are recorded, and if not, why not. By the
    record of how the session started (COORDINATOR_START), and the turn record its hooks wrote."""
    rec = read_coordinator(desk)
    pid = sess.get("pid") if isinstance(sess, dict) else None
    mine = notify.same(rec.get("session"), sess)
    written = isinstance(pid, int) and (turns_dir(desk) / f"{pid}.json").is_file()
    head = f"turns of {name}: "
    if mine and rec.get("kind") == "coordinator":
        words = [x for x in rec.get("words") or [] if isinstance(x, str)]
        if has_settings(words):
            return head + ("not recorded: it was started with a --settings of its own, which takes the place of "
                           "hansa's (with its Stop and StopFailure hooks)")
        if rec.get("turn_hooks") is True:
            return head + "recorded (hansa coordinator gave it Stop and StopFailure hooks)" + _turn_said(desk, pid)
        return head + OLDER_START
    if mine and rec.get("kind") == "notify":
        argv = [x for x in rec.get("argv") or [] if isinstance(x, str)]
        if rec.get("turn_hooks") is True or isinstance(rec.get("launch_settings"), str):
            return head + "recorded (hansa up gave it Stop and StopFailure hooks)" + _turn_said(desk, pid)
        if has_settings(argv[1:]):
            return head + ("not recorded: it was started with a --settings of its own, and hansa up adds no hooks "
                           "to such a command")
        if isinstance(rec.get("launch_rules"), list):  # notify-launch started it, before it gave the hooks
            return head + OLDER_START
        return head + ("not recorded: it was started by hand and recorded with hansa notify on; hansa up gives it "
                       "Stop and StopFailure hooks when it starts it")
    if written:
        return head + "recorded" + _turn_said(desk, pid)
    return head + "not recorded: hansa's record of the coordinator's start is of another session (or none)"


def _turn_said(desk: Desk, pid) -> str:
    rec = read_json(turns_dir(desk) / f"{pid}.json", {}) if isinstance(pid, int) else {}
    if not isinstance(rec, dict) or "ok" not in rec:
        return ", none ended yet"
    if rec.get("ok"):
        return ", the last ended normally"
    return f", the last {rec.get('fails')} ended with an API error ({rec.get('error')})"


def resumed(words: list, pick: list = ("--continue",)) -> list:
    """The words to start claude again with: pick (--continue, --resume ID, or nothing for a new conversation) in
    place of the words that picked a conversation."""
    words = unpicked(words)
    at = words.index("--") if "--" in words else len(words)
    return words[:at] + list(pick) + words[at:]


UNKNOWN = "unknown"  # picked's answer for words that pick a conversation hansa cannot name


def picked(words: list):
    """Which conversation claude's words start with: its id (--resume ID, --session-id ID), None for a new one (no
    word picks one), or UNKNOWN (--continue, the picker, a name, a fork: a conversation, but not one hansa knows)."""
    words = words[:words.index("--")] if "--" in words else words
    got, i = None, 0
    while i < len(words):
        name, eq, value = words[i].partition("=") if words[i].startswith("--") else (words[i], "", "")
        if name in ("-c", "--continue", "--fork-session"):
            return UNKNOWN
        if name in ("-r", "--resume", "--session-id"):
            if not eq:
                value = words[i + 1] if i + 1 < len(words) else ""
                i += 1 if value and not value.startswith("-") else 0
            if not CONVERSATION_ID.fullmatch(value):
                return UNKNOWN
            got = value
        i += 1
    return got


def conversation_of(desk: Desk, sid, transcript: str = "") -> dict:
    """What a start record keeps of the conversation a session runs (key 'conversation'): {'id', 'transcript'} for a
    known one (the transcript '' when its path is not known; the earlier record's when it is of the same id), None
    for a new one, and nothing (the key left out) for one hansa cannot name: hansa up then continues the folder's
    latest, as before records named it."""
    if sid is UNKNOWN:
        return {}
    if sid is None:
        return {"conversation": None}
    old = read_coordinator(desk).get("conversation")
    if not transcript and isinstance(old, dict) and old.get("id") == sid and isinstance(old.get("transcript"), str):
        transcript = old["transcript"]
    return {"conversation": {"id": sid, "transcript": transcript}}


def conversation_input(raw: str) -> dict | None:
    """Of a coordinator's hook's input (Stop, StopFailure, or SessionStart after a /clear), the conversation it
    names: {'id': its session id, 'transcript': its transcript's absolute path, or ''}. None when it names none."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("hook_event_name") not in (*TURN_EVENTS, "SessionStart"):
        return None
    sid, path = data.get("session_id"), data.get("transcript_path")
    if not isinstance(sid, str) or not CONVERSATION_ID.fullmatch(sid):
        return None
    return {"id": sid, "transcript": path if isinstance(path, str) and os.path.isabs(path) else ""}


def record_conversation(desk: Desk, pid: int, conv: dict) -> None:
    """Keep in the coordinator's start record the conversation its session runs now (conversation_input's), as its
    hooks give it: at each turn's end, and after a /clear, which gives the session a new id. Only for the recorded
    session (this pid, started when the record says), and only when it changed."""
    rec = read_coordinator(desk)
    if rec.get("kind") not in ("coordinator", "notify") or still_runs(rec) != pid or rec.get("conversation") == conv:
        return
    write_json(desk.state_dir / COORDINATOR_START, {**rec, "conversation": conv})


def transcript_of(env: dict, sid: str) -> str:
    """The transcript of the conversation with this id under the Claude Code folder of this environment
    (CLAUDE_CONFIG_DIR, else ~/.claude): projects/<folder>/<id>.jsonl; '' when none is found."""
    home = env.get("CLAUDE_CONFIG_DIR") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".claude")
    try:
        found = sorted(Path(os.path.expanduser(home), "projects").glob(f"*/{sid}.jsonl"))
    except OSError:
        return ""
    return str(found[0]) if found else ""


def conversation_plan(rec: dict) -> dict:
    """How a start again picks the coordinator's conversation, by its record: {'how': 'resume', 'id'} its own,
    by its id; {'how': 'new', 'id', 'why'} none, as it had none to resume ('none') or its transcript is gone
    ('gone'); or {'how': 'continue'} the folder's latest, for a record that does not say (an older one)."""
    if "conversation" not in rec:
        return {"how": "continue"}
    conv = rec["conversation"]
    sid = conv.get("id") if isinstance(conv, dict) else None
    if not isinstance(sid, str) or not CONVERSATION_ID.fullmatch(sid):
        return {"how": "new", "why": "none"}
    path = conv.get("transcript")
    if isinstance(path, str) and path:
        try:
            os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return {"how": "new", "id": sid, "why": "gone", "transcript": path}
        except OSError:  # cannot tell: it may well be there
            pass
    return {"how": "resume", "id": sid}


def pick_words(conv: dict) -> list:
    """claude's words for conversation_plan's choice."""
    return {"resume": ["--resume", conv.get("id", "")], "continue": ["--continue"]}.get(conv["how"], [])


def conversation_line(plan: dict) -> str:
    """For hansa up: which conversation the coordinator's session came back with."""
    conv = plan.get("conversation") or {"how": "new"}
    if conv["how"] == "resume":
        return f"conversation: it resumed the coordinator's own, session id {conv['id']}"
    if conv["how"] == "continue":
        return (f"conversation: it continued the most recent one in {plan['folder']} (--continue): hansa's record of "
                "the coordinator's start is older and does not name its conversation; it does from the session's next "
                "turn")
    if conv.get("why") == "gone":
        return (f"conversation: a new one; the coordinator's conversation {conv['id']} is gone (its transcript "
                f"{conv['transcript']} is not there)")
    if conv.get("why") == "none":
        return "conversation: a new one; the coordinator's last session had none to resume (it was never typed into)"
    return "conversation: a new one"


def prints(words: list) -> bool:
    """Whether these words start claude with --print: a run that ends, not an interactive session."""
    return any(x in PRINTS for x in words[:words.index("--") if "--" in words else len(words)])


# claude's words for a run that ends by itself: --print, and those that only print help or the version
ONE_OFF = (*PRINTS, "-h", "--help", "-v", "--version")


def interactive(words: list) -> bool:
    """Whether these words start claude as an interactive session: none of ONE_OFF before `--`. A run that is not
    interactive (hansa coordinator -p ...) leaves the records of the coordinator's start alone."""
    return not any(x in ONE_OFF for x in words[:words.index("--") if "--" in words else len(words)])


def running_coordinator(desk: Desk) -> dict:
    """The desk's coordinator's session that runs now, other than this process: the recorded one while its process
    still runs (still_runs), else a session hansa posts to whose process runs. {pid, name, attach}: name as its
    follower last recorded it ('' none yet); attach, the tmux command that reaches it when its start record says it
    runs in tmux, else ''. {} when none runs."""
    rec = read_coordinator(desk)
    lives = [r for r in notify.live_records(desk) if isinstance(r.get("session"), dict) and not notify.gone(r["session"])]
    sess = rec.get("session") if still_runs(rec) not in (None, os.getpid()) else None
    if sess is None:
        sess = next((r["session"] for r in lives if r["session"].get("pid") != os.getpid()), None)
    if not sess:
        return {}
    name = next((r.get("name") or "" for r in lives if notify.same(r.get("session"), sess)), "")
    saved = rec.get("tmux") if notify.same(rec.get("session"), sess) and isinstance(rec.get("tmux"), dict) else {}
    server, session = saved.get("server"), saved.get("session")
    attach = ""
    if isinstance(server, list) and len(server) == 2 and all(isinstance(x, str) for x in server) \
            and isinstance(session, str) and session:
        launch = rec.get("env") if isinstance(rec.get("env"), dict) else {}
        tmux = program("tmux") or program("tmux", env=launch) or "tmux"
        attach = attach_command(tmux, server, session, {k: v for k, v in launch.items() if k == "TMUX_TMPDIR"})
    return {"pid": sess.get("pid"), "name": name, "attach": attach}


def running_text(found: dict) -> str:
    """What hansa coordinator says when the desk's coordinator already runs (running_coordinator's)."""
    reach = (f"Reach it with: {found['attach']}" if found["attach"] else
             "It does not run in tmux: go to the terminal it runs in.")
    return (f"the desk's coordinator already runs: {notify.who(found['pid'], found['name'])}. hansa posts to that "
            "session; a second one would work beside it, and hansa would post to both. Nothing was started; once it "
            f"has ended, hansa coordinator starts a new one. {reach}")


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
        desk.state_dir.mkdir(mode=0o700, exist_ok=True)
        write_json(desk.state_dir / COORDINATOR_START, {**rec, "at": now()})
    except OSError as e:
        return why or f"{COORDINATOR_START} could not be written: {e}"
    return why


def recorded_folders(desk: Desk) -> list:
    """The folder hansa notify on or notify-launch recorded for the desk's coordinator's session, where hansa writes
    its copy of the card: in a list, empty when none is recorded or hansa notify off ran in that session since."""
    rec = read_coordinator(desk)
    f = rec.get("folder")
    return [f] if rec.get("kind") == "notify" and not rec.get("off") and isinstance(f, str) and os.path.isabs(f) else []


def notify_off(desk: Desk, sess: dict) -> None:
    """hansa notify off in the recorded session: its folder is no longer recorded (recorded_folders) until a start or
    a hansa notify on records it again. hansa up starts it again all the same."""
    rec = read_coordinator(desk)
    if rec.get("kind") == "notify" and notify.same(rec.get("session"), sess) and not rec.get("off"):
        try:
            write_json(desk.state_dir / COORDINATOR_START, {**rec, "off": now()})
        except OSError:  # its folder's files stay closed to workers: nothing opens by this
            pass


def record_coordinator(desk: Desk, clear: str, words: list) -> str:
    """How hansa coordinator started this session (this process, which becomes it), for hansa up: '' when
    recorded, else why not. Its own option, the words it gave claude, the desk, tmux and the launch settings."""
    rec = {"kind": "coordinator", "hansa": hansa_command(), "clear_daily": clear, "words": unpicked(words),
           "folder": str(desk.root), "tmux": record_tmux(), "env": start_env(desk, os.environ),
           "session": {"pid": os.getpid(), "start": proc_start(os.getpid())},
           "turn_hooks": True,  # hansa coordinator gives every session it starts the Stop and StopFailure hooks
           # the conversation it starts with (--resume <its id>, as hansa up gives it), or a new one; its hooks keep
           # it up to date (record_conversation)
           **conversation_of(desk, picked(words))}
    # the words kept: those that pick a conversation (as --resume <its id>) are not, so they refuse nothing
    return _keep(desk, rec, unsafe(desk, [clear, *rec["words"], str(desk.root)]))


def record_session(desk: Desk, sess: dict, argv: list = None, folder: str = None, rules: list = None,
                   settings: str = None, sid: str = "") -> str:
    """How a session hansa did not start began, for hansa up: its claude's exact arguments and folder, read from
    its process unless given (notify-launch gives its own), tmux, and the launch settings of this command's
    environment, which it has from the session. rules: the deny rules notify-launch starts it with (launch_rules),
    kept in the record, which so says the session has them, and left out of its arguments; settings: the --settings
    with the Stop and StopFailure hooks notify-launch gave it (turn_hooks), kept and left out the same way. sid: the
    session's id, as Claude Code gives it to a command run in the session (hansa notify on); else the conversation
    is the one its words pick. '' when recorded, else why not."""
    argv = proc_args(sess["pid"]) if argv is None else argv
    folder = proc_cwd(sess["pid"]) if folder is None else folder
    words = without_settings(without_rules(unpicked(argv[1:]), rules or []), settings) if argv else []
    env = start_env(desk, os.environ)
    conv = conversation_of(desk, sid, transcript_of(os.environ, sid)) if CONVERSATION_ID.fullmatch(sid or "") else \
        conversation_of(desk, picked(argv[1:]) if argv else UNKNOWN)
    rec = {"kind": "notify", "hansa": hansa_command(), "argv": argv and [argv[0], *words],
           "folder": folder, "tmux": record_tmux(), "env": env, "session": sess,
           **({"launch_rules": rules} if rules is not None else {}),
           **({"launch_settings": settings, "turn_hooks": True} if isinstance(settings, str) else {}), **conv}
    if not argv:
        return _keep(desk, rec, "the arguments of its claude process cannot be read", unread=True)
    if not folder or not os.path.isdir(folder):
        return _keep(desk, rec, "the folder of its claude process cannot be read", unread=True)
    # the words kept, as record_coordinator: not --resume <its id>, nor the inbox hansa up's launcher gave it
    return _keep(desk, rec, unsafe(desk, [*rec["argv"], folder]))


def restart_plan(desk: Desk, rec: dict, conv: dict = None) -> dict:
    """What hansa up runs to start the coordinator's session again, from its record: the command, the folder, the
    words claude was given, the command to run by hand, and which conversation it comes back with
    (conversation_plan, unless conv gives it, as {'how': 'new'} for the same start with a new conversation)."""
    words = [x for x in (rec.get("words") if isinstance(rec.get("words"), list) else []) if isinstance(x, str)]
    argv = [x for x in (rec.get("argv") if isinstance(rec.get("argv"), list) else []) if isinstance(x, str)]
    hansa = rec.get("hansa") if isinstance(rec.get("hansa"), str) and rec.get("hansa") else hansa_command()
    desk_words = ["--desk", str(desk.root)]
    conv = conv or conversation_plan(rec)
    if rec.get("kind") == "coordinator":
        clear = ["--clear-daily", rec["clear_daily"]] if isinstance(rec.get("clear_daily"), str) and \
            rec.get("clear_daily") else []
        cmd = [hansa, *desk_words, "coordinator", *clear, *resumed(words, pick_words(conv))]
        return {"cmd": cmd, "hand": cmd, "folder": str(desk.root), "words": words, "kind": "coordinator",
                "conversation": conv}
    if rec.get("kind") == "notify" and argv and isinstance(rec.get("folder"), str):
        claude = [argv[0], *resumed(argv[1:], pick_words(conv))]
        return {"cmd": [hansa, *desk_words, LAUNCHER, *claude], "hand": claude, "folder": rec["folder"],
                "words": argv[1:], "kind": "notify", "conversation": conv}
    cmd = [hansa_command(), *desk_words, "coordinator"]  # a new desk's first start, so nothing to continue
    return {"cmd": cmd, "hand": cmd, "folder": str(desk.root), "words": [], "kind": "", "conversation": {"how": "new"}}


def new_desk(desk: Desk) -> bool:
    """A desk that never had a coordinator, as after hansa init: hansa coordinator never prepared it
    (.hansa/coordinator.json), no session ever heard from it (nothing under .hansa/followers/), and no task was
    added from a Claude Code session (one has 'session'). Tasks added by hand, from a shell, are no coordinator's."""
    def has(d: Path, what) -> bool:
        try:
            return any(what(x) for x in d.iterdir())
        except FileNotFoundError:
            return False
        except OSError:
            return True  # cannot tell: not new

    def from_session(d: Path) -> bool:
        if not d.is_dir():
            return False
        try:
            m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            return True  # cannot tell: not new
        return not isinstance(m, dict) or bool(m.get("session"))
    return not (desk.state_dir / "coordinator.json").exists() and not has(desk.tasks_dir, from_session) \
        and not has(notify.register_dir(desk), lambda x: True)


def mark_new(desk: Desk) -> None:
    """While the desk is new, record that hansa records every coordinator's start on it from now on
    (STARTS_RECORDED). Not written on a desk that is no longer new: one from before hansa recorded starts."""
    try:
        if not (desk.state_dir / STARTS_RECORDED).exists() and new_desk(desk):
            desk.state_dir.mkdir(mode=0o700, exist_ok=True)
            write_json(desk.state_dir / STARTS_RECORDED, {"since": now()})
    except OSError:
        pass  # then the desk reads as an older one: hansa up starts nothing and says what to do


def none_yet(desk: Desk) -> bool:
    """A desk that is not new only for tasks added from a Claude Code session, which hansa saw new: no coordinator
    ran before hansa recorded starts there, and none has recorded its start since."""
    return (desk.state_dir / STARTS_RECORDED).exists() and not (desk.state_dir / "coordinator.json").exists() \
        and not _followers(desk)


def _followers(desk: Desk) -> bool:
    try:
        return any(notify.register_dir(desk).iterdir())
    except FileNotFoundError:
        return False
    except OSError:
        return True


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


def notify_on_line(desk: Desk, card: bool | None = None) -> str:
    """The line hansa notify on adds: how hansa up starts this session again, recorded now; or why it does not."""
    sess = notify.session_here()
    theirs = account.desk_account(desk.root)
    if theirs:  # root's session on the account's desk: root writes nothing there, and hansa up runs as the account
        return f"hansa up does not start this session again after a restart: it is root's, on {theirs}'s desk"
    removed = []
    if card is not None:
        write_json(desk.state_dir / COPY_ENABLED, card)
        if not card:
            removed = remove_copies(desk)
    rec = read_coordinator(desk)
    # hansa coordinator started this same session and recorded that itself. By the record, not HANSA_ROLE: a desk
    # owner's wrapper sets that on every hansa command, so a session it did not start runs notify on with it too
    if rec.get("kind") == "coordinator" and notify.same(rec.get("session"), sess):
        return f"After a restart, hansa up starts this session again {restart_line(desk, rec)}."
    # hansa up started this session through notify-launch, with the deny rules its record keeps
    rules = rec.get("launch_rules") if rec.get("kind") == "notify" and notify.same(rec.get("session"), sess) \
        and isinstance(rec.get("launch_rules"), list) else None
    settings = rec.get("launch_settings") if rules is not None and isinstance(rec.get("launch_settings"), str) else None
    why = record_session(desk, sess, rules=rules, settings=settings, sid=session_value("CLAUDE_CODE_SESSION_ID"))
    rec = read_coordinator(desk)
    kept = rec.get("kind") == "notify" and notify.same(rec.get("session"), sess)
    if why and not kept:
        return f"hansa up will not start this session again after a restart: {why}; nothing was recorded."
    then = f"{why}, so nothing new was recorded. " if why else ""
    placed = place_card(desk, rec)
    placed["removed"] = removed + placed["removed"]
    return "\n".join([f"{then}After a restart, hansa up starts this session again {restart_line(desk, rec)}.",
                      *([] if isinstance(rec.get("launch_rules"), list) else [unruled(desk, rec)]),
                      *card_lines(desk, rec, placed)])


# What hansa notify on says of a session it records that hansa up did not start (notify-launch)
UNRULED = ("This session has none of the deny rules hansa coordinator gives its own session (no pushing or "
           "publishing from its shell, no reading of the desk's secrets/ and .hansa/ or of the logins and keys under "
           "the home folder, no editing of the desk's files but notes/ and worktrees/) until hansa up starts it again, which gives "
           "it those; even then, not the principal's list of what workers may not read, nor the no-edit rule on the "
           "lanes' repositories, and no helpers.")


def unruled(desk: Desk, rec: dict) -> str:
    """UNRULED; for a session started in the desk's own folder, which loads the desk's .claude/settings.json, the
    deny rules it has from there (cli.permissions) and those hansa up adds."""
    from .cli import desk_spellings, permissions
    if rec.get("folder") not in desk_spellings(desk):
        return UNRULED
    deny, r = permissions(desk)["deny"], str(desk.root).lstrip("/")

    def names(tool: str) -> str:
        got = dict.fromkeys(x[len(tool) + len(r) + 4:-1].replace("/**", "/") for x in deny
                            if x.startswith(f"{tool}(//{r}/"))
        return " and ".join([", ".join([*got][:-1]), [*got][-1]] if len(got) > 1 else got)
    return ("Started in the desk's folder, this session has these of the deny rules hansa coordinator gives its own "
            "session, from the desk's .claude/settings.json: no pushing or publishing from its shell, no reading of "
            f"the desk's {names('Read')}, no editing of the desk's {names('Edit')}. The rest (no reading of the "
            "logins and keys under the home folder, no editing of the desk's other files but notes/ and worktrees/) "
            "come when hansa up starts it again; even then, not the principal's list of what workers may not read, "
            "nor the no-edit rule on the lanes' repositories, and no helpers.")


def card_import(folder: Path, card: Path):
    """The CLAUDE.md (or .claude/CLAUDE.md) in folder or a folder above it with a line that imports card: @path,
    a relative path read from the folder of the file that holds it. None when there is none."""
    for d in (folder, *folder.parents):
        for f in (d / "CLAUDE.md", d / ".claude" / "CLAUDE.md"):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(os.path.realpath(os.path.join(f.parent, os.path.expanduser(m))) == str(card)
                   for m in re.findall(r"(?:^|\s)@(\S+)", text)):
                return f
    return None


def account_rules(folder: Path, rec: dict) -> str:
    """Why folder/.claude/rules would reach every session of the account, '' when it would not: that .claude is
    the account's Claude Code folder (the home folder's, or CLAUDE_CONFIG_DIR's), or folder holds the home folder."""
    home = Path(os.path.realpath(os.path.expanduser("~")))
    env = rec.get("env") if isinstance(rec.get("env"), dict) else {}
    own = {os.path.realpath(home / ".claude"), *[os.path.realpath(os.path.expanduser(x)) for x in
                                                  (os.environ.get("CLAUDE_CONFIG_DIR"), env.get("CLAUDE_CONFIG_DIR"))
                                                  if isinstance(x, str) and x]}
    if os.path.realpath(folder / ".claude") in own:
        return (f"{folder / '.claude'} is the account's Claude Code folder, and rules there reach every session of the "
                "account")
    if folder in home.parents:
        return f"{folder} holds the home folder, and rules in its .claude reach every session started below it"
    return ""


def copy_owner(path) -> str | None:
    """The desk hansa's header in this copy of the card names; '' for a file without that header, None for none."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            first = f.readline().rstrip("\n")
    except FileNotFoundError:
        return None
    except OSError:  # a folder, or unreadable: not hansa's to replace
        return ""
    return first[len(COPY_MARK):-len(" -->")] if first.startswith(COPY_MARK) and first.endswith(" -->") else ""


def card_check(desk: Desk, rec: dict) -> dict:
    """Whether Claude Code loads the desk's card (its CLAUDE.md) for the session a hansa notify on record names.
    {} when it does, and for a session hansa coordinator started: that one runs in the desk. Else its state:
    'external' (imported from outside the session's folder), 'copy' (hansa's copy of it is in the folder's
    .claude/rules), 'account' (that folder would reach every session of the account), 'foreign' (a file hansa did
    not write is where the copy goes), 'off' (the owner turned the copy off) or 'missing', with the line that
    imports it."""
    if rec.get("kind") != "notify" or not isinstance(rec.get("folder"), str) or not os.path.isdir(rec["folder"]):
        return {}
    folder, root = Path(os.path.realpath(rec["folder"])), Path(os.path.realpath(desk.root))
    card = root / "CLAUDE.md"
    if folder == root or root in folder.parents:  # Claude Code loads it in the desk and the folders below it
        return {}
    inside, held = folder in root.parents, card_import(folder, card)
    if held and inside:
        return {}
    c = {"folder": rec["folder"], "card": str(card), "file": str(held or ""), "inside": inside,
         "copy": str(Path(rec["folder"]) / COPY),
         "line": "@" + (os.path.relpath(card, folder) if inside else str(card))}
    if held:
        return {**c, "state": "external"}
    if not card_copy_enabled(desk):
        return {**c, "state": "off"}
    why = account_rules(folder, rec)
    if why:
        return {**c, "state": "account", "why": why}
    owner = copy_owner(c["copy"])
    return {**c, "state": "copy" if owner == str(desk.root) else "foreign" if owner == "" else "missing"}


def _git(folder, *args):
    """git's answer in folder, without the GIT_ variables of the command's environment; None when it did not run."""
    try:
        return subprocess.run(["git", *args], cwd=folder, capture_output=True, text=True, timeout=10,
                              env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    except (OSError, subprocess.SubprocessError):
        return None


def git_top(folder) -> str:
    """The top of the git work tree folder is in; '' when it is in none."""
    r = _git(folder, "rev-parse", "--show-toplevel")
    return r.stdout.strip() if r and r.returncode == 0 else ""


def tracked(top: str, path) -> bool:
    """Whether git tracks path in the work tree at top."""
    r = top and _git(top, "ls-files", "--error-unmatch", "--",
                     os.path.relpath(os.path.realpath(path), os.path.realpath(top)))
    return bool(r) and r.returncode == 0


def exclude_path(top: str, path) -> tuple:
    """Keep path (a file or a folder) out of the repository whose work tree it is in: its path in that repository's
    info/exclude, once, and not when git already ignores it; never in a tracked .gitignore. Shared by the card's copy
    (exclude_copy) and a desk made inside a work tree (cli.keep_desk_out). What was done: ("ignored", ""),
    ("added", exclude), ("listed", exclude) when the line was there already, or ("failed", why)."""
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(top))
    if rel == ".":
        return "failed", f"{path} is the top folder of that work tree, which info/exclude cannot leave out"
    r = _git(top, "check-ignore", "-q", "--", rel)
    if r and r.returncode == 0:
        return "ignored", ""
    r = _git(top, "rev-parse", "--git-path", "info/exclude")
    if not r or r.returncode or not r.stdout.strip():
        return "failed", "the repository's info/exclude could not be found"
    exclude = Path(top, r.stdout.strip())
    line = "/" + re.sub(r"([*?\[\\])", r"\\\1", rel.replace(os.sep, "/"))
    try:
        text = exclude.read_text(encoding="utf-8", errors="replace") if exclude.is_file() else ""
        if line in text.splitlines():
            return "listed", str(exclude)
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with open(exclude, "a", encoding="utf-8") as f:
            f.write(("\n" if text and not text.endswith("\n") else "") + line + "\n")
    except OSError as e:
        return "failed", f"{exclude} could not be written ({e.strerror or e})"
    return "added", str(exclude)


def exclude_copy(top: str, path: Path) -> str:
    """Keep the copy out of the repository whose work tree it is in (exclude_path). What was done, as a sentence."""
    done, detail = exclude_path(top, path)
    if done == "ignored":
        return f"git in {top} ignores it."
    if done == "failed":
        return f"git in {top} lists it, as {detail}: commit it nowhere."
    return f"hansa added it to {detail}, so git in {top} leaves it out."


def copy_text(desk: Desk, path: Path) -> str:
    """The copy of the card: hansa's header, then hansa's own card (card.md, the one hansa brings the desk's to; never
    the desk's CLAUDE.md as it stands, which may be older or edited). An import line in it (@notes/PROJECT.md) names
    its file from the copy's folder when that file is inside the session's folder, and is a pointer to it otherwise."""
    folder = Path(os.path.realpath(path.parent.parent.parent))
    card = CARD.read_text(encoding="utf-8")

    def imported(m):
        target = Path(os.path.realpath(desk.root / os.path.expanduser(m.group(1))))
        if folder in target.parents:
            return "@" + os.path.relpath(target, os.path.realpath(path.parent))
        return f"(The card imports {target} here; this copy does not, as it is outside this folder: read it.)"
    header = (f"{COPY_MARK}{desk.root} -->\n"
              f"> hansa wrote this file for the desk at {desk.root}. It rewrites it when the card changes and removes "
              "it with `hansa notify off`, so edits to it are lost.\n>\n"
              "> This session was not started by `hansa coordinator`, so the helpers the card names (scout, "
              "reviewer, operator) and the coordinator's mod are not in it, but for hansa's status line when `hansa up` "
              "started it: where the card says to ask a helper, "
              "make a task.\n>\n"
              "> Started by `hansa up`, it has these of the rules `hansa coordinator` gives its session: no pushing or "
              "publishing from its shell, no reading of the desk's `secrets/` and `.hansa/` or of the logins and keys "
              "under the home folder, no editing of the desk's files but `notes/` and `worktrees/`. Started otherwise and recorded "
              "with `hansa notify on`, it has none of them until `hansa up` starts it. In both cases it is not denied "
              "Read on the principal's list of what workers may not read, nor Edit in the lanes' repositories: the "
              "card's rules still hold.\n>\n"
              "> This card is for the session that coordinates that desk: the one hansa posts its notices to. "
              "Any other session started in this folder or below it, such as a worker a script starts or a session "
              "for other work, is not that session: ignore this card.\n\n")
    return header + re.sub(r"(?m)^@([^\s/~]\S*)[ \t]*$", imported, card)


def remember_copies(desk: Desk, paths: list) -> None:
    try:
        write_json(desk.state_dir / COPIES, sorted(set(paths)))
    except OSError:
        pass


def known_copies(desk: Desk) -> list:
    listed = read_json(desk.state_dir / COPIES, [])
    return [x for x in listed if isinstance(x, str)] if isinstance(listed, list) else []


def remove_copies(desk: Desk, keep: str = "", only: str = "") -> list:
    """Remove the copies of the card hansa wrote for this desk (a file with its header that git does not track), all
    but keep, or only the one at only: their paths."""
    gone = []
    for p in dict.fromkeys(known_copies(desk)):
        if only and p != only:
            continue
        if p != keep and copy_owner(p) == str(desk.root) and not tracked(git_top(Path(p).parent), p):
            try:
                os.unlink(p)
                gone.append(p)
            except OSError:
                continue
    remember_copies(desk, [p for p in known_copies(desk) if p == keep or copy_owner(p) == str(desk.root)])
    return gone


def write_copy(desk: Desk, c: dict) -> dict:
    """Put the card in the session's folder's .claude/rules, as card_check found it: the state after, 'copy' when
    written (with 'git': what git was told), else 'missing' with 'why'. Never a file git tracks."""
    if not card_copy_enabled(desk):
        return {**c, "state": "off"}
    path = Path(c["copy"])
    top = git_top(c["folder"])
    if tracked(top, path):
        return {**c, "state": "missing", "why": f"git tracks {path} in {top}, and hansa changes no tracked file"}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        new = copy_text(desk, path)
        if not (path.is_file() and path.read_text(encoding="utf-8", errors="replace") == new):
            write_text(path, new, follow_symlinks=False)
    except OSError as e:
        return {**c, "state": "missing", "why": f"{path} could not be written ({e.strerror or e})"}
    remember_copies(desk, [*known_copies(desk), str(path)])
    written = read_json(desk.state_dir / COPIES_WRITTEN, {})
    try:
        write_json(desk.state_dir / COPIES_WRITTEN, {**(written if isinstance(written, dict) else {}),
                                                     str(path): hashlib.sha256(new.encode("utf-8")).hexdigest()})
    except OSError:
        pass
    return {**c, "state": "copy", "git": exclude_copy(top, path) if top else ""}


def copy_as_written(desk: Desk, path) -> bool:
    """Whether the copy at path still holds what hansa wrote there: its last write, as recorded, or the copy hansa
    would write now (a copy written before hansa recorded its writes)."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return False
    written = read_json(desk.state_dir / COPIES_WRITTEN, {})
    if isinstance(written, dict) and written.get(str(path)) == hashlib.sha256(data).hexdigest():
        return True
    try:
        return data.decode("utf-8") == copy_text(desk, Path(path))
    except (OSError, UnicodeDecodeError):
        return False


def stale_copies(desk: Desk) -> list:
    """The copies of the card hansa wrote for this desk that are still there but no longer kept up to date: all of
    them but the one the session recorded with hansa notify on reads (card_check's 'copy')."""
    c = card_check(desk, read_coordinator(desk))
    keep = c.get("copy") if c.get("state") == "copy" else ""
    return [p for p in dict.fromkeys(known_copies(desk)) if p != keep and copy_owner(p) == str(desk.root)]


def stale_text(path: str, why: str = "") -> str:
    """What hansa coordinator's start and hansa status say of a copy stale_copies lists."""
    return (f"hansa's copy of the card at {path} is no longer kept up to date"
            f"{f' ({why})' if why else ''}: hansa leaves it there, and every Claude Code session started in its "
            "folder still reads it; remove it by hand.")


def place_card(desk: Desk, rec: dict) -> dict:
    """hansa notify on's part: the card copied for the recorded session when it does not read it, any earlier copy
    of this desk's elsewhere removed (in 'removed'). card_check's state after."""
    c = card_check(desk, rec)
    if c.get("state") in ("missing", "copy"):
        c = write_copy(desk, c)
    return {**c, "removed": remove_copies(desk, c.get("copy", "") if c.get("state") == "copy" else "")}


def refresh_copy(desk: Desk, starting: bool = False) -> list:
    """After hansa writes the desk's card: its copy rewritten to match. At hansa coordinator's start (starting), the
    copy is removed instead: the session it starts in the desk reads the card there, and from then on nothing keeps
    the copy up to date. One outside the desk only while it is as hansa wrote it; an edited one is left where it is.
    What the start says of it: lines."""
    c = card_check(desk, read_coordinator(desk))
    if c.get("state") != "copy":
        return []
    if not starting:
        write_copy(desk, c)
        return []
    if c["inside"]:  # the session in the desk would read both
        remove_copies(desk)
        return []
    if not copy_as_written(desk, c["copy"]):
        return [stale_text(c["copy"], "it was edited after hansa wrote it, and the session hansa coordinator starts "
                                      "reads the desk's card")]
    return [f"hansa removed its copy of the card, {p}: the session hansa coordinator starts reads the desk's card, "
            "and sessions started in that folder no longer read it." for p in remove_copies(desk, only=c["copy"])]


def card_lines(desk: Desk, rec: dict, c: dict = None) -> list:
    """For hansa notify on (c: what place_card did) and hansa explain notify: how the recorded session reads the
    card, or what it lacks to, if anything."""
    c = card_check(desk, rec) if c is None else c
    placed = "removed" in c
    removed = [f"hansa removed its earlier copy of the card, {p}." for p in c.get("removed") or []]
    if not c.get("state"):
        return removed
    if c["state"] == "off":
        return ["This session does not read the coordinator's card because the copy is turned off for this desk. "
                "hansa notify on --card turns it on.", *removed]
    then = "This takes effect from the session's next start; until then hansa help card prints the card."
    reach = (f"every Claude Code session started in {c['folder']}, or in a folder below it, reads that file at each "
             "start (hansa's own runs leave it out)")
    if c["state"] == "copy":
        kept = "hansa rewrites it when the card changes and removes it with hansa notify off."
        if not placed:
            return [f"This session reads the coordinator's card from {c['copy']}, hansa's copy of it: {reach}. {kept}"]
        return [f"This session does not read the coordinator's card ({c['card']}, the desk's CLAUDE.md) from the desk, "
                f"so hansa wrote a copy of it to {c['copy']}: {reach}. This session reads it from its next start; "
                "until then hansa help card prints the card.", f"{kept} {c.get('git') or ''}".strip(), *removed]
    if c["state"] == "external":
        return [f"The coordinator's card ({c['card']}) is imported by {c['file']}, from outside the session's folder "
                f"{c['folder']}: it loads only once Claude Code is allowed to load imports from outside the folder. "
                f"Claude Code asks at a start: \"{EXTERNAL_ASK}\". {then}", *removed]
    lacks = (f"This session does not read the coordinator's card ({c['card']}, the desk's CLAUDE.md): Claude Code loads "
             f"it for a session started in the desk or a folder below it, and this one runs in {c['folder']}, whose "
             "CLAUDE.md files do not import it. The card's rules (when to make a task instead of reading on, what never "
             "to do) are not in its context.")
    if c["state"] == "account":
        return [lacks, f"hansa puts no copy of the card in {c['copy']}: {c['why']}. Nor a line in a CLAUDE.md there: a "
                f"CLAUDE.md in {c['folder']} reaches every session started below it. Start the coordinator in a "
                "folder of its own, or with hansa coordinator.", *removed]
    why = (f"{c['copy']} holds a file without hansa's header, so hansa leaves it and writes no copy of the card there."
           if c["state"] == "foreign" else f"hansa wrote no copy of the card: {c['why']}." if c.get("why") else
           f"hansa notify on in it writes a copy of the card to {c['copy']}; or:")
    return [lacks, why,
            f"Add this line to {os.path.join(c['folder'], 'CLAUDE.md')}: {c['line']}",
            *([] if c["inside"] else
              ["The card is outside the session's folder: Claude Code asks once, at the session's next start, whether "
               f"to allow imports from outside the folder (\"{EXTERNAL_ASK}\"), and loads nothing from there until it "
               "is allowed."]),
            f"{then} hansa does not write the line: the folder is another project's.", *removed]


def card_note(desk: Desk) -> str:
    """For hansa status's hansa posts to line: the same state as card_lines, short; and any copy of the card hansa
    no longer keeps up to date that is still there."""
    return " · ".join([x for x in (_card_note(desk),) if x] + [stale_text(p) for p in stale_copies(desk)])


def _card_note(desk: Desk) -> str:
    c = card_check(desk, read_coordinator(desk))
    if not c:
        return ""
    if c["state"] == "off":
        return ("the recorded session does not read the card because the copy is turned off for this desk; "
                "hansa notify on --card turns it on")
    if c["state"] == "copy":
        return f"the recorded session reads the card from {c['copy']}, hansa's copy (hansa explain notify)"
    if c["state"] == "external":
        return "the recorded session reads the card once external imports are allowed (hansa explain notify)"
    if c["state"] == "account":
        return ("the recorded session does not read the card, and hansa puts no copy in its folder: rules there "
                "reach every session of the account (hansa explain notify)")
    return f"the recorded session does not read the card: add {c['line']} to its CLAUDE.md (hansa explain notify)"


def coordinator_line(desk: Desk) -> str:
    """For hansa status while no session hears from hansa: what hansa up does about the coordinator's session."""
    rec = read_coordinator(desk)
    pid = still_runs(rec)
    if pid:
        return (f"the coordinator's session pid {pid} still runs and does not hear from hansa: hansa notify on in it "
                "(hansa up starts no second one)")
    if rec.get("kind") not in ("coordinator", "notify", "unrecorded"):
        if new_desk(desk):
            return ""
        if none_yet(desk):
            return f"hansa up starts no coordinator's session: {NONE_YET}. {TO_RECORD_FIRST}"
        return f"hansa up starts no coordinator's session: {NOT_YET}. {TO_RECORD}"
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
    """Whether the pane ended: only one tmux says is there and not dead (0) runs. For a pane that ended and closed,
    tmux may say nothing and exit 0."""
    r = subprocess.run([*prefix, "display-message", "-p", "-t", pane, "#{pane_dead}"], env=env,
                       capture_output=True, text=True, timeout=5)
    return r.returncode != 0 or r.stdout.strip() != "0"


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
        print(f"coordinator: {NONE_YET}; nothing started. {TO_RECORD_FIRST}" if none_yet(desk) else
              f"coordinator: {NOT_YET}; nothing started. {TO_RECORD}")
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
    out = desk.state_dir / PANE_OUTPUT
    try:
        desk.state_dir.mkdir(mode=0o700, exist_ok=True)
        out.unlink(missing_ok=True)  # an earlier pane's: not this start's reason
        made = new_pane(prefix, session, "coordinator", plan["folder"], kept_pane(env, plan["cmd"], tmux, out), env)
        pane = (made.stdout or "").strip()
        if not pane:  # no pane: tmux did not start (it exits 0 when it cannot make its server's socket)
            raise subprocess.CalledProcessError(made.returncode, made.args, made.stdout,
                                                made.stderr or "tmux started no pane")
        end = time.monotonic() + COORDINATOR_WAIT
        while not pane_ended(prefix, pane, env):
            if time.monotonic() >= end:
                print(f"coordinator started in tmux, in {plan['folder']}: {shlex.join(plan['hand'])}")
                print(conversation_line(plan))
                print("attach: " + attach_command(tmux, server, session, env))
                return True
            time.sleep(0.25)
        said = pane_output(out)
        print(desk.redact(
            f"coordinator failed to start: its pane ended. Its last output (kept in {out}):\n"
            + "\n".join("  " + x for x in said) if said else
            "coordinator failed to start: its pane ended and left no output"))
        print(desk.redact(f"Run it by hand: {by_hand(plan)}"))
        if plan["conversation"]["how"] == "resume":  # its conversation may be gone though its record names it
            new = restart_plan(desk, rec, {"how": "new"})
            print(desk.redact(f"If Claude Code says no conversation was found with the id "
                              f"{plan['conversation']['id']}, start a new one instead: {by_hand(new)}"))
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
    desk.state_dir.mkdir(mode=0o700, exist_ok=True)
    # One hansa up at a time per desk: two at once would each start the server (tmux: "duplicate session: serve")
    # and the coordinator. The second waits for the first, then finds what it started
    with open(desk.state_dir / UP_LOCK, "a") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another hansa up is starting this desk's server and coordinator; waiting for it to finish.",
                  flush=True)
            fcntl.flock(held, fcntl.LOCK_EX)
        ok = server_up(desk)
        return coordinator_up(desk) and ok


def installation_up(command: str) -> bool:
    """Whether a saved hansa can start a desk: it is there, executable, and knows `up` (the check `listed_desk_up` makes)."""
    return bool(command and os.path.isfile(command) and os.access(command, os.X_OK)
                and subprocess.run([command, "up", "--help"], capture_output=True, text=True,
                                   timeout=15).returncode == 0)


def listed_problem(desk: Desk, rec: dict) -> str:
    """What hansa prints for a listed desk whose saved installation cannot start it."""
    last = {**(read_json(desk.state_dir / "serve-start.json", {}) or {}), "hansa": rec.get("hansa", "")}
    return ("Its hansa is missing or too old for up; nothing started.\n"
            "Last server command: " + shlex.join(serve_command(desk, last)))


def listed_desk_up(root, rec: dict, *, stream: bool = False) -> bool:
    """One listed desk, by its own installation. The principal's menu streams its output untouched."""
    print(f"Desk {root}:", flush=True)
    desk = Desk(root)
    last = read_json(desk.state_dir / "serve-start.json", {}) or {}
    last["hansa"] = rec.get("hansa", "")
    command = rec.get("hansa") or ""
    try:
        if not installation_up(command):
            print(listed_problem(desk, rec))
            return False
        r = subprocess.run([command, "--desk", str(root), "up"], capture_output=not stream,
                           text=True, timeout=None if stream else 30)
        if not stream:
            print(desk.redact((r.stdout + r.stderr).rstrip()))
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        detail = f"Could not run its hansa: {e}"
        print(detail if stream else desk.redact(detail))
        print("Last server command: " + shlex.join(serve_command(desk, last)))
        return False


def no_desk_text() -> str:
    """`hansa up` outside a desk where the menu cannot open: it starts nothing, and says which desks the account
    has and the two commands that start them."""
    out = ["hansa: no desk here. Run inside a desk, pass --desk DIR, or set HANSA_DESK.",
           "hansa up outside a desk opens the menu, which needs a terminal outside an agent's session; nothing was "
           "started."]
    roots = [str(root) for root, _ in listed_desks()]
    problem = desks_list_problem()
    if problem:
        out.append(problem)
    elif roots:
        out.append("The account's desks:")
        out.extend(f"  {root}" for root in roots)
    else:
        out.append("No desks on this account's list. A desk gets on it when its server starts once (hansa serve).")
    out.append("Start one with hansa --desk DIR up, or every desk with hansa up --all.")
    return "\n".join(out)


def all_desks_up() -> bool:
    found, ok = False, True
    for root, rec in listed_desks():
        if found:
            print()
        found = True
        ok = listed_desk_up(root, rec) and ok
    if not found and desks_list_problem():
        print(desks_list_problem())
        return False
    if not found:
        print("No desks on this account's list. A desk gets on it when its server starts once (hansa serve).")
    return ok
