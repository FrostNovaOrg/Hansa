"""Run one design session in the current terminal. bh starts it inside tmux, where the
principal joins it:

    python3 -m bh.session <desk> <task>

The command and its environment are built here, in the session's own process, so a
provider's key never appears on a command line.
"""
import os
import sys
import traceback

from .desk import Desk
from .run import design_command


def main() -> int:
    desk, tid = Desk(sys.argv[1]), sys.argv[2]
    try:
        argv, env, cwd = design_command(desk, tid)
        os.chdir(cwd)
        os.execvpe(argv[0], argv, env)
    except Exception:  # noqa: BLE001 - whatever stops the start is kept for bh show
        (desk.tasks_dir / tid / "run.err").write_text(traceback.format_exc(), encoding="utf-8")
    return 1


if __name__ == "__main__":
    sys.exit(main())
