"""Fresh admission-time source tar, never a checkout or a cached worker tree.
Keep exclusions, descriptor walks, seals and caps while workers control source:
they prevent private reads, link escapes, mixed copies and unbounded transfers.
"""
from contextlib import contextmanager
import os
import tarfile
import tempfile

from .. import attach, run
from ..desk import home_secrets
from .channel import safe_open
from .archive import MAX_BYTES, MAX_ENTRIES, MAX_ARCHIVE, MAX_NAME

EXCLUDED = {n.split("/")[0].casefold() for n in home_secrets()} | {
    ".git", ".hansa", ".claude", "claude.md", "claude.local.md", "agents.md", ".mcp.json",
    ".env", ".cache", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv",
    "node_modules", *run.SANDBOX_PLACEHOLDERS}


def _stamp(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_uid,
            st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _same(seals, name, st):
    stamp = _stamp(st)
    if seals.setdefault(name, stamp) != stamp:
        raise ValueError("snapshot source changed")


@contextmanager
def _open(channel, name, directories):
    fd = os.dup(channel.worktree)
    try:
        _same(directories, "", os.fstat(fd))
        prefix = ""
        for part in name.split("/")[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            prefix += part + "/"
            _same(directories, prefix, os.fstat(fd))
        with os.fdopen(safe_open(fd, name.split("/")[-1]), "rb") as stream:
            yield stream
    finally:
        os.close(fd)


def _names(desk, channel):
    channel.check()
    # Tags protect leading whitespace from wgit's stripped textual result.
    output = run.wgit(desk, channel.directories[1][0], "ls-files", "-t", "-z",
                      "--cached", "--others", "--exclude-standard", "--full-name")
    if len(output) > MAX_ENTRIES * (MAX_NAME + 3):
        raise ValueError("snapshot metadata limit")
    if output and not output.endswith("\0"):
        raise ValueError("incomplete snapshot paths")
    entries = output.split("\0")[:-1]
    if len(entries) > MAX_ENTRIES:
        raise ValueError("snapshot entry limit")
    names = set()
    for entry in entries:
        name = entry[2:]
        parts = name.split("/")
        if (len(entry) < 3 or entry[1] != " " or any(p in ("", ".", "..") for p in parts)
                or "\\" in name or "\ufffd" in name or any(ord(c) < 32 for c in name)
                or len(name.encode("utf-8")) > MAX_NAME):
            raise ValueError("unsafe snapshot path")
        if not any(p.casefold() in EXCLUDED or p.casefold().startswith(".env.") for p in parts):
            names.add(name)
    return sorted(names)


def _scan(desk, channel, denied, directories):
    files, total = {}, 0
    for name in _names(desk, channel):
        try:
            with _open(channel, name, directories) as source:
                st = os.fstat(source.fileno())
                if attach.read_problem(desk, channel.directories[1][0] / name, st, denied):
                    continue
                total += st.st_size
                if total > MAX_BYTES:
                    raise ValueError("snapshot byte limit")
                files[name] = _stamp(st)
        except FileNotFoundError:
            pass  # Git still lists tracked files already deleted before admission.
    return files


def export(desk, channel):
    """At admission, return a rewound, validated temporary tar; caller must close it.
    Exceptions discard the partial tar. Only regular files and executable bits
    cross the boundary; no host ownership, timestamps or environment are sent.
    """
    denied = run.worker_read_denials(desk)
    directories = {"": _stamp(os.fstat(channel.worktree))}
    files = _scan(desk, channel, denied, directories)
    result = tempfile.TemporaryFile()
    try:
        length = 0
        with tarfile.open(fileobj=result, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name in files:
                with _open(channel, name, directories) as source:
                    st = os.fstat(source.fileno())
                    _same(files, name, st)
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = st.st_size, 0o755 if st.st_mode & 0o111 else 0o644
                    length += len(info.tobuf(format=tarfile.PAX_FORMAT)) + (info.size + 511) // 512 * 512
                    # Include PAX metadata, file padding, end markers and record padding.
                    if (length + 1024 + 10239) // 10240 * 10240 > MAX_ARCHIVE:
                        raise ValueError("snapshot archive limit")
                    archive.addfile(info, source)
                    _same(files, name, os.fstat(source.fileno()))
        if _scan(desk, channel, denied, directories) != files:
            raise ValueError("snapshot source changed")
        channel.check()
        _same(directories, "", os.fstat(channel.worktree))
        if result.tell() > MAX_ARCHIVE:
            raise ValueError("snapshot archive limit")
        result.seek(0)
        return result
    except BaseException:
        result.close()
        raise
