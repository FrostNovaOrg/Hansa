"""Push service registry. Definitions own parsing, masking, limits and requests.

A service supplies id, names/hints (en/zh), extra_secret, deadline_s, max_bytes,
levels, recognizes(value), normalize(value), secrets(address), hidden(address),
and requests(address, secret, title, body, level), with optional
validate(address, secret) for the completed credential pair. Requests use
alert_http.send's private interface; no response bodies or credentials cross
into public results.
"""
from urllib.parse import urlsplit

from .alert_bark import Bark, _pasted_address
from .alert_chat import DingTalk, Discord, Feishu, JsonWebhook, Slack, Telegram, WeCom
from .alert_phone import Gotify, Ntfy, PushDeer, ServerChan

SERVICES = {service.id: service for service in (
    Bark(), Ntfy(), Gotify(), PushDeer(), ServerChan(),
    WeCom(), DingTalk(), Feishu(), Telegram(), Discord(), Slack(), JsonWebhook())}


def _foreign_address(value: str):
    """Another service whose own address this is, or None.

    Bark takes the first path segment as its device key, so its loose grammar
    also accepts another service's endpoint and hides the wrong part. The
    JSON webhook accepts every address by design, so it names nothing here.
    """
    try:
        path = urlsplit(value).path
    except ValueError:
        path = ""
    for service in SERVICES.values():
        if service.id in ("bark", "json"):
            continue
        try:
            if service.recognizes(value):
                return service
        except ValueError:
            continue
    # A self-hosted Gotify or PushDeer server is not recognized by host, but
    # its fixed endpoint and credential query are still its own address.
    for service, endpoint in ((SERVICES["gotify"], "/message"), (SERVICES["pushdeer"], "/message/push")):
        if path != endpoint:
            continue
        try:
            service.normalize(value)
        except ValueError:
            continue
        return service
    return None


def parse(value: str, service: str = "recognize") -> tuple[str, str]:
    if not isinstance(value, str) or not isinstance(service, str):
        raise ValueError("push address and service must be text")
    if service == "recognize":
        try:
            matches = [s for s in SERVICES.values() if s.recognizes(value)]
        except ValueError:
            raise ValueError("invalid push address") from None
        if len(matches) != 1:
            raise ValueError("choose the push service for this address")
        definition = matches[0]
    else:
        definition = SERVICES.get(service)
        if definition is None:
            raise ValueError("unknown push service")
        if definition.id == "bark":
            # Inspect the URL Bark would use, as a pasted command may hold one.
            try:
                address = _pasted_address(value)
            except ValueError:
                address = value
            # A saved address is already Bark's own form, and a bare device key
            # is its own short form; only another shape asks the other services
            # whether it is theirs.
            try:
                own = definition.normalize(address) == address
            except ValueError:
                own = False
            if not own:
                try:
                    own = definition.recognizes(address)
                except ValueError:
                    pass
            if not own and (other := _foreign_address(address)) is not None:
                name = other.names["en"]
                raise ValueError(f"this address looks like a {name} address; choose {name} as the push service")
    try:
        return definition.id, definition.normalize(value)
    except ValueError:
        # A stable refusal lets every service use the page's language and keeps
        # arbitrary parser exception text (possibly containing a key) private.
        raise ValueError("this push service does not accept the address; check its hint and paste the whole address again") from None


def public() -> list[dict]:
    return [{"id": s.id, "name": s.names, "hint": s.hints, "extra_secret": s.extra_secret}
            for s in SERVICES.values()]
