"""A small JSON-over-HTTPS client built on the standard library.

BananaVibe holds a forge token and model keys, so the client is deliberately
strict: it never follows redirects (credentials must not travel to another
origin), ignores proxy and .netrc settings from the environment, bounds every
response body, and never includes response bodies or headers in exceptions.
"""

from dataclasses import dataclass
import http.client
from itertools import chain
import json
import math
import socket
import threading
import time
import urllib.error
import urllib.request

from . import USER_AGENT


class HTTPError(RuntimeError):
    """A non-2xx response. `body` is kept for callers but never printed."""

    def __init__(self, status, method, url, body=b"", headers=None):
        self.status, self.body, self.headers = status, body, headers or {}
        host = url.split("/")[2] if "://" in url else "?"
        super().__init__(f"{method} request to {host} returned HTTP {status}.")


class Unreachable(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# The system resolver cannot be interrupted portably. Bound both how long a
# request waits and how many daemon workers may still be in a stalled lookup.
_DNS_SLOTS = threading.BoundedSemaphore(8)


def _resolve(host, port, deadline):
    if not _DNS_SLOTS.acquire(timeout=deadline.remaining()):
        raise TimeoutError("The resolver is busy.")
    ready = threading.Event()
    result = []

    def lookup():
        try:
            result.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except Exception as error:
            result.append(error)
        finally:
            _DNS_SLOTS.release()
            ready.set()

    worker = threading.Thread(target=lookup, name="bananavibe-dns", daemon=True)
    try:
        worker.start()
    except BaseException:
        _DNS_SLOTS.release()
        raise
    if not ready.wait(deadline.remaining()):
        raise TimeoutError("The resolver took too long.")
    deadline.check()
    answer = result[0]
    if isinstance(answer, Exception):
        raise answer
    return answer


class _Deadline:
    """Interrupt trickling headers and bodies, including detached response sockets."""

    def __init__(self, seconds):
        self.end = time.monotonic() + seconds
        self.lock = threading.Lock()
        self.connections = []
        self.sockets = []
        self.timer = threading.Timer(seconds, self.abort)
        self.timer.name = "bananavibe-http-deadline"
        self.timer.daemon = True

    def check(self):
        if time.monotonic() >= self.end:
            raise TimeoutError("The request deadline expired.")

    def remaining(self):
        left = self.end - time.monotonic()
        if left <= 0:
            raise TimeoutError("The request deadline expired.")
        return left

    def connect_socket(self, address, _timeout=None, source_address=None):
        host, port = address
        addresses = _resolve(host, port, self)
        last_error = OSError("The resolver returned no addresses.")
        for family, kind, protocol, _name, endpoint in addresses:
            self.check()
            sock = socket.socket(family, kind, protocol)
            try:
                with self.lock:
                    self.sockets.append(sock)
                sock.settimeout(self.remaining())
                if source_address:
                    sock.bind(source_address)
                sock.connect(endpoint)
                # HTTPS wraps this socket next. Its handshake must inherit the
                # budget left after DNS/TCP rather than the original timeout.
                sock.settimeout(self.remaining())
                return sock
            except OSError as error:
                sock.close()
                last_error = error
        raise last_error

    def abort(self):
        with self.lock:
            sockets = self.sockets + [connection.sock for connection in self.connections]
        for sock in sockets:
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def factory(self, base):
        guard = self

        class Connection(base):
            def connect(self):
                guard.check()
                super().connect()
                guard.check()

            def getresponse(self):
                guard.check()
                # urllib closes/detaches the connection after returning headers.
                # Retain the socket that the HTTPResponse's file still owns.
                with guard.lock:
                    guard.sockets.append(self.sock)
                response = super().getresponse()
                try:
                    guard.check()
                except TimeoutError:
                    response.close()
                    raise
                return response

        def create(*args, **kwargs):
            guard.check()
            connection = Connection(*args, **kwargs)
            connection._create_connection = guard.connect_socket
            with guard.lock:
                guard.connections.append(connection)
            return connection

        return create

    def close(self):
        self.timer.cancel()
        self.timer.join(timeout=1)
        with self.lock:
            connections = self.connections
            sockets = self.sockets
            self.connections = []
            self.sockets = []
        for connection in connections:
            connection.close()
        for sock in sockets:
            if sock is not None:
                sock.close()


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, deadline):
        super().__init__()
        self.deadline = deadline

    def http_open(self, request):
        return self.do_open(self.deadline.factory(http.client.HTTPConnection), request)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, deadline):
        super().__init__()
        self.deadline = deadline

    def https_open(self, request):
        return self.do_open(self.deadline.factory(http.client.HTTPSConnection), request,
                            context=self._context)


