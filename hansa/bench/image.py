"""The guest's system image, kept between rebuilds in the desk's state (`.hansa/bench-image/`).

Rebuild removes the bench's private folder, Lima's download cache with it (Lima keeps it under its
HOME, and 2.2.1 has no setting to move it). So hansa keeps one verified copy of the image the recipe
names beside that folder, and before Lima starts puts it into the new private folder's cache, where
Lima finds it and does not download. A kept file is used only after hansa itself computes its sha512
and finds the recipe's digest; anything else there is removed. With no kept image, a good copy in the
account's own Lima cache is copied in instead of a download.
"""
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import stat

from .vm import _private

RECIPE = Path(__file__).resolve().parents[2] / "contrib/bench/lima.yaml"
DOWNLOAD_FAILED = ("The bench's system image could not be downloaded; run hansa bench rebuild again "
                   "(a good copy, once downloaded, is kept).")
PARTIAL = ".partial-"


def recipe_image(recipe=RECIPE):
    """(location, sha512 hex) of the recipe's aarch64 image."""
    text = "".join(line for line in recipe.read_text(encoding="utf-8").splitlines(True)
                   if not line.lstrip().startswith("#"))
    images = [i for i in json.loads(text)["images"] if i.get("arch") == "aarch64"]
    location, digest = images[0]["location"], images[0]["digest"]
    algorithm, _, value = digest.partition(":")
    if algorithm != "sha512" or len(value) != 128 or value.strip("0123456789abcdef"):
        raise ValueError("bench recipe image needs a sha512 digest")
    return location, value


def account_cache():
    """The account's own cache folder (macOS), not the bench's private HOME."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / "Library" / "Caches"


def lima_entry(cache, location):
    """Where Lima keeps a download of this URL text in a cache folder."""
    return Path(cache) / "lima" / "download" / "by-url-sha256" / hashlib.sha256(location.encode()).hexdigest()


def kept_dir(desk):
    return desk.state_dir / "bench-image"


def _name(digest):
    return "sha512-" + digest


def _copy_checked(source, target, digest, *, own=True):
    """Copy source to a new target, hashing the bytes copied; keep the target only on the recipe's
    digest. The source must be a plain file, not a link (and the account's, own=True). Returns
    whether it matched; on any mismatch the target is not left behind."""
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return False
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or (own and info.st_uid != os.getuid()):
        os.close(fd)
        return False
    with os.fdopen(fd, "rb") as stream:
        out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        matched = False
        try:
            hashed = hashlib.sha512()
            with os.fdopen(out, "wb") as written:
                while chunk := stream.read(1 << 20):
                    hashed.update(chunk)
                    written.write(chunk)
            matched = hashed.hexdigest() == digest
        finally:
            if not matched:
                os.unlink(target)
    return matched


def _remove(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _kept_ok(path):
    """The account's own private plain file, unlinked (vm._private's checks)."""
    try:
        _private(path)
        return True
    except (OSError, ValueError):
        return False


def _place(kept, private, location, digest):
    """Hash-copy the kept file into the private folder's Lima cache, beside the digest file Lima
    trusts (written only after hansa computed the digest itself)."""
    cache = Path(private) / "Library" / "Caches"
    entry = lima_entry(cache, location)
    for path in (cache.parent, cache, cache / "lima", cache / "lima" / "download", entry.parent, entry):
        _private(path, True)
    if not (_kept_ok(kept) and _copy_checked(kept, entry / "data", digest)):
        return False
    (entry / "sha512.digest").write_text("sha512:" + digest, encoding="utf-8")
    (entry / "url").write_text(location, encoding="utf-8")
    return True


def seed(desk, private, recipe=RECIPE):
    """Before Lima starts: put the verified image into the private folder's Lima cache. Returns
    "kept", "copied" (from the account's Lima cache) or "" (Lima downloads)."""
    location, digest = recipe_image(recipe)
    folder = kept_dir(desk)
    _private(folder, True)
    for entry in folder.iterdir():
        if entry.name.startswith(PARTIAL):
            _remove(entry)  # A copy hansa did not finish is never an image.
    kept = folder / _name(digest)
    if os.path.lexists(kept):
        if _place(kept, private, location, digest):
            return "kept"
        _remove(kept)  # Another recipe's bytes, truncated, a link, a folder or not the account's.
    partial = folder / (PARTIAL + str(os.getpid()))
    if not _copy_checked(lima_entry(account_cache(), location) / "data", partial, digest, own=False):
        return ""  # Left as it is: a link, a folder or other bytes there are not hansa's to remove.
    os.replace(partial, kept)
    if _place(kept, private, location, digest):
        return "copied"
    _remove(kept)
    return ""


def keep(desk, private, recipe=RECIPE):
    """After Lima's start: keep the image it downloaded, once its sha512 is the recipe's; then remove
    every other file in the kept folder (an earlier recipe's image)."""
    location, digest = recipe_image(recipe)
    folder = kept_dir(desk)
    _private(folder, True)
    kept = folder / _name(digest)
    if not (os.path.lexists(kept) and _kept_ok(kept)):
        partial = folder / (PARTIAL + str(os.getpid()))
        _remove(partial)
        source = lima_entry(Path(private) / "Library" / "Caches", location) / "data"
        if not _copy_checked(source, partial, digest):
            return False
        _remove(kept)
        os.replace(partial, kept)
    for entry in folder.iterdir():
        if entry.name != kept.name:
            _remove(entry)
    return True
