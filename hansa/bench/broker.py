"""Serialize poll/revoke off the serve thread; the caller owns channels and holds.
Keep epochs, caps and stop-on-uncertainty while executing untrusted submissions.
"""
from contextlib import ExitStack
from itertools import chain
import json
import os
import select
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import uuid

from . import archive, channel, snapshot
from .manage import Busy, exclusive


RETURN_ALLOWANCE = 30  # SSH startup, guest teardown (5 s) and return (10 s).


class Broker:
    def __init__(self, desk, machine, *, clock=time.monotonic):
        self.desk, self.vm, self.clock = desk, machine, clock
        self.epoch = uuid.uuid4().hex
        self.waiting, self.active, self.used = [], None, {}
        self.ready, self.closed, self.idle, self.vm.clean = False, False, clock(), False
        self.failure = ""

    def reply(self, job, result):
        try:
            channel.reply(job.channel, job.request["id"], result)
        except (OSError, ValueError):
            pass  # A replaced/removed worker directory must never redirect delivery.

    def revoke(self, worker=None):
        """Cancel and join before the caller closes channels or commits worker work."""
        for job in list(self.waiting):
            if worker is None or job.channel is worker:
                self.reply(job, {"status": "cancelled"})
                self.waiting.remove(job)
        job = self.active
        if job and (worker is None or job.channel is worker):
            job.cancel.set()
            job.thread.join()
            self.reply(job, job.result if self.closed else {"status": "cancelled"})
            self.active, self.idle = None, self.clock()

    def poll(self, workers, *, enabled=False, held=False):
        """workers maps recorded Channel objects to absolute monotonic deadlines.
        held is serve's existing combined resource hold; no extra reservation.
        """
        job = self.active
        if job and not job.thread.is_alive():
            job.thread.join()
            self.active = None
        if not enabled:
            self.revoke()
            self.ready, self.closed, self.vm.clean, self.failure = False, False, False, ""
            if self.vm.port is not None:
                try:
                    with exclusive(self.desk):
                        self.vm.stop(self.clock() + 30)
                except Busy:
                    pass  # Management owns stopping; retry on the next off poll.
        for job in list(self.waiting):
            if self.closed or self.clock() >= min(job.deadline, job.until):
                self.reply(job, {"status": "infrastructure", "error": self.failure} if self.closed else {"status": "timeout"})
                self.waiting.remove(job)
        for worker, deadline in workers.items():
            if worker.epoch != self.epoch:
                continue
            with os.scandir(worker.directory) as entries:
                for entry in entries:
                    rid = entry.name.removesuffix(".request") if entry.name.endswith(".request") else ""
                    if not rid or rid in worker.claimed:
                        continue
                    try:
                        request = channel.claim(worker, rid)
                    except (OSError, ValueError, RecursionError):
                        worker.claimed.add(rid)  # Consume even unreadable requests in this epoch.
                        self.reply(SimpleNamespace(channel=worker, request={"id": rid}), {"status": "refused"})
                        continue
                    job = SimpleNamespace(channel=worker, request=request, cancel=threading.Event(),
                                          deadline=min(deadline, self.clock() + request["timeout"]),
                                          until=self.clock() + request["wait"])
                    outstanding = self.waiting + ([self.active] if self.active else [])
                    if not enabled:
                        self.reply(job, {"status": "timeout", "error": "bench is off"})
                    elif self.closed:
                        self.reply(job, {"status": "infrastructure", "error": self.failure})
                    elif (any(j.channel.run == worker.run for j in outstanding)
                            or len(self.waiting) >= 4 + int(self.active is None and not held)):
                        self.reply(job, {"status": "busy"})
                    elif self.clock() >= job.deadline:
                        self.reply(job, {"status": "timeout"})
                    else:
                        self.waiting.append(job)
        if not enabled:
            return
        if not self.closed and (not self.ready or (
                self.active is None and self.vm.port is not None and self.clock() - self.idle >= 60)):
            try:
                with exclusive(self.desk):
                    self.vm.stop(self.clock() + 30)
                self.ready = True
            except Busy:
                return  # A management operation is temporary; retain queued requests.
            except Exception as error:
                self.closed, self.failure = True, "bench closed: " + str(error)
                for job in self.waiting:
                    self.reply(job, {"status": "infrastructure", "error": self.failure})
                self.waiting.clear()
        if self.waiting and self.active is None and not held and not self.closed:
            self.active = self.waiting.pop(0)
            self.active.thread = threading.Thread(target=self.execute, args=(self.active,))
            self.active.thread.start()

    def execute(self, job):
        """One executor; SSH runs in a joined thread while pipes are pumped/capped."""
        connection, errors, ownership, owned = None, [], ExitStack(), False
        job.result = {"status": "timeout", "error": "bench time limit too short"}
        def check():
            if job.cancel.is_set() or self.clock() >= job.deadline:
                raise TimeoutError("bench cancelled or deadline")
        try:
            if job.deadline - self.clock() < RETURN_ALLOWANCE + 1:
                return
            ownership.enter_context(exclusive(self.desk))
            owned = True
            with ExitStack() as stack:
                if self.clock() >= job.until:
                    raise TimeoutError("queue deadline")
                check()
                if not self.vm.clean:
                    try:
                        self.vm.reset(job.deadline)
                    except Exception:
                        self.closed = True
                        raise
                source = stack.enter_context(snapshot.export(self.desk, job.channel))
                check()
                if self.vm.port is None:
                    self.vm.start(job.deadline)
                check()
                header = {k: job.request[k] for k in ("version", "id", "script", "cwd")}
                header.update(timeout=int(job.deadline - self.clock() - RETURN_ALLOWANCE), length=source.seek(0, 2))
                if header["timeout"] < 1:
                    return
                data = json.dumps(header).encode()
                if len(data) > archive.HEADER_BYTES:
                    raise ValueError("header limit")
                source.seek(0)
                chunks = chain([len(data).to_bytes(4, "big") + data], iter(lambda: source.read(archive.HEADER_BYTES), b""))
                pending = next(chunks)
                reader, sender, receiver, writer = [stack.enter_context(os.fdopen(fd, mode, buffering=0))
                    for pair in (os.pipe(), os.pipe()) for fd, mode in zip(pair, ("rb", "wb"))]
                def connect():
                    try:
                        with reader, writer:
                            self.vm.connect(reader, writer, job.deadline)
                    except Exception as error:
                        errors.append(error)
                connection = threading.Thread(target=connect)
                connection.start()
                os.set_blocking(sender.fileno(), False)
                returned = stack.enter_context(tempfile.TemporaryFile())
                while True:
                    check()
                    readable, writable, _ = select.select([receiver], [sender] if pending else [], [], 0.1)
                    if writable:
                        pending = pending[os.write(sender.fileno(), pending):]
                        if not pending:
                            pending = next(chunks, b"")
                    if readable:
                        data = os.read(receiver.fileno(), archive.HEADER_BYTES)
                        if not data:
                            break
                        returned.write(data)
                        if returned.tell() > archive.RUN_BYTES + archive.OUTPUT_BYTES + archive.MAX_METADATA + archive.HEADER_BYTES + 4:
                            raise ValueError("return byte limit")
                connection.join()
                if errors:
                    raise errors[0]
                if pending:
                    raise ValueError("incomplete submission")
                check()
                returned.seek(0)
                size = int.from_bytes(returned.read(4), "big")
                if not 0 < size <= archive.HEADER_BYTES:
                    raise ValueError("return header limit")
                response = json.loads(returned.read(size), object_pairs_hook=channel._pairs)
                if (type(response) is not dict or set(response) != {"version", "id", "length"}
                        or type(response["version"]) is not int or response["version"] != 1
                        or response["id"] != job.request["id"]
                        or response["length"] != os.fstat(returned.fileno()).st_size - size - 4):
                    raise ValueError("return identity or length")
                result = archive.deliver(returned, response["length"], job.channel, self.used.get(job.channel.run, 0))
                self.used[job.channel.run] = result.pop("used")
                summary = result["result"]
                if (summary.get("id") != job.request["id"] or type(summary.get("exit")) is not int
                        or any(type(summary.get(k)) is not bool for k in ("truncated", "rejected"))
                        or summary.get("status") not in ("ok", "failed", "timeout")):
                    raise ValueError("invalid or uncertain guest result")
                for key in ("stdout", "stderr"):
                    result[key] = result[key].decode("utf-8", "replace")
                name = "bench-" + uuid.uuid4().hex + ".json"
                with os.fdopen(channel.safe_open(job.channel.directory, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "w") as report:
                    json.dump(result, report)
                job.result = {"status": summary["status"], "report": name}
        except Exception as error:
            self.ready, self.failure = False, "bench closed: " + str(error)
            self.vm.clean = False  # Uncertain guest slots require stop-and-purge before reuse.
            job.result = {"status": "cancelled" if job.cancel.is_set() and not self.closed else
                          "timeout" if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)) else "infrastructure"}
            try:
                if owned:
                    self.vm.stop(self.clock() + 30)
            except Exception as error:
                self.closed, self.failure = True, "bench closed: " + str(error)
                job.result = {"status": "infrastructure"}
            finally:
                if connection is not None:
                    connection.join()
            if job.result["status"] != "cancelled":
                job.result["error"] = self.failure
        finally:
            ownership.close()
            self.idle = self.clock()
            self.reply(job, job.result)
