"""Cooperating Bellhop writers on one local desk (macOS/Linux).

The desk lock is reentrant within a thread and excludes other threads/processes.
Keep flock files in place: unlinking a locked inode would let a second writer in.
This is not a transaction, a filesystem sandbox, or a lock against manual Git use.
Lane claim files deliberately retain their separate, fail-closed crash semantics.
"""
from __future__ import annotations

import contextlib
import fcntl
import threading
from functools import wraps

from .util import BellhopError

_guard = threading.Lock()
_locks = {}
_held = threading.local()


@contextlib.contextmanager
def file_lock(path, blocking=True):
    key = str(path.resolve())
    with _guard:
        mutex = _locks.setdefault(key, threading.RLock())
    if not mutex.acquire(blocking=blocking):
        raise BlockingIOError(key)
    held = getattr(_held, "paths", set())
    _held.paths = held
    try:
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        mutex.release()


def desk_lock(desk):
    return file_lock(desk.path("bells", ".locks", "desk.flock"))


def serialized(fn):
    @wraps(fn)
    def wrapped(desk, *args, **kwargs):
        with desk_lock(desk):
            return fn(desk, *args, **kwargs)
    return wrapped


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
    value = desk.cfg.get("max_parallel_workers", 2)
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
