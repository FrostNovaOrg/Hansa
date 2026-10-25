"""Account-wide rests, identified only by hashes; local state survives an unavailable account file."""
import fcntl
import hashlib
import json
import time

from .desk import claude_home, read_json, write_json


def rests_file():
    from .up import desks_file
    return desks_file().with_name("provider-rests.json")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def login(desk, name, p):
    if p["kind"] == "subscription":
        return fingerprint(str(claude_home(desk.state_dir, name, p).resolve()))
    return fingerprint([p.get("base_url"), desk.read_key(name)]) if desk.has_key(name) else ""


def read():
    try:
        entries = read_json(rests_file(), {})
        return entries if isinstance(entries, dict) else {}
    except (OSError, ValueError):
        return {}


def record(desk, name, **fields):
    try:
        key = login(desk, name, desk.settings()["providers"][name])
        if not key:
            return
        path = rests_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            entries = read()
            old = entries.get(key)
            entry = old.copy() if isinstance(old, dict) else {}
            entry.update(fields, source=fingerprint(str(desk.root)))
            entries[key] = entry
            write_json(path, entries)
    except (OSError, ValueError):
        pass  # sharing is best effort; the desk has already saved its own rest


def include(desk, states):
    entries = read()
    if not entries:
        return states
    for name, p in desk.settings()["providers"].items():
        try:
            entry = entries.get(login(desk, name, p))
            if not isinstance(entry, dict):
                continue
            credit, until, cleared = (entry.get(k, 0) for k in ("credit_since", "cool_until", "credit_cleared"))
            if not all(isinstance(x, (int, float)) and 0 <= x < float("inf") for x in (credit, until, cleared)):
                continue
            credit = credit if credit > cleared else 0
            st = states.setdefault(name, {})
            if st.get("credit_since") and st["credit_since"] <= cleared:
                st["credit_since"] = 0
            if entry.get("source") != fingerprint(str(desk.root)) and (credit or until > time.time()):
                if not st.get("credit_since") and (credit or until > st.get("cool_until", 0)):
                    st.update(credit_since=credit, cool_until=until, shared_rest=True,
                              reason="another desk found the usage limit", rest_how="shared", rest_minutes=None)
        except (OSError, ValueError):
            continue
    return states
