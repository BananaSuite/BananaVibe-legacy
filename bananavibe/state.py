"""Durable task records on the control repository's state branch.

Each task is one JSON file written through the contents API with the file's
previous blob SHA, so concurrent writers lose cleanly (compare-and-swap)
instead of overwriting each other. The format is schema 1, unchanged from
the preview release, so tasks started by it continue after an upgrade.
"""

import base64
import copy
import hashlib
import json
import math
import re
import tempfile
import time

from .forge import TRANSIENT, Conflict, ForgeError, Unavailable
from .gitutil import Git

SCHEMA = 1
TASK_BRANCH = re.compile(r"bananavibe/[0-9a-f]{16}-[1-9][0-9]*")
DIRECTORY = ".bananavibe-state/tasks"
MAX_BYTES = 500_000
MAX_EVENTS = 200
MAX_GUIDANCE = 100
MAX_GUIDANCE_CHARS = 200_000
ACTIVE = {"queued", "running", "stopping", "publishing"}
TERMINAL = {"complete"}
STATE_README = b"""This branch holds BananaVibe task records. It is managed automatically.
Do not merge it or delete it while tasks are active.
"""


def task_id(repository, number):
    return hashlib.sha256(f"{repository}#{int(number)}".encode()).hexdigest()[:16]


def issue_digest(issue):
    """Fingerprint of the issue text a maintainer approved."""
    text = f"{issue.get('title') or ''}\0{issue.get('body') or ''}"
    return hashlib.sha256(text.encode()).hexdigest()


def new_task(config, number):
    return {"schema": SCHEMA, "issue": int(number), "task_id": task_id(config.control_repository, number),
            "target_repository": config.target_repository, "base_branch": config.base_branch,
            "generation": 1, "branch": "", "base_sha": "", "checkpoint": "", "model": config.default_model,
            "status": "queued", "desired": "running", "request": 0, "operation": "continue", "runner": "",
            "lease_until": 0, "iterations": 0, "guidance": [], "events": [], "pr_url": "", "reason": "",
            "phase": "", "approved": "", "status_comment": 0, "pushing": ""}


def normalize(state):
    """Fill fields that older records may lack."""
    if state is None:
        return None
    defaults = {"generation": 1, "branch": "", "base_sha": "", "checkpoint": "", "request": 0,
                "operation": "continue", "runner": "", "lease_until": 0, "iterations": 0, "guidance": [],
                "events": [], "pr_url": "", "reason": "", "phase": "", "approved": "", "status_comment": 0,
                "pushing": ""}
    for key, value in defaults.items():
        state.setdefault(key, copy.deepcopy(value))
    return state


class StateError(RuntimeError):
    pass


def validate(state, repository, number=None):
    """Normalize compatible records and reject malformed data before using it.

    The state branch is editable by maintainers and can also be damaged by a
    interrupted manual restore. A bad record must fail explicitly, rather than
    crashing a command midway through a mutation or disappearing from a backup.
    """
    if (not isinstance(state, dict) or type(state.get("schema")) is not int or state["schema"] != SCHEMA
            or type(state.get("issue")) is not int or state["issue"] < 1
            or (number is not None and state["issue"] != number)):
        raise StateError("The saved task record is invalid.")
    state = normalize(state)
    strings = ("task_id", "target_repository", "base_branch", "model", "status", "desired", "operation", "branch",
               "base_sha", "checkpoint", "runner", "pr_url", "reason", "phase", "approved", "pushing")
    if any(not isinstance(state.get(key), str) for key in strings):
        raise StateError("The saved task record has invalid text fields.")
    if state["task_id"] != task_id(repository, state["issue"]):
        raise StateError("The saved task record has a different task identity.")
    counters = ("generation", "request", "iterations", "status_comment")
    if any(type(state[key]) is not int or state[key] < (1 if key == "generation" else 0) for key in counters):
        raise StateError("The saved task record has invalid counters.")
    lease = state["lease_until"]
    if type(lease) not in {int, float} or not 0 <= lease <= 10**12 or not math.isfinite(lease):
        raise StateError("The saved task record has an invalid lease.")
    if (not isinstance(state["events"], list) or len(state["events"]) > MAX_EVENTS
            or any(not isinstance(item, str) for item in state["events"])):
        raise StateError("The saved task record has invalid command history.")
    guidance = state["guidance"]
    if (not isinstance(guidance, list) or len(guidance) > MAX_GUIDANCE
            or any(not isinstance(item, dict) or not isinstance(item.get("text"), str)
                   or not isinstance(item.get("author"), str) for item in guidance)
            or sum(len(item["text"]) for item in guidance) > MAX_GUIDANCE_CHARS):
        raise StateError("The saved task record has invalid maintainer guidance.")
    return state


