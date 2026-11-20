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
                "description, tools, disallowedTools, model, effort, color and maxTurns.")
ANY = "*"  # as an argument: the definitions could not be read as the run started, so no launch goes through
KEY = re.compile(r"""^["']?([A-Za-z_][\w-]*)["']?\s*:""")


def agent_name(text: str) -> str:
    """The name as Claude Code compares agent types: NFKC, lower case, no white space, dashes or underscores."""
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(c for c in text if c != "_" and not c.isspace() and unicodedata.category(c) != "Pd")


def front_matter(text: str):
    """The top-level keys and the name of an agent definition's front matter; None when it cannot be read as such."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    keys, name = set(), ""
    for line in lines[1:]:
        if line.strip() == "---":
            return keys, name
        if not line.strip() or line.lstrip().startswith("#") or line[0] in " \t-":
            continue
        found = KEY.match(line)
        if not found:
            return None
        keys.add(found.group(1))
        if found.group(1) == "name":
            name = line.split(":", 1)[1].strip().strip("'\"")
    return None


def definition_dirs(cwd: str, account: bool = True) -> list:
    """Where Claude Code may find agent definitions for this run: .claude/agents in the folder and those above it,
    and the account's own; with account False, none of the account's (the home folder's, and its settings')."""
    home = Path.home()
    dirs = [d / ".claude" / "agents" for d in dict.fromkeys([Path(cwd), *Path(cwd).parents]) if account or d != home]
    return [*dirs, *([Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "agents"] if account else [])]


def reaching(cwd: str, account: bool = True):
    """The agent types (as agent_name gives them) whose definitions hold more than PLAIN_DEFINITION, or cannot be
    read as a definition (by the file's name); None when a folder or file cannot be read: then no type is safe.
    account: as definition_dirs."""
    found = []
    for d in definition_dirs(cwd, account):
        try:
            files = sorted(d.rglob("*.md")) if d.is_dir() else []
            for f in files:
                parsed = front_matter(f.read_text(encoding="utf-8"))
                if parsed is None or not parsed[0] <= PLAIN_DEFINITION:
                    found += [agent_name(f.stem), *([agent_name(parsed[1])] if parsed and parsed[1] else [])]
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
