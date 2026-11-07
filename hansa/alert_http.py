"""Private HTTPS transport for push channels.

The caller interfaces are send and safe_code. The same file runs under Python -I
as a child, without importing hansa, so request secrets travel only over stdin. Its stdout
protocol contains indices, attempt counts and fixed result fields, never secrets.
"""
from __future__ import annotations

import contextlib
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request


_HELPER = Path(__file__).resolve()
_BODY_LIMIT = 64 * 1024
_RESPONSE_LIMIT = 16 * 1024
_TRY_TIMEOUT = 5
_BACKOFF = (1, 3)
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_CATEGORIES = frozenset({"sent", "refused_url", "redirect", "timeout", "network", "tls",
                         "http_429", "http_4xx", "http_5xx", "too_big", "deadline", "refused_by_service"})


def safe_code(value):
    """A service code is a scalar, never an error message or nested response."""
    if type(value) is int or type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value):
        return value
    return None


def _result(why, status=None, tries=0, code=None):
    result = {"ok": why == "sent", "status": status, "tries": tries, "why": why}
    if why != "sent" and safe_code(code) is not None:
        result["code"] = code
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _make_opener():
    # Keep urllib's ProxyHandler (including macOS system proxies and no_proxy).
    # No address filtering or independent DNS lookup: local proxies may use 198.18/15.
    return urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def _valid_check(check):
    if not isinstance(check, dict) or set(check) - {"status", "field", "equals", "text"}:
        return False
    if "status" in check and (type(check["status"]) is not int or not 200 <= check["status"] < 300):
        return False
    if ("field" in check) != ("equals" in check):
        return False
    if "field" in check:
        expected = check["equals"]
        if not isinstance(check["field"], str) or not check["field"]:
            return False
        if not (expected is True or type(expected) is int or type(expected) is float and math.isfinite(expected)):
            return False
    return "text" not in check or isinstance(check["text"], str) and "field" not in check


def _prepare(given):
    """Refuse unsafe inputs before urllib can normalize a URL or open a connection.

    Keep these checks while channels can supply URLs/headers: urllib strips some
    controls, and redirects or userinfo could send a push secret elsewhere.
    """
    try:
        url, headers = given["url"], given.get("headers", {})
        if ("json" in given) == ("form" in given):
            return _result("refused_url")
        form = "form" in given
        body = given["form" if form else "json"]
        if (not isinstance(url, str) or any(unicodedata.category(c) == "Cc" for c in url)
                or "#" in url):
            return _result("refused_url")
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or "@" in parsed.netloc
                or any(c.isspace() for c in parsed.netloc)):
            return _result("refused_url")
        parsed.port  # reject malformed ports before opening anything
        if not isinstance(headers, dict) or not isinstance(body, dict):
            return _result("refused_url")
        for name, value in headers.items():
            if (not isinstance(name, str) or not isinstance(value, str)
                    or any(c in name or c in value for c in "\r\n")):
                return _result("refused_url")
        check = given.get("success", {})
        if (not _valid_check(check)
                or ("error_field" in given and not isinstance(given["error_field"], str))):
            return _result("refused_url")
        if form:
            if any(not isinstance(k, str) or not isinstance(v, str) for k, v in body.items()):
                return _result("refused_url")
            data = urllib.parse.urlencode(body, encoding="utf-8", errors="strict").encode("ascii")
        else:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(data) > _BODY_LIMIT:
            return _result("too_big")
        headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
        headers["Content-Type"] = ("application/x-www-form-urlencoded" if form else "application/json") + "; charset=utf-8"
        return urllib.request.Request(url, data=data, headers=headers, method="POST")
    except (KeyError, TypeError, ValueError, UnicodeError):
        return _result("refused_url")


def _answer(given, status, why, body):
    """Check acknowledgements here in the child; only a bounded code may leave."""
    check = given.get("success", {})
    parsed = None
    # A full read buffer may be only a prefix. Keep the existing read bound,
    # but never accept that prefix as a whole-body/JSON acknowledgement.
    complete = body is not None and len(body) < _RESPONSE_LIMIT
    if complete:
        try:
            parsed = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            pass
    matches = True
    if "text" in check:
        matches = complete and body == check["text"].encode("utf-8")
    elif "field" in check:
        actual = parsed.get(check["field"]) if isinstance(parsed, dict) else None
        expected = check["equals"]
        if expected is True:
            matches = actual is True
        else:
            matches = False
            if type(actual) in (int, float, str):
                try:
                    number = Decimal(str(actual))
                    matches = number.is_finite() and number == Decimal(str(expected))
                except InvalidOperation:
                    pass
    if why == "sent" and (not matches or "status" in check and check["status"] != status):
        why = "refused_by_service"
    code = None
    if why != "sent" and isinstance(parsed, dict):
        field = given.get("error_field", check.get("field"))
        code = safe_code(parsed.get(field))
    elif why != "sent" and "text" in check and complete:
        # Slack returns a single error word. Keep the whole-body and scalar
        # bounds here while response text must never become a public message.
        try:
            code = safe_code(body.decode("utf-8"))
        except UnicodeError:
            pass
    return why, code


def _status_category(status):
    if 200 <= status < 300:
        return "sent"
    if 300 <= status < 400:
        return "redirect"
    if status == 413:
        return "too_big"
    if status == 429:
        return "http_429"
    if 400 <= status < 500:
        return "http_4xx"
    if 500 <= status < 600:
        return "http_5xx"
    return "network"


