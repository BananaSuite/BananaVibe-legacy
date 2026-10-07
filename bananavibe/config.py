"""Trusted configuration, read from the control repository's default branch.

Every key accepted by the BananaVibe preview release keeps its meaning, so an
existing `.bananavibe.toml` loads unchanged. Unknown keys produce warnings
instead of errors, so an upgrade never stops a working installation; run
`bananavibe check-config` to see them.
"""

from dataclasses import dataclass, field
import fnmatch
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    pass


REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
FORGE_SECRETS = {"GITHUB_TOKEN", "FORGEJO_TOKEN", "GITEA_TOKEN", "BANANAVIBE_TOKEN"}
LEGACY_IMAGES = {"bananavibe-sandbox:2.0.0"}  # the preview release's default image

# The OpenCode provider package each adapter uses inside the sandbox.
PACKAGES = {"openai": "@ai-sdk/openai", "openai-compatible": "@ai-sdk/openai-compatible",
            "anthropic": "@ai-sdk/anthropic", "google": "@ai-sdk/google", "azure": "@ai-sdk/azure"}

DEFAULT_FORBIDDEN = (".github/workflows/", ".github/actions/", ".forgejo/workflows/", ".forgejo/actions/",
                     ".gitea/workflows/", ".gitea/actions/", ".bananavibe.toml", ".gitmodules")
# Untracked build and test caches that must never be committed as part of a
# draft. Tracked files matching these patterns are still committed normally.
DEFAULT_IGNORE = ("__pycache__/", "*.py[cod]", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
                  ".tox/", ".nox/", ".coverage", ".coverage.*", "htmlcov/", "node_modules/",
                  ".npm/", ".cache/", ".venv/", "*.egg-info/", ".DS_Store")

LIMITS = {  # name: (default, minimum, maximum)
    "max_minutes": (45, 1, 300),
    "max_iterations": (12, 1, 100),
    "max_calls": (1000, 1, 10000),
    "poll_seconds": (15, 1, 60),
    "progress_seconds": (120, 30, 1800),
    "lease_seconds": (300, 60, 900),
    "memory_mb": (4096, 512, 32768),
    "cpus": (2, 1, 32),
    "max_workspace_mb": (512, 16, 8192),
    "command_minutes": (20, 1, 240),
}
TOP_LEVEL = {"forge", "server_url", "api_url", "control_repository", "target_repository", "base_branch",
             "state_branch", "default_model", "open_issues", "allow_http", "prepare", "checks", "image",
             "forbidden_paths", "ignore", "models", "limits", "network"}
MODEL_KEYS = {"provider", "model", "endpoint", "api_key_env", "token_param"}


def repository(value, name="repository"):
    if not isinstance(value, str) or not REPOSITORY.fullmatch(value) or any(
            part in {".", ".."} for part in value.split("/")):
        raise ConfigurationError(f"{name} must be an owner/repository name.")
    return value


def branch(value, name="branch"):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,200}", value)
            or ".." in value or "//" in value or value.endswith(("/", ".lock", "."))
            or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))):
        raise ConfigurationError(f"{name} is not a valid branch name.")
    return value


def endpoint(value, *, allow_http=False, name="endpoint"):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ConfigurationError(f"{name} must be a URL.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ConfigurationError(f"{name} must be a URL with a valid host and port.") from None
    if parsed.scheme not in ({"https", "http"} if allow_http else {"https"}) or not parsed.hostname:
        raise ConfigurationError(f"{name} must use HTTPS (plain HTTP needs allow_http = true).")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError(f"{name} must not contain credentials, a query or a fragment.")
    if port == 0 or "\\" in parsed.netloc or parsed.netloc.endswith(":"):
        raise ConfigurationError(f"{name} must be a URL with a valid host and port.")
    return value.rstrip("/")


def _commands(data, name, required=False):
    value = data.get(name, [])
    if not isinstance(value, list) or (required and not value):
        raise ConfigurationError(f"{name} must be a list of argument lists, such as [[\"make\", \"test\"]].")
    for command in value:
        if (not isinstance(command, list) or not command
                or not all(isinstance(arg, str) and arg and "\0" not in arg for arg in command)):
            raise ConfigurationError(f"Each {name} entry must be a nonempty list of strings.")
    return tuple(tuple(command) for command in value)


def _paths(data, name, default):
    value = data.get(name, default)
    if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, str) and item and not item.startswith("/") and ".." not in item.split("/")
            and "\n" not in item for item in value):
        raise ConfigurationError(f"{name} must list relative repository paths.")
    return tuple(value)


