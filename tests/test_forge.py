"""Both forge wire protocols against a local HTTP server."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from bananavibe import httpclient
from bananavibe.forge import Conflict, Forge, ForgeError, Unavailable
from fakes import make_config


class API(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def handle_any(self):
        length = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(length) or b"{}")
        server = self.server
        server.calls.append((self.command, self.path, self.headers.get("Authorization"), data))
        route = self.path.split("?")[0]
        status, result = 200, {}
        if route.endswith("/user"):
            status, result = server.user_status, {"login": "bot"}
        elif route.endswith("/installation/repositories"):
            status, result = server.installation_status, {"repositories": []}
        elif "/collaborators/" in route:
            status = server.permission_status
            result = {"permission": "write" if "/maintainer/" in route else "read"}
        elif route.endswith("/limited"):
            self.send_response(403)
            self.send_header("Retry-After", "60")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif route.endswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/steal")
            self.end_headers()
            return
        elif "/pulls" in route and self.command == "POST":
            if data.get("draft") and server.draft_unsupported:
                status, result = 422, {"message": "Draft pull requests are not supported"}
            else:
                result = {"number": 9, "html_url": "https://forge.invalid/pull/9", **data}
        elif "/pulls/" in route and self.command == "PATCH":
            result = {"html_url": "https://forge.invalid/pull/7", **data}
        elif "/pulls/main/" in route:
            status = 404
        elif route.endswith("/pulls"):
            result = server.pulls
        elif "/contents/" in route and self.command != "GET":
            status = 409 if data.get("sha") == "stale" else 201
            result = {"content": {"sha": "new"}}
        elif route.endswith("/git/refs") or route.endswith("/branches"):
            status = 422 if server.branch_exists else 201
        raw = json.dumps(result).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PUT = do_PATCH = handle_any


@pytest.fixture
def api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), API)
    server.calls, server.pulls = [], []
    server.user_status, server.installation_status = 200, 200
    server.permission_status = 200
    server.draft_unsupported = server.branch_exists = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def forge_for(api, kind):
    url = f"http://127.0.0.1:{api.server_port}"
    return Forge(make_config(forge=kind, server_url=url, api_url=url, allow_http=True), "secret-token")


@pytest.mark.parametrize("kind", ["github", "forgejo"])
def test_protocols(api, kind):
    forge = forge_for(api, kind)
    assert forge.login == "bot"
    assert forge.authorized("maintainer") and not forge.authorized("stranger")
    forge.create_branch("team/app", "bananavibe/x-1", "a" * 40)
    forge.put_content(".bananavibe-state/tasks/x.json", b"{}")
    with pytest.raises(Conflict):
        forge.put_content(".bananavibe-state/tasks/x.json", b"{}", "stale")
    pull = forge.publish_pull("bananavibe/x-1", "Fix it", "Body")
    assert pull["html_url"]
    creation = next(call for call in api.calls if call[0] == "POST" and call[1].endswith("/pulls"))
    auth = ("Bearer " if kind == "github" else "token ") + "secret-token"
    assert all(call[2] == auth for call in api.calls)
    if kind == "github":
        assert creation[3]["draft"] is True and creation[3]["title"] == "Fix it"
    else:
        assert creation[3]["title"] == "WIP: Fix it" and "draft" not in creation[3]
        writes = [call[0] for call in api.calls if "/contents/" in call[1]]
        assert writes[0] == "POST" and writes[1] == "PUT"


def test_existing_branch_is_a_conflict(api):
    api.branch_exists = True
    with pytest.raises(Conflict):
        forge_for(api, "github").create_branch("team/app", "bananavibe/x-1", "a" * 40)


def test_draft_unavailable_falls_back_to_a_marked_ready_pr(api):
    api.draft_unsupported = True
    forge_for(api, "github").publish_pull("bananavibe/x-1", "Fix it", "Body")
    posts = [call[3] for call in api.calls if call[0] == "POST" and call[1].endswith("/pulls")]
    assert posts[-1]["title"] == "[Draft] Fix it" and posts[-1]["draft"] is False


@pytest.mark.parametrize("state", ["open", "closed", "merged"])
def test_existing_pull_request_is_updated_only_while_open(api, state):
    api.pulls = [{"number": 7, "state": "closed" if state != "open" else "open", "merged": state == "merged",
                  "head": {"ref": "bananavibe/x-1"}}]
    forge = forge_for(api, "github")
    if state == "open":
        assert forge.publish_pull("bananavibe/x-1", "New", "Body")["body"] == "Body"
    else:
        with pytest.raises(ValueError, match="closed or merged"):
            forge.publish_pull("bananavibe/x-1", "New", "Body")
    assert not any(call[0] == "POST" and "/pulls" in call[1] for call in api.calls)


def test_forgejo_finds_pull_requests_by_listing(api):
    api.pulls = [{"number": 4, "state": "open", "head": {"ref": "bananavibe/x-1"}}]
    assert forge_for(api, "forgejo").find_pull("bananavibe/x-1")["number"] == 4


def test_installation_tokens_are_verified(api):
    api.user_status = 403
    assert forge_for(api, "github").login == "x-access-token"
    api.installation_status = 401
    with pytest.raises(ForgeError):
        forge_for(api, "github")


@pytest.mark.parametrize("kind,status", [("github", 401), ("forgejo", 403)])
def test_bad_tokens_fail(api, kind, status):
    api.user_status = status
    with pytest.raises(ForgeError):
        forge_for(api, kind)
    assert not any("/installation/" in call[1] for call in api.calls)


@pytest.mark.parametrize("kind", ["forgejo", "github"])
def test_permission_checks_fail_closed_with_provider_specific_setup_errors(api, kind):
    api.permission_status = 403
    forge = forge_for(api, kind)
    if kind == "forgejo":
        with pytest.raises(RuntimeError, match="Give the dedicated bot Admin access") as error:
            forge.authorized("maintainer")
        assert "Instance administrator access is not required" in str(error.value)
        assert "secret-token" not in str(error.value)
    else:
        with pytest.raises(ForgeError) as error:
            forge.authorized("maintainer")
        assert error.value.status == 403
    assert all(method == "GET" for method, *_ in api.calls)
    assert not any(path.endswith("/assignees") or path.endswith("/collaborators") for _, path, *_ in api.calls)


def test_client_never_follows_redirects_or_leaks_bodies(api):
    url = f"http://127.0.0.1:{api.server_port}"
    with pytest.raises(httpclient.HTTPError) as error:
        httpclient.request("GET", url + "/redirect", headers={"Authorization": "Bearer secret"})
    assert error.value.status == 302
    assert "secret" not in str(error.value)
    with pytest.raises(httpclient.Unreachable):
        httpclient.request("GET", "http://127.0.0.1:1/", timeout=2)


def test_missing_token_is_explained():
    with pytest.raises(ValueError, match="BANANAVIBE_TOKEN"):
        Forge(make_config(), "")


def test_forgejo_lookup_survives_many_pull_requests(api):
    api.pulls = [{"number": n, "state": "open", "head": {"ref": f"feature-{n}"}} for n in range(50)]
    assert forge_for(api, "forgejo").find_pull("bananavibe/x-1") is None


def test_secondary_rate_limits_are_transient(api):
    with pytest.raises(ForgeError) as error:
        forge_for(api, "github").call("PUT", "/limited")
    assert error.value.status == 429


@pytest.mark.parametrize("payload", [b"not json", b"[]", b"null", b'{"login": null}', b'{"login": ""}'])
def test_invalid_identity_responses_fail_without_a_traceback_or_response_body(payload):
    def transport(*_, **__):
        return httpclient.Response(200, {}, payload)
    with pytest.raises(Unavailable) as error:
        Forge(make_config(), "secret-token", transport=transport)
    assert "secret-token" not in str(error.value) and "not json" not in str(error.value)
