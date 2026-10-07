"""Run current BananaVibe against an actual disposable Forgejo release.

The Docker container has only tmpfs data, a read-only root, no capabilities,
no Docker socket and a loopback-only HTTP port. All users, repos and credentials
are synthetic. No application repository or external forge is modified.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import shutil
import tempfile
import time
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.docker
from bananavibe import httpclient
from bananavibe.config import Config, Model
from bananavibe.forge import Conflict, Forge
from bananavibe.gitutil import Git
from bananavibe.state import StateStore, new_task


def command(*args, input=None):
    result = subprocess.run(args, input=input, capture_output=True, text=True, timeout=120)
    if result.returncode:
        # Never echo the command, stderr or stdout: CLI arguments and generated
        # token output can contain disposable credentials.
        raise RuntimeError(f"Local command failed with exit status {result.returncode}.")
    return result.stdout.strip()


def test_real_forgejo_acceptance():
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode:
        pytest.skip("Docker is not available")
    # Forgejo 16.0.5, pinned to the official registry manifest digest.
    image = "codeberg.org/forgejo/forgejo@sha256:cf5f5ae6acf2ababca0ee3d255705b83a47f35b25e07fc931d694d60664053fe"
    command("docker", "pull", image)
    image_info = json.loads(command("docker", "image", "inspect", image))[0]
    name = "bananavibe-forgejo-acceptance-" + uuid.uuid4().hex[:12]
    checks = []
    report = {
        "scope": "Actual local Forgejo, current BananaVibe. Synthetic users/repositories; no application commits, external writes or production deployment.",
        "official_download_reference": "https://forgejo.org/download/",
        "image": image,
        "image_id": image_info["Id"],
        "image_repo_digests": image_info["RepoDigests"],
    }

    def passed(label):
        checks.append(label)
        print("Passed: " + label, flush=True)

    try:
        command(
            "docker", "run", "--rm", "-d", "--name", name,
            "--read-only", "--user", "1000:1000", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--memory", "768m",
            "--cpus", "2", "--pids-limit", "128",
            "--tmpfs", "/data:rw,nosuid,nodev,size=512m,uid=1000,gid=1000,mode=0700",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
            "-p", "127.0.0.1::3000", "--entrypoint", "/bin/sh", image,
            "-c", "while [ ! -f /data/.ready ]; do sleep 0.1; done; exec forgejo --work-path /data --config /data/app.ini web",
        )
        port = command("docker", "port", name, "3000/tcp").rsplit(":", 1)[1]
        base = "http://127.0.0.1:" + port
        settings = f"""APP_NAME = BananaVibe acceptance fixture
