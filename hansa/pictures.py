"""Pictures a worker or a verifier chose to show the principal, kept with the task when the run ends.

Every worker and verifier run gets a folder for them in its temporary folder (FOLDER, made by run.make_worker_tmp),
named to its commands by ENV. When the run ends, however it ends, run.remove_sandbox_tmp calls take() before it
removes the temporary folder: what passes is copied to the task's own folder, tasks/<id>/pictures/ (kept_dir),
and the task's record lists it (meta "pictures"), with what was left out and why ("pictures_left_out") and a copy
that could not be made ("pictures_failed"). hansa show prints them (lines()).

The folder and everything in it were the worker's to change until its process ended, and a process it left behind
may still change them while hansa reads: hansa reads it outside any sandbox, so every name and byte is hostile.
Hence: the folder and each file are opened without following a link (O_NOFOLLOW, relative to the folder's own
descriptor), a file is read only after fstat says the descriptor is the regular file lstat saw, at most
LIMIT + 1 bytes of it, and it is kept only when its size and change time are the same after the read. Nothing is
decoded or re-encoded: the first bytes say the kind (kind_of). No shell is started. A picture holding a key the
desk knows, as bytes, is not kept; text drawn in a picture cannot be read, and is not looked for.

A run's outcome, its commit, its check and its verdict never depend on this: take() raises nothing.
These checks stay while hansa copies files a worker controlled out of its folder.
"""
from __future__ import annotations

import os
import re
import secrets
import stat
from pathlib import Path

ENV = "HANSA_PICTURES_DIR"  # the folder's path, in the environment of the run (run.worker_env)
FOLDER = "pictures"  # in the run's temporary folder
KEPT = "pictures"  # in the task's folder, tasks/<id>/
LIMIT = 2 * 1024 * 1024  # bytes a picture may have
MOST = 8  # pictures kept from one run
LOOKED = 1000  # names looked at in one run's folder; the rest are left out unread
NAME_MOST = 80  # characters in a kept name
EXTENSIONS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}
KIND_EXT = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}
# the reasons a file was left out, in the order hansa show gives them
NOT_PICTURE, TOO_LARGE, TOO_MANY, NOT_PLAIN, CHANGED, KEY, NOT_LOOKED = (
    "not a picture", "too large", f"more than {MOST}", "not a plain file", "changed while hansa read it",
    "holds a key", f"not looked at (more than {LOOKED} names)")
REASONS = (NOT_PICTURE, TOO_LARGE, TOO_MANY, NOT_PLAIN, CHANGED, KEY, NOT_LOOKED)
_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOCTTY", 0)


def parts() -> tuple:
    """The pictures folder's path under a run's temporary folder: in the per-user folder Claude Code makes there
    (run.commands_tmp), the TMPDIR of a worker's commands and the one place there its sandbox lets them write."""
    return f"claude-{os.getuid()}", FOLDER


def folder(tmp: str) -> str:
    """The pictures folder of the run whose temporary folder is tmp."""
    return os.path.join(tmp, *parts())


def kept_dir(desk, tid: str) -> Path:
    """Where a task's pictures are kept: in its own folder, which the coordinator reads and no worker does."""
    return desk.tasks_dir / tid / KEPT


