"""Run one worker session with its output read as it arrives.

`accept.run_session` starts a process, waits, and hands back everything it printed at
the end. That is the right shape for the verifier and the wrong one for a worker you
want to notice going quiet: nothing about the session is readable until it is over, so
a session that died in its first minute and one that is still thinking are the same
reading for as long as the timeout lasts.

This runner drains both pipes through a selector and tells an observer how many bytes
arrived and when. Everything else stays as `run_session` has it: the same process
group, the same wall clock, the same `killpg` when the clock runs out, the same result
dictionary. Watching a worker is not a reason to stop bounding it — a lane claim held
by a session nobody is timing is how a lane goes quiet for a day.

Nothing here decides anything. The thresholds, the records and the alerts belong to
`health.py`; this file counts bytes and passes them on.
"""
from __future__ import annotations

import os
import selectors
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

# How long the drain may continue after the process group was killed, so a session that
# printed its last words on the way out still has them recorded.
KILL_GRACE_S = 30
POLL_S = 1.0


def _decode(parts) -> str:
    """Join first, decode once: a chunk boundary can fall inside a UTF-8 code point.

    `errors="replace"` and the newline normalization are what `text=True` would have
    done, applied to the whole stream rather than to each arriving block.
    """
    return b"".join(parts).decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def run_worker(cmd, cwd: Path, env: dict, timeout_s: int, observation=None,
               on_error: Optional[Callable[[BaseException], None]] = None) -> dict:
    """Start one worker, drain it, and return what `run_session` returns.

    `observation` is anything with `activity(stream, size)` and `check()`. The first
    failure from either one turns observation off for the rest of the session and is
    reported once through `on_error`: a full disk under the health file must cost the
    desk its alerts, never the worker that is halfway through a task.
    """
    started = time.monotonic()
    failed = False
    # Never longer than the observer wants to be asked, never longer than a second: the
    # same loop is what notices the wall clock running out.
    poll_s = min(POLL_S, getattr(observation, "poll_s", POLL_S)) if observation is not None else POLL_S

    def observe(method, *args, **kwargs):
        nonlocal failed
        if observation is None or failed:
            return
        try:
            getattr(observation, method)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 -- see the docstring
            failed = True
            if on_error is not None:
                try:
                    on_error(exc)
                except Exception:  # noqa: BLE001
                    pass  # the log may be on the filesystem that just failed
    try:
        proc = subprocess.Popen([str(c) for c in cmd], cwd=str(cwd), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
    except OSError as exc:
        return {"exit_code": 127, "stdout": "", "stderr": f"could not launch the worker: {exc}",
                "timed_out": False, "duration_s": 0.0, "observed": False}
    from .accept import _kill_group

    chunks = {"stdout": [], "stderr": []}
    deadline = started + timeout_s
    timed_out = False
    grace_end = None
    try:
        with selectors.DefaultSelector() as selector:
            for name in ("stdout", "stderr"):
                pipe = getattr(proc, name)
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map():
                now = time.monotonic()
                if not timed_out and now >= deadline:
                    timed_out = True
                    _kill_group(proc)
                    grace_end = now + KILL_GRACE_S
                if grace_end is not None and now >= grace_end:
                    break
                for key, _mask in selector.select(poll_s):
                    try:
                        block = os.read(key.fd, 65536)
                    except (BlockingIOError, InterruptedError):
                        continue
                    if block:
                        chunks[key.data].append(block)
                        observe("activity", key.data, len(block))
                    else:
                        selector.unregister(key.fileobj)
                observe("check")
        # A child can exit while a grandchild still holds the pipes, and a child can
        # close its pipes and keep running: EOF on both and "the worker is finished"
        # are two different events. Wait for the child, still against the same clock.
        if not timed_out:
            try:
                proc.wait(timeout=max(1.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(proc)
        if timed_out:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # the group was signalled; the watchdog log and the receipt say dead
    except BaseException:
        _kill_group(proc)  # an interrupt or an OS failure, never the elapsed time
        raise
    finally:
        for name in ("stdout", "stderr"):
            try:
                getattr(proc, name).close()
            except OSError:
                pass
    observe("check", force=True)
    return {"exit_code": 124 if timed_out else proc.returncode,
            "stdout": _decode(chunks["stdout"]), "stderr": _decode(chunks["stderr"]),
            "timed_out": timed_out, "duration_s": round(time.monotonic() - started, 1),
            "observed": observation is not None and not failed}
