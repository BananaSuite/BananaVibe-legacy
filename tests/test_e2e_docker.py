"""End to end with real Docker, the real sandbox image, OpenCode and the gateway.

A scripted OpenAI-compatible model makes OpenCode run one shell command that
edits the project and writes the completion report. Everything else (Git,
checkpoints, the isolated checker, publication) is the production path, with
the Git-backed fake forge standing in for GitHub.

Run with:  python -m pytest -m docker tests/test_e2e_docker.py
The first run builds the sandbox image, which needs registry and GitHub access.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import shutil
import subprocess
import threading

import pytest

from bananavibe.commands import Command
from bananavibe.config import Model
from bananavibe.controller import Controller
from bananavibe.engine import ensure_image
from bananavibe.runner import TaskRunner
from fakes import LocalForge

pytestmark = pytest.mark.docker

COMMAND = ("printf 'answer = 2\\n' > app.py && mkdir -p .bananavibe-task && "
           "printf '{\"status\": \"complete\"}' > .bananavibe-task/result.json && "
           "{ curl -s -m 5 -o /dev/null -w 'proxy=%{http_code}' http://169.254.169.254/; "
           "curl -s -m 5 --noproxy '*' -o /dev/null -w ' direct=%{http_code}' http://1.1.1.1/; } > probe.txt || true")


class ScriptedModel(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(request)
        last = request["messages"][-1]
        if "chat/completions" not in self.path:
            self.send_error(404)
            return
        if request.get("tools") and last["role"] == "user" and self.server.commands:
            command = self.server.commands.pop(0)
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {
                "name": "bash", "arguments": json.dumps({"command": command, "description": "Apply the change"})}}]}
            finish = "tool_calls"
        else:
            delta, finish = {"role": "assistant", "content": "Done."}, "stop"
        if not request.get("stream"):
            message = {"role": "assistant", "content": delta.get("content") or "{\"title\": \"t\", \"body\": \"b\"}"}
            return self.json({"id": "x", "object": "chat.completion", "model": request["model"],
                              "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]})
        chunks = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}]
        body = "".join(f"data: {json.dumps({'id': 'x', 'object': 'chat.completion.chunk', 'model': request['model'], **chunk})}\n\n"
                       for chunk in chunks) + "data: [DONE]\n\n"
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def json(self, value):
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def docker_bridge_address():
    result = subprocess.run(["docker", "network", "inspect", "bridge", "--format",
                             "{{(index .IPAM.Config 0).Gateway}}"], capture_output=True, text=True)
    return result.stdout.strip() or "172.17.0.1"


@pytest.fixture
def model_server():
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode:
        pytest.skip("Docker is not available")
    server = ThreadingHTTPServer(("0.0.0.0", 0), ScriptedModel)
    server.requests, server.commands = [], [COMMAND]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("first_answer", [2, 3])
def test_full_task_in_the_real_sandbox(tmp_path, model_server, first_answer):
    if first_answer != 2:
        model_server.commands.insert(0, COMMAND.replace("answer = 2", f"answer = {first_answer}"))
    endpoint = f"http://{docker_bridge_address()}:{model_server.server_port}/v1"
    models = {"coding": Model("coding", "openai-compatible", "mock-model", endpoint, "")}
    forge = LocalForge(tmp_path, models=models, allow_http=True, egress=("*",),
                       checks=(("python3", "-c", "import app; assert app.answer == 2, app.answer"),))
    from bananavibe.config import Limits
    forge.config = forge.config.__class__(**{**forge.config.__dict__,
                                             "limits": Limits(max_minutes=10, poll_seconds=2, memory_mb=1024)})
    controller = Controller(forge, log=print)
    assert controller.command(3, Command("start"), "maintainer", "e2e") == 3
    runner = TaskRunner(forge, controller.store, 3, image_factory=ensure_image, log=print)
    outcome = runner.run()
    state = controller.store.read(3)[0]
    assert outcome == "complete", state["reason"]
    assert forge.show(state["branch"], "app.py") == "answer = 2"
    assert forge.pulls and "Checks:" in forge.pulls[0]["body"]
    # Through the gateway the metadata service is refused; around it there is no route at all.
    assert forge.show(state["branch"], "probe.txt") == "proxy=403 direct=000"
    # The agent's request carried the private issue text; the summary request saw only the diff.
    assert any("PRIVATE" in json.dumps(request) for request in model_server.requests)
    summary_requests = [r for r in model_server.requests if "<diff>" in json.dumps(r)]
    assert summary_requests and all("PRIVATE" not in json.dumps(r) for r in summary_requests)
    if first_answer != 2:
        assert any("acceptance checks failed on a clean checkout" in json.dumps(r) for r in model_server.requests)
    # No containers or networks are left behind.
    leftovers = subprocess.run(["docker", "ps", "-aq", "--filter", "label=org.bananavibe.task"],
                               capture_output=True, text=True).stdout.split()
    assert leftovers == []
