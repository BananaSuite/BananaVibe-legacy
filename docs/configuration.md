# Configuration reference

BananaVibe reads `.bananavibe.toml` from the checked-out default branch of the
control repository (the repository whose workflow runs). Treat it like a
workflow file: changing it changes what the bot may do. Validate it with:

```sh
python3 -m bananavibe check-config --config .bananavibe.toml
```

Unknown keys produce warnings, not errors, so an upgrade can never stop a
working installation. Check the workflow log for them.

## Repositories

| Key | Default | Meaning |
| --- | --- | --- |
| `forge` | `"github"` | `"github"` or `"forgejo"`. |
| `server_url` | `"https://github.com"` | Forge base URL. |
| `api_url` | derived | `https://api.github.com`, `<server>/api/v3` (GitHub Enterprise) or `<server>/api/v1` (Forgejo). |
| `control_repository` | the workflow's repository | Where issues, commands and task state live. When running in Actions, the workflow's own repository always wins. |
| `target_repository` | `control_repository` | Where task branches and draft PRs are created. |
| `base_branch` | `"main"` | Branch that new tasks start from and PRs target. |
| `state_branch` | `"bananavibe-state"` | Branch of the control repository holding task records. BananaVibe creates it as an orphan branch. |
| `open_issues` | `true` | Whether a maintainer opening an issue starts a task without `/banana start`. |
| `allow_http` | `false` | Permit `http://` URLs. Only for a local test forge. |

## Work

| Key | Default | Meaning |
| --- | --- | --- |
| `prepare` | `[]` | Commands (argument lists) that install dependencies. They run in the agent's container before work starts, and again in a fresh container before every check run. `/state` is private scratch space, such as `/state/venv`. |
| `checks` | required | Commands that must all pass on a clean checkout of the committed files before a PR opens. Choose checks that prove the requested behavior. |
| `forbidden_paths` | workflows, local actions, `.bananavibe.toml`, `.gitmodules` | Paths the agent may not change. Its edits there are reverted and it is told why. Setting this replaces the defaults. |
| `ignore` | `[]` | Extra untracked patterns never to commit, added to the built-in list of build and test caches (`__pycache__/`, `.pytest_cache/`, `node_modules/`, …). |
| `image` | `""` | Sandbox image. Empty means build it locally from this release (cached per runner). Set it to a published digest, such as `ghcr.io/bananasuite/bananavibe-sandbox@sha256:…`, to skip the build on hosted runners. |

Local sandbox builds require the Action checkout or the complete source
distribution, which include `Dockerfile.sandbox` and its build inputs. A
wheel-only installation must configure a prebuilt `image`; without one it
refuses startup with a setup error.

## Models

Each `[models.ALIAS]` table is one model maintainers may select with `/banana model ALIAS`. `default_model` names the alias new tasks use.

| Key | Meaning |
| --- | --- |
| `provider` | `openai`, `openai-compatible`, `anthropic`, `google` or `azure`. |
| `endpoint` | API base URL, such as `https://api.openai.com/v1` or `https://api.anthropic.com/v1`. |
| `model` | The provider's model ID. |
| `api_key_env` | Name of the environment variable (an Actions secret passed in the workflow's `env`) holding the key. Never the key itself. |
| `token_param` | Set to `"max_completion_tokens"` for OpenAI-compatible models that reject `max_tokens`. |

The key is read by BananaVibe and handed only to the per-task gateway. The sandbox gets a task-scoped token that works only for that model, only for the run, and only up to `limits.max_calls`. Set provider-side spending limits as well.

## Network

```toml
[network]
allow = ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org"]
```

`allow` lists the public hosts the sandbox may reach through the gateway. `*.example.org` matches subdomains, and `"*"` (the default, as in the preview release) allows any public host. Private, loopback, link-local and cloud-metadata addresses are always refused. Model inference does not count against this list. List what `prepare` needs: package registries, and Git hosts for Git dependencies.

## Limits

All are whole numbers under `[limits]`.

| Key | Default | Range | Meaning |
| --- | --- | --- | --- |
| `max_minutes` | 45 | 1–300 | Wall-clock time for one run, excluding the image build. Keep the workflow's `timeout-minutes` about 30 above it. |
| `max_iterations` | 12 | 1–100 | Agent turns per run. Each ends with a checkpoint. |
| `max_calls` | 1000 | 1–10000 | Model requests per run, enforced by the gateway. |
| `command_minutes` | 20 | 1–240 | Timeout for each `prepare` or `checks` command. |
| `memory_mb` | 4096 | 512–32768 | Memory for the agent and checker containers. |
| `cpus` | 2 | 1–32 | CPUs for those containers. |
| `max_workspace_mb` | 512 | 16–8192 | Maximum size of the working tree, including ignored files. |
| `poll_seconds` | 15 | 1–60 | How often the runner re-reads task state (commands, stops). At most a third of `lease_seconds`. |
| `progress_seconds` | 120 | 30–1800 | How often the status comment is refreshed. |
| `lease_seconds` | 300 | 60–900 | How long a silent runner keeps its claim on a task. |
