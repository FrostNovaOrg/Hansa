"""Private push destinations, upgrade, page edits and independent delivery."""
from __future__ import annotations

import os
import re
import unicodedata
import uuid
from pathlib import Path

from . import alert, alert_http, alert_services
from .desk import Desk, KeyScrubError, now, read_json, write_json, write_text

MAX_ENTRIES = 10
ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
LEGACY_PROBLEM = ("Bark's saved key could not be moved; the principal should paste the Bark address again "
                  "on the page (Settings, Alerts)")


def migration_problem(desk):
    return LEGACY_PROBLEM if ("alert_bark" in desk._saved_settings()
                              or (desk.secrets_dir / "alert-bark.device-key").exists()) else ""


def _remove_legacy_files(desk):
    (desk.secrets_dir / "alert-bark.device-key").unlink(missing_ok=True)
    (desk.state_dir / "alert-bark-last.json").unlink(missing_ok=True)


def problems(value) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_ENTRIES:
        return ["alert_push must be a list of at most 10 push addresses"]
    ids = set()
    for entry in value:
        if (not isinstance(entry, dict) or set(entry) - {"id", "service", "on", "name"}
                or not isinstance(entry.get("id"), str) or not ID.fullmatch(entry["id"])
                or entry["id"] in ids or not isinstance(entry.get("service"), str)
                or entry["service"] not in alert_services.SERVICES or type(entry.get("on")) is not bool
                or not isinstance(entry.get("name", ""), str) or len(entry.get("name", "")) > 100
                or any(ord(c) < 32 or ord(c) == 127 for c in entry.get("name", ""))):
            return ["alert_push entries need unique ids, a known service, on (boolean), and an optional name; no secrets"]
        ids.add(entry["id"])
    return []


def entries(settings) -> list[dict]:
    value = settings.get("alert_push", [])
    return value if not problems(value) else []


def private_metadata_problems(desk, value, private=()) -> list[str]:
    secrets = [*keys(desk).values(), *private]
    if any(secret and secret in entry.get("name", "") for entry in value for secret in secrets):
        return ["a push name must not contain a saved secret"]
    return []


def _file(desk, id: str, extra=False) -> Path:
    if not isinstance(id, str) or not ID.fullmatch(id):
        raise ValueError("invalid push entry id")
    return desk.secrets_dir / f"alert-push-{id}.{'secret' if extra else 'address'}"


def _read(desk, id, extra=False):
    try:
        return _file(desk, id, extra).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def has_address(desk, entry) -> bool:
    path = _file(desk, entry["id"])
    return path.is_file() and path.stat().st_size > 0


def enabled(desk, settings) -> list[dict]:
    return [e for e in entries(settings) if e["on"] and has_address(desk, e)]


def _write_secret(desk, id, value, extra=False):
    if not value:
        _file(desk, id, extra).unlink(missing_ok=True)
        return
    desk.secrets_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(desk.secrets_dir, 0o700)
    write_text(_file(desk, id, extra), value + "\n", mode=0o600, follow_symlinks=False)


def keys(desk) -> dict:
    """Also cover an interrupted save's orphan files, without calling settings()."""
    out = {}
    for path in sorted(desk.secrets_dir.glob("alert-push-*.*")):
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not value:
            continue
        out[path.name] = value
        if path.suffix == ".address":
            # Ask all definitions: the saved metadata may not yet have landed.
            for service in alert_services.SERVICES.values():
                try:
                    normalized = service.normalize(value)
                    for n, secret in enumerate(service.secrets(normalized)):
                        if secret:
                            out[f"{path.name}-{service.id}-{n}"] = secret
                except ValueError:
                    pass
    return out


