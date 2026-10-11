"""Give commands private blocking pipes: srt may make its inherited output non-blocking.

Run inside srt, before the lane's environment is applied. Forward each byte, retrying partial
writes and waiting on EAGAIN without changing the flags srt shares. No polling or shutdown grace.
"""
import os
from pathlib import Path
import select
import subprocess
import sys


def protected(command):
    return [sys.executable, str(Path(__file__).resolve()), *command]


def write_all(fd, data):
    pending = memoryview(data)
    while pending:
        try:
            pending = pending[os.write(fd, pending):]
        except BlockingIOError:
            select.select([], [fd], [])


def main():
    # Preserve stdout/stderr ordering when the caller merged them, and separate streams for probes.
    out, err = os.fstat(1), os.fstat(2)
    merged = (out.st_dev, out.st_ino) == (err.st_dev, err.st_ino)
    proc = subprocess.Popen(sys.argv[1:], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT if merged else subprocess.PIPE, bufsize=0)
    streams = {proc.stdout: 1}
    if not merged:
        streams[proc.stderr] = 2
    while streams:
        ready, _, _ = select.select(list(streams), [], [])
        for stream in ready:
            data = os.read(stream.fileno(), 65536)
            if data:
                write_all(streams[stream], data)
            else:
                del streams[stream]
                stream.close()
    rc = proc.wait()
    return rc if rc >= 0 else 128 - rc


if __name__ == "__main__":
    sys.exit(main())
