"""hansa: a small, file-based desk for a team of Claude Code agents.

One directory (the desk) holds the settings, the keys, the tasks and an
append-only event log. The CLI, the web page and the agents all read the
same files, so nobody has to read this code to know what is going on.
"""

import functools
import os
import subprocess
from pathlib import Path

__version__ = "0.5.0a1"


@functools.lru_cache(maxsize=1)
def build() -> str:
    """This version and which code it is: hansa/BUILD when an install wrote one, else the git commit. As
    another account (root's hansa run again as the desk's, account.py) git would refuse a checkout it does not
    own, so it is told this one is safe, and root's hansa hands its own answer over (HANSA_BUILD) as well."""
    handed = os.environ.get("HANSA_BUILD", "")
    if handed.startswith(f"{__version__} "):
        return handed
    here = Path(__file__).resolve().parent
    try:
        return f"{__version__} {(here / 'BUILD').read_text(encoding='utf-8').strip()}".strip()
    except OSError:
        pass
    try:
        sha = subprocess.run(["git", "-c", f"safe.directory={here.parent}", "-C", str(here), "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = ""
    return f"{__version__} {sha}".strip()
