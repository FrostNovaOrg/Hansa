"""hansa: a small, file-based desk for a team of Claude Code agents.

One directory (the desk) holds the settings, the keys, the tasks and an
append-only event log. The CLI, the web page and the agents all read the
same files, so nobody has to read this code to know what is going on.
"""

import fnmatch
import functools
import os
import subprocess
import time
from pathlib import Path

__version__ = "0.5.0a1"
_handed = os.environ.pop("HANSA_BUILD", "")  # root's answer to build(), read once and not passed on to what this starts

# What carries a Claude Code session's identity or messaging into a process started from it (its Bash tool, a
# tmux it opened, the ! prefix): with the inbox's socket and token a process can post into other sessions as that
# session. hansa takes every one of them out of its own environment as it starts, so nothing it starts (a release's
# command, the alert command, a design session, the preview, the account's hansa, git and its hooks ...) gets
# them, and keeps them here (SESSION) for what hansa's notifying needs, where it needs it (notify.py): the command
# run from the session reads CLAUDE_PID, to record which session asked, and CLAUDE_CODE_MESSAGING_SOCKET, the
# inbox `hansa notify on` gives its follower on the command line; the account's hansa is given CLAUDE_PID alone
# (account.py). By name, and by rule: every CLAUDE_CODE_MESSAGING_*, CLAUDE_CODE_SESSION_* and CLAUDE_CODE_CHILD_*.
# Not CLAUDE_ENV_FILE: hansa gives its workers one of its own, the lane's env (run.py), through hansa's own process.
SESSION_NAMES = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_EFFORT",
                 "CLAUDE_AGENT_SDK_VERSION", "AI_AGENT")
SESSION_PREFIXES = ("CLAUDE_CODE_MESSAGING_", "CLAUDE_CODE_SESSION_", "CLAUDE_CODE_CHILD_")


def session_var(name: str) -> bool:
    """Whether this environment variable carries a Claude Code session's identity or messaging."""
    return name in SESSION_NAMES or name.startswith(SESSION_PREFIXES)


def without_session(env) -> dict:
    """A copy of this environment without a Claude Code session's variables."""
    return {k: v for k, v in env.items() if not session_var(k)}


SESSION = {k: os.environ.pop(k) for k in [k for k in os.environ if session_var(k)]}


def session_vars() -> list:
    """The names of the session's variables hansa was started with (or that were set in its environment since),
    sorted: none outside a Claude Code session."""
    return sorted({*SESSION, *(k for k in os.environ if session_var(k))})


def session_value(name: str) -> str:
    """One of the session's variables, as hansa was started with it."""
    return os.environ.get(name) or SESSION.get(name, "")


@functools.lru_cache(maxsize=1)
def build() -> str:
    """This version and which code it is: hansa/BUILD when an install wrote one, else the git commit. As
    another account (root's hansa run again as the desk's, account.py) git would refuse a checkout it does not
    own, so it is told this one is safe, and root's hansa hands its own answer over (HANSA_BUILD) as well."""
    if _handed.startswith(f"{__version__} "):
        return _handed
    here = Path(__file__).resolve().parent
    try:
        return f"{__version__} {(here / 'BUILD').read_text(encoding='utf-8').strip()}".strip()
    except OSError:
        pass
    try:
        sha = subprocess.run(["git", "-c", f"safe.directory={here.parent}", "-C", str(here), "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = ""
    return f"{__version__} {sha}".strip()


SHIPPED = ("card.md", "ui.html", "no-signature")  # hansa's own files besides its Python sources and agents/*.md
# The coordinator's mod (plugin.py): its manifest, hooks file and script, which the session loads, and its own tests,
# which Claude Code's test kit runs only from inside the mod's folder (claude plugin test hansa/mod)
MOD_SHIPPED = ("mod/.claude-plugin/plugin.json", "mod/hooks/hooks.json", "mod/hooks/*.js", "mod/tests/*.test.ts")


def shipped(rel: Path) -> bool:
    """Whether a file at this path inside hansa/ is one of hansa's own: what an install puts in place."""
    if "__pycache__" in rel.parts or rel.name.startswith("._"):  # ._name: macOS's AppleDouble file beside a copy
        return False
    if len(rel.parts) == 1:
        return rel.suffix == ".py" or rel.name in SHIPPED
    if rel.parts[0] == "mod":  # not what Claude Code writes there as it loads it (its types, a tsconfig.json)
        return any(len(rel.parts) == x.count("/") + 1 and fnmatch.fnmatchcase(rel.as_posix(), x) for x in MOD_SHIPPED)
    return len(rel.parts) == 2 and rel.parts[0] == "agents" and rel.suffix == ".md"


@functools.lru_cache(maxsize=1)
def installed() -> float:
    """When the code running here was put in place: when hansa/BUILD was written where an install wrote one, else
    when hansa's newest own file last changed (a checkout's commit time says when it was committed, not when it was
    put in place): its Python sources and the other files it ships (shipped), not a .DS_Store or an editor's
    backup left beside them. Asked once: hansa serve asks as it starts,
    so it keeps the answer for the code it loaded (run.serve_code). 0 when unknown."""
    here = Path(__file__).resolve().parent
    try:
        return (here / "BUILD").stat().st_mtime
    except OSError:
        pass
    latest = 0.0
    for f in here.rglob("*"):
        if not shipped(f.relative_to(here)):
            continue
        try:
            if f.is_file():
                latest = max(latest, f.stat().st_mtime)
        except OSError:
            continue
    return latest


def installed_text(at: float = None) -> str:
    """'installed 2026-09-29 14:03', to the minute, in local time; '' when unknown. By default this code's."""
    at = installed() if at is None else at
    return time.strftime("installed %Y-%m-%d %H:%M", time.localtime(at)) if at else ""