def migrate(desk, loaded):
    """Move legacy Bark once under the desk lock; never duplicate its key."""
    old_key = desk.secrets_dir / "alert-bark.device-key"
    if "alert_bark" not in loaded and not old_key.exists():
        return loaded
    from .history import record
    from .alert_bark import DEFAULT_SERVER, problems as bark_problems
    with desk.lock():
        loaded = desk._saved_settings()
        if "alert_bark" not in loaded and not old_key.exists():
            return loaded
        new = {k: v for k, v in loaded.items() if k != "alert_bark"}
        config = loaded.get("alert_bark", {})
        try:
            if bark_problems(config) or problems(new.get("alert_push", [])):
                return loaded
            key = old_key.read_text(encoding="utf-8").strip() if old_key.exists() else ""
            value = list(new.get("alert_push", []))
            existing = next((e for e in value if e["id"] == "legacy-bark"), None)
            if key:
                _, address = alert_services.parse(config.get("server", DEFAULT_SERVER).rstrip("/") + "/" + key, "bark")
                # Full pasted URLs may have sample text after the key. A saved key
                # is different: migration must preserve every character or stop.
                if alert_services.SERVICES["bark"].secrets(address)[-1] != key:
                    return loaded
                if existing:
                    if existing["service"] != "bark" or _read(desk, existing["id"]) != address:
                        return loaded
                elif len(value) >= MAX_ENTRIES:
                    return loaded
        except (ValueError, OSError, UnicodeError):
            return loaded
        created = key and not existing
        if created:
            entry = {"id": "legacy-bark", "service": "bark", "on": config.get("on") is True, "name": ""}
            value.insert(0, entry)
        new["alert_push"] = value
        try:
            if created:
                _write_secret(desk, entry["id"], address)
                old_last = desk.state_dir / "alert-bark-last.json"
                if old_last.exists():
                    write_json(_last_file(desk, entry["id"]), read_json(old_last, None))
            write_json(desk.settings_path, new)
        except OSError:
            if created:
                _file(desk, entry["id"]).unlink(missing_ok=True)
                _last_file(desk, entry["id"]).unlink(missing_ok=True)
            return loaded
        record(desk, loaded, new, "hansa", "moved Bark setting and key to push addresses")
        _remove_legacy_files(desk)
        return new


def prepare(desk, settings, changes, *, by=""):
    """Validate all edits before writing anything. Return metadata and private writes."""
    if by != "page":
        raise ValueError("push addresses are the principal's, set only on the web page (Settings)")
    if issue := problems(settings.get("alert_push", [])):
        raise ValueError(issue[0])
    if not isinstance(changes, list) or len(changes) > 2 * MAX_ENTRIES:
        raise ValueError("push changes must be a list of at most 20 edits")
    value = [dict(e) for e in entries(settings)]
    writes, removes, seen = [], [], set()
    for edit in changes:
        if not isinstance(edit, dict) or set(edit) - {"id", "service", "on", "name", "address", "secret", "remove"}:
            raise ValueError("invalid push address edit")
        id = edit.get("id")
        entry = next((e for e in value if e["id"] == id), None)
        if id is not None and entry is None:
            raise ValueError("no such push address")
        if id in seen and id is not None:
            raise ValueError("edit each push address only once")
        seen.add(id)
        if type(edit.get("remove", False)) is not bool:
            raise ValueError("push remove must be true or false")
        if edit.get("remove"):
            if entry is None:
                raise ValueError("no such push address")
            value.remove(entry)
            removes.append(id)
            continue
        fresh = entry is None
        if fresh:
            entry = {"id": uuid.uuid4().hex[:12], "service": "", "on": True, "name": ""}
            value.append(entry)
        if "address" in edit:
            service, address = alert_services.parse(edit["address"], edit.get("service", entry["service"] or "recognize"))
            if not fresh and service != entry["service"]:
                writes.append((entry["id"], "", True))  # do not reuse another service's credential
            entry["service"] = service
            writes.append((entry["id"], address, False))
        elif fresh or edit.get("service", entry["service"]) != entry["service"]:
            raise ValueError("a new push service needs its address")
        for field in ("on", "name"):
            if field in edit:
                entry[field] = edit[field]
        if "secret" in edit:
            secret = edit["secret"]
            if not alert_services.SERVICES[entry["service"]].extra_secret:
                raise ValueError("this push service does not take an extra secret")
            if not isinstance(secret, str) or any(c.isspace() or unicodedata.category(c) == "Cc" for c in secret):
                raise ValueError("the extra push secret must be text without spaces or controls")
            writes.append((entry["id"], secret, True))
    if issue := problems(value):
        raise ValueError(issue[0])
    # Check the completed pair, including a retained token on an existing entry.
    # Keep this before writes: short ntfy topics need authenticated access, and
    # Gotify's token may have been pasted separately from its server address.
    pending = {(id, extra): text for id, text, extra in writes}
    for entry in value:
        service = alert_services.SERVICES[entry["service"]]
        validate = getattr(service, "validate", None)
        if validate and any(id == entry["id"] for id, _, _ in writes):
            address = pending.get((entry["id"], False), _read(desk, entry["id"]))
            secret = pending.get((entry["id"], True), _read(desk, entry["id"], True))
            validate(address, secret)
    # A name must never become another copy of a pasted credential in settings.
    private = []
    for id, address, extra in writes:
        service = alert_services.SERVICES[next(e["service"] for e in value if e["id"] == id)]
        private.extend([address] if extra else service.secrets(address))
    if issue := private_metadata_problems(desk, value, private):
        raise ValueError(issue[0])
    return value, writes, removes


