"""Local queue: register only trusted run/worktree records; each broker needs a fresh epoch.
Keep descriptor, link and epoch checks while workers can mutate the channel.
"""
import json
import os
from pathlib import Path
import re
import stat
import uuid

LIMIT = 64 * 1024


def safe_open(directory, name, flags=os.O_RDONLY):
    """Open one regular, single-link file relative to an already trusted descriptor."""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise ValueError("unsafe channel filename")
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid():
        os.close(fd)
        raise ValueError("channel file must be an owned, single-link regular file")
    return fd


def _pairs(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate channel field")
    return result


def _read(directory, name):
    with os.fdopen(safe_open(directory, name), "rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > LIMIT:
            raise ValueError("channel size limit")
        data = stream.read(LIMIT + 1)
        after = os.fstat(stream.fileno())
        if len(data) > LIMIT or any(getattr(before, k) != getattr(after, k) for k in
                                  ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")):
            raise ValueError("channel size limit or file changed")
    return json.loads(data, object_pairs_hook=_pairs)


def _publish(directory, name, value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(data) > LIMIT:
        raise ValueError("channel size limit")
    temporary = ".bench-" + uuid.uuid4().hex
    with os.fdopen(safe_open(directory, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as stream:
        try:
            stream.write(data)
            stream.flush()
            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        finally:
            os.unlink(temporary, dir_fd=directory)


def _token(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{32}", value)


def _validate(request):
    fields = {"version", "id", "epoch", "script", "cwd", "timeout", "wait"}
    if type(request) is not dict or set(request) != fields:
        raise ValueError("invalid request fields")
    if type(request["version"]) is not int or request["version"] != 1:
        raise ValueError("invalid request version")
    if not all(_token(request[k]) for k in ("id", "epoch")):
        raise ValueError("invalid request identity")
    if not isinstance(request["script"], str) or "\0" in request["script"]:
        raise ValueError("invalid script")
    cwd = request["cwd"]
    if not isinstance(cwd, str) or "\\" in cwd or "\0" in cwd or (
            cwd != "." and any(p in ("", ".", "..") for p in cwd.split("/"))):
        raise ValueError("cwd must be relative")
    for key, maximum in (("timeout", 900), ("wait", 300)):
        if type(request[key]) is not int or not 1 <= request[key] <= maximum:
            raise ValueError("invalid " + key)


def submit(directory, epoch, script, cwd=".", timeout=300, wait=60):
    """Publish a complete request; no host paths or worker identities cross the queue."""
    request = dict(version=1, id=uuid.uuid4().hex, epoch=epoch, script=script,
                   cwd=cwd, timeout=timeout, wait=wait)
    _validate(request)
    _publish(directory, request["id"] + ".request", request)
    return request["id"]


class Channel:
    """Trusted run/paths only. Share a fresh broker epoch; close at worker teardown."""
    def __init__(self, temporary, run, worktree, epoch=None):
        self.run, self.epoch = run, epoch or uuid.uuid4().hex
        self.claimed, self.replied, self.directories = set(), set(), []
        try:
            for path in (temporary, worktree):
                fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                self.directories.append((Path(path), fd, os.fstat(fd)))
        except BaseException:
            self.close()
            raise
        self.directory, self.worktree = (entry[1] for entry in self.directories)

    def check(self):
        for path, fd, recorded in self.directories:
            current = path.lstat()
            if (current.st_dev, current.st_ino, current.st_mode) != (
                    recorded.st_dev, recorded.st_ino, recorded.st_mode):
                raise ValueError("recorded directory replaced")

    def close(self):
        for _, fd, _ in self.directories:
            os.close(fd)
        self.directories.clear()


def claim(channel, request_id):
    channel.check()
    if not _token(request_id):
        raise ValueError("invalid request id")
    request = _read(channel.directory, request_id + ".request")
    _validate(request)
    if request["id"] != request_id or request["epoch"] != channel.epoch:
        raise ValueError("stale epoch or mismatched request")
    if request_id in channel.claimed:
        raise ValueError("request already claimed")
    # Exclusive marker covers competing claimers; memory also survives worker unlinking.
    fd = safe_open(channel.directory, request_id + ".claimed", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    os.close(fd)
    channel.claimed.add(request_id)
    return request


def reply(channel, request_id, result):
    channel.check()
    if request_id not in channel.claimed or request_id in channel.replied:
        raise ValueError("request is not awaiting a reply")
    _publish(channel.directory, request_id + ".reply", dict(id=request_id, epoch=channel.epoch, result=result))
    channel.replied.add(request_id)


def command(args):
    directory, epoch = os.environ.get("HANSA_BENCH_DIR"), os.environ.get("HANSA_BENCH_EPOCH")
    if not directory or not epoch:
        raise SystemExit("hansa bench: disabled for this worker")
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            script = Path(args.script)
            parent = os.open(script.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with os.fdopen(safe_open(parent, script.name), "rb") as stream:
                    text = stream.read(LIMIT + 1).decode("utf-8")
            finally:
                os.close(parent)
            print(submit(fd, epoch, text, args.cwd, args.timeout, args.wait))
        finally:
            os.close(fd)
    except (OSError, ValueError):
        raise SystemExit("hansa bench: request refused") from None
