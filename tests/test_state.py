import json
from dataclasses import replace

import pytest

from bananavibe.forge import ForgeError, Unavailable
from bananavibe.state import LeaseLost, StateError, new_task, normalize, task_id
from fakes import git


def test_state_branch_is_an_orphan_without_workflows(forge, store):
    store.ensure()
    head = forge.branch_sha(forge.config.control_repository, "bananavibe-state")
    assert head
    assert git("--git-dir", str(forge.control), "rev-list", "--count", head) == "1"
    files = git("--git-dir", str(forge.control), "ls-tree", "-r", "--name-only", head).split()
    assert files == ["README.md"]
    store.ensure()  # idempotent
    assert forge.branch_sha(forge.config.control_repository, "bananavibe-state") == head


def test_state_branch_never_uses_or_rewrites_the_default_branch(forge, store):
    # base_branch may be a release branch, so configuration parsing alone
    # cannot know that state_branch is the control repository's default.
    store.config = replace(store.config, state_branch="main", base_branch="release")
    original = forge.branch_sha(forge.config.control_repository, "main")
    with pytest.raises(StateError, match="default branch"):
        store.ensure()
    with pytest.raises(StateError, match="default branch"):
        store.make_orphan()
    with pytest.raises(StateError, match="default branch"):
        store.mutate(3, lambda _: new_task(store.config, 3))
    assert forge.branch_sha(forge.config.control_repository, "main") == original


@pytest.mark.parametrize("metadata", [None, [], {}, {"default_branch": None}, {"default_branch": ""}])
def test_state_writes_fail_closed_without_default_branch_metadata(forge, store, metadata):
    forge.repo = lambda _: metadata
    with pytest.raises(StateError, match="could not be determined"):
        store.ensure()
    with pytest.raises(StateError, match="could not be determined"):
        store.make_orphan()
    with pytest.raises(StateError, match="could not be determined"):
        store.mutate(3, lambda _: new_task(store.config, 3))


def test_compare_and_swap_mutation_and_claim(forge, store):
    store.ensure()
    store.mutate(3, lambda _: new_task(forge.config, 3))
    assert store.claim(3, "runner-a")["runner"] == "runner-a"
    assert store.claim(3, "runner-b") is None
    with pytest.raises(LeaseLost):
        store.owned(3, "runner-b", iterations=5)
    store.owned(3, "runner-a", iterations=1)
    store.release(3, "runner-a", status="paused", desired="paused")
    state, _ = store.read(3)
    assert state["runner"] == "" and state["status"] == "paused" and state["iterations"] == 1
    assert store.claim(3, "runner-b") is None  # desired is no longer running


def test_lost_write_race_retries_against_the_latest_record(forge, store):
    store.ensure()
    store.mutate(3, lambda _: new_task(forge.config, 3))
    raced = {"done": False}

    def change(state):
        if not raced["done"]:
            raced["done"] = True
            store.mutate(3, lambda other: {**other, "model": "other"})  # a concurrent writer wins first
        state["iterations"] += 1
        return state

    result = store.mutate(3, change)
    assert result["model"] == "other" and result["iterations"] == 1


def test_expired_lease_can_be_claimed(forge, store):
    store.ensure()
    store.mutate(3, lambda _: {**new_task(forge.config, 3), "runner": "dead", "lease_until": 1})
    assert store.claim(3, "fresh")["runner"] == "fresh"


def test_listing_reads_every_record_through_git(forge, store):
    store.ensure()
    for number in (3, 4, 5):
        forge.issues[number] = {"number": number, "title": "t", "body": "b"}
        store.mutate(number, lambda _, n=number: new_task(forge.config, n))
    assert sorted(state["issue"] for state in store.all()) == [3, 4, 5]


def test_legacy_record_without_new_fields_is_normalized(forge, store):
    store.ensure()
    legacy = new_task(forge.config, 3)
    for key in ("approved", "status_comment", "reason", "phase"):
        legacy.pop(key)
    forge.put_content(store.path(3), json.dumps(legacy).encode())
    state, _ = store.read(3)
    assert state["approved"] == "" and state["reason"] == ""
    assert normalize(None) is None


def test_record_for_another_issue_is_rejected(forge, store):
    store.ensure()
    forge.put_content(store.path(3), json.dumps(new_task(forge.config, 4)).encode())
    with pytest.raises(StateError):
        store.read(3)


