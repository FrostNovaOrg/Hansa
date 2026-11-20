"""The read-only helpers' (scout, reviewer) PreToolUse hook on Bash: a command that only reads goes on to Claude Code's
usual permission check; any other is blocked (exit 2, with the reason on stderr). Claude Code runs it only while that
helper is active (the hooks field of its definition, hansa/agents/*.md). Run as a file, with no hansa import, so it
starts fast and reads nothing of the desk: python3 -I hansa/helper_bash.py, its input Claude Code's hook JSON."""
from __future__ import annotations

import json
import shlex
import sys

# programs that only read, as a helper runs them (find without its actions that run or write, below)
READERS = {"grep", "rg", "find", "ls", "cat", "head", "tail", "wc", "file", "stat", "pwd", "diff"}
FIND_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
# options of the reading commands that run a program or write a file: refused in any spelling (--opt=value too).
# rg: --pre runs a program on each file, --pre-glob picks the files for it, -z/--search-zip runs decompressors,
# --hostname-bin runs a program for its links; file: -C/--compile writes a compiled magic file. A long option given
# by the start of its name (getopt takes those) counts as the option
RUNS_OR_WRITES = {"rg": ("--pre", "--pre-glob", "--search-zip", "--hostname-bin"), "file": ("--compile",)}
SHORT_RUNS_OR_WRITES = {"rg": "z", "file": "C"}
# hansa's commands that read: the words after hansa, each alone or with what may follow it
HANSA_READERS = {"show", "diff", "git", "status", "explain", "help", "decisions", "changelog", "cost"}
HANSA_READING_FORMS = {"goal": ("list", "show"), "gaps": ("", "--all", "show"), "lane": ("",)}


def refusal(command: str) -> str:
    """Why a helper may not run this command, '' when it only reads."""
    if not isinstance(command, str) or not command.strip():
        return "no command"
    if any(c in command for c in "$`\n\r"):
        return "it expands or runs more than one command"
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        words = list(lex)
    except ValueError:
        return "it cannot be read as one command"
    if any(w and all(c in "();<>|&" for c in w) for w in words):
        return "it runs more than one command or redirects one"
    first, rest = words[0], words[1:]
    if first in READERS:
        if first == "find" and FIND_ACTIONS & set(rest):
            return "find runs or writes there"
        for w in rest:
            if w == "--":
                break
            name = w.partition("=")[0]
            if w.startswith("--") and len(name) > 3 and any(r.startswith(name) for r in RUNS_OR_WRITES.get(first, ())):
                return f"{first} {name} runs a program or writes a file"
            if w.startswith("-") and not w.startswith("--") and set(w[1:]) & set(SHORT_RUNS_OR_WRITES.get(first, "")):
                return f"{first} {w} runs a program or writes a file"
        return ""
    if first != "hansa":
        return f"{first} is not one of the reading commands"
    if not rest or rest[0].startswith("-"):
        return "hansa's options go after its command"
    cmd, after = rest[0], rest[1:]
    if cmd in HANSA_READERS or cmd == "desks" and after in ([], ["--json"]):  # desks lists; --forget changes
        return ""
    if cmd in HANSA_READING_FORMS and (after[0] if after else "") in HANSA_READING_FORMS[cmd]:
        return ""
    if cmd == "notes" and (not after or after[0] != "archive"):
        return ""
    return f"hansa {cmd} {' '.join(after[:1])}".strip() + " changes something or is not a reading command"


def main() -> int:
    try:
        given = json.loads(sys.stdin.read(1 << 20) or "{}")
        command = ((given.get("tool_input") or {}).get("command") if isinstance(given, dict) else None) or ""
    except (ValueError, AttributeError):
        command = ""
    why = refusal(command)
    if not why:
        return 0
    print(f"hansa: this helper only reads, so it runs reading commands only ({why}); ask the coordinator to have "
          "it done", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
