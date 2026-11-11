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


def record(desk, name, *, checked=False, **fields):
    """A good check ends the login's usage rest only for its recorded model (or an old, unlabelled rest).
    Return the checked login/rest fingerprint so this provider can ignore that rest even for another model."""
    checked_rest = ""
    try:
        p = desk.settings()["providers"][name]
        key = login(desk, name, p)
        if not key:
            return
        path = rests_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            entries = read()
            old = entries.get(key)
            entry = old.copy() if isinstance(old, dict) else {}
            if checked:
                if not entry:
                    return ""
                checked_rest = fingerprint([key, entry.get("cool_since", 0)])
                if entry.get("model") not in (None, fingerprint(p["model"])):
                    return checked_rest
                entry.update(cool_until=0, cool_cleared=time.time(), quota_probe_ok=fields.get("quota_probe_ok", 0))
            else:
                entry.update(fields, source=fingerprint(str(desk.root)))
                if "cool_since" in fields:
                    entry.update(model=fingerprint(p["model"]), provider=fingerprint(name))
            entries[key] = entry
            write_json(path, entries)
    except (OSError, ValueError):
        pass  # sharing is best effort; local rest/check state does not depend on this file
    return checked_rest


def include(desk, states):
    entries = read()
    if not entries:
        return states
    for name, p in desk.settings()["providers"].items():
        try:
            key = login(desk, name, p)
            entry = entries.get(key)
            if not isinstance(entry, dict):
                continue
            credit, until, cleared, since, cool_cleared, reset, probe_ok = (entry.get(k, 0) for k in
                ("credit_since", "cool_until", "credit_cleared", "cool_since", "cool_cleared", "cool_reset", "quota_probe_ok"))
            if not all(isinstance(x, (int, float)) and 0 <= x < float("inf")
                       for x in (credit, until, cleared, since, cool_cleared, reset, probe_ok)):
                continue
            credit = credit if credit > cleared else 0
            st = states.setdefault(name, {})
            if entry.get("model") in (None, fingerprint(p["model"])) and probe_ok:
                st["quota_probe_ok"] = max(st.get("quota_probe_ok", 0), probe_ok)
            if st.get("credit_since") and st["credit_since"] <= cleared:
                st["credit_since"] = 0
            # Keep this distinction while usage limits can belong to one configured model: a check
            # of another model frees only that provider, and only until the next recorded limit.
            if (cool_cleared and entry.get("model") in (None, fingerprint(p["model"]))
                    and st.get("rest_how") in ("fixed", "reset") and not st.get("credit_since")
                    and st.get("cool_since", 0) <= cool_cleared):
                st.update(cool_until=0, reason="", rest_how="", rest_minutes=None)
            own_desk = entry.get("source") == fingerprint(str(desk.root))
            own_provider = own_desk and entry.get("provider") in (None, fingerprint(name))
            if own_provider or st.get("quota_checked") == fingerprint([key, since]):
                until = 0
            if own_desk:
                credit = 0  # credit sharing and its page-only top-up rules are unchanged
            if credit or until > time.time():
                if not st.get("credit_since") and (credit or until > st.get("cool_until", 0)):
                    st.update(credit_since=credit, cool_until=until, shared_rest=True,
                              reason="another desk found the usage limit", rest_how="shared", rest_minutes=None,
                              cool_since=since, cool_reset=reset,
                              quota_probe_blocked=entry.get("quota_probe_blocked", False))
        except (OSError, ValueError):
            continue
    return states
