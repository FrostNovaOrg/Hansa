"""Run unittest tests on dynamically fed subprocesses (Python 3.9+, macOS/Linux)."""
import argparse
import io
import json
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


class RemoteTest:
    def __init__(self, description):
        self.name, self.doc = description

    def __str__(self):
        return self.name

    def shortDescription(self):
        return self.doc


def worker(read_fd, write_fd, names):
    os.set_inheritable(read_fd, False)
    os.set_inheritable(write_fd, False)
    tests = load_tests(names)
    commands = os.fdopen(read_fd)
    replies = os.fdopen(write_fd, "w")
    progress = io.StringIO()
    result = unittest.TextTestResult(unittest.runner._WritelnDecorator(progress), True, 1)

    def send(kind, **extra):
        data = {"kind": kind, "count": result.testsRun, "progress": progress.getvalue()}
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
            for line in commands:
                index = json.loads(line)
                if index is None:
                    break
                started = time.perf_counter()
                yield tests[index]  # unittest invokes it here, on this process's main thread
                send("done", seconds=time.perf_counter() - started)

    # One suite keeps class/module fixtures between consecutive tests, just as unittest does.
    result.startTestRun()
    Incoming().run(result)
    result.stopTestRun()
    send("finished")  # includes errors from the last class/module teardown


class Worker:
    def __init__(self, names, selector):
        command_read, command_write = os.pipe()
        reply_read, reply_write = os.pipe()
        self.output = tempfile.TemporaryFile()
        self.offset = 0
        try:
            self.process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--worker",
                 str(command_read), str(reply_write), json.dumps(names)],
                pass_fds=(command_read, reply_write), stdout=self.output, stderr=self.output,
                start_new_session=True)
        except BaseException:
            os.close(command_write)
            os.close(reply_read)
            self.output.close()
            raise
        finally:
            os.close(command_read)
            os.close(reply_write)
        self.commands = command_write
        self.replies = reply_read
        os.set_blocking(reply_read, False)
        selector.register(reply_read, selectors.EVENT_READ, self)
        self.buffer = b""
        self.current = None
        self.started = 0
        self.finished = False

    def assign(self, index):
        self.current = index
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
        selector.unregister(self.replies)
        os.close(self.replies)
        os.close(self.commands)
        self.output.close()


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
    for pid in descendants - {w.process.pid for w in workers}:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for w in workers:
        try:
            os.killpg(w.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for w in workers:
        w.process.wait(timeout=10)


class Parallel:
    def __init__(self, names, jobs):
        self.names = names
        self.tests = load_tests(names)
        self.jobs = jobs
        self.timings = []
        self.interrupted = None

    def __call__(self, result):
        workers, next_index = [], 0
        selector = selectors.DefaultSelector()
        old_handlers = {}

        def interrupted(signum, frame):
            self.interrupted = signum

        def dispatch(w):
            nonlocal next_index
            index = next_index if next_index < len(self.tests) else None
            if index is not None:
                next_index += 1
            w.assign(index)

        def error(test, message):
            result.startTest(test)
            result.addError(test, (RuntimeError, RuntimeError(message), None))
            result.stopTest(test)

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
                    w.buffer += chunk
                    while b"\n" in w.buffer:
                        line, w.buffer = w.buffer.split(b"\n", 1)
                        data = json.loads(line)
                        w.drain_output(result.stream)
                        merge(data)
                        if data["kind"] == "done":
                            self.timings.append((data["seconds"], str(self.tests[w.current])))
                            w.current = None
                            try:
                                dispatch(w)
                            except BrokenPipeError:
                                pass  # EOF below attributes a lost assignment to this test
                        else:
                            w.finished = True
                    if chunk:
                        continue
                    # All protocol data was consumed before EOF, even if poll() saw the exit earlier.
                    w.process.wait(timeout=10)
                    w.drain_output(result.stream)
                    if w.current is not None:
                        test = self.tests[w.current]
                        error(test, f"worker exited with status {w.process.returncode} while running {test}")
                        self.timings.append((time.perf_counter() - w.started, str(test)))
                    elif not w.finished:
                        error(RemoteTest(["worker shutdown", None]),
                              f"worker exited with status {w.process.returncode} before reporting its final result")
                    stop_workers([w])
                    w.close(selector)
                    workers.remove(w)
                    if next_index < len(self.tests):
                        replacement = Worker(self.names, selector)
                        workers.append(replacement)
                        dispatch(replacement)
            if self.interrupted:
                message = f"runner received {signal.Signals(self.interrupted).name}"
                active = [self.tests[w.current] for w in workers if w.current is not None]
                for test in active or [RemoteTest(["interrupted runner", None])]:
                    error(test, f"{message} while running {test}")
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
    parser.add_argument("names", nargs="*", default=["tests.test_hansa"])
    args = parser.parse_args()
    if args.slowest < 0:
        parser.error("--slowest must be at least 0")
    suite = Parallel(args.names, args.jobs)
    result = unittest.TextTestRunner().run(suite)
    if args.slowest:
        print("\nSlowest tests:", file=sys.stderr)
        for seconds, name in sorted(suite.timings, reverse=True)[:args.slowest]:
            print(f"{seconds:.3f}s {name}", file=sys.stderr)
    return 1 if suite.interrupted or not result.wasSuccessful() else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--worker"]:
        worker(int(sys.argv[2]), int(sys.argv[3]), json.loads(sys.argv[4]))
    else:
        sys.exit(main())
