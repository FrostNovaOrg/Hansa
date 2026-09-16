"""Optional desk extensions: extra sentinel checks and extra board rows.

A desk sometimes has a local concern the framework has no business knowing about -
a queue only that desk runs, a device only that desk owns. Before this module the
only way to put such a concern on the board was to add a branch to the core and let
every other desk carry it.

An extension is a directory named in `bellhop.json`:

    {"extensions": ["contrib/some-extension", "/srv/desk/another"]}

Relative names are resolved against the desk root. The directory holds a
`bellhop_ext.py` with any of three functions, all optional:

    checks(desk)            -> [(name, ok, detail), ...]   extra sentinel checks
    board_rows(desk)        -> [("H"|"S", text), ...]      extra board rows
    fingerprint_parts(desk) -> [str, ...]                  extra board fingerprint

The direction is one way. The core imports no extension by name and knows nothing
about what any of them do; an extension imports the core as a library.

**An extension may not take the sentinel down with it.** Every call here is wrapped,
including the import, and a failure becomes one red check line naming the extension.
A sentinel that dies because a desk-local add-on raised is worse than no add-on: the
checks it stops running are the ones that catch a stalled desk, and a dead sentinel
and a quiet desk look alike from the outside. The same applies to the board - a page
that fails to print is a page the coordinator works without.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import List, Tuple

from .config import Desk

Check = Tuple[str, bool, str]
Row = Tuple[str, str]

ENTRY = "bellhop_ext.py"

_loaded = {}  # resolved directory -> module or the string that explains the failure


def names(desk: Desk) -> List[str]:
    """The desk's configured extension directories, as written."""
    configured = desk.cfg.get("extensions") or []
    return [str(x) for x in configured if isinstance(x, str) and x.strip()]


def _short(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    return text[:160]


def _label(name: str) -> str:
    return Path(name).name or name


def _load(desk: Desk, name: str):
    """The extension's module, or a string saying why there is none."""
    path = Path(name)
    if not path.is_absolute():
        path = desk.root / path
    try:
        path = path.resolve()
    except OSError as exc:
        return _short(exc)
    entry = path / ENTRY
    if not entry.is_file():
        return f"no {ENTRY} in {path}"
    key = str(entry)
    cached = _loaded.get(key)
    if cached is not None:
        return cached
    try:
        if str(path) not in sys.path:
            # The extension's own package sits beside its entry file; without this
            # its first internal import fails and the reason reads like a bug in it.
            # Appended, never prepended: a directory at the front of sys.path would let
            # an add-on shadow a module the framework itself imports by name.
            sys.path.append(str(path))
        spec = importlib.util.spec_from_file_location("bellhop_ext_" + _label(name).replace("-", "_"), entry)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - an extension may raise anything
        module = _short(exc)
    _loaded[key] = module
    return module


def _call(desk: Desk, name: str, fn_name: str):
    """(value, error). Never raises, whatever the extension does."""
    module = _load(desk, name)
    if isinstance(module, str):
        return None, module
    fn = getattr(module, fn_name, None)
    if fn is None:
        return None, None  # this extension does not use this hook
    try:
        return list(fn(desk)), None
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        return None, _short(exc)


def checks(desk: Desk) -> List[Check]:
    out: List[Check] = []
    for name in names(desk):
        label = _label(name)
        value, error = _call(desk, name, "checks")
        if error:
            out.append((f"ext:{label}", False, f"extension failed: {error}"))
            continue
        for row in value or []:
            try:
                key, ok, detail = row
                out.append((f"ext:{label}:{key}", bool(ok), str(detail)))
            except (TypeError, ValueError):
                out.append((f"ext:{label}", False, "extension returned a check that is not (name, ok, detail)"))
    return out


def board_rows(desk: Desk) -> List[Row]:
    out: List[Row] = []
    for name in names(desk):
        label = _label(name)
        value, error = _call(desk, name, "board_rows")
        if error:
            out.append(("S", f"RED: extension {label} failed: {error}"))
            continue
        for row in value or []:
            try:
                tag, text = row
                out.append(("H" if tag == "H" else "S", str(text)))
            except (TypeError, ValueError):
                out.append(("S", f"RED: extension {label} returned a row that is not (tag, text)"))
    return out


def fingerprint_parts(desk: Desk) -> List[str]:
    """Extension strings folded into the board fingerprint.

    A failure contributes nothing rather than a changing error string: an extension
    that raises a different message every run would otherwise keep the fingerprint
    moving and hold the wake brake open forever.
    """
    out: List[str] = []
    for name in names(desk):
        value, error = _call(desk, name, "fingerprint_parts")
        if error:
            continue
        out.extend(str(x) for x in (value or []))
    return out