class StateStore:
    def __init__(self, forge, *, clock=time.time, sleep=time.sleep):
        self.forge, self.config, self.clock, self.sleep = forge, forge.config, clock, sleep

    def path(self, number):
        return f"{DIRECTORY}/{task_id(self.config.control_repository, number)}.json"

    def check_branch(self):
        info = self.forge.repo(self.config.control_repository)
        default = info.get("default_branch") if isinstance(info, dict) else None
        if not isinstance(default, str) or not default:
            raise StateError("The control repository's default branch could not be determined; refusing state changes.")
        if default == self.config.state_branch:
            raise StateError("state_branch cannot be the control repository's default branch.")

    def ensure(self):
        """Create the state branch as an orphan branch holding only task records.

        Because it shares no files with the default branch, commits to it do
        not trigger the repository's push workflows.
        """
        repository = self.config.control_repository
        self.check_branch()
        if self.forge.branch_sha(repository, self.config.state_branch):
            return
        with tempfile.TemporaryDirectory(prefix="bananavibe-state-") as root:
            git = Git(f"{root}/state.git", token=self.forge.token, username=self.forge.login)
            blob = git.out("hash-object", "-w", "--stdin", input=STATE_README.decode())
            tree = git.out("mktree", input=f"100644 blob {blob}\tREADME.md\n")
            commit = git.out("commit-tree", tree, "-m", "Start BananaVibe task state")
            ref = "refs/heads/" + self.config.state_branch
            pushed = git.run("push", "--quiet", f"--force-with-lease={ref}:", self.forge.clone_url(repository),
                             f"{commit}:{ref}", check=False, timeout=120)
        if pushed.returncode and not self.forge.branch_sha(repository, self.config.state_branch):
            raise StateError("Could not create the state branch. The bot needs Contents write access "
                             "to the control repository.")

    def make_orphan(self):
        """Rewrite a preview-release state branch (a copy of the default branch) as an orphan.

        Keeps every task record, drops the copied project files, and pushes
        with a lease so a concurrent state write makes it fail harmlessly.
        Returns False when the branch already holds only task records.
        """
        repository, name = self.config.control_repository, self.config.state_branch
        self.check_branch()
        if not self.forge.branch_sha(repository, name):
            raise StateError(f"The state branch {name} does not exist yet; nothing to convert.")
        if any(state["runner"] and state["lease_until"] > self.clock() for state in self.all()):
            raise StateError("A task is running. Wait for it to finish (or its lease to expire) first.")
        with tempfile.TemporaryDirectory(prefix="bananavibe-state-") as root:
            git = Git(f"{root}/state.git", token=self.forge.token, username=self.forge.login)
            url, ref = self.forge.clone_url(repository), f"refs/heads/{name}"
            git.fetch(url, ref, depth=1)
            head = git.out("rev-parse", "FETCH_HEAD")
            names = git.out("ls-tree", "--name-only", "FETCH_HEAD").split("\n")
            if set(names) <= {"README.md", ".bananavibe-state"} and git.run(
                    "rev-list", "--count", "FETCH_HEAD", check=False).stdout.strip() == "1":
                return False
            entries = [f"100644 blob {git.out('hash-object', '-w', '--stdin', input=STATE_README.decode())}\tREADME.md"]
            records = git.run("rev-parse", "--verify", "--quiet", "FETCH_HEAD:.bananavibe-state", check=False)
            if records.returncode == 0:
                entries.append(f"040000 tree {records.stdout.strip()}\t.bananavibe-state")
            tree = git.out("mktree", input="\n".join(entries) + "\n")
            commit = git.out("commit-tree", tree, "-m", "Keep only BananaVibe task records on the state branch")
            pushed = git.run("push", "--quiet", f"--force-with-lease={ref}:{head}", url, f"{commit}:{ref}",
                             check=False, timeout=120)
        if pushed.returncode:
            raise StateError("The state branch changed during conversion, or is protected against "
                             "force pushes. Retry, or allow the bot to force-push that branch once.")
        return True

    def read(self, number):
        record = self.forge.content(self.path(number))
        if not record:
            return None, None
        if not isinstance(record, dict) or record.get("type", "file") != "file":
            raise StateError("The task record is not a file.")
        try:
            if int(record.get("size") or 0) > MAX_BYTES:
                raise StateError("The task record is too large.")
            encoded = record.get("content") or ""
            if not isinstance(encoded, str) or len(encoded) > (MAX_BYTES * 4 // 3 + 4) * 2:
                raise StateError("The task record is too large or has invalid encoding.")
            raw = base64.b64decode("".join(encoded.split()), validate=True)
            if len(raw) > MAX_BYTES:
                raise StateError("The task record is too large.")
            data = json.loads(raw)
            sha = record["sha"]
            if not isinstance(sha, str) or not sha:
                raise StateError("The task record has no revision.")
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise StateError("The saved task record is unreadable.") from None
        return validate(data, self.config.control_repository, int(number)), sha

    def mutate(self, number, change):
        """Apply change(copy) with compare-and-swap; None or no-op leaves it untouched.

        A lost race is retried with the fresh record. So is a write the forge
        failed transiently: if it did land after all, the retry re-reads it,
        and every change function is safe to apply to its own result.
        """
        self.check_branch()
        transient = 0
        for attempt in range(8):
            old, sha = self.read(number)
            new = change(copy.deepcopy(old))
            if new is None or new == old:
                return old
            new["events"] = new.get("events", [])[-MAX_EVENTS:]
            new["updated_at"] = self.clock()
            encoded = json.dumps(new, sort_keys=True, separators=(",", ":")).encode()
            if len(encoded) > MAX_BYTES:
                raise StateError("The task record would exceed its size limit. Start a new issue.")
            try:
                self.forge.put_content(self.path(number), encoded, sha)
                return new
            except Conflict:
                self.sleep(min(0.2 * 2 ** attempt, 3))
            except (ForgeError, Unavailable) as error:
                if isinstance(error, ForgeError) and error.status not in TRANSIENT or transient >= 4:
                    raise
                transient += 1
                self.sleep(2 ** transient)
        raise StateError("The task record kept changing; retry the command.")

    def claim(self, number, runner):
        def change(state):
            if not state or state["desired"] != "running" or state["status"] in TERMINAL:
                return None
            if state["runner"] and state["runner"] != runner and state["lease_until"] > self.clock():
                return None
            state.update(runner=runner, lease_until=self.clock() + self.config.limits.lease_seconds, status="running")
            return state
        state = self.mutate(number, change)
        return state if state and state.get("runner") == runner else None

    def owned(self, number, runner, **changes):
        def change(state):
            if not state or state["runner"] != runner:
                raise LeaseLost("This runner no longer holds the task lease.")
            state.update(changes)
            state["lease_until"] = self.clock() + self.config.limits.lease_seconds
            return state
        return self.mutate(number, change)

    def release(self, number, runner, **changes):
        def change(state):
            if not state or state["runner"] != runner:
                return None
            state.update(changes, runner="", lease_until=0)
            return state
        return self.mutate(number, change)

    def all(self):
        """Every task record, read through one shallow Git fetch of the state branch."""
        with tempfile.TemporaryDirectory(prefix="bananavibe-state-") as root:
            git = Git(f"{root}/state.git", token=self.forge.token, username=self.forge.login)
            if not self.forge.branch_sha(self.config.control_repository, self.config.state_branch):
                return []
            git.fetch(self.forge.clone_url(self.config.control_repository),
                      f"refs/heads/{self.config.state_branch}", depth=1)
            files = git.read_files("FETCH_HEAD", DIRECTORY + "/", limit=MAX_BYTES, strict=True)
        records = []
        for path, raw in sorted(files.items()):
            if not path.endswith(".json"):
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                raise StateError("An unreadable task record exists on the state branch.") from None
            state = validate(data, self.config.control_repository)
            if path != self.path(state["issue"]):
                raise StateError("A task record is stored under a different task identity.")
            records.append(state)
        return records


class LeaseLost(StateError):
    pass