def apply(desk, writes, removes):
    """Caller holds the settings lock and has validated the whole save."""
    from .history import record
    failed = []
    for id, value, extra in writes:
        _write_secret(desk, id, value, extra)
        record(desk, {}, {"alert_push": {id: {"secret": "changed"}}}, "page")
    if writes:
        for secret in set(keys(desk).values()):
            failed.extend(desk.scrub(secret, "push"))
    for id in removes:
        for path in (_file(desk, id), _file(desk, id, True), _last_file(desk, id)):
            path.unlink(missing_ok=True)
    saved = desk._saved_settings()
    bark_ids = {e["id"] for e in entries(saved) if e["service"] == "bark"}
    if any(id in bark_ids and value and not extra for id, value, extra in writes):
        # Only a saved Bark address supersedes an upgrade that could not move.
        if "alert_bark" in saved:
            write_json(desk.settings_path, {k: v for k, v in saved.items() if k != "alert_bark"})
        _remove_legacy_files(desk)
    return failed


def check_scrub(failed):
    """Report partial scrub only after the saved entry is available for retry."""
    if failed:
        raise KeyScrubError("The push address was saved, but some files could not be scrubbed; save it again after fixing file permissions")


def save(desk, settings, writes, removes):
    """Save metadata first: a failed settings write must leave no private files."""
    if issues := desk.save_settings(settings, by="page"):
        return issues
    check_scrub(apply(desk, writes, removes))
    return []


def change(desk, changes, *, by=""):
    with desk.lock():
        settings = desk.settings()
        value, writes, removes = prepare(desk, settings, changes, by=by)
        settings["alert_push"] = value
        if issues := desk.save_settings(settings, by=by, dry=True):
            raise ValueError(issues[0])
        if issues := save(desk, settings, writes, removes):
            raise ValueError(issues[0])


def _last_file(desk, id):
    _file(desk, id)  # validate before composing a path
    return desk.state_dir / f"alert-push-{id}-last.json"


def last_result(desk, id):
    value = read_json(_last_file(desk, id), None)
    if not isinstance(value, dict) or not set(value) >= {"at", "ok", "why"}:
        return None
    result = {k: value[k] for k in ("at", "ok", "why")}
    if alert_http.safe_code(value.get("code")) is not None:
        result["code"] = value["code"]
    return result


def public(desk, settings=None):
    out = []
    for entry in entries(desk.settings() if settings is None else settings):
        address = _read(desk, entry["id"])
        try:
            hidden = alert_services.SERVICES[entry["service"]].hidden(address) if address else ""
        except ValueError:
            hidden = ""
        out.append({**entry, "name": desk.redact(entry.get("name", "")), "address": hidden,
                    "address_set": bool(address), "secret_set": bool(_read(desk, entry["id"], True)),
                    "last": last_result(desk, entry["id"])})
    return out


