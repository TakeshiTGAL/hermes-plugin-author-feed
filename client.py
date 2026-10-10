"""One HTTPS GET to the public AppView. No key, no redirect, no second try."""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

API_BASE = "https://public.api.bsky.app"
PROFILE_PATH = "/xrpc/app.bsky.actor.getProfile"
FEED_PATH = "/xrpc/app.bsky.feed.getAuthorFeed"
TIMEOUT_SECONDS = 20
TOTAL_SECONDS = 30
READ_CHUNK = 65_536
BODY_CAP = 1_000_000
USER_AGENT = "hermes-plugin-author-feed/0.1"
REDIRECTS = frozenset({301, 302, 303, 307, 308})
TIMEOUT_MESSAGE = "Not fetched. Saved state is unchanged. 取れなかった。保存は変わっていない。"
UNCHANGED = "Not fetched. Saved state is unchanged."

Transport = Callable[[str], tuple[int, bytes]]


class ClientError(Exception):
    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop 301/302/303/307/308 before a second host is opened."""

    def http_error_301(self, req, fp, code, msg, headers):  # noqa: ANN001
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        return None


def origin() -> str:
    parsed = urllib.parse.urlsplit(API_BASE)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ClientError("bad_base", "The request URL is not usable, so nothing was sent. " + UNCHANGED)
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}"


def build_url(path: str, pairs: list[tuple[str, str]]) -> str:
    if path not in {PROFILE_PATH, FEED_PATH}:
        raise ClientError("bad_path", "Only getProfile and getAuthorFeed are requested, so nothing was sent.")
    query = urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote)
    base = origin() + path
    return f"{base}?{query}" if query else base


def _arm_read_timeout(response, seconds: float) -> None:
    """Cap one blocking read. A fill-to-N read would otherwise ignore the deadline.

    ``urlopen`` returns a wrapper whose ``fp`` is the HTTP response, and that
    response's ``fp`` is the buffered socket. Walk both.
    """
    current = response
    seen: set[int] = set()
    sock = None
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        fp = getattr(current, "fp", None)
        raw = getattr(fp, "raw", None) if fp is not None else None
        sock = getattr(raw, "_sock", None) if raw is not None else None
        if sock is None and raw is not None:
            sock = getattr(getattr(raw, "raw", None), "_sock", None)
        if sock is not None:
            break
        current = fp
    if sock is not None:
        sock.settimeout(max(0.05, seconds))


def _pinned_opener(box: dict[str, Any]):
    """Opener that records the socket so a deadline can close it."""
    lock = box["lock"]

    class HTTP(http.client.HTTPConnection):
        def connect(self):
            super().connect()
            with lock:
                box["sock"] = self.sock

    class HTTPS(http.client.HTTPSConnection):
        def connect(self):
            super().connect()
            with lock:
                box["sock"] = self.sock

    class HTTPOpen(urllib.request.HTTPHandler):
        def http_open(self, req):  # noqa: ANN001
            return self.do_open(HTTP, req)

    class HTTPSOpen(urllib.request.HTTPSHandler):
        def https_open(self, req):  # noqa: ANN001
            return self.do_open(HTTPS, req)

    return urllib.request.build_opener(_NoRedirect(), HTTPOpen(), HTTPSOpen())


def _close_socket(sock) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _open_bounded(opener, request, deadline: float, box: dict[str, Any]):
    """Return the response, or time out even if header bytes keep the idle timer reset.

    On timeout the recorded socket is closed, so the worker thread cannot keep
    the connection open after the call has returned.
    """
    lock = box["lock"]

    def run() -> None:
        try:
            response = opener.open(request, timeout=TIMEOUT_SECONDS)
        except BaseException as exc:
            with lock:
                box["err"] = exc
            return
        with lock:
            if box.get("abandoned"):
                try:
                    response.close()
                except Exception:
                    pass
            else:
                box["resp"] = response

    thread = threading.Thread(target=run, daemon=True, name="author-feed-get")
    thread.start()
    left = deadline - time.monotonic()
    thread.join(left if left > 0 else 0)
    with lock:
        if "resp" in box:
            return box["resp"]
        if "err" in box:
            raise box["err"]
        box["abandoned"] = True
        sock = box.get("sock")
    _close_socket(sock)
    thread.join(1.0)
    raise ClientError("timeout", TIMEOUT_MESSAGE)


def _read_capped(response, deadline: float) -> bytes:
    """Read until EOF, the size cap, or the deadline. One byte at a time.

    ``read(N)`` waits for N bytes or EOF, so a trickle under the idle timeout
    holds the turn open. The socket wait is also capped by the time left.
    The deadline is the same one that started before the connection opened.
    """
    parts: list[bytes] = []
    size = 0
    while size <= BODY_CAP:
        left = deadline - time.monotonic()
        if left <= 0:
            raise ClientError("timeout", TIMEOUT_MESSAGE)
        _arm_read_timeout(response, min(float(TIMEOUT_SECONDS), left))
        try:
            chunk = response.read(1)
        except (TimeoutError, socket.timeout) as exc:
            raise ClientError("timeout", TIMEOUT_MESSAGE) from exc
        if not chunk:
            break
        parts.append(chunk)
        size += len(chunk)
    return b"".join(parts)


def _urllib_transport(url: str) -> tuple[int, bytes]:
    box: dict[str, Any] = {"lock": threading.Lock()}
    opener = _pinned_opener(box)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        method="GET",
    )
    if request.get_method() != "GET":
        raise ClientError("method", "Only GET is sent. " + UNCHANGED)
    if request.has_header("Authorization"):
        raise ClientError("key", "Authorization is not sent. " + UNCHANGED)
    deadline = time.monotonic() + TOTAL_SECONDS
    try:
        response = _open_bounded(opener, request, deadline, box)
    except urllib.error.HTTPError as exc:
        try:
            body = _read_capped(exc.fp, deadline) if exc.fp is not None else b""
            return int(exc.code), body
        finally:
            exc.close()
    try:
        status = int(getattr(response, "status", 0) or getattr(response, "code", 0) or 0)
        return status, _read_capped(response, deadline)
    finally:
        response.close()


def fetch(path: str, pairs: list[tuple[str, str]], transport: Transport | None = None) -> dict[str, Any]:
    """Send one GET. Callers do not retry this function."""
    url = build_url(path, pairs)
    try:
        status, body = (transport or _urllib_transport)(url)
    except ClientError:
        raise
    except TimeoutError as exc:
        raise ClientError("timeout", TIMEOUT_MESSAGE) from exc
    except Exception as exc:
        if "timed out" in str(exc).lower():
            raise ClientError("timeout", TIMEOUT_MESSAGE) from exc
        raise ClientError("network", UNCHANGED) from exc
    return parse_response(path, status, body)


def _api_text(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    err = str(data.get("error") or "")
    msg = str(data.get("message") or "")
    text = f"{err}: {msg}".strip(": ")
    return text


def parse_response(path: str, status: int, body: bytes) -> dict[str, Any]:
    if status in REDIRECTS:
        raise ClientError(
            "redirect",
            f"HTTP {status} was not followed. Nothing was sent to the other host. " + UNCHANGED,
            status=status,
        )
    if len(body) > BODY_CAP:
        raise ClientError("oversize", "The response was too large. " + UNCHANGED, status=status)
    if status < 200 or status >= 300:
        detail = _api_text(body)
        extra = f" {detail}." if detail else ""
        raise ClientError(
            f"http_{status}",
            f"HTTP {status} is a failure, not an empty success.{extra} " + UNCHANGED,
            status=status,
        )
    if not body or not body.strip():
        raise ClientError("bad_body", "HTTP 200 with an empty body is a failure. " + UNCHANGED, status=status)
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise ClientError("bad_body", "HTTP 200 was not JSON. " + UNCHANGED, status=status) from exc
    if not isinstance(data, dict):
        raise ClientError("bad_body", "HTTP 200 was not a JSON object. " + UNCHANGED, status=status)
    if "error" in data:
        detail = _api_text(body) or str(data.get("error"))
        raise ClientError(
            "bad_body",
            f"HTTP 200 included an error ({detail}). This is a failure, not an empty page. " + UNCHANGED,
            status=status,
        )
    if path == FEED_PATH and not isinstance(data.get("feed"), list):
        raise ClientError("bad_body", "HTTP 200 did not include a feed array. " + UNCHANGED, status=status)
    if path == PROFILE_PATH and not data.get("did"):
        raise ClientError("bad_body", "HTTP 200 did not include did. " + UNCHANGED, status=status)
    return data
