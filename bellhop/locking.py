"""Cooperating Bellhop writers on one local desk (macOS/Linux).

The desk lock is reentrant within a thread and excludes other threads/processes.
Keep flock files in place: unlinking a locked inode would let a second writer in.
This is not a transaction, a filesystem sandbox, or a lock against manual Git use.
Lane claim files deliberately retain their separate, fail-closed crash semantics.

**Every wait has an end.** A blocking `flock` with no clock is a deadlock waiting for
one stuck holder: that process never exits, every other writer on the desk waits
behind it, and no file says why. So a wait carries a deadline (`LOCK_TIMEOUT_S`), the
holder writes its pid and argv into the lock file while it holds it, and a timeout
raises a `BellhopError` naming that holder. A lock nobody can take and a lock held by
a process somebody can go and look at must not read the same.

**Hook paths never block.** A `PreToolUse` or `UserPromptSubmit` hook runs inside the
coordinator's turn, so a hook that waits is a coordinator that waits. `soft_desk_lock`
gives those paths a two-second try and, when the lock does not come, lets the call
through unlocked and appends one line to `state/sentinel.log`. An unserialised append
is a small risk; a desk frozen by its own gate is not a small one.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import sys
import threading
import time
from functools import wraps
from pathlib import Path

from .util import BellhopError, ts

_guard = threading.Lock()
_locks = {}
_held = threading.local()

# How long a Bellhop writer waits before it gives up and says who is holding the lock.
LOCK_TIMEOUT_S = 30
# What a hook path waits: short enough that a stuck holder costs a pause, not a turn.
HOOK_LOCK_TIMEOUT_S = 2
_POLL_S = 0.05
SENTINEL_LOG = "state/sentinel.log"


def _holder_line(path: Path) -> str:
    """Whatever the lock file says about its current holder, on one line."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return " ".join(text.split())[:200]


def _write_holder(handle) -> None:
    """Name the holder inside the lock file, so a timeout elsewhere can name it too."""
    try:
        handle.seek(0)
        handle.truncate(0)
        handle.write("pid %d | %s | since %s\n" % (os.getpid(), " ".join(sys.argv)[:300], ts()))
        handle.flush()
    except (OSError, ValueError):
        pass


def _clear_holder(handle) -> None:
    try:
        handle.seek(0)
        handle.truncate(0)
        handle.flush()
    except (OSError, ValueError):
        pass


def _timed_out(path, limit: float) -> BellhopError:
    holder = _holder_line(Path(path))
    who = f", held by {holder}" if holder else "; the lock file names no holder"
    return BellhopError(
        f"waited {limit:g}s for the lock {Path(path).name}{who}. Nothing was written. "
        f"Check whether that process is still alive, then run this again"
    )


def _take(handle, path: Path, blocking: bool, limit: float) -> None:
    """`flock` with a deadline. `fcntl` has no timeout of its own, so this polls."""
    if not blocking:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BlockingIOError(str(path))
        return
    deadline = time.monotonic() + max(0.0, float(limit))
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise _timed_out(path, limit)
            time.sleep(_POLL_S)


@contextlib.contextmanager
def file_lock(path, blocking=True, timeout=None):
    path = Path(path)
    key = str(path.resolve())
    limit = LOCK_TIMEOUT_S if timeout is None else float(timeout)
    with _guard:
        mutex = _locks.setdefault(key, threading.RLock())
    if blocking and limit > 0:
        taken = mutex.acquire(timeout=limit)
    else:
        taken = mutex.acquire(blocking=False)
    if not taken:
        if not blocking:
            raise BlockingIOError(key)
        raise _timed_out(path, limit)
    held = getattr(_held, "paths", set())
    _held.paths = held
    try:
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+") as handle:
            _take(handle, path, blocking, limit)
            held.add(key)
            _write_holder(handle)
            try:
                yield
            finally:
                held.discard(key)
                _clear_holder(handle)
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        mutex.release()


def desk_lock_path(desk) -> Path:
    return desk.path("bells", ".locks", "desk.flock")


def desk_lock(desk, timeout=None):
    # A desk whose full board takes minutes holds this lock for minutes, and the wait
    # has to outlast it: `lock_timeout_seconds` is how a desk says so. The default is
    # the same 30s every other waiter gets.
    if timeout is None:
        try:
            timeout = float(desk.cfg.get("lock_timeout_seconds", LOCK_TIMEOUT_S) or LOCK_TIMEOUT_S)
        except (AttributeError, TypeError, ValueError):
            timeout = LOCK_TIMEOUT_S
    return file_lock(desk_lock_path(desk), timeout=timeout)


def serialized(fn):
    @wraps(fn)
    def wrapped(desk, *args, **kwargs):
        with desk_lock(desk):
            return fn(desk, *args, **kwargs)
    return wrapped


def note_unlocked(desk, what: str, reason: str) -> None:
    """One line in `state/sentinel.log` for a write that went ahead without the lock.

    `LOCK` rather than `GREEN`/`RED`: this is not a sentinel run, and a gap in the
    sentinel's own timestamps still has to read as a dead sentinel.
    """
    try:
        path = desk.path(SENTINEL_LOG)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts()} LOCK {what} went ahead without the desk lock: {reason}\n")
    except OSError:
        pass


@contextlib.contextmanager
def soft_desk_lock(desk, what: str, timeout: float = HOOK_LOCK_TIMEOUT_S):
    """Take the desk lock if it comes quickly; otherwise go ahead and leave a line.

    Yields True when the lock was taken and False when it was not, so a caller that
    cares can say so. Acquisition is kept out of the `with` body on purpose: a
    `BellhopError` raised by the body is the caller's, not a lock timeout.
    """
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(desk_lock(desk, timeout=timeout))
        locked = True
    except BellhopError as exc:
        locked = False
        note_unlocked(desk, what, str(exc))
    with stack:
        yield locked


def soft_serialized(what: str, timeout: float = HOOK_LOCK_TIMEOUT_S):
    """`serialized` for a path a hook calls: it waits briefly and never blocks a turn."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(desk, *args, **kwargs):
            with soft_desk_lock(desk, what, timeout):
                return fn(desk, *args, **kwargs)
        return wrapped
    return decorate


@contextlib.contextmanager
def acceptance_lock(desk, task):
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(file_lock(
                desk.path("bells", ".locks", "accept", f"{task}.flock"), blocking=False))
        except BlockingIOError:
            raise BellhopError(f"acceptance for {task} is busy; wait for its result")
        yield


def acceptance_session(fn):
    @wraps(fn)
    def wrapped(desk, task_id, *args, **kwargs):
        with acceptance_lock(desk, task_id):
            return fn(desk, task_id, *args, **kwargs)
    return wrapped


def worker_limit(desk):
    # One by default. The machinery below runs several lanes at once and still does;
    # only the number a fresh desk starts at changed. The README's own advice is to
    # start at 1, and a shipped default that disagrees with it is a trap.
    value = desk.cfg.get("max_parallel_workers", 1)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BellhopError("max_parallel_workers must be a positive integer")
    return value


@contextlib.contextmanager
def worker_slot(desk):
    # Shared by resident/session watchdogs and manual watch --once processes.
    with contextlib.ExitStack() as stack:
        for number in range(worker_limit(desk)):
            try:
                stack.enter_context(file_lock(
                    desk.path("bells", ".locks", "slots", f"{number}.flock"), blocking=False))
            except BlockingIOError:
                continue
            yield
            return
    raise BellhopError("all worker slots are busy")
