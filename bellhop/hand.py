"""The hand-window gate: the coordinator does not edit a lane repository unnoticed.

The coordinator's job is to dispatch. On a production desk it also, quietly,
edited the product repositories itself - a `sed` here, a one-line fix there -
and none of it reached any receipt, ledger entry or diff review. **An edit that
went through a worker and an edit the coordinator made by hand look the same in
the repository afterwards**; the only place they differ is in whether anyone
recorded it. So it was never recorded.

Reminding did not work. A hook that printed a warning on every hand edit fired
hundreds of times and changed nothing. What worked was blocking once: the
first write into a lane repository is refused, with the command to open a window
in the refusal. Declaring the intent takes one line; after that the window is
open and writes are allowed and logged.

The shape:

* **A window is a declaration** (`state/.hand-window`): minutes, and a reason.
* **The ledger is the record** (`state/hand-ledger.md`, append-only): what was
  opened, what was actually written, what was blocked. The two are deliberately
  separate - a window that says three edits are coming and a ledger that records
  three edits are not the same claim, and only the second one is evidence.
* **An expired window counts as closed.** A window closed on time and a window
  left open and forgotten look alike in the sentence "there was a window".
* **Reads are never gated.** `Read`, `Grep` and `Glob` cannot change a repository, and
  a `Bash` call is judged by what it would *write*, not by the paths it names: the
  first version blocked eleven reads on one desk and caught no write at all.
* **A broken gate never blocks.** If this module raises, the caller logs the
  failure and lets the tool through: a command stopped by a bug in the gate and a
  command stopped by a real breach look the same from the coordinator's seat, and
  only one of them is worth a stopped session.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .locking import serialized, soft_serialized
from .config import Desk
from .util import BellhopError, append, read, write

WINDOW = "state/.hand-window"
LEDGER = "state/hand-ledger.md"

LEDGER_HEADER = (
    "# Hand ledger (append-only; written by `bellhop hand` and the PreToolUse gate, never by hand)\n"
    "(one line per event, all starting `- `:\n"
    "  `- opened: <time> | <N> min | <reason>`\n"
    "  `- closed: <time> | <N> min used | <M> written[ | expired]`\n"
    "  `- written: <time> | <tool> | <path> | <command head> | open-window <verb>`\n"
    "  `- blocked: <time> | <tool> | <path> | <command head> | block-write <verb>`\n"
    "  `- allow-read: <time> | <tool> | <path> | <command head>`\n"
    "  `- role-refused: <time> | <tool> | <path> | <command head> | <why>`\n"
    "  `- role-open: <time> | <tool> | <path> | <command head> | <why>`\n"
    "The three gate decisions are `allow-read`, `block-write` (`blocked`) and\n"
    "`open-window` (`written`); the `role-` rows are the coordinator role gate.\n"
    "A window is the declaration; this file is the record. They are separate on purpose.)\n"
)

# Tools that can change a repository. `Read`, `Grep` and `Glob` are not here and
# are never gated, even though `Read` also carries a `file_path`.
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash")
READ_ONLY = ("Read", "Grep", "Glob", "NotebookRead", "WebFetch", "WebSearch", "TodoWrite", "Task")

COMMAND_HEAD_CHARS = 60
_CD = re.compile(r"(?:^|[;&|]\s*)cd\s+(?P<dir>(?:\"[^\"]*\"|'[^']*'|[^\s;&|]+))")
_ABS = re.compile(r"(?<![\w~])(/[A-Za-z0-9._~+@/\-]*)")
_HOME = re.compile(r"(?:^|(?<=[\s:=\"'(]))(?:~|\$HOME|\$\{HOME\})(?=/|\s|$)")


# -- paths -----------------------------------------------------------------
def window_path(desk: Desk) -> Path:
    return desk.path(WINDOW)


def ledger_path(desk: Desk) -> Path:
    return desk.path(LEDGER)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_ledger(desk: Desk) -> Path:
    p = ledger_path(desk)
    if not p.exists():
        write(p, LEDGER_HEADER)
    return p


# The `PreToolUse` gate writes through here, inside the coordinator's turn, so this
# one waits two seconds and then appends anyway rather than holding the turn open.
@soft_serialized("hand.note")
def note(desk: Desk, line: str) -> None:
    """Append one row. Append-only: this file is never rewritten."""
    append(_ensure_ledger(desk), "- " + line + "\n")


def rows(desk: Desk) -> List[str]:
    p = ledger_path(desk)
    return [l for l in read(p).splitlines() if l.startswith("- ")] if p.exists() else []


def note_failure(desk: Desk, exc: BaseException) -> None:
    """A gate that broke leaves a row. A missed hit and a hit that never happened
    look the same in an empty ledger."""
    note(desk, "gate-failed: %s | %s" % (_now(), type(exc).__name__))


# -- the window ------------------------------------------------------------
def window(desk: Desk) -> Dict[str, object]:
    """`state` is one of none / open / expired.

    An unreadable window file counts as **open**: refusing to read a file is not
    evidence of a breach, and a gate that blocks on its own confusion is worse
    than a gate that lets one edit through.
    """
    p = window_path(desk)
    if not p.exists():
        return {"state": "none"}
    data: Dict[str, str] = {}
    try:
        for line in read(p).splitlines():
            key, sep, value = line.partition(": ")
            if sep:
                data[key.strip()] = value.strip()
        opened_epoch = float(data["opened_epoch"])
        minutes = int(data.get("minutes", "30"))
    except (OSError, KeyError, ValueError):
        return {"state": "open", "reason": "(window file unreadable; counted as open)"}
    expires = opened_epoch + minutes * 60
    return {
        "state": "expired" if time.time() > expires else "open",
        "opened": data.get("opened", ""),
        "opened_epoch": opened_epoch,
        "minutes": minutes,
        "reason": data.get("reason", ""),
        "ledger_lines": int(data.get("ledger_lines", "0") or 0),
        "remaining_s": max(0, int(expires - time.time())),
    }


@serialized
def open_window(desk: Desk, minutes: Optional[int] = None, reason: str = "") -> str:
    reason = (reason or "").strip()
    if not reason:
        raise BellhopError('a window needs a reason: bellhop hand open --minutes 30 --reason "..."')
    if minutes is None:
        minutes = desk.cfg.get("hand_window_minutes", 30)
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise BellhopError("--minutes takes a whole number of minutes")
    if minutes <= 0:
        raise BellhopError("--minutes must be positive")
    current = window(desk)
    closed_note = ""
    if current["state"] == "open":
        raise BellhopError(
            "a hand window is already open (%s, %ss left); close it first with `bellhop hand close`"
            % (current.get("reason") or "no reason recorded", current.get("remaining_s"))
        )
    if current["state"] == "expired":
        closed_note = close_window(desk) + "\n"
    _ensure_ledger(desk)
    started = len(rows(desk))
    now, epoch = _now(), time.time()
    write(
        window_path(desk),
        "opened: %s\nopened_epoch: %d\nminutes: %d\nreason: %s\nledger_lines: %d\n"
        % (now, epoch, minutes, reason, started),
    )
    note(desk, "opened: %s | %d min | %s" % (now, minutes, reason))
    return closed_note + "hand window open for %d minutes: %s" % (minutes, reason)


@serialized
def close_window(desk: Desk) -> str:
    """No window is not an error. It is also not a reason to write a closing row."""
    p = window_path(desk)
    current = window(desk)
    if current["state"] == "none":
        return "no hand window open; nothing closed and nothing written to the ledger"
    _ensure_ledger(desk)
    started = int(current.get("ledger_lines") or 0)
    written = len([l for l in rows(desk)[started:] if l.startswith("- written:")])
    epoch = current.get("opened_epoch")
    used = max(0, int((time.time() - float(epoch)) // 60)) if epoch else 0
    expired = current["state"] == "expired"
    note(desk, "closed: %s | %d min used | %d written%s" % (_now(), used, written, " | expired" if expired else ""))
    try:
        p.unlink()
    except OSError as exc:
        raise BellhopError("could not remove %s: %s" % (p, exc))
    return "hand window closed: %d min used, %d write(s) recorded%s" % (used, written, " (it had expired)" if expired else "")


def status_text(desk: Desk) -> str:
    current = window(desk)
    if current["state"] == "none":
        return "hand window: none open. Lane repositories are gated; the first write is refused.\n"
    lines = ["hand window: %s" % current["state"]]
    if current.get("reason"):
        lines.append("  reason: %s" % current["reason"])
    if current.get("opened"):
        lines.append("  opened: %s (%s min)" % (current["opened"], current.get("minutes")))
    if current["state"] == "open":
        lines.append("  %ss left" % current.get("remaining_s"))
    else:
        lines.append("  expired, so it counts as closed; run `bellhop hand close` to record it")
    return "\n".join(lines) + "\n"


# -- what counts as a lane repository --------------------------------------
def lane_roots(desk: Desk) -> List[Tuple[str, str]]:
    """(lane name, real path) for every lane repository and lane worktree.

    A worktree is part of the repository it came from: a hand edit in
    `.worktrees/api` reaches the product through `bellhop merge` exactly like one
    made in the repository itself.
    """
    out: List[Tuple[str, str]] = []
    for name in desk.lane_names():
        try:
            lane = desk.lane(name)
        except BellhopError:
            continue
        for key in ("repo", "worktree"):
            value = lane.get(key) or ""
            if value:
                out.append((name, os.path.realpath(value)))
    return out


def _expand(text: str) -> str:
    return _HOME.sub(os.path.expanduser("~"), text or "")


def _unquote(word: str) -> str:
    if len(word) >= 2 and word[0] == word[-1] and word[0] in "\"'":
        return word[1:-1]
    return word


def _resolve(candidate: str, cwd: str) -> str:
    candidate = _expand(_unquote(candidate))
    if not candidate:
        return ""
    if not os.path.isabs(candidate):
        candidate = os.path.join(cwd or os.getcwd(), candidate)
    return os.path.realpath(candidate)


def mentions(tool: str, tool_input: dict, cwd: str) -> List[str]:
    """Every path this call names, resolved. A mention is not an intention to write."""
    return targets(tool, tool_input, cwd)


def targets(tool: str, tool_input: dict, cwd: str) -> List[str]:
    """Every path this call names, resolved to a real absolute path.

    This is the conservative scanner: it says a lane was *mentioned*, which is what
    the log needs to show that a read was seen and let through. What a call would
    *write* is `write_targets` below, and the two are deliberately different
    questions -- a gate that could not tell them apart blocked eleven reads in a row
    on one desk and caught no write at all.
    """
    tool_input = tool_input or {}
    cwd = _expand(cwd or "")
    if tool != "Bash":
        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return [_resolve(raw, cwd)] if raw else []
    command = _expand(" ".join((tool_input.get("command") or "").split()))
    if not command:
        return []
    found: List[str] = []
    for m in _CD.finditer(command):
        found.append(_resolve(m.group("dir"), cwd))
    for m in _ABS.finditer(command):
        found.append(os.path.realpath(m.group(1)))
    return [f for f in found if f]


def _inside(path: str, root: str) -> bool:
    return bool(path) and (path == root or path.startswith(root.rstrip(os.sep) + os.sep))


def _allowed_command(desk: Desk, command: str) -> bool:
    """Commands with a record of their own. `bellhop merge` writes to a lane by design."""
    words = command.split()
    if not words:
        return True
    first = os.path.basename(words[0])
    return first in tuple(desk.cfg.get("hand_allow_commands") or ())


# -- write intent ----------------------------------------------------------
# A gate that fires on the name of a lane is not a gate on writing into it. On one
# desk this hook blocked eleven times in a fortnight and **all eleven were reads**
# -- `rg`, `ls`, `find`, `cat`, `git status`, `bash -n` -- that happened to carry a
# lane path as an argument. Not one hand-write was caught, and the coordinator lost
# the evidence it was reading for. So the question here is not "does this command
# name a lane" but "does this command write", and the answer is only yes when
# something in it actually writes: a redirection onto a file, a command whose job is
# to change a file, or a git subcommand that moves the tree or the branch.
#
# The default for everything else is **pass**. That is a deliberate hole -- a script
# or an interpreter running a file it was handed can write anywhere and this
# classifier will not see it -- and it is the right side to err on: a false block
# costs the desk a turn and teaches everyone to keep a window open, which is the
# habit that made the first version of this hook useless.

# Commands whose purpose is to change a file. Matched on the basename, so
# `/usr/bin/rm` and `rm` are the same word.
WRITE_COMMANDS = frozenset({
    "tee", "mv", "cp", "rm", "rmdir", "unlink", "shred", "patch", "chmod", "chown",
    "chgrp", "touch", "mkdir", "install", "ln", "truncate", "dd", "rsync", "tar",
    "unzip", "zip", "gzip", "gunzip", "mktemp", "chflags", "xattr",
    # Editors. An editor opened on a lane file is a hand edit by definition.
    "vi", "vim", "nvim", "nano", "pico", "emacs", "ed", "ex", "micro", "joe", "kak",
    "hx", "helix", "code", "subl", "mate", "open",
})

# Readers. Anything unrecognised passes anyway, so this list changes nothing on its
# own; it is consulted first so that a name can never sit in both lists and have the
# write side win quietly. These are the commands the eleven false blocks were made of.
READ_COMMANDS = frozenset({
    "cat", "ls", "rg", "grep", "egrep", "fgrep", "find", "head", "tail", "wc", "diff",
    "less", "more", "file", "stat", "du", "df", "pwd", "which", "type", "echo",
    "printf", "sort", "uniq", "cut", "tr", "awk", "jq", "sed", "column", "basename",
    "dirname", "realpath", "readlink", "test", "true", "false", "date", "env",
})

# git subcommands that move the tree, the index, the branches or the config.
GIT_WRITE = frozenset({
    "add", "commit", "push", "checkout", "reset", "rebase", "merge", "stash", "clean",
    "apply", "restore", "rm", "mv", "switch", "cherry-pick", "revert", "am", "pull",
    "fetch", "init", "clone", "gc", "prune", "update-ref", "update-index", "mergetool",
    "filter-branch", "bisect", "rerere", "replace", "sparse-checkout", "maintenance",
})
# git subcommands that only read. Anything in neither list counts as a write: git is
# how a tree changes, and an unknown git verb is not the place to guess.
GIT_READ = frozenset({
    "status", "log", "diff", "show", "blame", "rev-parse", "rev-list", "ls-files",
    "ls-tree", "ls-remote", "cat-file", "describe", "shortlog", "grep", "reflog",
    "count-objects", "symbolic-ref", "for-each-ref", "name-rev", "merge-base",
    "diff-tree", "diff-index", "whatchanged", "check-ignore", "check-attr", "var",
    "help", "version", "fsck", "verify-commit", "verify-tag", "annotate", "cherry",
})
# git subcommands that read or write depending on their arguments, and the words
# that make each one a write.
GIT_MIXED = {
    "branch": ("-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy",
               "--set-upstream-to", "--unset-upstream", "--edit-description"),
    "tag": ("-d", "-D", "--delete", "-a", "-s", "-f", "--force", "-m"),
    "remote": ("add", "remove", "rm", "rename", "set-url", "set-head", "set-branches", "prune"),
    "worktree": ("add", "remove", "move", "prune", "repair", "lock", "unlock"),
    "stash": ("push", "pop", "apply", "drop", "save", "clear", "store", "create", "branch"),
    "notes": ("add", "append", "copy", "edit", "remove", "prune"),
    "submodule": ("add", "update", "init", "deinit", "sync", "absorbgitdirs", "set-url"),
    "config": ("--add", "--unset", "--unset-all", "--replace-all", "--edit", "-e", "--rename-section", "--remove-section"),
}
GIT_READ_FLAGS = {
    "branch": ("--list", "-l", "-a", "-r", "-v", "-vv", "--all", "--show-current", "--contains", "--merged", "--no-merged"),
    "tag": ("-l", "--list", "-n", "--contains", "--points-at"),
    "config": ("--get", "--get-all", "--get-regexp", "--list", "-l", "--get-urlmatch"),
}

# Wrappers that stand in front of the command that matters.
WRAPPERS = frozenset({"env", "sudo", "doas", "nohup", "time", "command", "builtin",
                      "exec", "nice", "ionice", "stdbuf", "timeout", "xargs"})

INTERPRETERS = frozenset({"python", "python2", "python3", "perl", "ruby", "node", "php"})
SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish"})

# What makes a `-c` one-liner a writer rather than a printer.
_INLINE_WRITE = re.compile(
    r"""\.write(_text|_bytes)?\s*\(|open\s*\([^)]*['"][rbt+]*[wax]|>>?\s*['"]?/|"""
    r"""os\.(remove|unlink|rename|replace|mkdir|makedirs|rmdir|chmod|symlink|link|truncate)|"""
    r"""shutil\.(copy\w*|move|rmtree)|Path\([^)]*\)\.(write\w*|unlink|mkdir|rename|touch|replace)|"""
    r"""print\s*\([^)]*file\s*=\s*open|subprocess|os\.system|json\.dump\s*\(""",
    re.X,
)
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def split_heredocs(command: str) -> Tuple[List[str], Dict[int, str]]:
    """(the command's lines with heredoc bodies removed, body by header-line index).

    A heredoc body is data the command is fed, not a chain of commands. Left in, a
    packet body being written to a scratch file that *mentions* `rm -rf <lane>` reads
    exactly like a command that runs it. The header line stays, so `cat > file
    <<'EOF'` is still the redirection it is, and the body is handed back separately
    for the one reader that should see it: an interpreter told to run it.
    """
    lines = command.split("\n")
    out: List[str] = []
    bodies: Dict[int, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        here = len(out) - 1
        marks = [m.group(2) for m in _HEREDOC.finditer(line)]
        i += 1
        for mark in marks:
            body: List[str] = []
            while i < len(lines) and lines[i].strip() != mark:
                body.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1  # the terminator itself
            if body:
                bodies[here] = (bodies.get(here, "") + "\n" + "\n".join(body)).strip()
    return out, bodies


def strip_heredocs(command: str) -> str:
    """The command with its heredoc bodies removed."""
    lines, _bodies = split_heredocs(command)
    return "\n".join(lines)


def split_segments(command: str) -> List[str]:
    """Split a command line into the simple commands it runs, quotes respected.

    `&&`, `||`, `;`, `|`, `&`, a newline, a subshell and a command substitution all
    start a new segment. A classifier that read only the first word of the whole line
    would pass `rg foo /lane && rm -rf /lane`.
    """
    out: List[str] = []
    buf: List[str] = []
    quote: Optional[str] = None
    depth = 0  # open command substitutions, so `open(...)` in code stays one word
    i, n = 0, len(command or "")
    while i < n:
        c = command[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            buf.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            buf.append(c)
            buf.append(command[i + 1])
            i += 2
            continue
        if c == "$" and i + 1 < n and command[i + 1] == "(":
            out.append("".join(buf))
            buf = []
            depth += 1
            i += 2
            continue
        if c == ")" and depth:
            out.append("".join(buf))
            buf = []
            depth -= 1
            i += 1
            continue
        if c in "`\n;":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        if c in "&|":
            out.append("".join(buf))
            buf = []
            i += 2 if i + 1 < n and command[i + 1] == c else 1
            continue
        buf.append(c)
        i += 1
    out.append("".join(buf))
    return [s.strip() for s in out if s.strip()]


def lex(segment: str) -> Tuple[List[str], List[str]]:
    """(words, files written by a redirection) for one simple command.

    `2>&1` and `>&2` duplicate a descriptor and write no file; `> out` and `>> out`
    do. Telling those apart is what keeps `rg pattern /lane 2>/dev/null` a read.
    """
    words: List[str] = []
    redirects: List[str] = []
    buf: List[str] = []
    has = False
    pending = False
    quote: Optional[str] = None
    i, n = 0, len(segment or "")

    def flush() -> None:
        nonlocal buf, has, pending
        if has:
            word = "".join(buf)
            (redirects if pending else words).append(word)
            pending = False
        buf, has = [], False

    while i < n:
        c = segment[i]
        if quote:
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(segment[i + 1])
                has = True
                i += 2
                continue
            if c == quote:
                quote = None
                has = True
                i += 1
                continue
            buf.append(c)
            has = True
            i += 1
            continue
        if c in "'\"":
            quote = c
            has = True
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            buf.append(segment[i + 1])
            has = True
            i += 2
            continue
        if c.isspace():
            flush()
            i += 1
            continue
        if c == ">":
            if has and "".join(buf).isdigit():
                buf, has = [], False  # the file descriptor, not a word
            flush()
            i += 1
            if i < n and segment[i] == ">":
                i += 1
            while i < n and segment[i] in " \t":
                i += 1
            if i < n and segment[i] == "&":
                i += 1
                while i < n and not segment[i].isspace():
                    i += 1
                continue
            pending = True
            continue
        if c == "<":
            flush()
            i += 1
            if i < n and segment[i] == "<":
                i += 1
            continue
        buf.append(c)
        has = True
        i += 1
    flush()
    return words, redirects


def _git_verb(args: List[str]) -> Optional[str]:
    """The git subcommand's verdict, or None when it only reads."""
    rest = list(args)
    while rest and rest[0].startswith("-"):
        opt = rest.pop(0)
        if opt in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path") and rest:
            rest.pop(0)
    if not rest:
        return None
    sub = rest[0]
    tail = rest[1:]
    if sub in GIT_READ:
        return None
    if sub in GIT_WRITE:
        return "git " + sub
    if sub in GIT_MIXED:
        if any(flag in tail for flag in GIT_READ_FLAGS.get(sub, ())):
            return None
        if any(word in GIT_MIXED[sub] for word in tail):
            return "git " + sub
        if sub in ("branch", "tag") and [w for w in tail if not w.startswith("-")]:
            return "git " + sub
        return None
    return "git " + sub  # an unknown git verb is not the place to guess


def _sed_in_place(args: List[str]) -> bool:
    for a in args:
        if a == "--in-place" or a.startswith("--in-place="):
            return True
        if a.startswith("-") and not a.startswith("--") and "i" in a.split("=")[0]:
            return True
    return False


INLINE = " inline"


def write_verb(words: List[str], raw: str = "") -> Optional[str]:
    """What makes this simple command a write, or None if it only reads.

    None is the default: this returns a verb only when it recognises one. `raw` is
    the segment as typed, plus any heredoc body fed to it, and is read for one thing
    only -- whether code handed to an interpreter writes. `python3 -c` that prints is
    a read; the same command opening a file for writing is not.
    """
    rest = list(words)
    while rest:
        first = rest[0]
        if _ENV_ASSIGN.match(first):
            rest.pop(0)
            continue
        if os.path.basename(first) in WRAPPERS:
            rest.pop(0)
            while rest and rest[0].startswith("-"):
                rest.pop(0)
            continue
        break
    if not rest:
        return None
    name = os.path.basename(rest[0])
    args = rest[1:]
    if name == "git":
        return _git_verb(args)
    if name in ("sed", "perl", "ruby", "gsed") and _sed_in_place(args):
        return name + " -i"
    if name in READ_COMMANDS:
        return None  # named first, so a reader can never be added to both lists
    if name in WRITE_COMMANDS:
        return name
    if name in INTERPRETERS or name in SHELLS:
        # `bash -n <file>` and a script file this gate cannot read stay reads; code
        # that is here on the line, in a `-c` or in a heredoc, is read for intent.
        return name + INLINE if _INLINE_WRITE.search(raw or " ".join(args)) else None
    return None


REDIRECT = "redirect"


_QUOTED = re.compile(r"""['"]([^'"\n]{1,200})['"]""")


def _candidate_paths(verb: str, words: List[str], redirects: List[str], raw: str) -> List[str]:
    """The words of a write command that could be the thing it writes.

    A bare redirection writes exactly one thing -- the file after the `>`. Counting
    the rest of the line too is how `rg pattern <lane> 2>/dev/null` became a block.
    Code handed to an interpreter names its paths inside itself, quoted, so those are
    read out of the text rather than out of the argument list.
    """
    out = list(redirects)
    if verb == REDIRECT:
        return out
    if verb.endswith(INLINE):
        out.extend(m.group(1) for m in _ABS.finditer(raw))
        out.extend(m.group(1) for m in _QUOTED.finditer(raw))
        return out
    for word in words[1:]:
        if word.startswith("-"):
            continue
        out.append(word)
    return out


def bash_writes(command: str, cwd: str) -> List[Tuple[str, str]]:
    """(verb, resolved path) for everything this command line would write.

    `cd` is followed between segments, so `cd <lane> && sed -i ... file.py` resolves
    `file.py` inside the lane the way the shell would.
    """
    here = _expand(cwd or "")
    found: List[Tuple[str, str]] = []
    lines, bodies = split_heredocs(_expand(command or ""))
    for n, line in enumerate(lines):
        body = bodies.get(n, "")
        for segment in split_segments(line):
            words, redirects = lex(segment)
            if not words and not redirects:
                continue
            if words and os.path.basename(words[0]) == "cd":
                if len(words) > 1:
                    here = _resolve(words[1], here)
                continue
            raw = segment + ("\n" + body if body else "")
            verb = write_verb(words, raw) if words else REDIRECT
            if not verb and redirects:
                verb = REDIRECT
            if not verb:
                continue
            for candidate in _candidate_paths(verb, words, redirects, raw):
                resolved = _resolve(candidate, here)
                if resolved:
                    found.append((verb, resolved))
    return found


def write_targets(tool: str, tool_input: dict, cwd: str) -> List[Tuple[str, str]]:
    """(verb, resolved path) for every path this tool call would write to."""
    tool_input = tool_input or {}
    if tool != "Bash":
        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        resolved = _resolve(raw, _expand(cwd or "")) if raw else ""
        return [(tool, resolved)] if resolved else []
    return bash_writes(tool_input.get("command") or "", cwd)


def _short(desk: Desk, path: str) -> str:
    root = os.path.realpath(str(desk.root))
    return path[len(root) + 1:] if _inside(path, root) and path != root else path


# -- the gate --------------------------------------------------------------
def _first_hit(paths, roots):
    for item in paths:
        verb, path = item if isinstance(item, tuple) else ("", item)
        for lane, root in roots:
            if _inside(path, root):
                return lane, path, verb
    return None


def gate(desk: Desk, data: dict) -> Tuple[int, str]:
    """PreToolUse. Returns (exit code, message): 2 blocks the call, 0 lets it through.

    Three decisions, all of them written to the ledger so a desk can count the gate's
    own false positives instead of taking its word for them:

    * `allow-read` -- the call named a lane and would not write into it;
    * `open-window` -- it would write, and a window is open (the row reads `written`);
    * `block-write` -- it would write, and no window is open (the row reads `blocked`).
    """
    tool = (data or {}).get("tool_name") or ""
    if tool in READ_ONLY or tool not in WRITE_TOOLS:
        return 0, ""
    if not desk.cfg.get("hand_gate", True):
        return 0, ""
    tool_input = (data or {}).get("tool_input") or {}
    cwd = (data or {}).get("cwd") or ""
    command = " ".join((tool_input.get("command") or "").split()) if tool == "Bash" else ""
    if tool == "Bash" and _allowed_command(desk, command):
        return 0, ""
    roots = lane_roots(desk)
    if not roots:
        return 0, ""
    head = command[:COMMAND_HEAD_CHARS] + "…" if len(command) > COMMAND_HEAD_CHARS else (command or "-")
    hit = _first_hit(write_targets(tool, tool_input, cwd), roots)
    if hit is None:
        read_hit = _first_hit(mentions(tool, tool_input, cwd), roots)
        if read_hit is not None and desk.cfg.get("hand_log_reads", True):
            lane, path, _verb = read_hit
            note(desk, "allow-read: %s | %s | %s | %s" % (_now(), tool, _short(desk, path), head))
        return 0, ""
    lane, path, verb = hit
    current = window(desk)
    if current["state"] == "open":
        note(desk, "written: %s | %s | %s | %s | open-window %s" % (_now(), tool, _short(desk, path), head, verb))
        return 0, ""
    expired = current["state"] == "expired"
    note(desk, "blocked: %s | %s | %s | %s | block-write %s%s"
         % (_now(), tool, _short(desk, path), head, verb, " | expired" if expired else ""))
    return 2, (
        "bellhop hand gate: %s would write into lane '%s' (%s, via `%s`)%s.\n"
        'Open a window first, then repeat the call: bellhop hand open --minutes 30 --reason "why this cannot go to a worker"\n'
        "Recorded as `blocked` in %s. Reading the lane is not gated, and dispatching a packet needs no window."
        % (tool, lane, path, verb, " and the window has expired" if expired else " with no hand window open", LEDGER)
    )
