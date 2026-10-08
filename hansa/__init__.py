"""hansa: a small, file-based desk for a team of Claude Code agents.

One directory (the desk) holds the settings, the keys, the tasks and an
append-only event log. The CLI, the web page and the agents all read the
same files, so nobody has to read this code to know what is going on.
"""

import functools
import os
import subprocess
import time
from pathlib import Path

__version__ = "0.5.0a1"
_handed = os.environ.pop("HANSA_BUILD", "")  # root's answer to build(), read once and not passed on to what this starts


@functools.lru_cache(maxsize=1)
def build() -> str:
    """This version and which code it is: hansa/BUILD when an install wrote one, else the git commit. As
    another account (root's hansa run again as the desk's, account.py) git would refuse a checkout it does not
    own, so it is told this one is safe, and root's hansa hands its own answer over (HANSA_BUILD) as well."""
    if _handed.startswith(f"{__version__} "):
        return _handed
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


SHIPPED = ("card.md", "ui.html", "no-signature")  # hansa's own files besides its Python sources and agents/*.md


def shipped(rel: Path) -> bool:
    """Whether a file at this path inside hansa/ is one of hansa's own: what an install puts in place."""
    if "__pycache__" in rel.parts:
        return False
    if len(rel.parts) == 1:
        return rel.suffix == ".py" or rel.name in SHIPPED
    return len(rel.parts) == 2 and rel.parts[0] == "agents" and rel.suffix == ".md"


@functools.lru_cache(maxsize=1)
def installed() -> float:
    """When the code running here was put in place: when hansa/BUILD was written where an install wrote one, else
    when hansa's newest own file last changed (a checkout's commit time says when it was committed, not when it was
    put in place): its Python sources and the other files it ships (shipped), not a .DS_Store or an editor's
    backup left beside them. Asked once: hansa serve asks as it starts,
    so it keeps the answer for the code it loaded (run.serve_code). 0 when unknown."""
    here = Path(__file__).resolve().parent
    try:
        return (here / "BUILD").stat().st_mtime
    except OSError:
        pass
    latest = 0.0
    for f in here.rglob("*"):
        if not shipped(f.relative_to(here)):
            continue
        try:
            if f.is_file():
                latest = max(latest, f.stat().st_mtime)
        except OSError:
            continue
    return latest


def installed_text(at: float = None) -> str:
    """'installed 2026-09-29 14:03', to the minute, in local time; '' when unknown. By default this code's."""
    at = installed() if at is None else at
    return time.strftime("installed %Y-%m-%d %H:%M", time.localtime(at)) if at else ""
