"""Give commands private blocking pipes: srt may make its inherited output non-blocking.

Run inside srt, before the lane's environment is applied. Forward each byte, retrying partial
writes and waiting on EAGAIN without changing the flags srt shares. No polling or shutdown grace.
"""
import fcntl
import os
from pathlib import Path
import select
import subprocess
import sys
import termios
import threading


def protected(command):
    return [sys.executable, str(Path(__file__).resolve()),
            "command" if sys.platform.startswith("linux") else "eof", *command]


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
    proc = subprocess.Popen(sys.argv[2:], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT if merged else subprocess.PIPE, bufsize=0)
    streams = {proc.stdout: 1}
    if not merged:
        streams[proc.stderr] = 2
    ended, wake, remaining = None, None, None
    if sys.argv[1] == "command":
        # Linux's PID namespace dies with us. Wake on the command's exit even if a leftover
        # holds its output open; drain a fixed snapshot, so a continuing writer cannot delay us.
        ended, wake = os.pipe()

        def wait():
            proc.wait()
            os.close(wake)

        threading.Thread(target=wait, daemon=True).start()
    try:
        while streams:
            ready, _, _ = select.select([*streams, *([ended] if ended is not None else [])], [], [])
            if ended in ready:
                os.close(ended)
                ended = None
                remaining = {s: int.from_bytes(fcntl.ioctl(s, termios.FIONREAD, b"\0" * 4), sys.byteorder)
                             for s in streams}
            for stream in list(streams):
                if remaining is not None and remaining[stream] == 0:
                    del streams[stream]
                    stream.close()
                elif stream in ready:
                    data = os.read(stream.fileno(), min(65536, remaining[stream]) if remaining is not None else 65536)
                    if data:
                        write_all(streams[stream], data)
                        if remaining is not None:
                            remaining[stream] -= len(data)
                            if remaining[stream] == 0:
                                del streams[stream]
                                stream.close()
                    else:
                        del streams[stream]
                        stream.close()
    except BrokenPipeError:
        # Give the command the closed reader it would have met without the relay, then
        # let it finish (including any steps after the failed write) and keep its exit code.
        pass
    finally:
        for stream in streams:
            stream.close()
        if ended is not None:
            os.close(ended)
    rc = proc.wait()
    return rc if rc >= 0 else 128 - rc


if __name__ == "__main__":
    sys.exit(main())