def _host_pattern(value):
    if value == "*":
        return value
    if not isinstance(value, str) or not re.fullmatch(r"(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", value):
        raise ConfigurationError("network.allow entries must be host names such as pypi.org or *.pythonhosted.org.")
    return value


def host_allowed(host, patterns):
    host = host.lower().rstrip(".")
    return any(pattern == "*" or host == pattern or (pattern.startswith("*.") and fnmatch.fnmatchcase(host, pattern))
               for pattern in patterns)


@dataclass(frozen=True)
class Model:
    alias: str
    provider: str
    model: str
    endpoint: str
    key_env: str = ""
    token_param: str = ""

    @property
    def package(self):
        return PACKAGES[self.provider]

    def api_key(self):
        if not self.key_env:
            return ""
        key = os.environ.get(self.key_env, "")
        if not key:
            raise ConfigurationError(f"The model credential {self.key_env} is not set in the workflow environment.")
        return key


@dataclass(frozen=True)
class Limits:
    max_minutes: int = 45
    max_iterations: int = 12
    max_calls: int = 1000
    poll_seconds: int = 15
    progress_seconds: int = 120
    lease_seconds: int = 300
    memory_mb: int = 4096
    cpus: int = 2
    max_workspace_mb: int = 512
    command_minutes: int = 20


