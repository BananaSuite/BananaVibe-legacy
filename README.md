<img src="banana-logo.png" alt="BananaVibe logo" width="64">

# BananaVibe

BananaVibe is a GitHub and Forgejo Action for maintainers. You describe a change in an issue, or comment `/banana start` on an existing one. BananaVibe runs the [OpenCode](https://github.com/anomalyco/opencode) coding agent in a Docker sandbox, runs your tests against what the agent committed, and opens a draft pull request once they pass. Merging stays with you.

We wrote it to keep [BananaWiki](https://github.com/BananaSuite/BananaWiki) and [BananaChat](https://github.com/BananaSuite/BananaChat) moving: first drafts of small features, bug fixes, regression tests, dependency bumps and documentation. It works with any repository that has a test command.

## How it works

1. A maintainer starts a task. Only people with write access to both the issue repository and the target repository can use commands, and other comments never reach the agent.
2. BananaVibe creates a `bananavibe/…` branch and starts OpenCode in a container that holds neither the forge token nor the real model key. The container reaches the model and the internet only through a per-task gateway, which enforces a model, a call budget and, when you configure one with `[network] allow`, a host allowlist.
3. After every agent turn, the working tree is committed and pushed. A stop or timeout saves the work so far; a hard crash can lose at most the turn in progress.
4. When the agent says it is done, BananaVibe runs your checks itself, in a fresh container on a clean copy of the commit. Failures go back to the agent.
5. When everything passes, a draft PR is opened. Its description is written from the diff alone, so issue text and guidance from a private repository are never given to it.

If the issue is edited after a maintainer approved it, the task pauses until someone approves the new text. The Action needs nothing beyond Python's standard library and Docker.

## Quick start (GitHub)

1. In the repository where issues will live (it can be the target itself, or a private "prompts" repository), copy [`examples/github.yml`](examples/github.yml) to `.github/workflows/bananavibe.yml`.
2. Copy [`examples/bananawiki.toml`](examples/bananawiki.toml) to `.bananavibe.toml` and edit the repositories, model, `prepare` and `checks`. Every key is documented in [configuration](docs/configuration.md).
3. Add Actions secrets: `BANANAVIBE_TOKEN` (a bot token or GitHub App token with Contents, Issues and Pull requests read/write on both repositories) and the model key named by `api_key_env`.
4. Validate locally: `python3 -m bananavibe check-config --config .bananavibe.toml` (from a checkout of this repository).
5. Commit both files to the default branch, open an issue describing a change, and watch the status comment.

Forgejo uses [`examples/forgejo.yml`](examples/forgejo.yml) and a host runner.
Its dedicated bot needs repository **Admin** access to both repositories so it
can verify command authors' permissions; authors need only write access.
See [deployment](docs/deployment.md).

## Commands

Commands must be the entire comment.

| Command | Effect |
| --- | --- |
| `/banana start` | Begin work on this issue. |
| `/banana status` | Show state, branch, checkpoint and pull request. |
| `/banana answer TEXT` | Give guidance and continue. On a finished task, revise its open PR. |
| `/banana stop` | Stop at the next safe point; work stays on the branch. |
| `/banana resume` | Continue from the last checkpoint (also approves an edited issue). |
| `/banana restart` | Start over from the base branch on a new branch; the old one is kept. |
| `/banana models` | List the configured models. |
| `/banana model ALIAS` | Switch model, even while a task runs. |
| `/banana fail REASON` | Mark the task failed. |
| `/banana help` | List the commands. |

Each run is bounded by `limits.max_minutes` and `limits.max_iterations`. When the agent needs a decision it asks on the issue and waits for `/banana answer`. See the [task lifecycle](docs/task-lifecycle.md).

## Upgrading from the preview release

BananaVibe 1.6 is a rewrite of the preview release (commit `7dde06e`). Change the Action reference to `@v1.6.0` (or its commit SHA). Configuration, task records, branches and backups are compatible, and running tasks continue. Then optionally run `bananavibe migrate-state` once. See [UPGRADING.md](UPGRADING.md), and the [review of the preview release](docs/review-preview.md) for what changed and why.

## Documentation

- [Deployment](docs/deployment.md): tokens, runners, GitHub and Forgejo
- [Configuration](docs/configuration.md): every `.bananavibe.toml` key
- [Task lifecycle](docs/task-lifecycle.md): states, recovery, revising PRs
- [Security model](docs/security-model.md): boundaries and what they do not cover
- [Backups](docs/backups.md): optional encrypted off-site copies
- [Development](docs/development.md): architecture and running the tests
- [Release verification](docs/readiness.md): advertised features, failure coverage and deployment evidence

## History, ownership and license

Started by Luca Zani ([OverloadedTech](https://github.com/OverloadedTech)) on 13 August 2026 as BananaAgent, at Officina Tecnologica. It was internal until September 2026. The public history starts from a clean export because the private history contains credentials; see [NOTICE](NOTICE).

Copyright © 2026 Luca Zani and all contributors. Each contributor retains copyright in their contributions. BananaVibe is licensed **AGPL-3.0-only**. OpenCode is MIT-licensed; see [third-party notices](THIRD_PARTY_NOTICES.md). Please read [contributing](CONTRIBUTING.md), the [code of conduct](CODE_OF_CONDUCT.md) and [security reporting](SECURITY.md).
