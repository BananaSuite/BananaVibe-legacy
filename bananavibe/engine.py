"""OpenCode in disposable Docker containers.

Topology for one task:

    controller ──127.0.0.1:port──▶ gateway :8082 (control, needs a secret) ──▶ agent :4096 (OpenCode)
    agent ──internal network──▶ gateway :8000 (inference, task-scoped token) ──▶ model provider
    agent/checker ──internal network──▶ gateway :8080 (egress proxy, public hosts in network.allow)

The agent and checker containers sit on an internal network with no route
out; only the gateway, on its own per-task bridge, reaches the internet. The
real model key and the control secret exist only inside the gateway.
"""

import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import time

from . import __version__, httpclient
from .workspace import give_to_sandbox

SOURCE = Path(__file__).resolve().parents[1]
IMAGE_INPUTS = ("Dockerfile.sandbox", "sandbox/gateway.py", "sandbox/fetch_opencode.py", "sandbox/OPENCODE-LICENSE",
                "LICENSE", "NOTICE")
DOCKER_ENV_KEYS = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT", "DOCKER_CERT_PATH",
                   "DOCKER_TLS_VERIFY", "XDG_RUNTIME_DIR", "TMPDIR")
TAIL = 12_000
TASK_LABEL = "org.bananavibe.task-id"


class EngineError(RuntimeError):
    """The engine or model provider failed; the message is safe to show."""


class CommandTimeout(EngineError):
    pass


def docker_env():
    # Model keys and the forge token stay out of the Docker CLI's environment.
    return {key: os.environ[key] for key in DOCKER_ENV_KEYS if key in os.environ}


def docker(*args, check=True, timeout=120):
    result = subprocess.run(["docker", *map(str, args)], capture_output=True, text=True, timeout=timeout,
                            env=docker_env())
    if check and result.returncode:
        detail = result.stderr.strip().splitlines()[-1:] or ["no output"]
        raise EngineError(f"docker {args[0]} failed: {detail[0][:300]}")
    return result


def sweep(task):
    """Remove what a hard-killed earlier run of this task left on this host.

    Only the runner holding the task's lease calls this, so nothing it finds
    for the task can belong to a live run. That includes the temporary
    directory holding the real model key.
    """
    if not re.fullmatch(r"[0-9a-f]{16}", task or ""):
        return
    label = f"label={TASK_LABEL}={task}"
    containers = docker("ps", "--all", "--quiet", "--filter", label, check=False, timeout=60).stdout.split()
    if containers:
        docker("rm", "--force", "--volumes", *containers, check=False, timeout=60)
    for network in docker("network", "ls", "--quiet", "--filter", label, check=False, timeout=60).stdout.split():
        docker("network", "rm", network, check=False, timeout=30)
    for leftover in Path(tempfile.gettempdir()).glob(f"bananavibe-engine-{task}-*"):
        shutil.rmtree(leftover, ignore_errors=True)


def image_tag():
    """A local tag derived from the sandbox sources, so any change rebuilds."""
    digest = hashlib.sha256()
    for name in IMAGE_INPUTS:
        digest.update(name.encode() + b"\0" + (SOURCE / name).read_bytes() + b"\0")
    return f"bananavibe-sandbox:{__version__}-{digest.hexdigest()[:12]}"


def ensure_image(config, pulse=lambda *_: None, log=print):
    """Return a usable sandbox image: the configured one, or a local build."""
    if not config.image and not (SOURCE / "Dockerfile.sandbox").is_file():
        raise EngineError("The sandbox sources are not available here. Run BananaVibe as the Action (or from a "
                          "checkout), or set `image` to a published sandbox image.")
    image = config.image or image_tag()
    if docker("image", "inspect", image, check=False, timeout=30).returncode == 0:
        return image
    with tempfile.TemporaryFile() as output:
        if config.image:
            log(f"Pulling sandbox image {image}")
            command = ["docker", "pull", image]
        else:
            log(f"Building sandbox image {image} (first run on this runner; later runs reuse it)")
            command = ["docker", "build", "--pull", "-f", str(SOURCE / "Dockerfile.sandbox"), "-t", image, str(SOURCE)]
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, env=docker_env())
        deadline = time.monotonic() + 1800
        try:
            while process.poll() is None:
                pulse("Preparing the sandbox image")
                if time.monotonic() > deadline:
                    raise EngineError("Preparing the sandbox image took longer than 30 minutes.")
                time.sleep(2)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        if process.returncode:
            output.seek(0)
            tail = output.read().decode(errors="replace")[-4000:]
            log("Sandbox image output (tail):\n" + tail)
            raise EngineError("The sandbox image could not be prepared; see the workflow log. "
                              "Check Docker and registry access.")
    return image


