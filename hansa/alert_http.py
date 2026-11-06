"""Private HTTPS transport for future push channels; nothing in hansa calls it yet.

Only send is a caller interface. The same file runs under Python -I as a child,
without importing hansa, so request secrets travel only over stdin. Its stdout
protocol contains indices, attempt counts and fixed result fields, never secrets.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
from pathlib import Path
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
                         "http_429", "http_4xx", "http_5xx", "too_big", "deadline"})


def _result(why, status=None, tries=0):
    return {"ok": why == "sent", "status": status, "tries": tries, "why": why}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _make_opener():
    # Keep urllib's ProxyHandler (including macOS system proxies and no_proxy).
    # No address filtering or independent DNS lookup: local proxies may use 198.18/15.
    return urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def _prepare(given):
    """Refuse unsafe inputs before urllib can normalize a URL or open a connection.

    Keep these checks while channels can supply URLs/headers: urllib strips some
    controls, and redirects or userinfo could send a push secret elsewhere.
    """
    try:
        url, body, headers = given["url"], given["json"], given.get("headers", {})
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
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(data) > _BODY_LIMIT:
            return _result("too_big")
        headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
        headers["Content-Type"] = "application/json; charset=utf-8"
        return urllib.request.Request(url, data=data, headers=headers, method="POST")
    except (KeyError, TypeError, ValueError, UnicodeError):
        return _result("refused_url")


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
    tries, status = 0, None
    try:
        opener = opener if opener is not None else _make_opener()
        while tries < 3:
            remaining = deadline - clock()
            if remaining <= 0:
                return _result("deadline", status, tries)
            tries += 1
            status, retry_after = None, None
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
                    with contextlib.suppress(OSError):
                        response.read(_RESPONSE_LIMIT)
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
                return _result(why, status, tries)
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
                results[index] = _result(why, status, tries)
        except (ValueError, TypeError, KeyError):
            pass  # a partial line at SIGKILL cannot hide earlier completed requests


def send(requests: list[dict], deadline_s: float = 30) -> list[dict]:
    """POST UTF-8 JSON in one isolated child; return ordered, secret-free results.

    The deadline includes child startup, stdin transfer, all tries and backoff.
    A killed child's completed results survive; unfinished entries say deadline.
    """
    deadline = time.monotonic() + max(0, deadline_s)
    results = [_result("deadline") for _ in requests]
    if not requests or deadline_s <= 0 or not math.isfinite(deadline_s):
        return results
    payload = json.dumps({"requests": requests, "deadline": deadline}, ensure_ascii=False).encode("utf-8")
    try:
        proc = subprocess.Popen([sys.executable, "-I", str(_HELPER)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return [_result("network") for _ in requests]
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
