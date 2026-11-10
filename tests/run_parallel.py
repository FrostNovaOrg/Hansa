"""Run unittest tests on dynamically fed subprocesses (Python 3.9+, macOS/Linux)."""
import argparse
from collections import deque
import inspect
import io
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.getcwd())  # like python -m unittest, including outside this checkout
FIELDS = ("failures", "errors", "skipped", "expectedFailures", "unexpectedSuccesses")
# What a run with no names runs, and GitHub's machines too (.github/workflows/tests.yml): hansa's tests (with the
# modules tests.test_hansa imports: tests/test_menu.py, test_doctor.py, test_install.py and tests/bench/), its
# push services', its documentation links, the coordinator's mod (claude plugin validate and test, tests/test_mod.py),
# then the optional Codex adapter's.
SUITES = ("tests.test_hansa", "tests.test_push_services", "tests.test_docs", "tests.test_mod", "contrib.codex.tests")


def load_tests(names):
    # TestProgram also understands file paths and unittest's discovery spelling.
    class Collect:
        def run(self, suite):
            return suite

    suite = unittest.TestProgram(module=None, argv=["unittest"] + names,
                                 testRunner=Collect(), exit=False).test

    def flatten(suite):
        for test in suite:
            if isinstance(test, unittest.TestSuite):
                yield from flatten(test)
            else:
                yield test
    return list(flatten(suite))


def describe(test):
    return [str(test), test.shortDescription()]


def source_length(test):
    try:
        method = getattr(test, test._testMethodName)
        return len(inspect.getsource(inspect.unwrap(method)))
    except (AttributeError, OSError, TypeError):
        return 0  # unreadable sources follow readable ones, in unittest's order


class RemoteTest:
    failureException = AssertionError

    def __init__(self, description):
        self.name, self.doc = description

    def __str__(self):
        return self.name

    def shortDescription(self):
        return self.doc


def guard(read_fd, group):
    # Only the runner holds the writing end. Even SIGKILL closes it; no test thread
    # or polling in the worker is needed, and children in this group die with it.
    try:
        while os.read(read_fd, 1):
            pass
    finally:
        os.killpg(group, signal.SIGKILL)


def worker(read_fd, write_fd, guard_fd, names):
    os.set_inheritable(read_fd, False)
    os.set_inheritable(write_fd, False)
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--guard",
                      str(guard_fd), str(os.getpgrp())], pass_fds=(guard_fd,),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(guard_fd)
    tests = load_tests(names)
    commands = os.fdopen(read_fd)
    replies = os.fdopen(write_fd, "w")
    progress = io.StringIO()
    result = unittest.TextTestResult(unittest.runner._WritelnDecorator(progress), True, 1)

    def send(kind, **extra):
        data = {"kind": kind, "pid": os.getpid(), "index": last_index,
                "count": result.testsRun, "progress": progress.getvalue()}
        for field in FIELDS:
            values = getattr(result, field)
            data[field] = ([describe(t) for t in values] if field == "unexpectedSuccesses" else
                           [[describe(t), detail] for t, detail in values])
            values.clear()
        result.testsRun = 0
        progress.seek(0)
        progress.truncate()
        sys.stdout.flush()
        sys.stderr.flush()
        replies.write(json.dumps(dict(data, **extra)) + "\n")
        replies.flush()

    class Incoming(unittest.TestSuite):
        _cleanup = False

        def __iter__(self):
            nonlocal last_index
            for line in commands:
                index = json.loads(line)
                if index is None:
                    break
                last_index = index
                started = time.perf_counter()
                yield tests[index]  # unittest invokes it here, on this process's main thread
                send("done", seconds=time.perf_counter() - started)

    # One suite keeps class/module fixtures between consecutive tests, just as unittest does.
    last_index = None
    result.startTestRun()
    Incoming().run(result)
    result.stopTestRun()
    send("finished")  # includes errors from the last class/module teardown


