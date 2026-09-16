"""Opt-in: notice a worker that has gone quiet, and say so where the desk already looks.

A worker that is thinking and a worker whose session died in its first minute look the
same on the board: one in-flight row, no receipt, nothing else. The only reading that
tells them apart today is the worker timeout at twice the packet's budget — an hour and
a half for a 45-minute packet — and by then the reading says nothing about *when* the
silence started. On the desk this came from, the failures were in the layer between the
desk and the model service: rate limits and truncated streams, which end a session
without ending the process.

Two decisions shape this file.

**Bytes are a proxy, not proof.** Output means the session is alive. It does not mean
the work is going anywhere, and nothing here should be read as progress.

**The alert travels the path the desk already has.** A record goes in one append-only
file, `state/worker-health.jsonl`, and the sentinel is red while an alert is
unacknowledged — so the wake that already exists, with its busy gate and its brake,
carries this too. A second notifier with its own retries would be a second thing to
keep alive, and the desk that wrote the first version of this had one and could not
tell whether it had delivered anything.

Off by default. `worker_health.enabled` turns it on; everything else is a threshold.
"""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from typing import List, Optional

from .config import Desk
from .util import BellhopError, human_bytes, read, ts

EVENTS = "state/worker-health.jsonl"

DEFAULTS = {
    "enabled": False,
    "interval_s": 60,     # how often a running worker is measured
    "idle_s": 600,        # silence this long is an alert
    "soft_runtime_s": 2700,   # a long run is worth one alert; it is not a kill
}

# The kinds a worker's life can leave behind. Only the two warnings are alerts: they
# are the readings a person has to look at, and the rest are the story around them.
ALERTS = ("idle_warning", "runtime_warning")
KINDS = ALERTS + ("started", "recovered", "finished", "ack")


def options(desk: Desk) -> dict:
    """The desk's `worker_health` settings, merged over the defaults and checked.

    A threshold that is text, negative or infinite is a configuration mistake that
    would otherwise be discovered as "the alert never fired", which is the same reading
    as a worker that never went quiet.
    """
    supplied = desk.cfg.get("worker_health", {})
    if not isinstance(supplied, dict):
        raise BellhopError("worker_health must be an object")
    unknown = sorted(set(supplied) - set(DEFAULTS))
    if unknown:
        raise BellhopError(f"worker_health: unknown setting(s) {', '.join(unknown)} "
                           f"(known: {', '.join(sorted(DEFAULTS))})")
    cfg = dict(DEFAULTS, **supplied)
    if not isinstance(cfg["enabled"], bool):
        raise BellhopError("worker_health.enabled must be true or false")
    for key in ("interval_s", "idle_s", "soft_runtime_s"):
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise BellhopError(f"worker_health.{key} must be a positive number of seconds")
    return cfg


def events_path(desk: Desk):
    return desk.path(EVENTS)


