"""Append-only settings history, separate from the desk's activity log."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .desk import HIDDEN_BY, append_event, event_record, now

VALUE_LIMIT = 2048  # characters of a value's JSON spelling, before an explicit truncation marker
MISSING = object()
SECRET_FIELDS = {"key", "token", "api_key", "api_token", "auth_token", "access_token", "secret"}


def _json(value):
    # As settings.json does, preserve filesystem bytes as escapes without hiding ordinary Unicode.
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8", "backslashreplace").decode("utf-8")


def _changes(old, new, path=()):
    if old == new and type(old) is type(new):
        return
    # env is one lane setting, as are lists. Named lanes, providers and targets are maps of settings.
    if (not path or path[-1] != "env") and (isinstance(old, dict) or old is MISSING) and (
            isinstance(new, dict) or new is MISSING):
        before, after = ({} if old is MISSING else old), ({} if new is MISSING else new)
        if before or after:
            for key in sorted(before.keys() | after.keys()):
                if not path and key == HIDDEN_BY:  # provenance of the no-read list, not another setting
                    continue
                yield from _changes(before.get(key, MISSING), after.get(key, MISSING), (*path, key))
            return
    if path:
        yield path, old, new


def _credentials(value):
    """Also cover credentials in an older settings spelling, including a removed provider."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in SECRET_FIELDS and isinstance(child, str) and child:
                yield child
            yield from _credentials(child)
    elif isinstance(value, list):
        for child in value:
            yield from _credentials(child)


def _keys(desk):
    keys = list(desk.keys().values())
    token_file = desk.state_dir / "web-token"
    if token_file.is_file():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            keys.append(token)
    return keys


def _private(value, keys):
    if isinstance(value, str):
        return any(key in value for key in keys)
    if isinstance(value, dict):
        return any(k.lower() in SECRET_FIELDS or _private(k, keys) or _private(v, keys)
                   for k, v in value.items())
    if isinstance(value, list):
        return any(_private(v, keys) for v in value)
    return False


def _clean(text, keys):
    for key in sorted(keys, key=len, reverse=True):
        text = text.replace(key, "[key]")
    return text


def _start(desk):
    """Caller holds the desk lock; the first line persists the actual start, even on an older desk."""
    path = desk.state_dir / "settings-history.jsonl"
    if not path.exists() or not path.stat().st_size:
        append_event(path, _json({"recording_since": now()}))
    return path


def record(desk, old: dict, new: dict, by: str = "", command: str = ""):
    """Call under the same desk lock as the write, with the values actually read and written.

    Check complete values before truncation: even a key beyond the retained prefix hides both values.
    Keep this check while settings or standing instructions can contain credentials.
    """
    changes = list(_changes(old, new))
    if not changes:
        return
    with desk.lock():
        keys = list(dict.fromkeys([*_keys(desk), *_credentials(old), *_credentials(new)]))
        path, ts = _start(desk), now()
        for parts, before, after in changes:
            entry = {"ts": ts, "by": _clean("principal" if by == "page" else by or "unknown", keys),
                     "path": _clean(".".join(parts), keys)}
            if command:
                entry["command"] = _clean(command, keys)
            if (any(p.lower() in SECRET_FIELDS for p in parts) or _private(before, keys)
                    or _private(after, keys)):
                entry["redacted"] = True
            else:
                for side, value in (("old", before), ("new", after)):
                    if value is MISSING:
                        entry[side + "_missing"] = True
                        continue
                    spelling = _json(value)
                    if len(spelling) > VALUE_LIMIT:
                        entry[side] = spelling[:VALUE_LIMIT]
                        entry[side + "_truncated"] = True
                    else:
                        entry[side] = value
            append_event(path, _json(entry))


def _value(entry, side):
    if entry.get(side + "_missing"):
        return "(unset)"
    if entry.get(side + "_truncated"):
        return entry[side] + f"… [truncated at {VALUE_LIMIT} characters]"
    return _json(entry[side])


def _time(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def text(desk, kind: str = "", name: str = "", last: int | None = None) -> str:
    with desk.lock():
        path = _start(desk)
        # The general event log is never opened. Ignore an interrupted append, as event readers do.
        with path.open("rb") as source:
            records = [e for line in source if (e := event_record(line))]
        keys = _keys(desk)
    # The first append may itself be interrupted; use the surviving start or earliest entry.
    since = next((e["recording_since"] for e in records if "recording_since" in e), None)
    if since is None:
        since = min((e["ts"] for e in records if "ts" in e), default=now())
    prefix = f"{'lanes' if kind == 'lane' else 'providers'}.{name}." if kind else ""
    entries = [e for e in reversed(records) if "path" in e and (not prefix or e["path"].startswith(prefix))]
    if last is not None:
        entries = entries[:last]
    lines = [f"Settings changes recorded since {_time(since)}; earlier changes are unavailable."]
    for entry in entries:
        who = entry["by"] + (f" ({entry['command']})" if entry.get("command") else "")
        change = ("changed (values withheld)" if entry.get("redacted")
                  or _private(entry.get("old"), keys) or _private(entry.get("new"), keys)
                  else f"{_value(entry, 'old')} → {_value(entry, 'new')}")
        lines.append(f"{_time(entry['ts'])}  {who}  {entry['path']}: {change}")
    if not entries:
        lines.append("No settings changes recorded" + (f" for {kind} {name}" if kind else "") + ".")
    return _clean("\n".join(lines), keys)
