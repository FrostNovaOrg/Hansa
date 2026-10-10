"""Run one design session in the current terminal. hansa starts it inside tmux, where the
principal joins it:

    python3 -m hansa.session <desk> <task>

The command and its environment are built here, in the session's own process, so a
provider's key never appears on a command line.
"""
import os
import sys
import traceback

from .desk import Desk, write_text
from .run import design_command


def main() -> int:
    desk, tid = Desk(sys.argv[1]), sys.argv[2]
    try:
        argv, env, cwd = design_command(desk, tid)
        os.chdir(cwd)
        os.execvpe(argv[0], argv, env)
    except Exception:  # noqa: BLE001 - whatever stops the start is kept for hansa show
        write_text(desk.raw_dir(tid) / "run.err", traceback.format_exc())
    return 1


if __name__ == "__main__":
    sys.exit(main())
