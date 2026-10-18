"""Remember a server's start, and bring a desk back up after it has stopped."""
from __future__ import annotations

import fcntl
import os
import pwd
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import account, without_session
from .desk import Desk, now, program, read_json, write_json
from .run import server_state

DEFAULTS = {"host": "127.0.0.1", "port": 8765, "every": 3.0}
START_ENV = ("PATH", "LANG", "HANSA_CLAUDE", "HANSA_TMUX", "CLAUDE_CONFIG_DIR", "TMPDIR",
             "XDG_RUNTIME_DIR", "XDG_STATE_HOME", "TMUX_TMPDIR")
LOG_LIMIT = 20 * 1024 * 1024
START_WAIT = 8.0


def hansa_command() -> str:
    return str(Path(__file__).resolve().parent.parent / "bin" / "hansa")


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
    listed.parent.mkdir(parents=True, exist_ok=True)
    # Different desks may start together. Lock the stable inode, replace just the JSON.
    with open(listed.with_suffix(".lock"), "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        entries = read_json(listed, [])
        entries = [x for x in entries if isinstance(x, dict) and x.get("desk") != str(desk.root)] \
            if isinstance(entries, list) else []
        entries.append({"desk": str(desk.root), "hansa": rec["hansa"], "at": rec["at"]})
        write_json(listed, entries)


def serve_command(desk: Desk, rec: dict) -> list:
    opts = {**DEFAULTS, **rec.get("options", {})}
    return [rec.get("hansa") or hansa_command(), "--desk", str(desk.root), "serve",
            *[str(x) for k in DEFAULTS for x in ("--" + k, opts[k])]]


def clean_env(desk: Desk, recorded: dict) -> dict:
    pw = pwd.getpwuid(os.getuid())
    env = {"HOME": os.environ.get("HOME", pw.pw_dir), "USER": os.environ.get("USER", pw.pw_name),
           "LOGNAME": os.environ.get("LOGNAME", pw.pw_name), "SHELL": os.environ.get("SHELL", pw.pw_shell),
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


def launch_tmux(desk: Desk, rec: dict, cmd: list, env: dict, tmux: str, log: Path) -> str:
    saved = rec.get("tmux") or {}
    server = saved.get("server") or ["-L", "hansa-" + (re.sub(r"[^A-Za-z0-9_-]", "-", desk.root.name) or "desk")]
    session = saved.get("session") or "serve"
    prefix = [tmux, *server]
    # An existing tmux server has its own environment. Clear that too, keeping only tmux's pane identity.
    pane_env = shlex.join(["/usr/bin/env", "-i", *[f"{k}={v}" for k, v in env.items()]])
    pane_env += ' TMUX="$TMUX" TMUX_PANE="$TMUX_PANE" TERM="$TERM"'
    pane_env += ' TERM_PROGRAM="${TERM_PROGRAM-}" COLORTERM="${COLORTERM-}"'
    script = shlex.join(cmd) + " 2>&1 | /usr/bin/tee -a " + shlex.quote(str(log))
    pane = "exec " + pane_env + " /bin/sh -c " + shlex.quote(script)
    exists = subprocess.run([*prefix, "has-session", "-t", "=" + session], env=env, cwd=desk.root,
                            capture_output=True, text=True, timeout=5)
    create = ["new-window", "-d", "-t", "=" + session, "-n", "serve"] if exists.returncode == 0 else \
        ["new-session", "-d", "-s", session]
    subprocess.run([*prefix, *create, "-c", str(desk.root), pane], env=env, cwd=desk.root,
                   capture_output=True, text=True, timeout=10, check=True)
    attach = [*prefix, "attach-session", "-t", "=" + session]
    if env.get("TMUX_TMPDIR") and server[0] == "-L":
        attach = ["env", "TMUX_TMPDIR=" + env["TMUX_TMPDIR"], *attach]
    return shlex.join(attach)


def server_up(desk: Desk) -> bool:
    theirs = account.desk_account(desk.root)
    if theirs:
        command = shlex.join([hansa_command(), "--desk", str(desk.root), "up"])
        print(f"This desk belongs to {theirs}; run as {theirs}: {command}")
        return False
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


def desk_up(desk: Desk) -> bool:
    """The desk's startup sequence. Coordinator startup will follow the server here."""
    return server_up(desk)


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