def kind_of(data: bytes) -> str:
    """png, jpeg or webp, from the first bytes; '' for anything else."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


def safe_name(name: str, kind: str, taken: set) -> str:
    """The name a picture is kept under: its own, letters, digits, -, _ and . only (others become -), at most
    NAME_MOST characters, with the lower-cased extension its bytes say (.jpeg kept where the name had it), and -2,
    -3... before it when another kept name is the same (as the file system may compare them: casefolded). Adds it
    to taken."""
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    if EXTENSIONS.get(ext) != kind:
        ext = KIND_EXT[kind]
    stem = re.sub(r"[^A-Za-z0-9._-]", "-", stem).lstrip(".") or "picture"
    n = 1
    while True:
        suffix = f"-{n}" if n > 1 else ""
        out = stem[:NAME_MOST - len(ext) - len(suffix)] + suffix + ext
        if out.casefold() not in taken:
            taken.add(out.casefold())
            return out
        n += 1


def _read(dfd: int, name: str, seen: os.stat_result) -> tuple:
    """(bytes, '') of the regular file `name` in the folder dfd, the one lstat saw (seen); (b'', reason) else."""
    try:
        fd = os.open(name, _FLAGS | os.O_NONBLOCK, dir_fd=dfd)
    except OSError:
        return b"", CHANGED  # gone, or swapped for a link (ELOOP) or something not to be opened, since the look
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (seen.st_dev, seen.st_ino):
            return b"", CHANGED
        if st.st_nlink != seen.st_nlink:
            return b"", NOT_PLAIN
        if st.st_size > LIMIT:
            return b"", TOO_LARGE
        chunks, got = [], 0
        while got <= LIMIT:
            chunk = os.read(fd, LIMIT + 1 - got)
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
        data = b"".join(chunks)
        if len(data) > LIMIT:  # it was not when it was opened
            return b"", CHANGED
        after = os.fstat(fd)
        if len(data) != after.st_size or (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                st.st_size, st.st_mtime_ns, st.st_ctime_ns):
            return b"", CHANGED
        return data, ""
    except OSError:
        return b"", CHANGED
    finally:
        os.close(fd)


def _keys(desk) -> list:
    try:
        return [k.encode("utf-8") for k in desk.keys().values() if k]
    except Exception:  # noqa: BLE001 - keys that cannot be read: then nothing is kept (gather says why)
        return None


def gather(desk, tmp: str) -> tuple:
    """What of the run's pictures folder may be kept: ([(name as found, bytes, kind)], {reason: count}), the first
    in name order. Nothing at all for a folder that is not there, or is not a folder (a link, a file)."""
    left = {}

    def leave(why, n=1):
        left[why] = left.get(why, 0) + n

    dirs = []
    try:
        try:
            dirs.append(os.open(tmp, _FLAGS | getattr(os, "O_DIRECTORY", 0)))
            for part in parts():  # each opened in the one before, no link followed
                dirs.append(os.open(part, _FLAGS | getattr(os, "O_DIRECTORY", 0), dir_fd=dirs[-1]))
        except OSError:
            return [], {}
        dfd = dirs[-1]
        if not stat.S_ISDIR(os.fstat(dfd).st_mode):
            return [], {}
        names = sorted(os.listdir(dfd))
        if len(names) > LOOKED:
            leave(NOT_LOOKED, len(names) - LOOKED)
            names = names[:LOOKED]
        looks = {}
        for name in names:
            try:
                looks[name] = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except OSError:
                leave(CHANGED)
        inodes = {}
        for st in looks.values():
            inodes[(st.st_dev, st.st_ino)] = inodes.get((st.st_dev, st.st_ino), 0) + 1
        keys, out = None, []
        for name, st in looks.items():
            # a link to a file elsewhere (its count of links more than its names here) is not read: it may be
            # one of the worktree's or the account's files
            if not stat.S_ISREG(st.st_mode) or st.st_nlink > inodes[(st.st_dev, st.st_ino)]:
                leave(NOT_PLAIN)
                continue
            if os.path.splitext(name)[1].lower() not in EXTENSIONS:
                leave(NOT_PICTURE)
                continue
            if st.st_size > LIMIT:
                leave(TOO_LARGE)
                continue
            data, why = _read(dfd, name, st)
            if why:
                leave(why)
                continue
            kind = kind_of(data)
            if not kind:
                leave(NOT_PICTURE)
                continue
            if len(out) >= MOST:
                leave(TOO_MANY)
                continue
            if keys is None:
                keys = _keys(desk)
            if keys is None or any(k in data for k in keys):
                leave(KEY)
                continue
            out.append((name, data, kind))
        return out, left
    finally:
        for fd in dirs:
            os.close(fd)


def under_floor(desk, need: int) -> bool:
    """Whether the desk's volume has less free than its floor (hansa disk-floor) once `need` bytes are written."""
    from . import disk
    free, total = disk.desk_free(desk)
    if free is None:
        return True
    fl = disk.floor(desk.settings())
    return free - need < disk.vol_floor(fl, {"total": total}) * disk.GB if fl else free < need


def store(desk, tid: str, found: list, run: int) -> list:
    """Write the pictures found into the task's folder: their records, {name, bytes, kind, run}. All or none: a
    write that fails removes what this call wrote, and raises."""
    d = kept_dir(desk, tid)
    taken = {(r.get("name") or "").casefold() for r in desk.meta(tid).get("pictures") or []}
    try:
        taken |= {n.casefold() for n in os.listdir(d)}
    except FileNotFoundError:
        pass
    d.mkdir(mode=0o700, exist_ok=True)
    if d.is_symlink() or not d.is_dir():
        raise OSError(f"{d} is not a folder")
    made, out = [], []
    try:
        for name, data, kind in found:
            kept = safe_name(name, kind, taken)
            part = d / f".{kept}.{secrets.token_hex(4)}.part"
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FLAGS, 0o600)  # O_RDONLY is 0
            made.append(part)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            if os.path.lexists(d / kept):
                raise OSError(f"{d / kept} is there already")
            os.rename(part, d / kept)
            made[-1] = d / kept
            out.append({"name": kept, "bytes": len(data), "kind": kind, "run": run})
    except BaseException:
        for p in made:
            try:
                os.unlink(p)
            except OSError:
                pass
        raise
    return out