def _read(response, limit, deadline):
    deadline.check()
    raw = response.read(limit + 1)
    # Socket shutdown can produce a short read or EOF rather than an exception.
    deadline.check()
    return raw


def _json(body):
    def refuse_constant(_value):
        raise ValueError("JSON numbers must be finite.")

    try:
        payload = json.loads(body, parse_constant=refuse_constant)
        frames = [(iter((payload,)), 0)]
        while frames:
            children, depth = frames[-1]
            try:
                value = next(children)
            except StopIteration:
                frames.pop()
                continue
            if isinstance(value, str):
                value.encode("utf-8")
            elif isinstance(value, float) and not math.isfinite(value):
                raise ValueError("JSON numbers must be finite.")
            elif isinstance(value, (dict, list)):
                if depth >= 64:
                    raise ValueError("JSON nesting is too deep.")
                contents = chain(value.keys(), value.values()) if isinstance(value, dict) else iter(value)
                frames.append((iter(contents), depth + 1))
        return payload
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("The backend returned invalid JSON.") from None


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes

    def json(self):
        return _json(self.body) if self.body else None


def request(method, url, *, headers=None, body=None, timeout=45, limit=8 * 1024 * 1024,
            retries=0, sleep=time.sleep):
    """Send one request; `body` may be bytes or a JSON-serializable value.

    Each attempt has a total timeout, covering response headers and bodies.
    Only requests the caller marks as safe to repeat are retried, on transport
    errors and on 429/502/503/504 (honouring a short Retry-After).
    """
    if type(timeout) not in {int, float} or not 0 < timeout <= threading.TIMEOUT_MAX or not math.isfinite(timeout):
        raise ValueError("The request timeout must be positive and finite.")
    data = body if body is None or isinstance(body, bytes) else json.dumps(body).encode()
    all_headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    if data is not None and not isinstance(body, bytes):
        all_headers.setdefault("Content-Type", "application/json")
    for attempt in range(retries + 1):
        prepared = urllib.request.Request(url, data=data, method=method, headers=all_headers)
        deadline = _Deadline(timeout)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                            _HTTPHandler(deadline), _HTTPSHandler(deadline))
        deadline.timer.start()
        try:
            with opener.open(prepared, timeout=timeout) as response:
                raw = _read(response, limit, deadline)
                if len(raw) > limit:
                    raise Unreachable(f"The response from {url.split('/')[2]} exceeded {limit} bytes.")
                return Response(response.status, dict(response.headers), raw)
        except urllib.error.HTTPError as error:
            try:
                raw = _read(error, 65535, deadline) if error.fp else b""
            except (OSError, http.client.HTTPException):
                raw = b""
            finally:
                error.close()
            status = error.code
            retry_after = error.headers.get("Retry-After", "") if error.headers else ""
            if attempt < retries and status in {429, 502, 503, 504}:
                deadline.close()
                retry_after = retry_after.strip(" \t")
                wait = (min(int(retry_after), 60)
                        if len(retry_after) <= 10 and retry_after.isascii() and retry_after.isdecimal()
                        else 2 ** attempt)
                sleep(wait)
                continue
            raise HTTPError(status, method, url, raw, dict(error.headers or {})) from None
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as error:
            if attempt < retries:
                deadline.close()
                sleep(2 ** attempt)
                continue
            host = url.split("/")[2] if "://" in url else url
            raise Unreachable(f"{host} could not be reached ({type(error).__name__}).") from None
        finally:
            deadline.close()
    raise AssertionError("unreachable")
