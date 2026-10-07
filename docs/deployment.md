# Deploying BananaVibe

BananaVibe is an Action. There is no server to run: each issue event starts a
workflow job, which records the request and, when there is work, runs the
task in Docker on that job's runner.

## Choose repositories

BananaVibe uses two repositories, which can be the same one. The control repository holds `.bananavibe.toml`, the workflow, the issues and the `bananavibe-state` branch; make it private if prompts and guidance should stay private, since its Actions logs and state branch will then be private too. The target repository receives the `bananavibe/<task>-<n>` branches and the draft PRs, and can be public. Both must be on the same forge instance.

Protect the target's default branch: require reviews and CI, and don't enable auto-merge for bot PRs. The bot needs to push `bananavibe/*` branches, so don't protect that pattern. Task branches run your `pull_request` CI with the secrets that same-repository branches receive. Keep deployment credentials in protected environments; see the [security model](security-model.md).

## Credentials

`BANANAVIBE_TOKEN` must be able to, on **both** repositories:

- read metadata and collaborator permissions
- read and write contents (branches, and the state branch in the control repository)
- read and write issues (comments, close, reopen)
- create and update pull requests (target)

On GitHub, use a fine-grained token for a dedicated bot account (Contents, Issues and Pull requests read/write, Metadata read), or a GitHub App installation token generated in an earlier step (`actions/create-github-app-token`). Installation tokens last an hour, so keep `limits.max_minutes` under about 45. The default `GITHUB_TOKEN` usually cannot write to a second repository and cannot start further workflows.

On Forgejo, use a dedicated bot account's access token with repository and issue
read/write scope, and give that bot **Admin access to both repositories**.
Forgejo lets an ordinary collaborator query only its own permission; repository
Admin access is required to verify the permission of the person issuing a
command. Instance administrator access is not required. Command authors still
need only write access to both repositories. The bot's repository Admin role
also permits managing collaborators and protections, so keep its token on a
trusted runner and outside the coding sandbox. A write-only bot is refused with
a setup error; BananaVibe does not infer authorization from collaborator or
assignee membership.

Add one secret per `api_key_env` in the configuration, and pass each in the workflow step's `env`, as the examples show.

## GitHub Actions

1. Copy [`examples/github.yml`](../examples/github.yml) to `.github/workflows/bananavibe.yml` in the control repository.
2. Pin the Action to a reviewed release, preferably by commit SHA (`BananaSuite/BananaVibe@<sha> # v1.6.0`).
3. Add `.bananavibe.toml` and the secrets, and commit to the default branch.

Hosted `ubuntu-latest` runners have everything needed: Python 3.11+, Git and Docker. The first task on a fresh runner builds the sandbox image, which takes about 3–5 minutes and isn't counted in `max_minutes`. To skip the build, set `image` to the digest published with each release.

The example skips the job for comments that aren't commands and for people with no association to the repository, before a runner starts. BananaVibe still checks write access itself.

## Forgejo Actions

1. Register a runner with the label `bananavibe:host` on a **dedicated, disposable Linux VM** with Python 3.11+, Git and Docker Engine, and let the runner user use Docker. Don't give a shared or untrusted runner a Docker socket.
2. Allow at least two concurrent jobs, so that `/banana stop` can run while a task is running.
3. Copy [`examples/forgejo.yml`](../examples/forgejo.yml) to `.forgejo/workflows/bananavibe.yml`, and set `forge = "forgejo"` and `server_url` in the configuration.
4. If the instance cannot reach GitHub, mirror `actions/checkout` and BananaVibe to your instance, pin the mirrors to reviewed commits, and set `image` to a sandbox image in a registry the runner can reach.

The runner reaches the gateway's control port on `127.0.0.1`, so the job must run on the Docker host (host mode), not inside a job container.

The runner must support Docker's internal bridge, IPv4 gateway inhibition and
per-container IPv6 disable sysctls. BananaVibe explicitly creates and inspects
an IPv4-only task network and disables IPv6 in the agent and checker; it refuses
unsafe or unreadable network settings. This also applies when the daemon enables
IPv6 by default. The trusted gateway keeps its normal provider egress.

## Concurrency

Don't add a workflow `concurrency` group that cancels runs: a stop command must reach a running task. Leases in the state branch make sure only one runner works on an issue. A command that arrives while a task runs is picked up by that runner within `poll_seconds`.

## Operations

The scheduled trigger is a watchdog: it marks tasks whose runner vanished (a lost runner or a hard timeout) as interrupted. It reads all task records with one Git fetch, and hourly is enough because `/banana status` checks that issue's task at once, and `/banana start` or `resume` restarts a task whose runner vanished.

Every decision is logged in the job, and the Action sets `outcome` and `issue` outputs. Only a failed task fails the job; blocked and paused tasks are waiting for a person, not errors.

Size the disposable runner's disk for the configured number of concurrent
tasks. `max_workspace_mb` is checked when saving checkpoints; it is not a
filesystem quota on each write. Verify the real bot permissions, provider/model
access and allowlist on the chosen runner before enabling maintainer commands.

To remove BananaVibe, stop active tasks, delete the workflow, then revoke the token. Task branches, PRs and the state branch stay for you to review and delete. For off-site copies of task data, see [backups](backups.md).