def take(desk, tid: str, tmp: str, run: int) -> None:
    """Keep what the run's pictures folder holds that passes, with the task, and record it (see the module's
    text). Never raises; nothing said when there was nothing there."""
    try:
        found, left = gather(desk, tmp)
    except Exception:  # noqa: BLE001 - a folder hansa could not go through: as if it held nothing
        return
    failed = ""
    kept = []
    if found:
        try:
            if under_floor(desk, sum(len(x[1]) for x in found)):
                failed = "the desk's disk is under its floor of free space (hansa disk-floor)"
            else:
                kept = store(desk, tid, found, run)
        except Exception as e:  # noqa: BLE001 - the run's end goes on without them
            failed = f"the copy failed ({getattr(e, 'strerror', None) or e})"
    if not (kept or left or failed):
        return
    try:
        desk.change(tid, lambda m: {
            "pictures": [*(m.get("pictures") or []), *kept],
            "pictures_left_out": [*(m.get("pictures_left_out") or []),
                                  *({"run": run, "why": w, "count": left[w]} for w in REASONS if w in left)],
            "pictures_failed": [*(m.get("pictures_failed") or []),
                                *([{"run": run, "count": len(found), "why": desk.redact(failed)[:300]}]
                                  if failed else [])]})
    except Exception:  # noqa: BLE001
        pass


MIME = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}
_KEPT_NAME = re.compile(rf"[A-Za-z0-9_-][A-Za-z0-9._-]{{0,{NAME_MOST - 1}}}")  # as safe_name makes them


def kept_bytes(desk, tid: str, n: int):
    """For the page (web.py, /api/picture): (bytes, kind, name) of picture n (from 0) of the task's record, or None
    when there is no such picture or the file no longer is what the record says: gone, not a regular file in the
    task's pictures folder (a link, a hard link, a folder, put there since), of another size, or first bytes of
    another kind. The folder and the file are opened without following a link, and nothing is read before fstat
    says the file is the one recorded; then at most its recorded size and one byte."""
    try:
        recs = desk.meta(tid).get("pictures") or []
    except (SystemExit, OSError, ValueError, AttributeError):
        return None
    if not isinstance(recs, list) or not 0 <= n < len(recs) or not isinstance(recs[n], dict):
        return None
    name, kind, size = recs[n].get("name"), recs[n].get("kind"), recs[n].get("bytes")
    if not (isinstance(name, str) and _KEPT_NAME.fullmatch(name) and kind in MIME
            and type(size) is int and 0 < size <= LIMIT):
        return None
    fds = []
    try:
        fds.append(os.open(kept_dir(desk, tid), _FLAGS | getattr(os, "O_DIRECTORY", 0)))
        fds.append(os.open(name, _FLAGS | os.O_NONBLOCK, dir_fd=fds[0]))
        st = os.fstat(fds[1])
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size != size:
            return None
        def upto(want: int) -> bytes:
            chunks, got = [], 0
            while got < want:
                chunk = os.read(fds[1], want - got)
                if not chunk:
                    break
                chunks.append(chunk)
                got += len(chunk)
            return b"".join(chunks)

        data = upto(min(12, size))
        if kind_of(data) != kind:
            return None  # the first bytes say another kind, or none: nothing more is read
        data += upto(size + 1 - len(data))
        return (data, kind, name) if len(data) == size else None
    except OSError:
        return None
    finally:
        for fd in fds:
            os.close(fd)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def own_lines(desk, m: dict, label: str = "pictures") -> list:
    """A task's or a verification's kept pictures, with the folder, what was left out and copies that failed."""
    kept = m.get("pictures") or []
    out = []
    if kept:
        out.append(f"{label}: {_plural(len(kept), 'picture')} in {kept_dir(desk, m['id'])}")
        out += [f"  {p['name']} ({p['bytes']} bytes, {p['kind']}, run {p['run']})" for p in kept]
    for x in m.get("pictures_left_out") or []:
        out.append(f"{label} left out: {x['why']}: {_plural(x['count'], 'file')} (run {x['run']})")
    for x in m.get("pictures_failed") or []:
        out.append(f"{label} not kept: {x['why']}; {_plural(x['count'], 'picture')} of run {x['run']} lost")
    return out


def lines(desk, m: dict) -> list:
    """For hansa show: the task's own pictures; for a task, its verifications'; for a follow-up, the earlier
    task's only as a count."""
    out = own_lines(desk, m)
    if m.get("kind") == "verify":
        return out
    for v in desk.tasks():
        if v.get("kind") == "verify" and v.get("parent") == m.get("id"):
            out += own_lines(desk, v, f"verification {v['id']}'s pictures")
    if m.get("parent"):
        try:
            n = len(desk.meta(m["parent"]).get("pictures") or [])
        except (SystemExit, OSError, ValueError):
            n = 0
        if n:
            out.append(f"pictures: earlier: {m['parent']} has {n}")
    return out