class Engine:
    """One task's gateway, agent and (on demand) checker containers."""

    def __init__(self, config, model, workspace, pulse, *, image, task="", clock=time.monotonic, sleep=time.sleep):
        self.config, self.model, self.workspace, self.pulse = config, model, Path(workspace), pulse
        self.image, self.task, self.clock, self.sleep = image, task, clock, sleep
        self.name = "bananavibe-" + secrets.token_hex(6)
        self.internal, self.outside = self.name + "-internal", self.name + "-egress"
        self.gateway, self.agent = self.name + "-gateway", self.name + "-agent"
        self.server_secret, self.control_secret = secrets.token_hex(24), secrets.token_hex(24)
        sweep(task)
        self._temporary = tempfile.TemporaryDirectory(prefix=f"bananavibe-engine-{task}-" if task else
                                                      "bananavibe-engine-")
        self.root = Path(self._temporary.name)
        self.port = None
        self.session = None
        self.poll = max(1, min(config.limits.poll_seconds, 5))

    # Lifecycle

    def _limits(self, memory_mb, *, disable_ipv6=True):
        uid, gid = (os.getuid(), os.getgid()) if os.getuid() else (65532, 65532)
        limits = ["--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--pids-limit=512",
                "--memory", f"{memory_mb}m", "--memory-swap", f"{memory_mb}m", "--cpus", str(self.config.limits.cpus),
                "--user", f"{uid}:{gid}", "--ulimit", "fsize=268435456:268435456", "--ulimit", "nofile=4096:4096",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g,mode=1777", "--label", "org.bananavibe.task=" + self.name,
                *self._task_label(), "--log-driver", "none"]
        if disable_ipv6:
            limits += ["--sysctl", "net.ipv6.conf.all.disable_ipv6=1",
                       "--sysctl", "net.ipv6.conf.default.disable_ipv6=1"]
        return limits

    def _verify_internal_network(self):
        """Refuse a bridge whose actual policy can expose host listeners."""
        result = docker("network", "inspect", self.internal, check=False, timeout=30)
        try:
            networks = json.loads(result.stdout)
            info = networks[0] if isinstance(networks, list) and len(networks) == 1 else {}
        except ValueError:
            info = {}
        options = info.get("Options") if isinstance(info, dict) else None
        if (result.returncode or not isinstance(info, dict) or info.get("Driver") != "bridge"
                or info.get("Internal") is not True or info.get("EnableIPv6") is not False
                or not isinstance(options, dict)
                or options.get("com.docker.network.bridge.inhibit_ipv4") != "true"):
            raise EngineError("The task bridge must be internal, IPv4-only and have no host IPv4 gateway. "
                              "Docker returned an unsafe or unreadable network policy.")

    def _task_label(self):
        return ["--label", f"{TASK_LABEL}={self.task}"] if self.task else []

    def _private_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        if os.geteuid() == 0:
            os.chown(path, 65532, 65532)
        return path

    def _state_dir(self, name):
        path = self.root / name
        path.mkdir(mode=0o700)
        give_to_sandbox(path)
        return path

    def _proxy_env(self):
        proxy = "http://gateway:8080"
        return {"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy,
                "NO_PROXY": "localhost,127.0.0.1,gateway", "no_proxy": "localhost,127.0.0.1,gateway",
                "HOME": "/state", "XDG_CACHE_HOME": "/state/cache", "CI": "true"}

    def __enter__(self):
        try:
            self.start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_):
        self.close()

    def start(self):
        task_token = secrets.token_hex(24)
        limits = self.config.limits
        gateway_config = self._private_json("gateway.json", {
            "token": task_token, "control_token": self.control_secret, "endpoint": self.model.endpoint,
            "api_key": self.model.api_key(), "model": self.model.model, "provider": self.model.provider,
            "token_param": self.model.token_param, "max_calls": limits.max_calls,
            "expires_at": time.time() + (limits.max_minutes + 15) * 60, "egress": list(self.config.egress)})
        package = "@ai-sdk/openai-compatible" if self.model.provider == "azure" else self.model.package
        agent_config = self._private_json("opencode.json", {
            "$schema": "https://opencode.ai/config.json", "model": "task/" + self.model.model,
            "provider": {"task": {"npm": package, "name": "Task model",
                                  "options": {"baseURL": "http://gateway:8000/api", "apiKey": task_token},
                                  "models": {self.model.model: {"name": self.model.model, "tool_call": True}}}},
            "enabled_providers": ["task"], "permission": "allow", "autoupdate": False, "share": "disabled"})
        state = self._state_dir("agent-state")

        docker("network", "create", "--internal", "--ipv6=false", "-o", "com.docker.network.bridge.inhibit_ipv4=true",
               *self._task_label(), self.internal)
        self._verify_internal_network()
        docker("network", "create", "-o", "com.docker.network.bridge.enable_icc=false", *self._task_label(),
               self.outside)
        docker("create", "--name", self.gateway, "--network", self.outside, "--publish", "127.0.0.1::8082",
               *self._limits(256, disable_ipv6=False), "--mount", f"type=bind,src={gateway_config},dst=/run/gateway.json,readonly",
               "--entrypoint", "python3", self.image, "/opt/bananavibe/gateway.py", "/run/gateway.json")
        docker("network", "connect", "--alias", "gateway", self.internal, self.gateway)
        docker("start", self.gateway)

        environment = {**self._proxy_env(), "XDG_DATA_HOME": "/state/data", "XDG_CONFIG_HOME": "/state/config",
                       "XDG_STATE_HOME": "/state/state", "OPENCODE_CONFIG": "/run/opencode.json",
                       "OPENCODE_SERVER_USERNAME": "opencode", "OPENCODE_SERVER_PASSWORD": self.server_secret,
                       "OPENCODE_DISABLE_PROJECT_CONFIG": "true", "OPENCODE_DISABLE_AUTOUPDATE": "true",
                       "OPENCODE_DISABLE_SHARE": "true"}
        docker("run", "--detach", "--name", self.agent, "--network", self.internal, "--network-alias", "agent",
               *self._limits(limits.memory_mb), "--workdir", "/workspace",
               "--mount", f"type=bind,src={self.workspace},dst=/workspace",
               "--mount", f"type=bind,src={state},dst=/state",
               "--mount", f"type=bind,src={agent_config},dst=/run/opencode.json,readonly",
               *[item for key, value in environment.items() for item in ("--env", f"{key}={value}")],
               "--entrypoint", "opencode", self.image, "serve", "--hostname", "0.0.0.0", "--port", "4096")

        ports = json.loads(docker("inspect", self.gateway).stdout)[0]["NetworkSettings"]["Ports"].get("8082/tcp")
        if not ports or ports[0].get("HostIp") not in {"127.0.0.1", "::1"}:
            raise EngineError("The engine control port was not bound to loopback.")
        self.port = int(ports[0]["HostPort"])
        deadline = self.clock() + 180
        while self.clock() < deadline:
            self.pulse("Starting the coding engine")
            try:
                if (self.api("GET", "/global/health") or {}).get("healthy"):
                    return self
            except EngineError:
                pass
            self.sleep(2)
        raise EngineError("The coding engine did not become ready within three minutes.")

    def close(self):
        try:
            if self.session and self.port:
                try:
                    self.api("POST", f"/session/{self.session}/abort")
                except (EngineError, OSError, subprocess.SubprocessError):
                    pass
            operations = [("rm", "--force", "--volumes", self.agent, self.agent + "-check", self.gateway)]
            operations += [("network", "rm", network) for network in (self.internal, self.outside)]
            for operation in operations:
                try:
                    docker(*operation, check=False, timeout=60 if operation[0] == "rm" else 30)
                except (EngineError, OSError, subprocess.SubprocessError) as error:
                    # Cleanup must still remove the private credential file if
                    # the Docker daemon becomes unreachable during shutdown.
                    print(f"Sandbox cleanup could not remove {operation[0]} resources ({type(error).__name__}).")
        finally:
            self._temporary.cleanup()

    @contextmanager
    def paused(self):
        """Freeze every process in the agent so a snapshot is consistent."""
        docker("pause", self.agent)
        try:
            yield
        finally:
            docker("unpause", self.agent, check=False)

    # OpenCode API through the gateway's authenticated control port

    def api(self, method, path, body=None, params=None):
        query = "&".join(f"{key}={value}" for key, value in {"directory": "/workspace", **(params or {})}.items())
        token = f"opencode:{self.server_secret}".encode()
        headers = {"Authorization": "Basic " + base64.b64encode(token).decode(),
                   "X-BananaVibe-Control": self.control_secret}
        try:
            response = httpclient.request(method, f"http://127.0.0.1:{self.port}{path}?{query}", headers=headers,
                                          body=body if body is not None else (b"" if method == "POST" else None),
                                          timeout=15 if method == "GET" else 60, retries=2 if method == "GET" else 0,
                                          sleep=lambda seconds: (self.pulse(), self.sleep(seconds)))
        except httpclient.HTTPError as error:
            raise EngineError(f"The coding engine returned HTTP {error.status} for {path.split('/')[1]}.") from None
        except httpclient.Unreachable:
            raise EngineError("The coding engine is not reachable.") from None
        try:
            return response.json()
        except ValueError:
            raise EngineError("The coding engine returned an unreadable response.") from None

    def _latest(self, limit=6):
        messages = self.api("GET", f"/session/{self.session}/message", params={"limit": limit}) or []
        return sorted((item for item in messages if isinstance(item, dict)), key=lambda item: item["info"]["id"])

    def prompt(self, text, *, phase="Working on the task"):
        """Send a prompt and wait until the agent finishes, errors, or asks a question."""
        if not self.session:
            self.session = self.api("POST", "/session", {"title": "Maintenance task"})["id"]
        before = self._latest(1)
        baseline = before[-1]["info"]["id"] if before else ""
        self.api("POST", f"/session/{self.session}/prompt_async", {
            "model": {"providerID": "task", "modelID": self.model.model}, "agent": "build",
            "parts": [{"type": "text", "text": text}]})
        submitted, notice = self.clock(), ""
        while True:
            self.pulse(phase)
            question = self._pending_question()
            if question:
                return {"status": "blocked", "question": question}
            status = (self.api("GET", "/session/status") or {}).get(self.session) or {"type": "idle"}
            if status.get("type") == "retry" and status.get("message") != notice:
                notice = status.get("message") or ""
                print(f"Model provider asked to retry: {notice[:300]}")
            if status.get("type") == "idle":
                latest = self._latest()
                reply = latest[-1] if latest else None
                info = (reply or {}).get("info", {})
                if reply and info.get("role") == "assistant" and info.get("id", "") > baseline:
                    if info.get("error"):
                        raise EngineError(describe_error(info["error"]))
                    return {"status": "idle"}
                if self.clock() - submitted > 120:
                    raise EngineError("The coding engine stopped without replying. Check the model configuration.")
            self.sleep(self.poll)

    def _pending_question(self):
        for kind in ("question", "permission"):
            for item in self.api("GET", "/" + kind) or []:
                if not isinstance(item, dict):
                    continue
                if kind == "question":
                    text = " ".join(str(entry.get("question", "")) for entry in item.get("questions", []))
                else:
                    text = f"permission to use {item.get('permission', 'a tool')}"
                return ("The coding agent asked: " + text.strip())[:4000]
        return None

    def interrupt(self):
        """Stop the current prompt and wait until the session is idle."""
        if not self.session:
            return
        self.api("POST", f"/session/{self.session}/abort")
        for _ in range(30):
            status = (self.api("GET", "/session/status") or {}).get(self.session) or {"type": "idle"}
            if status.get("type") == "idle":
                return
            self.sleep(1)

    # Commands

    def run_command(self, container, command, *, phase):
        """Run one command in a container, bounded by limits.command_minutes."""
        timeout = self.config.limits.command_minutes * 60
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(["docker", "exec", "--workdir", "/workspace", container, *command],
                                       stdout=log, stderr=subprocess.STDOUT, env=docker_env())
            started = self.clock()
            try:
                while process.poll() is None:
                    self.pulse(phase)
                    if self.clock() - started > timeout:
                        raise CommandTimeout(f"{json.dumps(list(command))} did not finish within "
                                             f"{self.config.limits.command_minutes} minutes.")
                    if log.tell() > 32 * 1024 * 1024:
                        raise EngineError(f"{json.dumps(list(command))} produced more than 32 MiB of output.")
                    self.sleep(1)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            log.seek(max(0, log.tell() - TAIL))
            return process.returncode, log.read().decode(errors="replace")

    def prepare(self):
        for command in self.config.prepare:
            code, output = self.run_command(self.agent, command, phase="Installing dependencies")
            if code:
                print(f"Preparation command {json.dumps(list(command))} exited with {code}:\n{output[-3000:]}")
                raise EngineError(f"The preparation command {json.dumps(list(command))} failed (exit {code}). "
                                  "Correct the dependency setup, then resume.")

    def check(self, tree):
        """Run prepare + checks on a clean copy of the committed tree.

        A fresh container and a fresh /state mean the agent cannot influence
        the result by editing tools, caches or virtual environments it used
        while working. Returns a list of failure descriptions.
        """
        container = self.agent + "-check"
        state = self.root / "check-state"
        shutil.rmtree(state, ignore_errors=True)
        self._state_dir("check-state")
        try:
            self._verify_internal_network()
            docker("run", "--detach", "--name", container, "--network", self.internal,
                   *self._limits(self.config.limits.memory_mb), "--workdir", "/workspace",
                   "--mount", f"type=bind,src={tree},dst=/workspace", "--mount", f"type=bind,src={state},dst=/state",
                   *[item for key, value in self._proxy_env().items() for item in ("--env", f"{key}={value}")],
                   "--entrypoint", "sleep", self.image, "infinity")
            for command in self.config.prepare:
                code, output = self.run_command(container, command, phase="Preparing a clean checkout for checks")
                if code:
                    return [f"Preparation command {json.dumps(list(command))} failed on a clean checkout "
                            f"(exit {code}):\n{output}"]
            failures = []
            for command in self.config.checks:
                try:
                    code, output = self.run_command(container, command, phase="Running the acceptance checks")
                except CommandTimeout as error:
                    failures.append(str(error))
                    break
                if code:
                    failures.append(f"Check {json.dumps(list(command))} failed (exit {code}):\n{output}")
            return failures
        finally:
            docker("rm", "--force", container, check=False, timeout=60)
            shutil.rmtree(state, ignore_errors=True)


def describe_error(error):
    name = error.get("name", "Error") if isinstance(error, dict) else "Error"
    data = error.get("data", {}) if isinstance(error, dict) else {}
    message = str(data.get("message") or "")[:500]
    if name == "ProviderAuthError":
        return "The model provider rejected the credentials. Check the model's api_key_env secret."
    if name == "MessageAbortedError":
        return "The coding engine's reply was aborted."
    if name == "ContextOverflowError":
        return "The conversation exceeded the model's context window. Resume to start a fresh session."
    if name == "APIError" and data.get("statusCode"):
        return f"The model provider returned HTTP {data['statusCode']}: {message}"
    return f"The coding engine failed ({name}): {message}"