@pytest.mark.parametrize("changes", [
    {"schema": True}, {"issue": True}, {"task_id": "another-task"}, {"model": []}, {"runner": {}},
    {"request": "3"}, {"generation": 0}, {"iterations": -1}, {"status_comment": True},
    {"lease_until": "later"}, {"lease_until": float("nan")}, {"lease_until": float("inf")},
    {"lease_until": 10**1000}, {"guidance": "bad"}, {"guidance": [{"author": "m", "text": None}]},
    {"guidance": [{"author": "m", "text": "x" * 200001}]}, {"events": [3]},
])
def test_malformed_record_fields_are_rejected_before_commands_use_them(forge, store, changes):
    store.ensure()
    forge.put_content(store.path(3), json.dumps({**new_task(forge.config, 3), **changes}).encode())
    with pytest.raises(StateError):
        store.read(3)
    with pytest.raises(StateError):
        store.all()


def test_corrupt_json_is_never_silently_omitted_from_a_backup(forge, store):
    store.ensure()
    forge.put_content(store.path(3), b"{broken")
    with pytest.raises(StateError, match="unreadable"):
        store.read(3)
    with pytest.raises(StateError, match="unreadable"):
        store.all()


def test_record_size_is_enforced_even_when_api_metadata_understates_it(forge, store):
    from bananavibe.state import MAX_BYTES
    import base64
    forge.content = lambda _: {"type": "file", "size": 1, "sha": "revision",
                               "content": base64.b64encode(b" " * (MAX_BYTES + 1)).decode()}
    with pytest.raises(StateError, match="too large"):
        store.read(3)


@pytest.mark.parametrize("content", ["%not-base64", "bm90LWpzb24=", [], 1])
def test_unreadable_contents_api_records_have_a_safe_error(forge, store, content):
    forge.content = lambda _: {"type": "file", "size": 1, "sha": "revision", "content": content}
    with pytest.raises(StateError):
        store.read(3)


def test_task_id_matches_2x_so_existing_records_and_branches_are_found():
    # Value computed with the preview release; changing it would orphan every existing task.
    assert task_id("team/prompts", 3) == "05a87b2318842377"


def test_2x_state_branch_is_converted_to_an_orphan_keeping_records(forge, store):
    main = forge.branch_sha("team/prompts", "main")
    forge.create_branch("team/prompts", "bananavibe-state", main)  # how the preview release created it
    store.mutate(3, lambda _: new_task(forge.config, 3))
    assert store.make_orphan() is True
    head = forge.branch_sha("team/prompts", "bananavibe-state")
    assert git("--git-dir", str(forge.control), "rev-list", "--count", head) == "1"
    files = git("--git-dir", str(forge.control), "ls-tree", "-r", "--name-only", head).split()
    assert files == [".bananavibe-state/tasks/05a87b2318842377.json", "README.md"]
    assert store.read(3)[0]["issue"] == 3
    assert store.make_orphan() is False


def test_conversion_waits_for_running_tasks(forge, store):
    main = forge.branch_sha("team/prompts", "main")
    forge.create_branch("team/prompts", "bananavibe-state", main)
    store.mutate(3, lambda _: new_task(forge.config, 3))
    store.claim(3, "runner")
    with pytest.raises(StateError, match="running"):
        store.make_orphan()


@pytest.mark.parametrize("error", [ForgeError(502, "save task state"), Unavailable("forge down")])
def test_transient_write_failures_are_retried(forge, store, error):
    store.ensure()
    store.mutate(3, lambda _: new_task(forge.config, 3))
    put, failures = forge.put_content, [error]

    def flaky(*args, **kwargs):
        if failures:
            raise failures.pop()
        return put(*args, **kwargs)
    forge.put_content = flaky
    store.mutate(3, lambda state: {**state, "iterations": 7})
    assert store.read(3)[0]["iterations"] == 7 and not failures


def test_permanent_write_failures_are_not_retried(forge, store):
    store.ensure()
    store.mutate(3, lambda _: new_task(forge.config, 3))
    calls = []

    def refused(*args, **kwargs):
        calls.append(args)
        raise ForgeError(403, "save task state")
    forge.put_content = refused
    with pytest.raises(ForgeError):
        store.mutate(3, lambda state: {**state, "iterations": 7})
    assert len(calls) == 1
