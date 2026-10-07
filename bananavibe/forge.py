"""GitHub and Forgejo REST calls. The forge token never leaves this process
and its Git child processes; the coding sandbox never sees it."""

import base64
from urllib.parse import quote

from . import httpclient

WRITE_PERMISSIONS = {"write", "maintain", "admin", "owner"}


class ForgeError(RuntimeError):
    def __init__(self, status, what):
        self.status = status
        super().__init__(f"The forge refused to {what} (HTTP {status}). Check the bot's repository access.")


class Conflict(ForgeError):
    """The resource changed or already exists (optimistic write lost)."""


class Unavailable(RuntimeError):
    """The forge API could not be reached; the request may be retried."""


# Statuses after which a write may be retried: the request never took effect,
# or it did and a compare-and-swap retry notices.
TRANSIENT = {429, 500, 502, 503, 504}


def _rate_limited(error):
    """GitHub reports secondary rate limits as 403 with Retry-After or no remaining quota."""
    headers = {key.lower(): value for key, value in error.headers.items()}
    return error.status == 403 and ("retry-after" in headers or headers.get("x-ratelimit-remaining") == "0")


class Forge:
    def __init__(self, config, token, *, transport=httpclient.request):
        if not token:
            raise ValueError("BANANAVIBE_TOKEN is not set. Add the bot token as an Actions secret.")
        self.config, self.token, self._transport = config, token, transport
        self.github = config.forge == "github"
        self.headers = {"Authorization": ("Bearer " if self.github else "token ") + token}
        if self.github:
            self.headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        self.identity = self._identify()

    def _identify(self):
        try:
            data = self.call("GET", "/user", what="read the bot identity")
            if not isinstance(data, dict) or not isinstance(data.get("login"), str) or not data["login"]:
                raise Unavailable("The forge returned an invalid bot identity.")
            return data
        except ForgeError as error:
            # GitHub App installation tokens have no user profile but can
            # still list their repositories; confirm that before trusting it.
            if not self.github or error.status != 403:
                raise
            data = self.call("GET", "/installation/repositories", params={"per_page": 1},
                             what="read the installation")
            if not isinstance(data, dict) or not isinstance(data.get("repositories"), list):
                raise ValueError("Unexpected GitHub installation response.") from error
            return {"login": "x-access-token", "type": "Bot"}

    @property
    def login(self):
        return str(self.identity.get("login") or "")

    def call(self, method, path, *, body=None, params=None, missing=False, what="complete a request",
             conflict=(409,)):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Invalid API path.")
        url = self.config.api_url + path
        if params:
            url += "?" + "&".join(f"{quote(str(k))}={quote(str(v), safe='')}" for k, v in params.items())
        try:
            response = self._transport(method, url, headers=self.headers, body=body, timeout=45,
                                       retries=2 if method == "GET" else 0)
        except httpclient.HTTPError as error:
            if error.status == 404 and missing:
                return None
            status = 429 if _rate_limited(error) else error.status
            raise (Conflict if status in conflict else ForgeError)(status, what) from None
        except httpclient.Unreachable as error:
            raise Unavailable(f"The forge API could not be reached: {error}") from None
        try:
            return response.json()
        except (ValueError, UnicodeError):
            raise Unavailable("The forge returned an unreadable API response.") from None

    def pages(self, path, params=None, *, maximum=50, what="list items", complete=True):
        """Every item of a list; with complete=False, the first `maximum` pages are enough."""
        items = []
        for page in range(1, maximum + 1):
            data = self.call("GET", path, params={**(params or {}), "page": page,
                                                  ("per_page" if self.github else "limit"): 50}, what=what)
            if not isinstance(data, list):
                raise RuntimeError("Unexpected forge list response.")
            items.extend(data)
            if len(data) < 50:
                return items
        if not complete:
            return items
        raise RuntimeError(f"More than {maximum * 50} results; archive old items first.")

    # Repositories and permissions

    def repo(self, name):
        return self.call("GET", f"/repos/{name}", what=f"read {name}")

    def permission(self, repository, login):
        if not login or "/" in login:
            return "none"
        try:
            data = self.call("GET", f"/repos/{repository}/collaborators/{quote(login, safe='')}/permission",
                             missing=True, what="read collaborator permissions")
        except ForgeError as error:
            if not self.github and error.status == 403:
                # Forgejo allows a collaborator to read only their own role.
                # Checking command authors requires repository-admin access;
                # membership/assignee lists cannot establish write permission.
                raise RuntimeError(
                    "Forgejo refused collaborator permission checks. Give the dedicated bot Admin access "
                    "to both the control and target repositories, with repository and issue token scopes. "
                    "Instance administrator access is not required."
                ) from None
            raise
        return str((data or {}).get("permission") or "none")

    def authorized(self, login):
        """Commands need write access to both the control and the target repository."""
        repositories = {self.config.control_repository, self.config.target_repository}
        return all(self.permission(name, login) in WRITE_PERMISSIONS for name in repositories)

    # Issues and comments (always in the control repository)

    def _issues(self, suffix=""):
        return f"/repos/{self.config.control_repository}/issues{suffix}"

    def issue(self, number):
        return self.call("GET", self._issues(f"/{int(number)}"), what="read the issue")

    def comments(self, number):
        return self.pages(self._issues(f"/{int(number)}/comments"), what="read issue comments")

    def comment(self, number, text):
        data = self.call("POST", self._issues(f"/{int(number)}/comments"), body={"body": text[:60000]},
                         what="comment on the issue")
        return (data or {}).get("id")

    def edit_comment(self, comment_id, text):
        return self.call("PATCH", self._issues(f"/comments/{int(comment_id)}"), body={"body": text[:60000]},
                         what="update a status comment")

    def set_issue_state(self, number, state):
        return self.call("PATCH", self._issues(f"/{int(number)}"), body={"state": state},
                         what=f"mark the issue {state}")

    # Branches and state files

    def branch_sha(self, repository, name):
        if self.github:
            data = self.call("GET", f"/repos/{repository}/git/ref/heads/{quote(name, safe='/')}", missing=True,
                             what="read a branch")
            return data["object"]["sha"] if isinstance(data, dict) and "object" in data else None
        data = self.call("GET", f"/repos/{repository}/branches/{quote(name, safe='')}", missing=True,
                         what="read a branch")
        return data["commit"]["id"] if data else None

    def create_branch(self, repository, name, sha):
        if self.github:
            return self.call("POST", f"/repos/{repository}/git/refs", body={"ref": "refs/heads/" + name, "sha": sha},
                             what="create a branch", conflict=(409, 422))
        return self.call("POST", f"/repos/{repository}/branches", body={"new_branch_name": name, "old_ref_name": sha},
                         what="create a branch", conflict=(409, 422))

    def content(self, path):
        return self.call("GET", f"/repos/{self.config.control_repository}/contents/{quote(path, safe='/')}",
                         params={"ref": self.config.state_branch}, missing=True, what="read task state")

    def put_content(self, path, raw, previous_sha=None, message="Record BananaVibe task state"):
        body = {"message": message, "branch": self.config.state_branch, "content": base64.b64encode(raw).decode()}
        if previous_sha:
            body["sha"] = previous_sha
        # Forgejo creates files with POST and updates them with PUT.
        method = "PUT" if self.github or previous_sha else "POST"
        return self.call(method, f"/repos/{self.config.control_repository}/contents/{quote(path, safe='/')}",
                         body=body, what="save task state", conflict=(409, 422))

    # Pull requests (always in the target repository)

    def find_pull(self, branch):
        repository = self.config.target_repository
        if self.github:
            owner = repository.split("/")[0]
            pulls = self.call("GET", f"/repos/{repository}/pulls",
                              params={"state": "all", "head": f"{owner}:{branch}", "per_page": 50},
                              what="list pull requests")
        else:
            pull = self.call("GET", f"/repos/{repository}/pulls/{quote(self.config.base_branch, safe='')}/"
                             f"{quote(branch, safe='/')}", missing=True, what="find the pull request")
            if pull:
                return pull
            # Older Forgejo versions lack the lookup above. A task's pull request
            # is recently updated, so the newest pages are enough to find it.
            pulls = self.pages(f"/repos/{repository}/pulls", {"state": "all", "sort": "recentupdate"},
                               maximum=10, what="list pull requests", complete=False)
        matches = [pull for pull in pulls or [] if (pull.get("head") or {}).get("ref") == branch]
        return max(matches, key=lambda pull: pull.get("number", 0)) if matches else None

    @staticmethod
    def pull_open(pull):
        return pull.get("state") == "open" and not pull.get("merged")

    def publish_pull(self, branch, title, body):
        """Create a draft PR for the branch, or refresh the open one."""
        repository = self.config.target_repository
        title, body = title[:180], body[:60000]
        existing = self.find_pull(branch)
        if existing:
            if not self.pull_open(existing):
                raise ValueError("The task's pull request was closed or merged. "
                                 "Use `/banana restart` to prepare a new contribution.")
            if not self.github:
                title = "WIP: " + title
            return self.call("PATCH", f"/repos/{repository}/pulls/{int(existing['number'])}",
                             body={"title": title, "body": body}, what="update the pull request")
        if not self.github:
            return self.call("POST", f"/repos/{repository}/pulls",
                             body={"title": "WIP: " + title, "body": body, "head": branch,
                                   "base": self.config.base_branch}, what="open a pull request")
        payload = {"title": title, "body": body, "head": branch, "base": self.config.base_branch, "draft": True}
        try:
            return self.call("POST", f"/repos/{repository}/pulls", body=payload, what="open a draft pull request",
                             conflict=())
        except ForgeError as error:
            # Draft PRs are unavailable for private repositories on some plans.
            if error.status != 422:
                raise
            payload.update(draft=False, title="[Draft] " + title)
            return self.call("POST", f"/repos/{repository}/pulls", body=payload, what="open a pull request")

    def clone_url(self, repository):
        return f"{self.config.server_url}/{repository}.git"
