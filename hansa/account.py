"""On a Linux server the desk may belong to an ordinary account of its own while the coordinator
stays root: root installs software and sets up the machine, and Claude Code's sandbox, which every
worker runs in, cannot start as root. Then every `hansa` command but `coordinator` runs itself again
as the desk's account, so the desk, the lanes' repositories and the provider logins stay the account's.
A desk owned by the user who runs hansa (every desk on macOS) changes nothing here.
"""
from __future__ import annotations

import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

LINUX = sys.platform.startswith("linux")
PATH_OPTIONS = ("--desk", "--repo")  # made full paths before switching: the account starts elsewhere
DUBIOUS = re.compile(r"dubious ownership in repository at '([^']+)'")


def uid() -> int:
    """The user hansa runs as (tests replace this, since they cannot run as root)."""
    return os.getuid()


def owner(path) -> int:
    """Who owns this path (tests replace this too)."""
    return os.stat(path).st_uid


def name_of(u: int) -> str:
    try:
        return pwd.getpwuid(u).pw_name
    except KeyError:
        return f"user id {u}"


def desk_account(root) -> str:
    """The account whose desk this is, when hansa runs as root on Linux and that is another account;
    '' when hansa runs as whoever runs it."""
    if not LINUX or uid() != 0:
        return ""
    o = owner(root)
    return "" if o == 0 else name_of(o)


def runuser_bin() -> str:
    for c in (shutil.which("runuser"), "/usr/sbin/runuser", "/sbin/runuser"):
        if c and os.access(c, os.X_OK):
            return c
    return ""


def can_enter(path: Path, account: str) -> bool:
    """Whether the account may enter every folder down to this one, by their permission bits."""
    pw = pwd.getpwnam(account)
    groups = set(os.getgrouplist(account, pw.pw_gid))
    for d in [*reversed(path.parents), path]:
        try:
            st = os.stat(d)
        except OSError:
            return False
        bit = stat.S_IXUSR if st.st_uid == pw.pw_uid else stat.S_IXGRP if st.st_gid in groups else stat.S_IXOTH
        if not st.st_mode & bit:
            return False
    return True


def switch_args(argv: list, desk_root: Path) -> list:
    """The same arguments, as the account needs them: the desk named, path options as full paths,
    and `--file -` for a --file, whose text root reads and hands over on standard input (root can
    read files the account cannot, and an argument would show the text to every user)."""
    out, i, rest = [], 0, []
    j = 0  # `hansa git LANE ...`: what follows the word git is git's, and stays as it is
    while j < len(argv) and argv[j].startswith("-"):
        j += 1 if "=" in argv[j] or argv[j] not in PATH_OPTIONS + ("--desk",) else 2
    if j < len(argv) and argv[j] == "git":
        argv, rest = argv[:j + 1], argv[j + 1:]
    elif "--" in argv:
        argv, rest = argv[:argv.index("--")], argv[argv.index("--"):]
    while i < len(argv):
        x = argv[i]
        opt, eq, val = x.partition("=")
        if opt == "--file":
            out.append("--file=-")
            i += 1 if eq else 2
            continue
        if opt in PATH_OPTIONS:
            if not eq and i + 1 < len(argv):
                val, i = argv[i + 1], i + 1
            out.append(f"{opt}={os.path.abspath(val)}")
        else:
            out.append(x)
        i += 1
    if not any(x.startswith("--desk=") for x in out):
        out.insert(0, f"--desk={desk_root}")
    return out + rest


def _as(account: str, args: list, desk_root: Path) -> tuple:
    """What runs `hansa ARGS` as the account, with the same environment (runuser sets HOME, USER and
    LOGNAME for it), and the folder to run it in: the current one, unless the account cannot enter it."""
    runuser = runuser_bin()
    if not runuser:
        raise SystemExit(f"hansa: this desk belongs to the account {account}, and hansa runs its commands as that "
                         f"account; that needs runuser (part of util-linux), which is not on this machine. Install "
                         f"util-linux, or run hansa as {account} yourself")
    env = dict(os.environ)
    here = str(Path(__file__).resolve().parent.parent)  # this hansa, whatever the account's PATH holds
    env["PYTHONPATH"] = here + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    cwd = os.getcwd() if can_enter(Path.cwd(), account) else str(desk_root)
    return [runuser, "-u", account, "--", sys.executable, "-m", "hansa", *args], env, cwd


def switch(account: str, argv: list, desk_root: Path) -> None:
    """Run this hansa command again as the desk's account, in its place. Does not return."""
    cmd, env, cwd = _as(account, switch_args(argv, desk_root), desk_root)
    os.chdir(cwd)
    os.execve(cmd[0], cmd, env)


def run(account: str, argv: list, desk_root: Path, **kw):
    """Run a hansa command as the desk's account as a child, and wait for it (subprocess.run's keywords:
    input, capture_output, text ...)."""
    cmd, env, cwd = _as(account, switch_args(argv, desk_root), desk_root)
    return subprocess.run(cmd, env=env, cwd=cwd, **kw)


def foreign_repo(repo) -> str:
    """Why hansa cannot work in this repository because another user owns it; '' when it can."""
    try:
        o = owner(repo)
    except OSError:
        return ""
    if o == uid():
        return ""
    me = name_of(uid())
    return (f"{repo} belongs to {name_of(o)}, not to {me}, whom hansa runs as, so git refuses to work in it "
            f"(\"dubious ownership\"). Give it to {me}: chown -R {me} {repo}")


def git_refusal(text: str, cwd) -> str:
    """The plain message for git's "dubious ownership" refusal in this text; '' when it is not one."""
    m = DUBIOUS.search(text)
    if not m:
        return ""
    return foreign_repo(m.group(1)) or foreign_repo(cwd) or (
        f"git refuses to work in {m.group(1)}: another user owns it. Give it to {name_of(uid())}: "
        f"chown -R {name_of(uid())} {m.group(1)}")
