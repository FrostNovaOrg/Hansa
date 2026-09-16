from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


NEW_FILE_MODE = 0o644


class BellhopError(Exception):
    """A user-facing error. The CLI prints the message and exits with `code`.

    The default is `exits.USAGE`, because most of these say something is wrong with what
    was asked for. A raiser that knows better says so -- `code=exits.NOT_FOUND` for a
    task nobody dispatched, `code=exits.CONFIG` for a desk that is not configured -- and
    the CLI carries that out. The table is `bellhop/exits.py` and nowhere else.
    """

    def __init__(self, *args, code: Optional[int] = None):
        super().__init__(*args)
        from .exits import USAGE
        self.code = USAGE if code is None else int(code)


def now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def ts(dt: Optional[datetime] = None) -> str:
    return (dt or now()).strftime("%Y-%m-%dT%H:%M:%S%z")


def stamp(dt: Optional[datetime] = None) -> str:
    return (dt or now()).strftime("%Y%m%d-%H%M%S")


def today(dt: Optional[datetime] = None) -> str:
    return (dt or now()).strftime("%Y-%m-%d")


def iso_week(dt: Optional[datetime] = None) -> str:
    y, w, _ = (dt or now()).isocalendar()
    return f"{y}-W{w:02d}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def read(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Readers (board, sentinel, watchdog) must not observe half a JSON/card rewrite.
    # Writers still need a lock around read/modify/write and a backup where required.
    # `mkstemp` makes the file 0600. An existing file keeps the mode it had; a new one
    # gets 0644, because every one of these is a desk record something else reads --
    # the board, the sentinel, a person with a pager -- and a state file nobody but
    # this user can open is a record that is not there.
    previous_mode = path.stat().st_mode & 0o777 if path.exists() else NEW_FILE_MODE
    fd, temporary = tempfile.mkstemp(prefix=".bellhop-write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, previous_mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def append(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def read_json(path: Path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    write(path, json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def slugify(text: str, max_len: int = 48) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "task"


def run(
    cmd: Iterable[str],
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
    check: bool = False,
    capture: bool = True,
    timeout: Optional[int] = None,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess:
    cmd = [str(c) for c in cmd]
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=capture,
        text=True,
        timeout=timeout,
        input=input_text,
    )
    if check and proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise BellhopError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{err}")
    return proc


def git(repo: Path, *args: str, check: bool = True, env: Optional[dict] = None) -> str:
    proc = run(["git", "-C", str(repo), *args], check=check, env=env)
    return (proc.stdout or "").strip()


def scrubbed_env(allow: Iterable[str] = ()) -> dict:
    """Return a minimal environment for a worker process."""
    keep = {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "SHELL"}
    keep.update(allow)
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.setdefault("LANG", "C.UTF-8")
    return env


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} MB"
