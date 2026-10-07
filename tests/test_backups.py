"""Export, review-restore and state replay of BananaVibe data (no network)."""

import io
import json
from pathlib import Path
import tarfile
import time

import pytest

from banana_backup.git import Git
from bananavibe import backups
from bananavibe.state import StateStore, new_task
from fakes import git


@pytest.fixture
def fixture(forge, store, tmp_path):
    store.ensure()
    base = forge.branch_sha("team/app", "main")
    branch = f"bananavibe/{new_task(forge.config, 3)['task_id']}-1"
    forge.create_branch("team/app", branch, base)
    state = {**new_task(forge.config, 3), "branch": branch, "base_sha": base, "checkpoint": base,
             "status": "running", "runner": "old-runner", "lease_until": time.time() + 300}
    store.mutate(3, lambda _: state)
    forge.comment(3, "private maintainer guidance")
    configuration = tmp_path / "configuration.toml"
    configuration.write_text('control_repository = "team/prompts"\n')

    def factory(work, settings, token_file):
        return Git(work, settings, token_file, allow_local=True)
    return forge, store, configuration, state, factory


def export(fixture, tmp_path):
    forge, _, configuration, _, factory = fixture
    return backups.export_package(forge, configuration, tmp_path / "package.tar.gz", git_factory=factory)


def test_export_and_review_restore(fixture, tmp_path):
    forge, store, configuration, state, _ = fixture
    result = backups.restore_package(export(fixture, tmp_path), tmp_path / "recovery")
    root = Path(result["directory"])
    assert (root / "configuration.toml").read_text() == configuration.read_text()
    assert json.loads((root / "tasks.json").read_text())[0]["checkpoint"] == state["checkpoint"]
    assert "private maintainer guidance" in (root / "issues.json").read_text()
    assert (root / "target.bundle").stat().st_size and (root / "control.bundle").stat().st_size
    assert set(json.loads((root / "metadata.json").read_text())["files"]) == {
        "configuration.toml", "tasks.json", "issues.json", "control.bundle", "target.bundle"}
    assert store.read(3)[0]["runner"] == "old-runner"  # export changes nothing


def test_bundle_pins_the_recorded_checkpoint(fixture, tmp_path):
    forge, _, _, state, _ = fixture
    work = tmp_path / "newer"
    git("clone", "-q", "-b", state["branch"], str(forge.target), str(work))
    (work / "app.py").write_text("answer = 7\n")
    git("commit", "-qam", "unrecorded", cwd=work)
    git("push", "-q", "origin", state["branch"], cwd=work)
    root = Path(backups.restore_package(export(fixture, tmp_path), tmp_path / "recovery")["directory"])
    heads = git("bundle", "list-heads", str(root / "target.bundle"))
    assert f"{state['checkpoint']} refs/heads/{state['branch']}" in heads


def test_restore_state_waits_for_leases_then_restores_paused(fixture, tmp_path):
    forge, store, _, state, factory = fixture
    backups.restore_package(export(fixture, tmp_path), tmp_path / "recovery")
    with pytest.raises(ValueError, match="still running"):
        backups.restore_state(forge, tmp_path / "recovery", "team/prompts", git_factory=factory)
    store.mutate(3, lambda current: {**current, "lease_until": 0})
    git("--git-dir", str(forge.target), "update-ref", "-d", f"refs/heads/{state['branch']}")
    result = backups.restore_state(forge, tmp_path / "recovery", "team/prompts", git_factory=factory)
    restored = store.read(3)[0]
    assert result["tasks_started"] == 0
    assert restored["status"] == "paused" and restored["desired"] == "paused" and not restored["runner"]
    assert forge.branch_sha("team/app", state["branch"]) == state["checkpoint"]
    assert list((tmp_path / "recovery").glob("before-restore-*.json"))