class Worker:
    def __init__(self, names, selector):
        command_read, command_write = os.pipe()
        reply_read, reply_write = os.pipe()
        guard_read, self.guard_write = os.pipe()
        self.output = tempfile.TemporaryFile()
        self.offset = 0
        try:
            self.process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--worker",
                 str(command_read), str(reply_write), str(guard_read), json.dumps(names)],
                pass_fds=(command_read, reply_write, guard_read), stdout=self.output, stderr=self.output,
                start_new_session=True)
        except BaseException:
            os.close(command_write)
            os.close(reply_read)
            self.stop_guard()
            self.output.close()
            raise
        finally:
            os.close(command_read)
            os.close(reply_write)
            os.close(guard_read)
        self.commands = command_write
        self.replies = reply_read
        os.set_blocking(reply_read, False)
        selector.register(reply_read, selectors.EVENT_READ, self)
        self.buffer = b""
        self.current = None
        self.last = None
        self.started = 0
        self.finished = False

    def assign(self, index):
        self.current = index
        if index is not None:
            self.last = index
        self.started = time.perf_counter()
        os.write(self.commands, (json.dumps(index) + "\n").encode())

    def drain_output(self, stream):
        # pread leaves the shared file offset alone: a test's children may still be writing.
        while True:
            data = os.pread(self.output.fileno(), 65536, self.offset)
            if not data:
                break
            self.offset += len(data)
            stream.write(data.decode("utf-8", errors="replace"))
        stream.flush()

    def close(self, selector):
        self.stop_guard()
        selector.unregister(self.replies)
        os.close(self.replies)
        os.close(self.commands)
        self.output.close()

    def stop_guard(self):
        if self.guard_write is not None:
            os.close(self.guard_write)
            self.guard_write = None


def stop_workers(workers):
    """Kill whole worker groups, plus descendants that opened sessions of their own."""
    if not workers:
        return
    descendants = {w.process.pid for w in workers}
    try:
        listing = subprocess.check_output(["ps", "-axo", "pid=,ppid="], text=True,
                                          stderr=subprocess.DEVNULL, timeout=10)
        pairs = [tuple(map(int, line.split())) for line in listing.splitlines()]
        while True:
            more = {pid for pid, parent in pairs if parent in descendants}
            if more <= descendants:
                break
            descendants.update(more)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass  # the process groups still cover children that stayed in their worker's session
    for w in workers:
        w.stop_guard()
    for pid in descendants - {w.process.pid for w in workers}:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    for w in workers:
        try:
            os.killpg(w.process.pid, signal.SIGKILL)
        except OSError:
            pass
    for w in workers:
        try:
            w.process.wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass  # a refusal or timeout for one process must not abandon the others


