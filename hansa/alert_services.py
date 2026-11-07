"""Push service registry. Definitions own parsing, masking, limits and requests.

A service supplies id, names/hints (en/zh), extra_secret, deadline_s, max_bytes,
levels, recognizes(value), normalize(value), secrets(address), hidden(address),
and requests(address, secret, title, body, level). Requests use alert_http.send's
private interface; no response bodies or credentials cross into public results.
"""
from .alert_bark import Bark
from .alert_phone import Gotify, Ntfy, PushDeer, ServerChan

SERVICES = {service.id: service for service in (Bark(), Ntfy(), Gotify(), PushDeer(), ServerChan())}


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
    try:
        return definition.id, definition.normalize(value)
    except ValueError:
        # A stable refusal lets every service use the page's language and keeps
        # arbitrary parser exception text (possibly containing a key) private.
        raise ValueError("this push service does not accept the address; check its hint and paste the whole address again") from None


def public() -> list[dict]:
    return [{"id": s.id, "name": s.names, "hint": s.hints, "extra_secret": s.extra_secret}
            for s in SERVICES.values()]