@dataclass(frozen=True)
class Config:
    forge: str
    server_url: str
    api_url: str
    control_repository: str
    target_repository: str
    base_branch: str
    models: dict
    default_model: str
    checks: tuple
    prepare: tuple = ()
    state_branch: str = "bananavibe-state"
    image: str = ""
    forbidden_paths: tuple = DEFAULT_FORBIDDEN
    ignore: tuple = DEFAULT_IGNORE
    egress: tuple = ("*",)
    open_issues: bool = True
    allow_http: bool = False
    limits: Limits = field(default_factory=Limits)
    warnings: tuple = ()

    @property
    def same_repository(self):
        return self.control_repository.casefold() == self.target_repository.casefold()

    @classmethod
    def load(cls, path, *, control_repository=None):
        try:
            with Path(path).open("rb") as source:
                data = tomllib.load(source)
        except tomllib.TOMLDecodeError as error:
            raise ConfigurationError(f"{path} is not valid TOML: {error}") from None
        return cls.parse(data, control_repository=control_repository)

    @classmethod
    def parse(cls, data, *, control_repository=None):
        warnings = [f"Unknown configuration key '{key}' is ignored." for key in sorted(set(data) - TOP_LEVEL)]
        forge = data.get("forge", "github")
        if forge not in {"github", "forgejo"}:
            raise ConfigurationError("forge must be \"github\" or \"forgejo\".")
        allow_http, open_issues = data.get("allow_http", False), data.get("open_issues", True)
        if type(allow_http) is not bool or type(open_issues) is not bool:
            raise ConfigurationError("allow_http and open_issues must be true or false.")
        server = endpoint(data.get("server_url", "https://github.com"), allow_http=allow_http, name="server_url")
        if server == "https://github.com":
            default_api = "https://api.github.com"
        else:
            default_api = server + ("/api/v1" if forge == "forgejo" else "/api/v3")
        api = endpoint(data.get("api_url", default_api), allow_http=allow_http, name="api_url")

        # The workflow's own repository wins: a configuration copied from
        # another installation cannot redirect commands to a different place.
        control = repository(control_repository or data.get("control_repository", ""), "control_repository")
        target = repository(data.get("target_repository", control), "target_repository")

        models = {}
        raw_models = data.get("models", {})
        if not isinstance(raw_models, dict):
            raise ConfigurationError("models must contain [models.ALIAS] tables.")
        for alias, item in raw_models.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", alias) or not isinstance(item, dict):
                raise ConfigurationError(f"Invalid model alias '{alias}'.")
            warnings += [f"Unknown key '{key}' in [models.{alias}] is ignored." for key in sorted(set(item) - MODEL_KEYS)]
            if item.get("provider") not in PACKAGES:
                raise ConfigurationError(f"[models.{alias}] provider must be one of: {', '.join(PACKAGES)}.")
            model_id = item.get("model", "")
            if not isinstance(model_id, str) or not re.fullmatch(r"[A-Za-z0-9_./:@-]{1,200}", model_id):
                raise ConfigurationError(f"[models.{alias}] model is not a valid provider model ID.")
            key_env = item.get("api_key_env", "")
            if not isinstance(key_env, str) or (key_env and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", key_env)):
                raise ConfigurationError(f"[models.{alias}] api_key_env must name an environment variable.")
            if key_env in FORGE_SECRETS:
                raise ConfigurationError("A forge token cannot be used as a model credential.")
            token_param = item.get("token_param", "")
            if token_param not in {"", "max_completion_tokens"}:
                raise ConfigurationError(f"[models.{alias}] token_param may only be \"max_completion_tokens\".")
            models[alias] = Model(alias, item["provider"], model_id,
                                  endpoint(item.get("endpoint"), allow_http=allow_http, name=f"[models.{alias}] endpoint"),
                                  key_env, token_param)
        default_model = data.get("default_model", next(iter(models), ""))
        if not models or default_model not in models:
            raise ConfigurationError("Configure at least one [models.ALIAS] table and a default_model naming one.")

        raw_limits = data.get("limits", {})
        if not isinstance(raw_limits, dict):
            raise ConfigurationError("limits must be a table.")
        warnings += [f"Unknown key '{key}' in [limits] is ignored." for key in sorted(set(raw_limits) - set(LIMITS))]
        limits = {}
        for name, (default, low, high) in LIMITS.items():
            value = raw_limits.get(name, default)
            if type(value) is not int or not low <= value <= high:
                raise ConfigurationError(f"limits.{name} must be a whole number from {low} to {high}.")
            limits[name] = value
        if limits["poll_seconds"] * 3 > limits["lease_seconds"]:
            # The heartbeat renews the lease once less than half is left, checking every poll.
            raise ConfigurationError("limits.poll_seconds must be at most a third of limits.lease_seconds, "
                                     "or the lease can expire between two heartbeats.")

        network = data.get("network", {})
        if not isinstance(network, dict) or set(network) - {"allow"}:
            raise ConfigurationError("[network] accepts only allow = [host names].")
        egress = network.get("allow", ["*"])
        if not isinstance(egress, list) or not egress:
            raise ConfigurationError("network.allow must list at least one host name, or \"*\".")
        egress = tuple(_host_pattern(item.lower() if isinstance(item, str) else item) for item in egress)

        state = branch(data.get("state_branch", "bananavibe-state"), "state_branch")
        base = branch(data.get("base_branch", "main"), "base_branch")
        if state == base or state.startswith("bananavibe/"):
            raise ConfigurationError("state_branch must differ from base_branch and from bananavibe/ task branches.")

        image = data.get("image", "")
        if not isinstance(image, str) or (image and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,250}", image)):
            raise ConfigurationError("image must be a container image reference.")
        if image in LEGACY_IMAGES:
            warnings.append(f"image = \"{image}\" was the preview release's default; BananaVibe now builds its matching image.")
            image = ""

        return cls(forge=forge, server_url=server, api_url=api, control_repository=control,
                   target_repository=target, base_branch=base, models=models, default_model=default_model,
                   checks=_commands(data, "checks", required=True), prepare=_commands(data, "prepare"),
                   state_branch=state, image=image,
                   forbidden_paths=_paths(data, "forbidden_paths", DEFAULT_FORBIDDEN),
                   ignore=DEFAULT_IGNORE + _paths(data, "ignore", ()), egress=egress,
                   open_issues=open_issues, allow_http=allow_http, limits=Limits(**limits),
                   warnings=tuple(warnings))