def _append_line(path, line: str) -> None:
    """One line, one `O_APPEND` write, and never glued to a torn one.

    Written without the desk lock, the way the watchdog log is: this is called from the
    thread draining a worker's pipes, and taking the desk lock there would make a
    worker's output wait behind somebody else's `git`. A single short write through
    `O_APPEND` is what keeps two threads from interleaving. If a process was killed
    halfway through a previous line, the next line starts a new one rather than
    continuing it: one torn record should cost one record, not two.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = b""
    if path.exists() and path.stat().st_size:
        with path.open("rb") as check:
            check.seek(-1, os.SEEK_END)
            if check.read(1) != b"\n":
                prefix = b"\n"
    with path.open("ab") as fh:
        fh.write(prefix + line.encode("utf-8") + b"\n")


def record(desk: Desk, task: str, kind: str, detail: str = "", **fields) -> dict:
    """Append one event. Returns it, with the id a person acknowledges."""
    if kind not in KINDS:
        raise BellhopError(f"unknown worker health event kind: {kind}")
    event = dict(fields, event_id=uuid.uuid4().hex[:12], at=ts(), task=task, kind=kind)
    if detail:
        event["detail"] = detail
    _append_line(events_path(desk), json.dumps(event, sort_keys=True, ensure_ascii=False))
    return event


def entries(desk: Desk) -> List[dict]:
    """Every readable event, oldest first.

    A line that does not parse is skipped rather than raising: the file is appended to
    from worker threads, and the last line of a file being written while a process was
    killed is not a reason for `bellhop health status` to fail.
    """
    path = events_path(desk)
    if not path.exists():
        return []
    out = []
    for line in read(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("kind") in KINDS:
            out.append(row)
    return out


def alerts(desk: Desk) -> List[dict]:
    """Alerts nobody has acknowledged yet, oldest first."""
    rows = entries(desk)
    acked = {row.get("event") for row in rows if row.get("kind") == "ack"}
    return [row for row in rows if row.get("kind") in ALERTS and row.get("event_id") not in acked]


def ack(desk: Desk, event_id: str) -> dict:
    """Acknowledge one open alert by id. Acknowledging is *looked at*, not *fixed*.

    An id that is not open is refused rather than written twice: "I acknowledged it and
    nothing happened" and "I acknowledged the wrong id" would otherwise read the same.
    """
    for row in alerts(desk):
        if row.get("event_id") == event_id:
            return record(desk, row.get("task", ""), "ack", event=event_id)
    raise BellhopError(f"no unacknowledged worker health alert with id {event_id}")


def status(desk: Desk, limit: int = 20, task: Optional[str] = None) -> dict:
    cfg = options(desk)
    rows = [r for r in entries(desk) if task is None or r.get("task") == task]
    open_alerts = [r for r in alerts(desk) if task is None or r.get("task") == task]
    return {"enabled": cfg["enabled"], "settings": cfg, "alerts": open_alerts,
            "recent": rows[-limit:], "total": len(rows)}


class Observation:
    """One running worker, measured on a monotonic clock.

    `activity` is called with every block of bytes the runner reads; `check` is called
    between reads and does the deciding. An idle *episode* gets one warning: output
    ends the episode, and only a new silence after that can warn again. A long runtime
    gets exactly one warning per worker, because the second one says nothing new.
    """

    def __init__(self, desk: Desk, task: str, cfg: Optional[dict] = None, clock=time.monotonic):
        self.desk, self.task = desk, task
        self.cfg = cfg or options(desk)
        self.clock = clock
        self.started = self.last = self.checked = clock()
        self.counts = {"stdout": 0, "stderr": 0}
        self.blocks = 0
        self.episode = 0
        self.idle_warned = False
        self.runtime_warned = False
        self.alerts = 0
        record(desk, task, "started")

    # -- what the runner tells it ------------------------------------------
    def activity(self, stream: str, size: int) -> None:
        if size <= 0:
            return
        self.counts[stream] = self.counts.get(stream, 0) + size
        self.blocks += 1
        silence = self.idle_seconds()  # read before the clock moves, or it reads zero
        self.last = self.clock()
        if self.idle_warned:
            record(self.desk, self.task, "recovered",
                   f"output again after {silence:.0f}s of silence", episode=self.episode,
                   idle_s=round(silence, 1))
            self.idle_warned = False

    def check(self, force: bool = False) -> None:
        current = self.clock()
        if not force and current - self.checked < self.cfg["interval_s"]:
            return
        self.checked = current
        idle, runtime = current - self.last, current - self.started
        if idle >= self.cfg["idle_s"] and not self.idle_warned:
            self.episode += 1
            self.idle_warned = True
            self.alerts += 1
            record(self.desk, self.task, "idle_warning",
                   f"no output for {idle:.0f}s (running {runtime:.0f}s, {human_bytes(self.total())} so far)",
                   episode=self.episode, idle_s=round(idle, 1), runtime_s=round(runtime, 1))
        if runtime >= self.cfg["soft_runtime_s"] and not self.runtime_warned:
            self.runtime_warned = True
            self.alerts += 1
            record(self.desk, self.task, "runtime_warning",
                   f"running {runtime:.0f}s, past the soft limit of {self.cfg['soft_runtime_s']:.0f}s",
                   runtime_s=round(runtime, 1))

    @property
    def poll_s(self) -> float:
        """How often the runner should come back to `check`, in seconds.

        A runner that polls once a second cannot honour a five-second idle limit, and a
        desk that set one would read "the alert never fires" as "the worker was busy".
        """
        return max(0.05, min(self.cfg["interval_s"], self.cfg["idle_s"]))

    # -- readings -----------------------------------------------------------
    def total(self) -> int:
        return sum(self.counts.values())

    def idle_seconds(self) -> float:
        return self.clock() - self.last

    def summary(self) -> dict:
        """What the receipt's meta carries: bytes seen, and whether anything was said."""
        return {"bytes": self.total(), "blocks": self.blocks,
                "idle_episodes": self.episode, "alerts": self.alerts,
                "runtime_s": round(self.clock() - self.started, 1),
                "idle_s_at_end": round(self.idle_seconds(), 1)}

    def finish(self, exit_code=None) -> dict:
        """The closing line. Defensive on purpose: this one has no runner around it.

        `activity` and `check` are called from inside the runner, which turns
        observation off on the first failure. This is called after the session, where a
        failed write would otherwise turn a finished worker into an exception.
        """
        summary = self.summary()
        try:
            record(self.desk, self.task, "finished", exit_code=exit_code, **summary)
        except OSError:
            summary["recorded"] = False
        return summary


def observation(desk: Desk, task: str, on_error=None) -> Optional[Observation]:
    """An `Observation` when the desk asked for one, `None` when it did not.

    A health file that cannot be written costs the desk its alerts, never the worker:
    the caller gets `None` and one line in the watchdog log.
    """
    cfg = options(desk)
    if not cfg["enabled"]:
        return None
    try:
        return Observation(desk, task, cfg)
    except OSError as exc:
        if on_error is not None:
            on_error(exc)
        return None
