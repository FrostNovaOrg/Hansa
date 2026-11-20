"""Claude Code PreToolUse hook for the coordinator's Agent tool (formerly Task)."""
import json
import sys
import unicodedata


def main():
    try:
        args = json.load(sys.stdin)["tool_input"]
        role = unicodedata.normalize("NFKC", args.get("subagent_type", "")).lower()
    except (ValueError, AttributeError, KeyError, TypeError):
        print("hansa could not read the agent launch arguments", file=sys.stderr)
        return 2  # Claude Code blocks the tool call
    # Claude Code also resolves role names with differences in case, whitespace, dashes or underscores.
    role = "".join(c for c in role if c != "_" and not c.isspace() and unicodedata.category(c) != "Pd")
    if role in ("scout", "reviewer", "operator") and (args.get("name") or args.get("team_name")):
        print(f"Start {role} as a subagent by omitting name and team_name. A teammate receives messaging tools.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
