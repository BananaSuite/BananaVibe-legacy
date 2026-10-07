"""Engine logic against a fake OpenCode server (no Docker needed)."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import subprocess
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from bananavibe import __version__
from bananavibe import engine as engine_module
from bananavibe.config import Model
from bananavibe.engine import Engine, EngineError, describe_error, image_tag
from fakes import make_config


class OpenCode(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, value, status=200):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        server, url = self.server, urlsplit(self.path)
        server.requests.append(("GET", url.path))
        assert parse_qs(url.query)["directory"] == ["/workspace"]
        assert self.headers["X-BananaVibe-Control"] == "control"
        if server.unavailable:
            server.unavailable -= 1
            return self.reply({}, 503)
        if url.path == "/session/status":
            server.polls += 1
            busy = server.polls <= server.busy_polls
            return self.reply({"ses_1": {"type": "busy"}} if busy else {})
        if url.path == "/session/ses_1/message":
            limit = int(parse_qs(url.query)["limit"][0])
            return self.reply(server.messages[-limit:])
        if url.path == "/question":
            return self.reply(server.questions)
        if url.path == "/permission":
            return self.reply([])
        return self.reply({"healthy": True})

    def do_POST(self):
        server, path = self.server, urlsplit(self.path).path
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        server.requests.append(("POST", path))
        if server.unavailable:
            server.unavailable -= 1
            return self.reply({}, 503)
        if path == "/session":
            return self.reply({"id": "ses_1"})
        if path.endswith("/prompt_async"):
            server.messages.append({"info": {"id": "msg_002", "role": "user"}, "parts": []})
            if server.answer is not None:
                server.messages.append({"info": {"id": "msg_003", "role": "assistant", "time": {"completed": 1},
                                                 **server.answer}, "parts": []})
            self.send_response(204)
            self.end_headers()
            return None
        return self.reply(True)


@pytest.fixture
def opencode():
    server = ThreadingHTTPServer(("127.0.0.1", 0), OpenCode)
    server.requests, server.polls, server.busy_polls, server.unavailable = [], 0, 2, 0
    server.messages = [{"info": {"id": "msg_001", "role": "assistant"}, "parts": []}]
    server.questions, server.answer = [], {}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def engine(opencode):
    engine = object.__new__(Engine)
    engine.config = make_config()
    engine.model = Model("coding", "openai-compatible", "code-model", "https://models.example/v1")
    engine.port, engine.server_secret, engine.control_secret = opencode.server_port, "secret", "control"
    engine.session, engine.poll, engine.phases = None, 1, []
    engine.now = 0.0
    engine.clock = lambda: engine.now

    def sleep(seconds):
        engine.now += seconds
    engine.sleep = sleep
    engine.pulse = lambda phase=None: engine.phases.append(phase)
    return engine


def test_prompt_waits_for_a_completed_reply(engine, opencode):
    assert engine.prompt("do it") == {"status": "idle"}
    assert opencode.polls == 3
    assert ("POST", "/session/ses_1/prompt_async") in opencode.requests


def test_provider_errors_become_readable_failures(engine, opencode):
    opencode.answer = {"error": {"name": "ProviderAuthError", "data": {"providerID": "task", "message": "401"}}}
    with pytest.raises(EngineError, match="rejected the credentials"):
        engine.prompt("do it")


def test_questions_block_the_task(engine, opencode):
    opencode.questions = [{"id": "que_1", "sessionID": "ses_1", "questions": [{"question": "Which database?"}]}]
    assert engine.prompt("do it") == {"status": "blocked", "question": "The coding agent asked: Which database?"}


def test_idle_without_a_reply_fails_instead_of_hanging(engine, opencode):
    opencode.answer, opencode.busy_polls = None, 0
    with pytest.raises(EngineError, match="stopped without replying"):
        engine.prompt("do it")


def test_reads_are_retried_but_submissions_are_not(engine, opencode):
    opencode.unavailable = 1
    assert engine.api("GET", "/global/health")["healthy"]
    opencode.unavailable = 1
    with pytest.raises(EngineError):
        engine.api("POST", "/session")
    assert opencode.requests.count(("POST", "/session")) == 1


def test_stop_is_observed_while_waiting(engine):
    class Stop(Exception):
        pass

    def pulse(phase=None):
        raise Stop()
    engine.pulse = pulse
    with pytest.raises(Stop):
        engine.prompt("do it")


def test_error_descriptions():
    assert "HTTP 429" in describe_error({"name": "APIError", "data": {"message": "slow down", "statusCode": 429}})
    assert "context window" in describe_error({"name": "ContextOverflowError", "data": {"message": "x"}})


def test_image_tag_tracks_sandbox_sources():
    assert image_tag().startswith(f"bananavibe-sandbox:{__version__}-")


def test_leftovers_of_a_killed_run_of_the_same_task_are_removed(monkeypatch, tmp_path):
    calls = []

    def fake_docker(*args, **_):
        calls.append(args)
        listed = {"ps": "c1\nc2\n", "network": "n1\n"}.get(args[0], "") if "--filter" in args else ""
        return subprocess.CompletedProcess(args, 0, listed, "")
    monkeypatch.setattr(engine_module, "docker", fake_docker)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    mine, other = tmp_path / "bananavibe-engine-0123456789abcdef-x1", tmp_path / "bananavibe-engine-fedcba9876543210-x2"
    mine.mkdir(), other.mkdir()
    engine_module.sweep("0123456789abcdef")
    assert ("rm", "--force", "--volumes", "c1", "c2") in calls and ("network", "rm", "n1") in calls
    assert all("label=org.bananavibe.task-id=0123456789abcdef" in call for call in calls if "--filter" in call)
    assert not mine.exists() and other.exists()
    calls.clear()
    engine_module.sweep("*")
    assert calls == [] and other.exists()


def test_shutdown_removes_private_credentials_even_when_docker_times_out(monkeypatch):
    engine = object.__new__(Engine)
    engine.session, engine.port = None, None
    engine.agent, engine.gateway = "agent", "gateway"
    engine.internal, engine.outside = "internal", "outside"
    engine._temporary = tempfile.TemporaryDirectory(prefix="test-bananavibe-cleanup-")
    from pathlib import Path
    root = Path(engine._temporary.name)
    (root / "gateway.json").write_text('{"api_key": "test-secret"}')
    calls = []

    def unreachable_docker(*args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired(["docker", *args], kwargs["timeout"])
    monkeypatch.setattr(engine_module, "docker", unreachable_docker)
    engine.close()
    assert not root.exists()
    assert calls == [("rm", "--force", "--volumes", "agent", "agent-check", "gateway"),
                     ("network", "rm", "internal"), ("network", "rm", "outside")]
