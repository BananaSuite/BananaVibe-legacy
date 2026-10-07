"""Untrusted containers must not inherit IPv6-enabled daemon defaults."""

import json
from pathlib import Path
import subprocess

import pytest

from bananavibe import engine as engine_module
from bananavibe.engine import Engine, EngineError
from fakes import make_config


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    calls = []
    network = {"Driver": "bridge", "Internal": True, "EnableIPv6": False,
               "Options": {"com.docker.network.bridge.inhibit_ipv4": "true"}}
    response = {"body": json.dumps([network]), "returncode": 0}

    def fake_docker(*args, **_):
        calls.append(args)
        if args[:2] == ("network", "inspect"):
            return subprocess.CompletedProcess(args, response["returncode"], response["body"], "")
        if args[0] == "inspect":
            body = [{"NetworkSettings": {"Ports": {"8082/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18082"}]}}}]
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(engine_module, "docker", fake_docker)
    config = make_config(checks=())
    engine = Engine(config, config.models["coding"], tmp_path, lambda *_: None, image="fixture")
    monkeypatch.setattr(engine, "api", lambda *_args, **_kwargs: {"healthy": True})
    yield engine, calls, network, response
    engine.close()


def test_agent_and_checker_disable_ipv6_but_gateway_keeps_provider_egress(sandbox):
    engine, calls, _, _ = sandbox
    engine.start()
    create = next(call for call in calls if call[:2] == ("network", "create"))
    assert "--ipv6=false" in create and "--internal" in create
    inspect = ("network", "inspect", engine.internal)
    assert calls.index(inspect) < next(i for i, call in enumerate(calls) if call[0] == "create")
    engine.check(engine.workspace)
    commands = [call for call in calls if call[0] in {"create", "run"}]
    for call in commands:
        assert "--read-only" in call and "--cap-drop=ALL" in call
        assert "--security-opt=no-new-privileges:true" in call
        disabled = call[call.index("--name") + 1] != engine.gateway
        assert ("net.ipv6.conf.all.disable_ipv6=1" in call) is disabled
        assert ("net.ipv6.conf.default.disable_ipv6=1" in call) is disabled


@pytest.mark.parametrize("change", [
    {"EnableIPv6": True}, {"EnableIPv6": "false"}, {"EnableIPv6": None},
    {"Internal": False}, {"Driver": "overlay"}, {"Options": {}},
    {"Options": {"com.docker.network.bridge.inhibit_ipv4": "false"}},
])
def test_unsafe_inspected_bridge_refused_before_any_container_launch(sandbox, change):
    engine, calls, network, response = sandbox
    network.update(change)
    response["body"] = json.dumps([network])
    private = Path(engine.root)
    with pytest.raises(EngineError, match="unsafe or unreadable"):
        with engine:
            pytest.fail("Unsafe network must not start the engine")
    assert not any(call[0] in {"create", "run", "start"} for call in calls)
    assert not private.exists()


@pytest.mark.parametrize("body,returncode", [("not json", 0), ("[]", 0), ("[null]", 0), ("", 1)])
def test_unreadable_or_failed_inspection_refused(sandbox, body, returncode):
    engine, calls, _, response = sandbox
    response.update(body=body, returncode=returncode)
    with pytest.raises(EngineError, match="unsafe or unreadable"):
        engine.start()
    assert not any(call[0] in {"create", "run", "start"} for call in calls)


def test_checker_rechecks_reused_bridge_before_launch(sandbox):
    engine, calls, network, response = sandbox
    engine.start()
    network["EnableIPv6"] = True
    response["body"] = json.dumps([network])
    with pytest.raises(EngineError, match="unsafe or unreadable"):
        engine.check(engine.workspace)
    assert not any(call[0] == "run" and engine.agent + "-check" in call for call in calls)
