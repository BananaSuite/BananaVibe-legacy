"""Real response sockets must obey finite budgets and release deadline workers."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import shutil
import socket
import ssl
import subprocess
import threading
import time

import pytest

from bananavibe import httpclient


@pytest.fixture
def backend():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            try:
                if self.path == "/headers":
                    for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\nConnection: close\r\n\r\n":
                        self.wfile.write(bytes((byte,)))
                        self.wfile.flush()
                        time.sleep(0.04)
                    return
                self.send_response(503 if self.path == "/error" else 200)
                self.send_header("Connection", "close")
                if self.path == "/chunked":
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header("Content-Length", "12")
                self.end_headers()
                if self.path == "/fast":
                    self.wfile.write(b"abcdefghijkl")
                    return
                for _ in range(12):
                    self.wfile.write(b"1\r\nx\r\n" if self.path == "/chunked" else b"x")
                    self.wfile.flush()
                    time.sleep(0.04)
                if self.path == "/chunked":
                    self.wfile.write(b"0\r\n\r\n")
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def deadline_threads():
    return {thread for thread in threading.enumerate() if thread.name == "bananavibe-http-deadline"}


@pytest.mark.parametrize("path", ["/headers", "/body", "/chunked"])
def test_trickling_responses_cannot_extend_total_timeout(backend, path):
    before = deadline_threads()
    started = time.monotonic()
    with pytest.raises(httpclient.Unreachable) as caught:
        httpclient.request("GET", backend + path, timeout=0.15)
    assert time.monotonic() - started < 0.6
    assert "xxxxxxxx" not in str(caught.value)
    assert deadline_threads() == before


def test_error_body_obeys_deadline_and_preserves_known_status(backend):
    before = deadline_threads()
    started = time.monotonic()
    with pytest.raises(httpclient.HTTPError) as caught:
        httpclient.request("GET", backend + "/error", timeout=0.15)
    assert time.monotonic() - started < 0.6
    assert caught.value.status == 503 and caught.value.body == b""
    assert deadline_threads() == before


def test_closed_connection_success_and_oversize_cleanup(backend):
    before = deadline_threads()
    for _ in range(5):
        assert httpclient.request("GET", backend + "/fast", timeout=1).body == b"abcdefghijkl"
    with pytest.raises(httpclient.Unreachable, match="exceeded"):
        httpclient.request("GET", backend + "/fast", timeout=1, limit=4)
    assert deadline_threads() == before


def test_only_explicitly_retryable_requests_repeat_after_timeout(backend):
    sleeps = []
    started = time.monotonic()
    with pytest.raises(httpclient.Unreachable):
        httpclient.request("GET", backend + "/body", timeout=0.08, retries=1, sleep=sleeps.append)
    assert sleeps == [1] and time.monotonic() - started < 0.6
    assert deadline_threads() == set()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 10**400, True])
def test_invalid_deadlines_are_refused_before_connecting(timeout):
    with pytest.raises(ValueError, match="positive and finite"):
        httpclient.request("GET", "http://127.0.0.1:1/", timeout=timeout)


def test_deep_provider_json_is_a_controlled_decode_error():
    response = httpclient.Response(200, {}, b"[" * 2000 + b"0" + b"]" * 2000)
    with pytest.raises(ValueError, match="invalid JSON"):
        response.json()


@pytest.mark.parametrize("body", [b'{"ignored":"\\ud800"}', b'{"\\udfff":1}', b'{"x":NaN}',
                                 b'{"x":Infinity}', b'{"x":1e9999}', b'{"x":', b'"\xff"'])
def test_unsafe_provider_json_is_rejected_without_echoing_the_body(body):
    with pytest.raises(ValueError, match="invalid JSON") as caught:
        httpclient.Response(200, {}, body).json()
    assert str(caught.value) == "The backend returned invalid JSON."
    assert caught.value.__suppress_context__


def test_ordinary_unicode_and_json_values_remain_supported():
    body = b'{"emoji":"\\ud83d\\ude00","list":[true,null,1,1.5]}'
    assert httpclient.Response(200, {}, body).json() == {"emoji": "\U0001f600", "list": [True, None, 1, 1.5]}


def test_tls_verifies_certificates_and_enforces_response_deadlines(tmp_path, monkeypatch):
    if not shutil.which("openssl"):
        pytest.skip("TLS certificate fixture requires OpenSSL")
    certificate, key = tmp_path / "certificate.pem", tmp_path / "key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(certificate), "-days", "1",
        "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1",
    ], check=True, capture_output=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "12")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                if self.path == "/fast":
                    self.wfile.write(b"abcdefghijkl")
                    return
                for _ in range(12):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.04)
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    url = f"https://127.0.0.1:{server.server_port}"
    before = deadline_threads()
    try:
        with pytest.raises(httpclient.Unreachable):
            httpclient.request("GET", url + "/fast", timeout=1)
        monkeypatch.setenv("SSL_CERT_FILE", str(certificate))
        assert httpclient.request("GET", url + "/fast", timeout=1).body == b"abcdefghijkl"
        started = time.monotonic()
        with pytest.raises(httpclient.Unreachable):
            httpclient.request("GET", url + "/slow", timeout=0.15)
        assert time.monotonic() - started < 0.6
        assert deadline_threads() == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_slow_dns_is_bounded_and_reuses_resolver_capacity(backend, monkeypatch):
    original = socket.getaddrinfo
    release = threading.Event()
    entered = []
    entry = threading.Condition()

    def lookup(*args, **kwargs):
        with entry:
            entered.append(threading.current_thread())
            entry.notify_all()
        release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    try:
        for _ in range(8):
            started = time.monotonic()
            with pytest.raises(httpclient.Unreachable):
                httpclient.request("GET", backend + "/fast", timeout=0.03)
            assert time.monotonic() - started < 0.3
        # A request may time out before its resolver thread is scheduled under
        # runner load. Synchronize entry separately from the request deadline.
        with entry:
            assert entry.wait_for(lambda: len(entered) == 8, timeout=1)
            assert len(entered) == 8
        started = time.monotonic()
        with pytest.raises(httpclient.Unreachable):
            httpclient.request("GET", backend + "/fast", timeout=0.03)
        assert time.monotonic() - started < 0.3
        assert len(entered) == 8  # a stalled resolver cannot create unlimited workers
    finally:
        release.set()
        for worker in entered:
            worker.join(2)
    assert httpclient.request("GET", backend + "/fast", timeout=1).body == b"abcdefghijkl"
    assert deadline_threads() == set()


def test_prior_deadline_workers_close_before_retry_backoff(backend):
    sleeps = []

    def backoff(seconds):
        assert deadline_threads() == set()
        sleeps.append(seconds)

    with pytest.raises(httpclient.HTTPError):
        httpclient.request("GET", backend + "/error", timeout=0.1, retries=1, sleep=backoff)
    assert sleeps == [1]


@pytest.mark.parametrize("value,expected", [("\u00b2", 1), ("9" * 5000, 1), ("900", 60), ("5", 5), (" 5 ", 5)])
def test_retry_after_is_bounded_ascii_decimal_without_uncaught_errors(backend, monkeypatch, value, expected):
    original = BaseHTTPRequestHandler.send_header

    def retry_header(handler, name, content):
        if name == "Connection":
            original(handler, "Retry-After", value)
        return original(handler, name, content)

    monkeypatch.setattr(BaseHTTPRequestHandler, "send_header", retry_header)
    sleeps = []
    with pytest.raises(httpclient.HTTPError) as caught:
        httpclient.request("GET", backend + "/error", timeout=0.15, retries=1, sleep=sleeps.append)
    assert caught.value.status == 503 and sleeps == [expected]
    assert deadline_threads() == set()


def test_tls_handshake_inherits_remaining_dns_budget(monkeypatch):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    release = threading.Event()

    def stall_handshake():
        connection, _address = server.accept()
        with connection:
            release.wait(2)  # accept TCP but never answer the TLS handshake

    thread = threading.Thread(target=stall_handshake, daemon=True)
    thread.start()
    original = socket.getaddrinfo

    def slow_lookup(*args, **kwargs):
        time.sleep(0.12)
        return original(*args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", slow_lookup)
    started = time.monotonic()
    try:
        with pytest.raises(httpclient.Unreachable):
            httpclient.request("GET", f"https://127.0.0.1:{server.getsockname()[1]}", timeout=0.2)
        assert time.monotonic() - started < 0.29
        assert deadline_threads() == set()
    finally:
        release.set()
        server.close()
        thread.join(2)
