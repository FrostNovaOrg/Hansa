"""Append-only settings history, separate from the desk's activity log."""
from __future__ import annotations

import json
import time

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


def _private(value, keys, path=()):
    # Item 2: keep this rule while Item 2 applies.
    if path in (("alert",), ("alert", "command")):
        return True
    if isinstance(value, str):
        return any(key in value for key in keys)
    if isinstance(value, dict):
        return any(k.lower() in SECRET_FIELDS or _private(k, keys) or _private(v, keys, (*path, k))
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
            if (any(p.lower() in SECRET_FIELDS for p in parts) or _private(before, keys, parts)
                    or _private(after, keys, parts)):
                entry["redacted"] = True
                if parts[-1].lower() in SECRET_FIELDS and (isinstance(after, str) or after is MISSING):
                    entry["secret_action"] = "cleared" if after is MISSING or after == "" else "set"
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


def read(desk, kind: str = "", name: str = "", last: int | None = None) -> dict:
    """Newest matching entries, safe for either the command or the page; never write history."""
    with desk.lock():
        path = desk.state_dir / "settings-history.jsonl"
        # The general event log is never opened. Ignore an interrupted append, as event readers do.
        records = []
        if path.exists():
            with path.open("rb") as source:
                records = [e for line in source if (e := event_record(line))]
        keys = _keys(desk)
    # The first append may itself be interrupted; use the surviving start or earliest entry.
    since = next((e["recording_since"] for e in records if "recording_since" in e), None)
    if since is None:
        since = min((e["ts"] for e in records if "ts" in e), default=None)
    prefix = f"{'lanes' if kind == 'lane' else 'providers'}.{name}." if kind else ""
    entries = [e for e in reversed(records) if "path" in e and (not prefix or e["path"].startswith(prefix))]
    if last is not None:
        entries = entries[:last]
    for entry in entries:
        parts = tuple(entry["path"].split("."))
        if entry.get("redacted") or _private(entry.get("old"), keys, parts) or _private(entry.get("new"), keys, parts):
            entry["redacted"] = True
            for side in ("old", "new"):
                entry.pop(side, None)
        for field in ("by", "command", "path"):
            if field in entry:
                entry[field] = _clean(entry[field], keys)
        action = entry.get("secret_action")
        entry["change"] = ((action if action in ("set", "cleared") else "changed") + " (values withheld)" if entry.get("redacted")
                           else f"{_value(entry, 'old')} → {_value(entry, 'new')}")
    return {"recording_since": since, "entries": entries}


def _time(ts):
    """Local time, in the form hansa status and hansa show use; the JSON keeps the timestamp."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def changes(entries: list) -> list:
    """Records grouped by the change that wrote them: one command or page save writes all its fields with one
    time, one actor and one command. Order kept (newest first, as read gives them)."""
    groups = []
    for entry in entries:
        key = (entry.get("ts"), entry.get("by"), entry.get("command"))
        if groups and groups[-1][0] == key:
            groups[-1][1].append(entry)
        else:
            groups.append((key, [entry]))
    return [fields for _, fields in groups]


def how(entry, word: str = "with") -> str:
    """Who made a recorded change, as a phrase: 'with hansa init', 'by the principal on the web page'."""
    if entry.get("command"):
        return f"{word} {entry['command']}"
    return "by the principal on the web page" if entry.get("by") == "principal" else "by " + entry.get("by", "unknown")


def origin(desk, kind: str, name: str, made_field: str):
    """(made, last) for a lane or provider, from the records: the newest record that created made_field, and the
    newest change after it by another command (None when there is none). (None, None) when nothing is recorded."""
    entries = read(desk, kind, name)["entries"]
    if not entries:
        return None, None
    prefix = f"{'lanes' if kind == 'lane' else 'providers'}.{name}."
    made = next((e for e in entries if e["path"] == prefix + made_field and e.get("old_missing")), None)
    newest = entries[0]
    if made is not None and (newest.get("by"), newest.get("command")) == (made.get("by"), made.get("command")):
        newest = None  # the command that made it, its later records included (hansa lane add --like's brief)
    return made, newest


def text(desk, kind: str = "", name: str = "", last: int | None = None) -> str:
    """Newest first, one change per command or page save with its fields under it; last counts changes."""
    with desk.lock():
        _start(desk)  # preserve the command's persistent start on an older desk
        data = read(desk, kind, name)
    lines = [f"Settings changes recorded since {_time(data['recording_since'] or now())}; earlier changes are unavailable."]
    groups = changes(data["entries"])
    if last is not None:
        groups = groups[:last]
    for fields in groups:
        first = fields[0]
        who = first["by"] + (f" ({first['command']})" if first.get("command") else "")
        lines.append(f"{_time(first['ts'])}  {who}" + (f", {len(fields)} fields" if len(fields) > 1 else ""))
        lines += [f"  {entry['path']}: {entry['change']}" for entry in reversed(fields)]  # in the order written
    if not groups:
        lines.append("No settings changes recorded" + (f" for {kind} {name}" if kind else "") + ".")
    return "\n".join(lines)
