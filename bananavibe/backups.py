"""BananaVibe's part of the shared encrypted-backup system.

A package holds the trusted configuration, the task records, the issue
transcripts of those tasks, a bundle of the control repository's default and
state branches, and a bundle of the task branches (pinned to each unfinished
task's recorded checkpoint). The format is unchanged from the preview
release, so older snapshots restore with this version and vice versa.
Encryption, upload and retention live in `banana_backup`, which is shared with
BananaWiki and BananaChat and must not diverge (see scripts/sync_backups.py).
"""

import argparse
from datetime import datetime, UTC
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from banana_backup.cli import add_commands, handle
from banana_backup.files import atomic_write, digest, directory, private_bytes, publish, write_json
from banana_backup.git import Git
from banana_backup.store import Store

from .config import Config, ConfigurationError, branch
from .forge import Forge
from .state import SCHEMA, TASK_BRANCH, StateError, StateStore, task_id, validate

PAYLOAD = {"configuration.toml", "tasks.json", "issues.json", "control.bundle", "target.bundle"}
REQUIRED = {"configuration.toml", "control.bundle", "tasks.json", "issues.json"}
COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def _manifest(metadata):
    if (not isinstance(metadata, dict) or type(metadata.get("schema")) is not int or metadata["schema"] != 1
            or metadata.get("product") != "BananaVibe" or not isinstance(metadata.get("files"), dict)
            or not REQUIRED <= set(metadata["files"]) <= PAYLOAD
            or any(not isinstance(metadata.get(key), str)
                   for key in ("server_url", "control_repository", "target_repository"))):
        raise ValueError("The BananaVibe archive has an invalid manifest.")
    for info in metadata["files"].values():
        if (not isinstance(info, dict) or type(info.get("bytes")) is not int or info["bytes"] < 0
                or not isinstance(info.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", info["sha256"])):
            raise ValueError("The BananaVibe archive has an invalid file manifest.")
    return metadata


def _git_settings(forge, repository, maximum):
    return {"url": forge.clone_url(repository), "forge": forge.config.forge,
            "username": forge.login or "git", "max_mib": maximum}


def _valid_record(forge, state):
    try:
        validate(state, forge.config.control_repository)
    except StateError:
        return False
    return (type(state.get("issue")) is int and state["issue"] > 0
            and state.get("task_id") == task_id(forge.config.control_repository, state["issue"])
            and state.get("target_repository") == forge.config.target_repository
            and (not state.get("branch") or TASK_BRANCH.fullmatch(state["branch"])))


def export_package(forge, configuration, destination, *, maximum=512, git_factory=Git):
    destination = Path(destination).absolute()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    states = list(StateStore(forge).all())
    if not all(_valid_record(forge, state) for state in states):
        raise ValueError("A saved task record is invalid; inspect the state branch before backing up.")
    limit = maximum * 1024 * 1024
    with tempfile.TemporaryDirectory(prefix="bananavibe-export-", dir=destination.parent) as name:
        work = Path(name)
        payload = directory(work / "payload")
        raw_config = Path(configuration).read_bytes()
        if len(raw_config) > 1024 * 1024:
            raise ValueError("The configuration file exceeds 1 MiB.")
        atomic_write(payload / "configuration.toml", raw_config)
        write_json(payload / "tasks.json", states)

        transcripts, total = [], 0
        for state in states:
            record = {"issue": forge.issue(state["issue"]), "comments": forge.comments(state["issue"])}
            total += len(json.dumps(record).encode())
            if total > limit:
                raise ValueError("Issue transcripts exceed the configured backup size limit.")
            transcripts.append(record)
        write_json(payload / "issues.json", transcripts)

        token = work / "forge.token"
        atomic_write(token, forge.token)
        control = git_factory(directory(work / "control"),
                              _git_settings(forge, forge.config.control_repository, maximum), token)
        refs = {branch(forge.repo(forge.config.control_repository)["default_branch"])}
        if forge.branch_sha(forge.config.control_repository, forge.config.state_branch):
            refs.add(forge.config.state_branch)
        for ref in sorted(refs):
            control.run("fetch", "--no-tags", control.url, f"refs/heads/{ref}:refs/heads/{ref}", bounded=True)
        control.run("bundle", "create", str(payload / "control.bundle"), "--branches", bounded=True)

        target = git_factory(directory(work / "target"),
                             _git_settings(forge, forge.config.target_repository, maximum), token)
        listing = target.run("ls-remote", "--heads", target.url, "refs/heads/bananavibe/*")
        if len(listing) > 1024 * 1024:
            raise ValueError("Too many task branches; delete merged ones before backing up.")
        task_refs = [ref for ref in (line.split("\t", 1)[1].removeprefix("refs/heads/")
                                     for line in listing.decode().splitlines() if "\t" in line)
                     if TASK_BRANCH.fullmatch(ref)]
        for ref in task_refs:
            target.run("fetch", "--no-tags", target.url, f"refs/heads/{ref}:refs/heads/{ref}", bounded=True)
        # Pin each unfinished task to the checkpoint its record names, even if
        # a running task has pushed newer work since.
        for state in states:
            ref, checkpoint = state.get("branch"), state.get("checkpoint")
            if not ref or not checkpoint:
                continue
            if not COMMIT.fullmatch(checkpoint):
                raise ValueError("A task record has an invalid checkpoint.")
            if ref not in task_refs:
                if state.get("status") == "complete":
                    continue  # merged branches are often deleted
                raise ValueError(f"The unfinished task branch {ref} is missing; recover it before backing up.")
            target.run("cat-file", "-e", checkpoint + "^{commit}")
            target.run("update-ref", "refs/heads/" + ref, checkpoint)
        if task_refs:
            target.run("bundle", "create", str(payload / "target.bundle"), "--branches", bounded=True)

        files = {path.name: {"sha256": digest(path), "bytes": path.stat().st_size} for path in payload.iterdir()}
        if sum(info["bytes"] for info in files.values()) > limit:
            raise ValueError("The backup exceeds the configured size limit.")
        metadata = {"schema": 1, "product": "BananaVibe", "created_at": datetime.now(UTC).isoformat(),
                    "server_url": forge.config.server_url, "control_repository": forge.config.control_repository,
                    "target_repository": forge.config.target_repository, "files": files}
        package = work / "package.tar.gz"
        with tarfile.open(package, "w:gz") as archive:
            for path in sorted(payload.iterdir()):
                archive.add(path, arcname=path.name, recursive=False)
            content = json.dumps(metadata, sort_keys=True).encode()
            info = tarfile.TarInfo("metadata.json")
            info.size, info.mode = len(content), 0o600
            archive.addfile(info, io.BytesIO(content))
        package.chmod(0o600)
        publish(package, destination)
    return destination


def restore_package(package, destination, *, maximum=1024):
    """Unpack a package into a new private review directory. Nothing is replayed."""
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Choose a new recovery directory; existing paths are never replaced.")
    if any(parent.is_symlink() for parent in destination.parents):
        raise ValueError("The recovery directory cannot be reached through symbolic links.")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bananavibe-restore-", dir=destination.parent) as name:
        work, seen, total = Path(name), set(), 0
        with tarfile.open(package, "r|*") as archive:
            for member in archive:
                if (not member.isfile() or member.name not in PAYLOAD | {"metadata.json"}
                        or member.name in seen or member.size < 0):
                    raise ValueError("Unsafe, duplicate, or unexpected entry in the BananaVibe archive.")
                total += member.size
                if total > maximum * 1024 * 1024 or (member.name == "metadata.json" and member.size > 65536):
                    raise ValueError("The BananaVibe archive exceeds the extracted-size limit.")
                seen.add(member.name)
                with archive.extractfile(member) as source, (work / member.name).open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                (work / member.name).chmod(0o600)
        if "metadata.json" not in seen:
            raise ValueError("The BananaVibe archive has no manifest.")
        metadata = _manifest(json.loads((work / "metadata.json").read_text()))
        if set(metadata["files"]) != seen - {"metadata.json"}:
            raise ValueError("The BananaVibe archive does not match its manifest.")
        for filename, info in metadata["files"].items():
            if (work / filename).stat().st_size != info.get("bytes") or digest(work / filename) != info.get("sha256"):
                raise ValueError("The BananaVibe archive failed its integrity check.")
        destination.mkdir(mode=0o700)  # exclusive: fails if something raced us
        try:
            for filename in seen:
                shutil.copyfile(work / filename, destination / filename)
                (destination / filename).chmod(0o600)
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
    return {"outcome": "restored for review", "directory": str(destination),
            "next": "Review the configuration and transcripts, recreate runner secrets, and use "
                    "`restore-state` only with the BananaVibe workflow disabled."}


def restore_state(forge, recovery, confirm_repository, *, git_factory=Git, clock=time.time):
    """Recreate missing checkpoint branches and write records back paused."""
    recovery = directory(recovery)
    metadata = _manifest(json.loads(private_bytes(recovery / "metadata.json", 65536)))
    if (confirm_repository != forge.config.control_repository
            or metadata.get("control_repository") != confirm_repository
            or metadata.get("target_repository") != forge.config.target_repository
            or metadata.get("server_url") != forge.config.server_url):
        raise ValueError("Confirm the original issue repository, with a matching forge and target configuration.")
    for name in ("tasks.json", "target.bundle"):
        info = metadata["files"].get(name)
        if info and ((recovery / name).stat().st_size != info["bytes"] or digest(recovery / name) != info["sha256"]):
            raise ValueError(f"The recovered {name} was modified after restore; refusing to apply it.")
    states = json.loads(private_bytes(recovery / "tasks.json", 64 * 1024 * 1024))
    if not isinstance(states, list):
        raise ValueError("Invalid task archive.")
    store, current, missing, numbers = StateStore(forge), {}, {}, set()
    store.check_branch()
    for state in states:
        number = state.get("issue") if isinstance(state, dict) else None
        if not isinstance(state, dict) or state.get("schema") != SCHEMA or not _valid_record(forge, state) \
                or number in numbers:
            raise ValueError("Invalid recovered task record.")
        numbers.add(number)
        if not forge.issue(number):
            raise ValueError(f"Issue #{number} no longer exists; issue numbers cannot be recreated.")
        old, sha = store.read(number)
        if old and old["lease_until"] > clock():
            raise ValueError("A task is still running. Disable the workflow and wait for leases to expire.")
        current[number] = {"state": old, "sha": sha}
        ref, checkpoint = state.get("branch"), state.get("checkpoint")
        if ref and checkpoint and state.get("status") != "complete":
            if not COMMIT.fullmatch(checkpoint):
                raise ValueError("Invalid recovered checkpoint.")
            existing = forge.branch_sha(forge.config.target_repository, ref)
            if existing and existing != checkpoint:
                raise ValueError(f"{ref} has newer or different work. Review it manually; restore never overwrites it.")
            if not existing:
                missing[ref] = checkpoint
    write_json(recovery / f"before-restore-{time.time_ns()}.json", current)
    if missing:
        if "target.bundle" not in metadata["files"]:
            raise ValueError("The backup has no checkpoint bundle for its unfinished tasks.")
        with tempfile.TemporaryDirectory(prefix=".replay-", dir=recovery) as name:
            work = Path(name)
            token = work / "forge.token"
            atomic_write(token, forge.token)
            git = git_factory(work, _git_settings(forge, forge.config.target_repository, 1024), token)
            for ref, checkpoint in missing.items():
                git.run("-c", "protocol.file.allow=always", "fetch", "--no-tags", str(recovery / "target.bundle"),
                        "refs/heads/" + ref, bounded=True)
                if git.run("rev-parse", "FETCH_HEAD").decode().strip() != checkpoint:
                    raise ValueError("A bundled checkpoint does not match its task record.")
            for ref, checkpoint in missing.items():
                git.push(checkpoint, "refs/heads/" + ref)
    store.ensure()
    restored = []
    for state in states:
        number, complete = state["issue"], state.get("status") == "complete"
        record = {**state, "status": "complete" if complete else "paused", "desired": "complete" if complete else "paused",
                  "runner": "", "lease_until": 0, "updated_at": clock(),
                  "reason": "Restored from a backup; a maintainer must review and resume."}
        forge.put_content(store.path(number), json.dumps(record).encode(), current[number]["sha"],
                          message="Restore BananaVibe task state")
        restored.append(number)
    return {"outcome": "state restored", "issues": restored, "tasks_started": 0}


def _load(path):
    """Like the main command: inside a workflow, its own repository is the control repository."""
    return Config.load(path, control_repository=workflow_repository())


def workflow_repository():
    return os.environ.get("GITHUB_REPOSITORY") or os.environ.get("FORGEJO_REPOSITORY") or None


def main(argv):
    parser = argparse.ArgumentParser(prog="bananavibe", description="Encrypted backups of BananaVibe data.")
    command = add_commands(parser.add_subparsers(dest="command", required=True), agent=True)
    command.add_argument("--root", type=Path, default=Path(os.environ.get(
        "BANANAVIBE_BACKUP_ROOT", "~/.local/state/bananavibe-backups")).expanduser())
    command.add_argument("--config", default=".bananavibe.toml")
    args = parser.parse_args(["backups", *argv])
    store = Store(args.root, "BananaVibe")
    try:
        def create():
            forge = Forge(_load(args.config), os.environ.get("BANANAVIBE_TOKEN", ""))
            return export_package(forge, args.config, store.root / f"bananavibe-{time.time_ns()}.tar.gz",
                                  maximum=store.settings()["max_mib"])
        result = handle(args, store, create_package=create,
                        restore_package=lambda package, options: restore_package(package, options.output))
        print(json.dumps(result, indent=2))
        return 0
    except (ConfigurationError, ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"bananavibe backups: {error}", file=sys.stderr)
        return 1


def replay_main(argv):
    parser = argparse.ArgumentParser(prog="bananavibe restore-state",
                                     description="Apply reviewed task state with the workflow disabled.")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--config", default=".bananavibe.toml")
    parser.add_argument("--confirm-repository", required=True)
    args = parser.parse_args(argv)
    try:
        forge = Forge(_load(args.config), os.environ.get("BANANAVIBE_TOKEN", ""))
        print(json.dumps(restore_state(forge, args.directory, args.confirm_repository), indent=2))
        return 0
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"bananavibe restore-state: {error}", file=sys.stderr)
        return 1