class Parallel:
    def __init__(self, names, jobs):
        self.names = names
        self.tests = load_tests(names)
        self.order = list(range(len(self.tests)))
        if jobs > 1:
            self.order.sort(key=lambda index: -source_length(self.tests[index]))
        self.jobs = jobs
        self.timings = []
        self.interrupted = None

    def __call__(self, result):
        workers, pending = [], deque(self.order)
        completed = set()
        selector = selectors.DefaultSelector()
        old_handlers = {}

        def interrupted(signum, frame):
            self.interrupted = signum

        def dispatch(w):
            index = pending.popleft() if pending else None
            w.assign(index)

        def error(test, message, count=True):
            if count:
                result.startTest(test)
            result.addError(test, (RuntimeError, RuntimeError(message), None))
            if count:
                result.stopTest(test)

        def reply(line, w):
            """Validate before merging: a forked test can inherit both protocol pipes."""
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError("reply is not an object")
            if data.get("pid") != w.process.pid:
                raise ValueError("reply from another process")
            kind = data.get("kind")
            expected = w.current if kind == "done" else w.last
            if (kind not in ("done", "finished") or w.finished or
                    (kind == "done" and w.current is None) or
                    (kind == "finished" and w.current is not None) or
                    type(data.get("index")) is not int or data["index"] != expected):
                raise ValueError("reply about a test the worker was not given")
            if (type(data.get("count")) is not int or data["count"] < 0 or
                    not isinstance(data.get("progress"), str)):
                raise ValueError("unreadable test result")
            for field in FIELDS:
                if not isinstance(data.get(field), list):
                    raise ValueError("unreadable test result")
                for item in data[field]:
                    if field != "unexpectedSuccesses":
                        if not isinstance(item, list) or len(item) != 2 or not isinstance(item[1], str):
                            raise ValueError("unreadable test result")
                        item = item[0]
                    if (not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str) or
                            (item[1] is not None and not isinstance(item[1], str))):
                        raise ValueError("unreadable test description")
            if kind == "done" and (not isinstance(data.get("seconds"), (int, float)) or
                                   not math.isfinite(data["seconds"]) or data["seconds"] < 0):
                raise ValueError("unreadable test duration")
            return data

        def lost(w, message, index):
            # An old reply may arrive after the next assignment. Return that assignment
            # to the queue rather than losing it or counting the previous test twice.
            if w.current is not None and w.current != index:
                pending.appendleft(w.current)
            count = index == w.current and index not in completed and index is not None
            test = self.tests[index] if index is not None else RemoteTest(["worker shutdown", None])
            error(test, message, count=count)
            if count:
                completed.add(index)
                self.timings.append((time.perf_counter() - w.started, str(test)))
            stop_workers([w])
            w.drain_output(result.stream)
            w.close(selector)
            workers.remove(w)
            if pending:
                replacement = Worker(self.names, selector)
                workers.append(replacement)
                dispatch(replacement)

        def merge(data):
            result.testsRun += data["count"]
            result.stream.write(data["progress"])
            result.stream.flush()
            for field in FIELDS:
                values = getattr(result, field)
                for item in data[field]:
                    values.append(RemoteTest(item) if field == "unexpectedSuccesses" else
                                  (RemoteTest(item[0]), item[1]))

        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                old_handlers[sig] = signal.signal(sig, interrupted)
            for _ in range(min(self.jobs, len(self.tests))):
                w = Worker(self.names, selector)
                workers.append(w)
                dispatch(w)
            while workers and not self.interrupted:
                ready = {key.data for key, _ in selector.select(timeout=0.1)}
                for w in list(workers):
                    if w not in ready and w.process.poll() is None:
                        continue
                    try:
                        chunk = os.read(w.replies, 65536)
                    except BlockingIOError:
                        if w.process.poll() is None:
                            continue
                        chunk = b""  # a forked child may still hold the pipe open after the worker died
                    except OSError as exc:
                        lost(w, f"worker reply could not be read: {exc}", w.current if w.current is not None else w.last)
                        continue
                    w.buffer += chunk
                    invalid = False
                    while b"\n" in w.buffer:
                        line, w.buffer = w.buffer.split(b"\n", 1)
                        try:
                            data = reply(line, w)
                        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
                            index = w.current if w.current is not None else w.last
                            try:
                                about = json.loads(line).get("index")
                                if type(about) is int and 0 <= about < len(self.tests):
                                    index = about
                            except (ValueError, TypeError, AttributeError, RecursionError):
                                pass
                            lost(w, f"invalid worker reply: {exc}", index)
                            invalid = True
                            break
                        w.drain_output(result.stream)
                        merge(data)
                        if data["kind"] == "done":
                            self.timings.append((data["seconds"], str(self.tests[w.current])))
                            completed.add(w.current)
                            w.current = None
                            try:
                                dispatch(w)
                            except BrokenPipeError:
                                pass  # EOF below attributes a lost assignment to this test
                        else:
                            w.finished = True
                    if invalid:
                        continue
                    if chunk:
                        continue
                    # All protocol data was consumed before EOF, even if poll() saw the exit earlier.
                    w.drain_output(result.stream)
                    try:
                        status = w.process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        status = None
                    if w.buffer:
                        lost(w, "worker reply could not be read: incomplete reply",
                             w.current if w.current is not None else w.last)
                    elif w.current is not None or not w.finished or status not in (None, 0):
                        index = w.current if w.current is not None else w.last
                        test = self.tests[index]
                        message = (f"worker exited with status {status} after {test}" if status is not None else
                                   f"worker closed its reply pipe before finishing {test}")
                        lost(w, message, index)
                    else:
                        stop_workers([w])
                        w.close(selector)
                        workers.remove(w)
            if self.interrupted:
                message = f"runner received {signal.Signals(self.interrupted).name}"
                active = [self.tests[w.current] for w in workers if w.current is not None]
                for test in active:
                    error(test, f"{message} while running {test}")
                if not active:
                    error(RemoteTest(["interrupted runner", None]), message, count=False)
        finally:
            stop_workers(workers)
            for w in workers:
                w.drain_output(result.stream)
                w.close(selector)
            selector.close()
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=positive, default=max(1, min(8, (os.cpu_count() or 1) // 2)))
    parser.add_argument("--slowest", type=int, default=0, metavar="K")
    parser.add_argument("names", nargs="*", default=list(SUITES))
    args = parser.parse_args()
    if args.slowest < 0:
        parser.error("--slowest must be at least 0")
    suite = Parallel(args.names, args.jobs)
    result = unittest.TextTestRunner().run(suite)
    if args.slowest:
        print("\nSlowest tests:", file=sys.stderr)
        for seconds, name in sorted(suite.timings, reverse=True)[:args.slowest]:
            print(f"{seconds:.3f}s {name}", file=sys.stderr)
    failed = result.errors + result.failures  # the order TextTestResult.printErrors uses
    if failed:
        names = [str(test) for test, _ in failed[:10]]
        if len(failed) > 10:
            names.append(f"and {len(failed) - 10} more")
        print("Failed tests: " + ", ".join(names), file=result.stream)
    return 1 if suite.interrupted or not result.wasSuccessful() else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--worker"]:
        worker(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), json.loads(sys.argv[5]))
    elif sys.argv[1:2] == ["--guard"]:
        guard(int(sys.argv[2]), int(sys.argv[3]))
    else:
        sys.exit(main())
