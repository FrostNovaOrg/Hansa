"""Paths a command explicitly names outside its repository; never execute or expand the command."""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import stat
from pathlib import Path

MAX_BYTES = 16 * 1024 * 1024


def _paths(command: str):
    """Read quoted words too, including a shell command passed as one word to bash -c."""
    pending, seen = [command], set()
    while pending:
        line = pending.pop()
        if line in seen:
            continue
        seen.add(line)
        try:
            lex = shlex.shlex(line, posix=True, punctuation_chars=";&|()<>")
            lex.whitespace_split = True
            lex.commenters = ""
            words = list(lex)
        except ValueError:
            words = line.split()
        for word in words:
            # Options and remote arguments can hold a path after '=' or ':'. No variable but HOME is read.
            values = [word] if word.startswith(("/", "~/", "$HOME/", "${HOME}/", "./", "../")) else re.split(r"[=:]", word)
            for value in values:
                value = value.replace("${HOME}/", str(Path.home()) + "/").replace("$HOME/", str(Path.home()) + "/")
                if "$" in value or "`" in value or (value.startswith("~") and not value.startswith("~/")):
                    continue
                p = Path(value).expanduser()
                if p.is_absolute() or ".." in p.parts:
                    yield p
            if word != line and any(c.isspace() or c in "'\"" for c in word):
                pending.append(word)


def inspect(command: str, repository, fingerprint: bool = True, directory=None) -> dict:
    """Existing outside text files (first 8 KB without NUL) and folders, following links and HOME.
    Unreadable or oversized files are named but have no hash. Devices and binaries are left out.
    Call only at a target change, release request/check or tick save/evaluation; views may omit hashes.
    """
    root = Path(repository).expanduser().resolve()
    files, folders, seen = [], [], set()
    if not isinstance(command, str):
        return {"outside": files, "outside_folders": folders}
    for candidate in _paths(command):
        p = Path(directory or root) / candidate
        try:
            p = p.resolve()
            if p == root or root in p.parents or p in seen:
                continue
            seen.add(p)
            st = p.stat()
            if stat.S_ISDIR(st.st_mode):
                folders.append(str(p))
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
        except PermissionError:
            files.append({"path": str(p), "why": "unreadable", "why_zh": "读不出来"})
            continue
        except (OSError, RuntimeError, ValueError):
            continue
        row = {"path": str(p)}
        try:
            with p.open("rb") as fh:
                head = fh.read(8192)
                if b"\0" in head:
                    continue
                if os.fstat(fh.fileno()).st_size > MAX_BYTES:
                    row.update(why="over 16 MB", why_zh="超过 16 MB")
                elif fingerprint:
                    digest, size = hashlib.sha256(head), len(head)
                    while size <= MAX_BYTES:
                        block = fh.read(min(65536, MAX_BYTES + 1 - size))
                        if not block:
                            break
                        digest.update(block)
                        size += len(block)
                    if size > MAX_BYTES:
                        row.update(why="over 16 MB", why_zh="超过 16 MB")
                    else:
                        row["sha256"] = digest.hexdigest()
        except OSError:
            row.update(why="unreadable", why_zh="读不出来")
        files.append(row)
    return {"outside": files, "outside_folders": folders}


def changed(before: list, after: list) -> str:
    """The first changed, gone or new fingerprinted file; unfingerprintable files never invalidate anything."""
    old, new = ({x["path"]: x for x in rows} for rows in (before, after))
    for path in dict.fromkeys([*old, *new]):
        a, b = old.get(path), new.get(path)
        if (a is not None and not a.get("sha256")) or (b is not None and not b.get("sha256")):
            continue
        if (a or {}).get("sha256") != (b or {}).get("sha256"):
            return path
    return ""


def lines(found: dict) -> list:
    return [f"outside file: {x['path']}" + (f"; sha256 {x['sha256']}" if x.get("sha256") else
            f"; not fingerprinted: {x['why']}" if x.get("why") else "") for x in found.get("outside") or []] + [
            f"outside folder the command runs in: {p}; no fingerprint" for p in found.get("outside_folders") or []]