def record(desk, entry, why, code=None):
    # Neither service builders nor transport exceptions may add arbitrary text.
    allowed = {"off", "no_address", "empty", "too_big", "refused_url", "sent", "network", "timeout", "tls",
               "redirect", "http_429", "http_4xx", "http_5xx", "deadline", "refused_by_service"}
    why = why if isinstance(why, str) and why in allowed else "network"
    with desk.lock():
        last = {"at": now(), "ok": why == "sent", "why": why}
        if why != "sent" and alert_http.safe_code(code) is not None:
            last["code"] = code
        write_json(_last_file(desk, entry["id"]), last)
        fields = {"channel": "push", "id": entry["id"], "service": entry["service"], "why": why}
        desk.event("alert_channel", **fields, ok=why == "sent")
        if why != "sent":
            desk.event("alert_failed", **fields)
    return why


def send(desk, entry, items, held=0, *, testing=False):
    try:
        if not testing and not entry["on"]:
            return record(desk, entry, "off")
        address = _read(desk, entry["id"])
        if not address:
            return record(desk, entry, "no_address")
        if not items:
            return record(desk, entry, "empty")
        service = alert_services.SERVICES[entry["service"]]
        try:
            address = service.normalize(address)
            validate = getattr(service, "validate", None)
            if validate:
                validate(address, _read(desk, entry["id"], True))
        except ValueError:
            return record(desk, entry, "refused_url")
        title, body = _text(desk, items, held)
        level = max((x.get("level", alert.level_of(x["kind"])) for x in items), key=alert.LEVELS.index)
        if not service.levels:
            title = urgent_marker(title, level, chinese=alert.chinese(desk))
        try:
            requests = service.requests(address, _read(desk, entry["id"], True), title, body, level)
        except ValueError:
            return record(desk, entry, "too_big")
        results = alert_http.send(requests, deadline_s=service.deadline_s)
        failure = next((r for r in results if r["why"] != "sent"), None)
        return record(desk, entry, failure["why"], failure.get("code")) if failure else record(desk, entry, "sent")
    except Exception:
        return record(desk, entry, "network")


def test(desk, id):
    entry = next((e for e in entries(desk.settings()) if e["id"] == id), None)
    if entry is None:
        return "no_address"
    return send(desk, entry, [{"kind": "test", "level": "active", "text": "a test alert from hansa",
                             "translations": {"en": "a test alert from hansa", "zh": "来自 hansa 的测试提醒"}}], testing=True)


def urgent_marker(text, level, *, chinese=False):
    """Shared by every service without levels, including future chat channels."""
    return (("紧急：" if chinese else "Urgent: ") + text) if level == "urgent" else text


def _text(desk: Desk, items: list[dict], held: int) -> tuple[str, str]:
    lang = "zh" if alert.chinese(desk) else "en"
    safe = []
    codes = [x["code"] for x in items if x.get("code")]
    for item in items:
        translations = item.get("translations") or {}
        text = translations.get(lang, item.get("text", ""))
        if item["kind"] == "release_waiting" and item.get("code") and lang not in translations:
            # A caller passed the command's in-memory batch. Never reuse its
            # code-bearing wording, even for an older queue without translations.
            text = "发布等你在网页上点头" if lang == "zh" else "the release waits for your yes on the page"
        for code in codes:
            text = text.replace(code, "")
        safe.append({**item, "text": text, "code": ""})
    # Reuse the command's language, grouping and character cap, without its codes.
    title = desk.redact(alert.desk_name(desk))
    header = f"[hansa] {'工作台' if lang == 'zh' else 'desk'} {title}"
    body = alert.text_of(desk, safe, held).removeprefix(header).removeprefix(": ").lstrip()
    return title, body