def _send_one(given, deadline, *, opener=None, clock=time.monotonic, sleep=time.sleep, progress=None):
    request = _prepare(given)
    if isinstance(request, dict):
        return request
    tries, status, code = 0, None, None
    try:
        opener = opener if opener is not None else _make_opener()
        while tries < 3:
            remaining = deadline - clock()
            if remaining <= 0:
                return _result("deadline", status, tries, code)
            tries += 1
            status, retry_after, code = None, None, None
            if progress is not None:
                progress(tries)
            try:
                try:
                    # ProxyHandler mutates host, scheme and selector. Reusing its
                    # Request can turn a later HTTPS try into plain HTTP; clone
                    # the untouched preflight request on every attempt.
                    attempt = urllib.request.Request(request.full_url, data=request.data,
                                                     headers=dict(request.header_items()), method="POST")
                    response = opener.open(attempt, timeout=min(_TRY_TIMEOUT, remaining))
                except urllib.error.HTTPError as error:
                    response = error  # error bodies have the same read limit and must be closed
                with contextlib.closing(response):
                    status = response.code
                    why = _status_category(status)
                    if status == 429:
                        retry_after = response.headers.get("Retry-After", "").strip()
                    # The status already acknowledges the POST. Failure while
                    # discarding its body must not resend it or retry a redirect.
                    body = None
                    with contextlib.suppress(OSError):
                        body = response.read(_RESPONSE_LIMIT)
                    why, code = _answer(given, status, why, body)
                retry = status in _RETRY_STATUSES
            except (urllib.error.URLError, OSError) as error:
                reason = error.reason if isinstance(error, urllib.error.URLError) else error
                if isinstance(reason, ssl.SSLError):
                    why, retry = "tls", False
                elif isinstance(reason, (TimeoutError, socket.timeout)):
                    why, retry = "timeout", True
                else:
                    why, retry = "network", True
            except (ValueError, UnicodeError):
                return _result("refused_url", status, tries)
            if not retry or tries == 3:
                return _result(why, status, tries, code)
            remaining = deadline - clock()
            delay = _BACKOFF[tries - 1]
            # Retry-After's HTTP-date form is deliberately unsupported. Ignore a
            # seconds value that would consume the deadline and use normal backoff.
            if retry_after and retry_after.isascii() and retry_after.isdecimal():
                seconds = float(retry_after)
                if seconds < remaining:
                    delay = seconds
            sleep(max(0, min(delay, remaining)))
    except Exception:  # never return or log an opener exception's text
        return _result("network", status, tries)


def _main():
    given = json.load(sys.stdin)
    lock = threading.Lock()

    def emit(index, **fields):
        with lock:
            print(json.dumps({"index": index, **fields}), flush=True)

    def worker(index, request):
        result = _send_one(request, given["deadline"], progress=lambda n: emit(index, tries=n))
        emit(index, **result)

    threads = [threading.Thread(target=worker, args=(i, request)) for i, request in enumerate(given["requests"])]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def _collect(output, results):
    """Only copy allowlisted scalar fields across the child boundary, even on failure."""
    for line in output.splitlines():
        try:
            row = json.loads(line)
            index, tries = row["index"], row["tries"]
            if type(index) is not int or not 0 <= index < len(results) or type(tries) is not int or not 0 <= tries <= 3:
                continue
            if "why" not in row:
                if results[index]["why"] == "deadline":
                    results[index]["tries"] = tries
                continue
            why, status = row["why"], row["status"]
            if (why in _CATEGORIES and (status is None or type(status) is int and 100 <= status < 600)):
                results[index] = _result(why, status, tries, row.get("code"))
        except (ValueError, TypeError, KeyError):
            pass  # a partial line at SIGKILL cannot hide earlier completed requests


def send(requests: list[dict], deadline_s: float = 30) -> list[dict]:
    """POST UTF-8 JSON or form data in one isolated child; return scalar results.

    The deadline includes child startup, stdin transfer, all tries and backoff.
    A killed child's completed results survive; unfinished entries say deadline.
    """
    deadline = time.monotonic() + max(0, deadline_s)
    results = [_result("deadline") for _ in requests]
    if not requests or deadline_s <= 0 or not math.isfinite(deadline_s):
        return results
    encoded = []
    for index, request in enumerate(requests):
        try:
            encoded.append(json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError, UnicodeError, RecursionError):
            # Refuse this entry without losing the rest of the batch. A null
            # placeholder keeps child indices aligned and cannot open a URL.
            results[index] = _result("refused_url")
            encoded.append(b"null")
    payload = (b'{"requests": [' + b", ".join(encoded) + b'], "deadline": ' +
               json.dumps(deadline).encode("ascii") + b"}")
    try:
        proc = subprocess.Popen([sys.executable, "-I", str(_HELPER)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return [result if result["why"] == "refused_url" else _result("network") for result in results]
    output = b""
    try:
        output, _ = proc.communicate(payload, timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        # -I avoids caller import hooks; a new session makes this PID its process
        # group leader, so SIGKILL stops stuck DNS, TLS, reads and every worker.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        output, _ = proc.communicate()
    finally:
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                stream.close()
    _collect(output, results)
    return results


if __name__ == "__main__":
    try:
        _main()
    except Exception:
        sys.exit(1)  # no traceback: inputs, response bodies and errors can contain secrets
