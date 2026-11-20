"""Claude Code PreToolUse hook for a worker's Agent tool (formerly Task): only a plain local subagent starts.

The call may carry the fields in PLAIN only. A name or team makes a teammate that has messaging tools, isolation
"remote" starts the agent in a cloud environment, and a field Claude Code adds later is unknown here: all are
refused, before anything is sent. The agent definition a call names (a project's .claude/agents) may carry only
the keys in PLAIN_DEFINITION: one with isolation, hooks, MCP servers or anything else is refused the same way.
Claude Code reads the definitions as it starts, so hansa also gives this hook, as its arguments, the agent types
whose definitions in the checkout and the folders above it reached further then (reaching): a definition changed
later to look plain is still refused."""
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

PLAIN = {"prompt", "description", "subagent_type", "model", "effort", "run_in_background"}
PLAIN_DEFINITION = {"name", "description", "tools", "disallowedTools", "model", "effort", "color", "maxTurns"}
REFUSED = "Start a plain subagent: give only prompt, description, subagent_type, model, effort and run_in_background."
REFUSED_TYPE = ("Start a plain subagent with another subagent_type: this one's definition holds more than name, "
                "description, tools, disallowedTools, model, effort, color and maxTurns, or hansa cannot read every "
                "agent definition exactly.")
ANY = "*"  # as an argument: the definitions could not be read as the run started, so no launch goes through
# How Claude Code 2.1.329 finds the front matter (after taking off one byte-order mark): the text from a first line
# of --- to the next --- anywhere, its \s as JavaScript's.
JS_SPACE = "[\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]"
BLOCK = re.compile(f"---{JS_SPACE}*\n([\\s\\S]*?)---")
# The only lines read: a key at the start of the line, unquoted, then nothing, or a space and its value.
KEY = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*):(?: (.*))?")
# A name read exactly: plain words, or quoted with no escapes; nothing after it (no comment, no tag, no anchor).
NAME = re.compile(r"""\s*(?:([A-Za-z0-9_][A-Za-z0-9 _.-]*?)|'([^'\n]*)'|"([^"\\\n]*)")\s*""")
# Characters a YAML reader may take as a line break or that hansa does not read: refused anywhere in the block.
ODD = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\ufeff]")


def agent_name(text: str) -> str:
    """The name as Claude Code compares agent types: NFKC, lower case, no white space, dashes or underscores."""
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(c for c in text if c != "_" and not c.isspace() and unicodedata.category(c) != "Pd")


def front_matter(text: str):
    """The top-level keys and the name of an agent definition's front matter, read exactly as Claude Code would
    read them, or None when hansa cannot be sure of that: then no agent type is taken to be plain.
    The block is found as Claude Code finds it (BLOCK); none at all defines no agent. In it, each line ends in \\n,
    or \\r\\n, and holds no other break or control character (ODD); a line is blank, a comment starting at its
    first column, a key at its first column (KEY: unquoted, then : and a space or nothing; each key once), or,
    once a key was read, an indented line, part of that key's value. Anything else (an indented line before the
    first key, a key in quotes or followed by a tab, a flow mapping, a merge key, a directive) is not read: None.
    The name must be plain or simply quoted on its own line (NAME), with no indented line going on from it."""
    if text.startswith("\ufeff"):
        text = text[1:]
    block = BLOCK.match(text)
    if not block:
        return set(), ""
    keys, name, last = set(), "", None
    for line in block.group(1).split("\n"):
        line = line[:-1] if line.endswith("\r") else line
        if ODD.search(line.replace("\t", "")):
            return None
        if not line.strip(" \t"):
            continue
        if line[0] in " \t":
            if last is None or last == "name":
                return None  # the start of the block indented, or the name going on past its line
            continue
        if line.startswith("#"):
            continue
        found = KEY.fullmatch(line)
        if not found or found.group(1) in keys:
            return None
        last = found.group(1)
        keys.add(last)
        if last == "name":
            value = NAME.fullmatch(found.group(2) or "")
            if not value:
                return None
            name = next(v for v in value.groups() if v is not None)
    return keys, name


def definition_dirs(cwd: str, account: bool = True) -> list:
    """Where Claude Code may find agent definitions for this run: .claude/agents in the folder and those above it,
    and the account's own; with account False, none of the account's (the home folder's, and its settings')."""
    home = Path.home()
    dirs = [d / ".claude" / "agents" for d in dict.fromkeys([Path(cwd), *Path(cwd).parents]) if account or d != home]
    return [*dirs, *([Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "agents"] if account else [])]


def definition_files(folder: Path) -> list:
    """The .md files in a folder of agent definitions and the folders in it, links followed (and anything else named
    *.md, which then cannot be read); [] when there is no such folder. OSError when one cannot be listed, as
    pathlib's rglob would pass over it."""
    try:
        if not folder.is_dir():
            return []
    except OSError:
        pass  # listing it below says what is wrong
    def fail(error):
        raise error
    files, seen = [], set()
    for top, dirs, names in os.walk(folder, onerror=fail, followlinks=True):
        if os.path.realpath(top) in seen:
            dirs[:] = []
            continue
        seen.add(os.path.realpath(top))
        files += [Path(top) / n for n in [*names, *dirs] if n.endswith(".md")]  # a folder so named: not read
    return sorted(files)


def reaching(cwd: str, account: bool = True):
    """The agent types (as agent_name gives them: Claude Code names a type by its definition's name) whose
    definitions hold more than PLAIN_DEFINITION; None when a folder or file cannot be read, or a definition cannot
    be read exactly (front_matter): its type is unknown then, so no type is safe. account: as definition_dirs."""
    found = []
    for d in definition_dirs(cwd, account):
        try:
            for f in definition_files(d):
                parsed = front_matter(f.read_bytes().decode("utf-8"))  # its line ends as they are, \r too
                if parsed is None:
                    return None
                if not parsed[0] <= PLAIN_DEFINITION:
                    found += [agent_name(f.stem), *([agent_name(parsed[1])] if parsed[1] else [])]
        except (OSError, UnicodeError):
            return None
    return list(dict.fromkeys(found))


def definition_refused(wanted: str, cwd: str, earlier=()) -> bool:
    """Whether a definition of this agent type holds more than PLAIN_DEFINITION, now or when the run started
    (earlier), or one that may be it cannot be read."""
    now = reaching(cwd)
    return now is None or ANY in earlier or wanted in now or wanted in earlier


def main():
    try:
        event = json.load(sys.stdin)
        args = event["tool_input"]
        plain = isinstance(args, dict) and args.keys() <= PLAIN
        kind = args.get("subagent_type") if plain else None
        if plain and not isinstance(kind, (str, type(None))):
            plain = False
        if plain:
            cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else os.getcwd()
            if definition_refused(agent_name(kind or "general-purpose"), cwd, sys.argv[1:]):
                print(REFUSED_TYPE, file=sys.stderr)
                return 2
            return 0  # leave the permission decision to Claude Code
    except (KeyError, TypeError, ValueError, AttributeError, OSError):
        pass
    print(REFUSED, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
