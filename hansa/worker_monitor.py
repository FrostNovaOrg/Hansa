"""Allow Claude Code's Monitor to watch a worker's sandboxed command. Other sources can open connections."""
import json
import sys


def main():
    try:
        args = json.load(sys.stdin)["tool_input"]
        if isinstance(args, dict) and isinstance(args.get("command"), str) and args["command"].strip() and (
                args.keys() <= {"command", "description", "timeout_ms", "persistent"}):
            return 0  # leave the permission decision and the command sandbox to Claude Code
    except (KeyError, TypeError, ValueError):
        pass
    print("Use Monitor with a background shell command in this worker.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