RUN_USER = git
RUN_MODE = prod
[server]
PROTOCOL = http
ROOT_URL = {base}/
HTTP_ADDR = 0.0.0.0
HTTP_PORT = 3000
DISABLE_SSH = true
OFFLINE_MODE = true
[database]
DB_TYPE = sqlite3
PATH = /data/forgejo.db
[repository]
ROOT = /data/repositories
DEFAULT_BRANCH = main
[security]
INSTALL_LOCK = true
SECRET_KEY = {secrets.token_hex(32)}
[service]
DISABLE_REGISTRATION = true
ENABLE_CAPTCHA = false
[mailer]
ENABLED = false
[actions]
ENABLED = false
[session]
PROVIDER = file
PROVIDER_CONFIG = /data/sessions
[log]
MODE = console
LEVEL = Error
"""
        command("docker", "exec", "-i", name, "sh", "-c", "umask 077; cat > /data/app.ini", input=settings)

        def admin(*args):
            return command("docker", "exec", name, "forgejo", "--work-path", "/data", "--config", "/data/app.ini", *args)

        admin("migrate")
        for login in ("maintainer", "fixture-bot", "reader"):
            args = ["admin", "user", "create", "--username", login, "--password", secrets.token_urlsafe(32),
                    "--email", login + "@example.invalid", "--must-change-password=false"]
            if login == "maintainer":
                args.append("--admin")
            admin(*args)
        admin_token = admin("admin", "user", "generate-access-token", "--username", "maintainer", "--token-name", "acceptance", "--scopes", "all", "--raw")
        bot_token = admin("admin", "user", "generate-access-token", "--username", "fixture-bot", "--token-name", "acceptance", "--scopes", "all", "--raw")
        command("docker", "exec", name, "touch", "/data/.ready")
        for _attempt in range(60):
            try:
                version = httpclient.request("GET", base + "/api/v1/version", timeout=2).json()
                break
            except httpclient.Unreachable:
                time.sleep(0.25)
        else:
            raise RuntimeError("The disposable Forgejo did not become ready.")
        assert version["version"].startswith("16.0.5"), version
        report["reported_version"] = version["version"]
        passed("actual Forgejo release and restricted container startup")

        def api(method, route, body=None):
            response = httpclient.request(method, base + "/api/v1" + route,
                                          headers={"Authorization": "token " + admin_token}, body=body, timeout=20)
            return response.json()

        for repository in ("control", "target"):
            api("POST", "/user/repos", {"name": repository, "private": True, "auto_init": True, "default_branch": "main"})
            api("PUT", f"/repos/maintainer/{repository}/collaborators/fixture-bot", {"permission": "write"})
            api("PUT", f"/repos/maintainer/{repository}/collaborators/reader", {"permission": "read"})
        config = Config(forge="forgejo", server_url=base, api_url=base + "/api/v1",
                        control_repository="maintainer/control", target_repository="maintainer/target",
                        base_branch="main", models={"coding": Model("coding", "openai-compatible", "fixture", base + "/v1", "")},
                        default_model="coding", checks=(("python", "-m", "pytest"),), allow_http=True)
        forge = Forge(config, bot_token)
        assert forge.login == "fixture-bot"
        metadata = forge.repo(config.control_repository)
        assert metadata["permissions"] == {"admin": False, "push": True, "pull": True}
        try:
            forge.authorized("maintainer")
        except RuntimeError as error:
            assert "Give the dedicated bot Admin access" in str(error)
        else:
            raise AssertionError("Unsupported write-only bot was not refused.")
        passed("real write-only bot refusal with actionable setup error")
        for repository in ("control", "target"):
            api("PUT", f"/repos/maintainer/{repository}/collaborators/fixture-bot", {"permission": "admin"})
        assert forge.authorized("maintainer") and forge.authorized("fixture-bot")
        assert not forge.authorized("reader") and not forge.authorized("unknown-person")
        passed("real bot identity and write authorization on both repositories")

        created = api("POST", "/repos/maintainer/control/issues", {"title": "Synthetic acceptance task", "body": "Check the local task lifecycle."})
        number = created["number"]
        issue = forge.issue(number)
        assert issue["title"] == "Synthetic acceptance task"
        comment = forge.comment(number, "Synthetic status: queued")
        forge.edit_comment(comment, "Synthetic status: running")
        assert forge.comments(number)[0]["body"] == "Synthetic status: running"
        assert forge.set_issue_state(number, "closed")["state"] == "closed"
        assert forge.set_issue_state(number, "open")["state"] == "open"
        passed("real issue reads, comments, status edits, close and reopen")

        main_before = forge.branch_sha(config.control_repository, "main")
        store = StateStore(forge)
        store.ensure()
        assert forge.branch_sha(config.control_repository, "main") == main_before
        assert forge.branch_sha(config.control_repository, config.state_branch)
        assert store.make_orphan() is False
        passed("real authenticated orphan state push preserves the default branch")
        state = new_task(config, number)
        state["reason"] = "initial"
        store.mutate(number, lambda _: state)
        loaded, first_sha = store.read(number)
        assert loaded["reason"] == "initial"
        store.mutate(number, lambda current: {**current, "reason": "updated"})
        loaded, current_sha = store.read(number)
        assert loaded["reason"] == "updated" and current_sha != first_sha
        try:
            forge.put_content(store.path(number), json.dumps(state).encode(), previous_sha=first_sha)
        except Conflict:
            pass
        else:
            raise AssertionError("Real stale file update was not refused.")
        assert store.read(number)[0]["reason"] == "updated"
        assert len(store.all()) == 1
        passed("real task file create/update/list and stale compare-and-swap refusal")

        branch = "bananavibe/acceptance-1"
        target_main = forge.branch_sha(config.target_repository, "main")
        forge.create_branch(config.target_repository, branch, target_main)
        try:
            forge.create_branch(config.target_repository, branch, target_main)
        except Conflict:
            pass
        else:
            raise AssertionError("Duplicate real branch creation was not refused.")
        api("POST", "/repos/maintainer/target/contents/fixture.txt",
            {"branch": branch, "message": "Synthetic acceptance fixture", "content": base64.b64encode(b"fixture-only\n").decode()})
        pull = forge.publish_pull(branch, "Check the fixture", "Synthetic draft description")
        assert pull["title"].startswith("WIP: ") and pull["state"] == "open"
        assert forge.find_pull(branch)["number"] == pull["number"]
        updated = forge.publish_pull(branch, "Updated fixture", "Updated synthetic description")
        assert updated["number"] == pull["number"] and updated["body"] == "Updated synthetic description"
        passed("real nested task branch conflicts and draft PR create/find/update")
        api("PATCH", f"/repos/maintainer/target/pulls/{pull['number']}", {"state": "closed"})
        try:
            forge.publish_pull(branch, "Must be refused", "No reopening")
        except ValueError:
            pass
        else:
            raise AssertionError("Closed real PR was reopened by publication.")
        assert forge.branch_sha(config.target_repository, "main") == target_main
        passed("real closed-PR refusal and target default branch preservation")

        with tempfile.TemporaryDirectory(prefix="bananavibe-forgejo-review-") as temporary:
            git = Git(Path(temporary) / "state.git", token=bot_token, username=forge.login)
            git.fetch(forge.clone_url(config.control_repository), "refs/heads/" + config.state_branch)
            assert git.out("ls-tree", "--name-only", "FETCH_HEAD").splitlines() == [".bananavibe-state", "README.md"]
            assert git.out("rev-list", "--count", "FETCH_HEAD") == "3"
        passed("real authenticated Git fetch and state-only tree")
        report["status"] = "passed"
        report["checks"] = checks
        report["source_sha256"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                  for name in ("bananavibe/forge.py", "bananavibe/httpclient.py", "bananavibe/state.py", "bananavibe/gitutil.py")}
        print(json.dumps(report, sort_keys=True), flush=True)
        print(f"Actual Forgejo acceptance passed {len(checks)} groups.", flush=True)
    finally:
        subprocess.run(["docker", "rm", "--force", "--volumes", name], capture_output=True, timeout=30, check=False)
