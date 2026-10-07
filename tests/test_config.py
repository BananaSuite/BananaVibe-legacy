from pathlib import Path

import pytest

from bananavibe.config import DEFAULT_IGNORE, Config, ConfigurationError, host_allowed

ROOT = Path(__file__).resolve().parents[1]
MINIMAL = {"control_repository": "team/prompts", "checks": [["make", "test"]],
           "models": {"coding": {"provider": "openai-compatible", "endpoint": "https://m.example/v1", "model": "m"}}}


def parse(**changes):
    return Config.parse({**MINIMAL, **changes})


@pytest.mark.parametrize("name", ["bananawiki.toml", "bananachat.toml"])
def test_examples_load(name):
    config = Config.load(ROOT / "examples" / name)
    assert config.target_repository.startswith("BananaSuite/") and not config.warnings


def test_preview_configuration_loads_unchanged(tmp_path):
    legacy = tmp_path / "legacy.toml"
    legacy.write_text("""
forge = "forgejo"
server_url = "https://forge.example.org"
control_repository = "maintainers/prompts"
target_repository = "suite/BananaWiki"
base_branch = "main"
state_branch = "bananavibe-state"
default_model = "coding"
open_issues = true
image = "bananavibe-sandbox:2.0.0"
prepare = [["python3", "-m", "venv", "/state/venv"]]
checks = [["/state/venv/bin/python", "-m", "pytest", "-q"]]
forbidden_paths = [".github/workflows/", ".bananavibe.toml"]
[models.coding]
provider = "openai"
endpoint = "https://api.openai.com/v1"
model = "some-model"
api_key_env = "OPENAI_API_KEY"
token_param = "max_completion_tokens"
[limits]
max_minutes = 45
max_iterations = 12
max_calls = 1000
poll_seconds = 15
progress_seconds = 120
lease_seconds = 300
memory_mb = 4096
cpus = 2
max_workspace_mb = 512
""")
    config = Config.load(legacy)
    assert config.api_url == "https://forge.example.org/api/v1"
    assert config.image == ""  # the old default tag is replaced by the matching build
    assert config.forbidden_paths == (".github/workflows/", ".bananavibe.toml")
    assert config.limits.max_minutes == 45 and config.egress == ("*",)
    assert any("preview release's default" in warning for warning in config.warnings)


def test_workflow_repository_overrides_the_file():
    assert Config.parse(MINIMAL, control_repository="real/repo").control_repository == "real/repo"
    assert parse().target_repository == "team/prompts"


def test_unknown_keys_warn_instead_of_failing():
    config = parse(limit={"max_minutes": 3}, extra=True)
    assert any("'limit'" in warning for warning in config.warnings)
    assert any("'extra'" in warning for warning in config.warnings)


@pytest.mark.parametrize("changes", [
    {"forge": "gitlab"},
    {"checks": []},
    {"checks": [["ok"], []]},
    {"server_url": "http://forge.example"},
    {"server_url": "https://user:pass@forge.example"},
    {"server_url": "https://forge.example:wrong"},
    {"server_url": "https://forge.example:65536"},
    {"server_url": "https://forge.example:0"},
    {"server_url": "https://forge.example:"},
    {"server_url": "https://[broken"},
    {"server_url": "https://forge.example\x00/path"},
    {"server_url": "https://forge.example\\other.example"},
    {"default_model": "missing"},
    {"state_branch": "main"},
    {"state_branch": "bananavibe/state"},
    {"base_branch": "../main"},
    {"limits": {"max_minutes": 0}},
    {"limits": {"cpus": "2"}},
    {"network": {"allow": ["10.0.0.1/8"]}},
    {"network": {"deny": ["x"]}},
    {"forbidden_paths": ["/etc"]},
    {"models": {"bad alias": {"provider": "openai", "endpoint": "https://x", "model": "m"}}},
    {"models": {"m": {"provider": "openai", "endpoint": "https://x", "model": "m", "api_key_env": "BANANAVIBE_TOKEN"}}},
    {"models": {"m": {"provider": "cohere", "endpoint": "https://x", "model": "m"}}},
    {"image": "bad image"},
])
def test_invalid_values_are_rejected(changes):
    with pytest.raises(ConfigurationError):
        parse(**changes)


def test_http_requires_explicit_opt_in():
    assert parse(allow_http=True, server_url="http://localhost:3000").server_url == "http://localhost:3000"


def test_ignore_extends_the_defaults():
    config = parse(ignore=["dist/"])
    assert config.ignore == (*DEFAULT_IGNORE, "dist/")


def test_egress_patterns():
    config = parse(network={"allow": ["PyPI.org", "*.pythonhosted.org"]})
    assert config.egress == ("pypi.org", "*.pythonhosted.org")
    assert host_allowed("files.pythonhosted.org", config.egress)
    assert host_allowed("pypi.org.", config.egress)
    assert not host_allowed("pythonhosted.org.evil.example", config.egress)
    assert not host_allowed("evilpypi.org", config.egress)
    assert host_allowed("anything.example", ("*",))


def test_missing_model_secret_is_reported(monkeypatch):
    model = parse(models={"m": {"provider": "openai", "endpoint": "https://x", "model": "m",
                                "api_key_env": "MODEL_KEY"}}).models["m"]
    monkeypatch.delenv("MODEL_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="MODEL_KEY"):
        model.api_key()
    monkeypatch.setenv("MODEL_KEY", "k")
    assert model.api_key() == "k"


def test_poll_interval_must_fit_the_lease():
    assert parse(limits={"poll_seconds": 20, "lease_seconds": 60}).limits.poll_seconds == 20
    with pytest.raises(ConfigurationError, match="third of limits.lease_seconds"):
        parse(limits={"poll_seconds": 60, "lease_seconds": 60})


def test_gitmodules_is_protected_by_default():
    assert ".gitmodules" in parse().forbidden_paths
