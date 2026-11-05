"""Shared host/guest tar handling; install this standalone module in the guest.
Keep strict headers, caps and descriptor delivery while either peer is untrusted.
Only regular files and one PAX path record are needed by our producers.
"""
from contextlib import closing, contextmanager
import io
import json
import os
import stat
import tarfile
import tempfile
import uuid

MIB = 1024 * 1024
MAX_BYTES, MAX_ENTRIES, MAX_NAME = 256 * MIB, 50_000, 4096
MAX_METADATA, HEADER_BYTES = 32 * MIB, 64 * 1024
MAX_ARCHIVE = MAX_BYTES + MAX_METADATA
FILE_BYTES, RUN_BYTES, OUTPUT_BYTES, ARTIFACT_ENTRIES = 16 * MIB, 64 * MIB, MIB, 1000


def safe_open(directory, name, flags=os.O_RDONLY):
    """Open one owned, single-link regular file through a trusted descriptor."""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise ValueError("unsafe channel filename")
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid():
        os.close(fd)
        raise ValueError("channel file must be an owned, single-link regular file")
    return fd


def _name(name):
    if (any(p in ("", ".", "..") for p in name.split("/")) or "\\" in name
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in name)
            or len(name.encode("utf-8")) > MAX_NAME or name.count("/") > 64):
        raise ValueError("unsafe archive path")
    return name


def _copy(source, target, size):
    while size:
        data = source.read(min(size, HEADER_BYTES))
        if not data:
            raise ValueError("incomplete archive")
        target.write(data)
        size -= len(data)


@contextmanager
def _load(source, length, maximum, entries, per_file, raw_limit):
    # Spool exactly the declared length: stdin must remain open for cancellation.
    if type(length) is not int or not 1024 <= length <= raw_limit or length % 512:
        raise ValueError("archive length limit")
    with tempfile.TemporaryFile() as stream:
        _copy(source, stream, length)
        stream.seek(0)
        members, pending, total, metadata = {}, None, 0, 0
        while True:
            block = stream.read(512)
            if block == bytes(512):
                if pending is not None or stream.read(512) != bytes(512):
                    raise ValueError("incomplete archive terminator")
                while True:
                    tail = stream.read(HEADER_BYTES)
                    if not tail:
                        break
                    if any(tail):
                        raise ValueError("trailing archive data")
                break
            info = tarfile.TarInfo.frombuf(block, "utf-8", "strict")
            end = stream.tell() + (info.size + 511) // 512 * 512
            metadata += 512 + (-info.size % 512)
            if info.size < 0 or end > length - 1024 or metadata > MAX_METADATA:
                raise ValueError("archive size or metadata limit")
            if info.type == tarfile.XHDTYPE:
                if pending is not None or info.size > HEADER_BYTES:
                    raise ValueError("archive metadata limit")
                data = stream.read(info.size)
                count, separator, value = data.partition(b" path=")
                if (not separator or not count.isdigit() or len(count) > 8
                        or int(count) != len(data) or not value.endswith(b"\n")):
                    raise ValueError("unsupported archive metadata")
                pending = _name(value[:-1].decode("utf-8"))
                metadata += info.size
            else:
                if info.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
                    raise ValueError("archive requires regular files")
                info.name = _name(pending if pending is not None else info.name)
                pending = None
                total += info.size
                if info.name in members or len(members) >= entries or info.size > per_file or total > maximum:
                    raise ValueError("duplicate archive path or file limit")
                info.offset_data = stream.tell()
                info.mode = 0o755 if info.mode & 0o111 else 0o644
                members[info.name] = info
            stream.seek(end)
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode="r:") as archive:
            yield archive, members


def unpack(source, length, destination):
    """Guest only: destination must be new, inside the private, inactive slot."""
    with _load(source, length, MAX_BYTES, MAX_ENTRIES, MAX_BYTES, MAX_ARCHIVE) as (archive, members):
        os.mkdir(destination, 0o700)
        for member in members.values():
            archive.extract(member, destination, filter="data")


def _walk(directory, skip, prefix=""):
    """Yield entries depth-first; the consumer adds a directory's name to skip to prune it."""
    with os.scandir(directory) as entries:
        for entry in entries:
            name = prefix + entry.name
            directory_entry = entry.is_dir(follow_symlinks=False)
            yield directory, entry.name, name, directory_entry
            if directory_entry and name not in skip:
                child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    yield from _walk(child, skip, name + "/")
                finally:
                    os.close(child)


