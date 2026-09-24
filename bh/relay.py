"""Start the principal's browser tool for one task, in that task's own folder.

    python3 relay.py <folder> <command>

Claude Code tells every tool it starts which folder it works in (for a worker, the
repository), and the browser tool saves files it is given a name for there. This sits
on the tool's input and leaves that part out of the first message, so the browser tool
keeps to the folder it was started in. Its output goes straight back to Claude Code.
"""
import json
import signal
import subprocess
import sys


def main() -> int:
    folder, command = sys.argv[1], sys.argv[2]
    child = subprocess.Popen(["/bin/sh", "-c", "exec " + command], cwd=folder, stdin=subprocess.PIPE)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        for line in sys.stdin.buffer:
            try:
                msg = json.loads(line)
                if msg.get("method") == "initialize":
                    msg["params"]["capabilities"].pop("roots", None)
                    line = (json.dumps(msg) + "\n").encode()
            except (ValueError, TypeError, KeyError, AttributeError):
                pass
            try:
                child.stdin.write(line)
                child.stdin.flush()
            except BrokenPipeError:
                break
    finally:
        try:
            child.stdin.close()  # the browser tool quits when its input ends
        except BrokenPipeError:
            pass
    return child.wait()


if __name__ == "__main__":
    sys.exit(main())