def test_restore_state_never_overwrites_newer_work(fixture, tmp_path):
    forge, store, _, state, factory = fixture
    backups.restore_package(export(fixture, tmp_path), tmp_path / "recovery")
    store.mutate(3, lambda current: {**current, "lease_until": 0})
    with pytest.raises(ValueError, match="original issue repository"):
        backups.restore_state(forge, tmp_path / "recovery", "other/repo", git_factory=factory)
    work = tmp_path / "newer"
    git("clone", "-q", "-b", state["branch"], str(forge.target), str(work))
    (work / "app.py").write_text("answer = 9\n")
    git("commit", "-qam", "newer", cwd=work)
    git("push", "-q", "origin", state["branch"], cwd=work)
    with pytest.raises(ValueError, match="newer or different work"):
        backups.restore_state(forge, tmp_path / "recovery", "team/prompts", git_factory=factory)


def test_tampered_review_copy_is_not_replayed(fixture, tmp_path):
    forge, store, _, _, factory = fixture
    backups.restore_package(export(fixture, tmp_path), tmp_path / "recovery")
    tasks = tmp_path / "recovery" / "tasks.json"
    tasks.write_text(tasks.read_text().replace("paused", "running"))
    tasks.write_text(tasks.read_text() + " ")
    with pytest.raises(ValueError, match="modified"):
        backups.restore_state(forge, tmp_path / "recovery", "team/prompts", git_factory=factory)


@pytest.mark.parametrize("name,kind", [("../outside", tarfile.REGTYPE), ("tasks.json", tarfile.SYMTYPE),
                                       (".git/hooks/post-checkout", tarfile.REGTYPE)])
def test_unsafe_archives_are_refused(tmp_path, name, kind):
    package = tmp_path / "unsafe.tar.gz"
    with tarfile.open(package, "w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.type, member.linkname = kind, "/tmp/outside"
        member.size = 1 if kind == tarfile.REGTYPE else 0
        archive.addfile(member, io.BytesIO(b"x") if member.size else None)
    with pytest.raises(ValueError, match="Unsafe"):
        backups.restore_package(package, tmp_path / "recovered")
    assert not (tmp_path / "recovered").exists()


def test_invalid_records_block_the_export(fixture, tmp_path):
    forge, store, _, _, _ = fixture
    store.mutate(3, lambda current: {**current, "branch": "main"})
    with pytest.raises(ValueError, match="invalid"):
        export(fixture, tmp_path)


def test_state_store_listing_is_used(fixture):
    forge = fixture[0]
    assert [state["issue"] for state in StateStore(forge).all()] == [3]


def test_backups_use_the_workflow_repository(tmp_path, monkeypatch):
    configuration = tmp_path / "config.toml"
    configuration.write_text('checks = [["make", "test"]]\n'
                             '[models.coding]\nprovider = "openai"\nendpoint = "https://m.example/v1"\nmodel = "m"\n')
    monkeypatch.delenv("FORGEJO_REPOSITORY", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "team/prompts")
    assert backups._load(configuration).control_repository == "team/prompts"


@pytest.mark.parametrize("metadata", [[], None, {}, {"schema": 1, "product": "BananaVibe", "files": []},
                                      {"schema": True, "product": "BananaVibe", "files": {}}])
def test_malformed_manifests_fail_cleanly_without_publishing_review_files(tmp_path, metadata):
    package = tmp_path / "malformed.tar.gz"
    raw = json.dumps(metadata).encode()
    with tarfile.open(package, "w:gz") as archive:
        member = tarfile.TarInfo("metadata.json")
        member.size = len(raw)
        archive.addfile(member, io.BytesIO(raw))
    with pytest.raises(ValueError, match="manifest"):
        backups.restore_package(package, tmp_path / "review")
    assert not (tmp_path / "review").exists()


def test_interrupted_review_copy_removes_only_its_new_directory(fixture, tmp_path, monkeypatch):
    package = export(fixture, tmp_path)

    def failed_copy(*_, **__):
        raise OSError("disk full")
    monkeypatch.setattr(backups.shutil, "copyfile", failed_copy)
    with pytest.raises(OSError, match="disk full"):
        backups.restore_package(package, tmp_path / "review")
    assert not (tmp_path / "review").exists()