def _add(archive, name, source, size):
    info = tarfile.TarInfo(name)
    info.size, info.mode = size, 0o600
    archive.addfile(info, source)


@contextmanager
def collect(directory, result, stdout=b"", stderr=b""):
    """After namespace teardown, yield a temporary result tar from an /out fd.
    Directory entries also consume the traversal budget; rejected is a flag.
    """
    total, rejected, skipped = 0, False, set()
    with tempfile.TemporaryFile() as stream:
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
            with closing(_walk(directory, skipped)) as entries:
                for count, (fd, base, name, is_directory) in enumerate(entries):
                    if count >= ARTIFACT_ENTRIES:
                        rejected = True
                        break
                    try:
                        _name("artifacts/" + name)
                        if is_directory:
                            continue
                        source = os.fdopen(safe_open(fd, base), "rb")
                    except (OSError, ValueError):
                        rejected = True
                        if is_directory:
                            skipped.add(name)  # Prune its subtree; later artifacts still return.
                        continue
                    with source:
                        before = os.fstat(source.fileno())
                        if before.st_size > FILE_BYTES or total + before.st_size > RUN_BYTES:
                            rejected = True
                            continue
                        _add(archive, "artifacts/" + name, source, before.st_size)
                        after = os.fstat(source.fileno())
                        if any(getattr(before, k) != getattr(after, k) for k in
                               ("st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")):
                            raise ValueError("artifact changed")
                        total += before.st_size
            summary = dict(result, rejected=rejected,
                           truncated=bool(result.get("truncated")) or len(stdout) + len(stderr) > OUTPUT_BYTES)
            data = json.dumps(summary, allow_nan=False).encode("utf-8")
            if len(data) > HEADER_BYTES:
                raise ValueError("result metadata limit")
            stdout = stdout[:OUTPUT_BYTES]
            for name, data in (("result.json", data), ("stdout", stdout),
                               ("stderr", stderr[:OUTPUT_BYTES - len(stdout)])):
                _add(archive, name, io.BytesIO(data), len(data))
        stream.seek(0)
        yield stream


def deliver(source, length, channel, used):
    """Copy through the recorded worker fd; broker retains returned used across runs.
    Failure removes partial deliveries and never returns a successful result.
    """
    if type(used) is not int or not 0 <= used <= RUN_BYTES:
        raise ValueError("worker artifact allowance")
    created, files, total = [], {}, 0
    channel.check()
    try:
        with _load(source, length, RUN_BYTES + OUTPUT_BYTES + HEADER_BYTES, ARTIFACT_ENTRIES + 3,
                   max(FILE_BYTES, OUTPUT_BYTES, HEADER_BYTES), RUN_BYTES + OUTPUT_BYTES + MAX_METADATA) as (archive, members):
            if not {"result.json", "stdout", "stderr"} <= members.keys():
                raise ValueError("missing result metadata or output")
            if (members["result.json"].size > HEADER_BYTES
                    or sum(members[n].size for n in ("stdout", "stderr")) > OUTPUT_BYTES):
                raise ValueError("result metadata or output limit")
            result = json.load(archive.extractfile(members["result.json"]))
            if type(result) is not dict:
                raise ValueError("invalid result metadata")
            json.dumps(result, allow_nan=False)
            stdout, stderr = (archive.extractfile(members[n]).read() for n in ("stdout", "stderr"))
            for member in members.values():
                if member.name in ("result.json", "stdout", "stderr"):
                    continue
                total += member.size
                if not member.name.startswith("artifacts/") or member.size > FILE_BYTES or total > RUN_BYTES:
                    raise ValueError("artifact path or run allowance")
                if used + member.size > RUN_BYTES:
                    result["rejected"] = True
                    continue
                target = "bench-" + uuid.uuid4().hex
                with os.fdopen(safe_open(channel.directory, target, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as out:
                    created.append(target)
                    _copy(archive.extractfile(member), out, member.size)
                used += member.size
                files[member.name[10:]] = target
        channel.check()
        return dict(result=result, stdout=stdout, stderr=stderr, artifacts=files, used=used)
    except BaseException:
        for name in created:
            os.unlink(name, dir_fd=channel.directory)
        raise
