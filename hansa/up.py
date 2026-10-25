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
from .desk import Desk, _desks_entries, desks_file, desks_list_problem, listed_desks, now, program, read_json, write_json, write_text
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
            entries, problem = _desks_entries()
            if problem:  # rewriting it would drop the other desks and let runs start with nothing of them hidden
                print(desk.redact(f"hansa serve: left the account's list of desks as it is: {problem}"),
                      file=sys.stderr, flush=True)
                return
            entries = [x for x in entries if isinstance(x, dict) and x.get("desk") != str(desk.root)]
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
           "session": {"pid": os.getpid(), "start": proc_start(os.getpid())}}
    # the words kept: those --continue replaces (as --resume <its id>) are not recorded, so they refuse nothing
    return _keep(desk, rec, unsafe(desk, [clear, *rec["words"], str(desk.root)]))


def record_session(desk: Desk, sess: dict, argv: list = None, folder: str = None, rules: list = None) -> str:
    """How a session hansa did not start began, for hansa up: its claude's exact arguments and folder, read from
    its process unless given (notify-launch gives its own), tmux, and the launch settings of this command's
    environment, which it has from the session. rules: the deny rules notify-launch starts it with (launch_rules),
    kept in the record, which so says the session has them, and left out of its arguments. '' when recorded, else
    why not."""
    argv = proc_args(sess["pid"]) if argv is None else argv
    folder = proc_cwd(sess["pid"]) if folder is None else folder
    words = without_rules(unpicked(argv[1:]), rules or []) if argv else []
    rec = {"kind": "notify", "hansa": hansa_command(), "argv": argv and [argv[0], *words],
           "folder": folder, "tmux": record_tmux(), "env": start_env(desk, os.environ), "session": sess,
           **({"launch_rules": rules} if rules is not None else {})}
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
    why = record_session(desk, sess, rules=rules)
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


def exclude_copy(top: str, path: Path) -> str:
    """Keep the copy out of the repository whose work tree it is in: its path in that repository's info/exclude,
    once, and not when git already ignores it. What was done, as a sentence."""
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(top))
    r = _git(top, "check-ignore", "-q", "--", rel)
    if r and r.returncode == 0:
        return f"git in {top} ignores it."
    r = _git(top, "rev-parse", "--git-path", "info/exclude")
    if not r or r.returncode or not r.stdout.strip():
        return f"git in {top} lists it, as the repository's info/exclude could not be found: commit it nowhere."
    exclude = Path(top, r.stdout.strip())
    line = "/" + re.sub(r"([*?\[\\])", r"\\\1", rel.replace(os.sep, "/"))
    try:
        text = exclude.read_text(encoding="utf-8", errors="replace") if exclude.is_file() else ""
        if line not in text.splitlines():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with open(exclude, "a", encoding="utf-8") as f:
                f.write(("\n" if text and not text.endswith("\n") else "") + line + "\n")
    except OSError as e:
        return f"git in {top} lists it, as {exclude} could not be written ({e.strerror or e}): commit it nowhere."
    return f"hansa added it to {exclude}, so git in {top} leaves it out."


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
              "reviewer, operator) and the coordinator's mod are not in it: where the card says to ask a helper, "
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


def remove_copies(desk: Desk, keep: str = "") -> list:
    """Remove the copies of the card hansa wrote for this desk (a file with its header that git does not track), all
    but keep: their paths."""
    gone = []
    for p in dict.fromkeys(known_copies(desk)):
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
    return {**c, "state": "copy", "git": exclude_copy(top, path) if top else ""}


def place_card(desk: Desk, rec: dict) -> dict:
    """hansa notify on's part: the card copied for the recorded session when it does not read it, any earlier copy
    of this desk's elsewhere removed (in 'removed'). card_check's state after."""
    c = card_check(desk, rec)
    if c.get("state") in ("missing", "copy"):
        c = write_copy(desk, c)
    return {**c, "removed": remove_copies(desk, c.get("copy", "") if c.get("state") == "copy" else "")}


def refresh_copy(desk: Desk, starting: bool = False) -> None:
    """After hansa writes the desk's card: its copy rewritten to match. At hansa coordinator's start (starting), a
    copy in the desk's folder or above it is removed instead: the session it starts in the desk would read both."""
    c = card_check(desk, read_coordinator(desk))
    if c.get("state") != "copy":
        return
    if starting and c["inside"]:
        remove_copies(desk)
    else:
        write_copy(desk, c)


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
    """For hansa status's hansa posts to line: the same state as card_lines, short."""
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
    found, ok = False, True
    for root, rec in listed_desks():
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
    if not found and desks_list_problem():
        print(desks_list_problem())
        return False
    if not found:
        print("No desks on this account's list. A desk gets on it when its server starts once (hansa serve).")
    return ok
