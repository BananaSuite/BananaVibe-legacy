# Changelog

## Unreleased

- Correct Forgejo setup requirements: the dedicated bot needs repository Admin
  access to check other users' permissions. Refuse insufficient access with a
  clear setup error without falling back to membership-based authorization.
- Recheck issue approval throughout a run and immediately before publication;
  approved edits restart the agent with the current issue text.
- Recheck the task branch after generating the PR description, so a concurrent
  push cannot publish an unchecked commit.
- Prevent state creation, writes, migration and recovery from targeting the
  control repository's default branch.
- Reject malformed or oversized task records explicitly, including during
  backup listing and recovery, instead of dropping records from a snapshot.
- Safely render unusual filenames in fallback PR descriptions and reject
  malformed URL hosts, ports and control characters during configuration checks.
- Retry transient OpenCode release downloads with checksum, size and time
  limits; install the verified binary atomically.
- Remove private gateway credentials even if Docker shutdown operations fail.
- Include sandbox sources, docs, workflows and test fixtures in source packages.
- Enforce total forge/provider response deadlines across bounded DNS, TCP and
  TLS, even when headers or bodies arrive slowly; release request deadline
  workers before retries and on success or failure.
- Reject invalid Unicode, non-finite numbers and excessive nesting in provider
  JSON with a controlled decode error.
- Require a security-fixed source distribution builder (setuptools 83+).

## 1.6.0

A rewrite of the preview release for production use and open-source release.
Configuration, state and backups are compatible with the preview release; see
[UPGRADING.md](UPGRADING.md). The review that motivated each change is in
[docs/review-preview.md](docs/review-preview.md).

### Security
- Issue text is fingerprinted when a maintainer approves it. An edit afterwards blocks the task until it is re-approved.
- Acceptance checks run in a fresh container on a clean export of the committed tree, out of the agent's reach.
- `[network] allow` restricts the sandbox's internet access to listed hosts.
- The egress proxy refuses NAT64 addresses, which could otherwise reach private and metadata addresses.
- `.gitmodules` is protected by default.
- PR text cannot mention people or auto-close issues.
- Local composite actions (`.github/actions/`, `.forgejo/actions/`) are protected by default.
- Each task's gateway gets its own isolated bridge network.
- No runtime dependencies or installs. Docker gets a minimal environment.

### Reliability
- Commands that arrive while a run is finishing are no longer lost.
- Work is checkpointed on every exit path, including model and preparation failures.
- OpenCode is polled with bounded message reads. Long sessions no longer fail at 8 MiB.
- An engine that goes idle without replying is detected within two minutes.
- Per-command timeouts (`limits.command_minutes`).
- Pushes are leased against the last checkpoint and never overwrite outside commits.
- Draft PRs fall back gracefully where drafts are unavailable.
- Heartbeat and worker threads share state safely.
- A checkpoint is recorded before it is pushed, so a run killed between the push and the record resumes instead of failing with "commits BananaVibe did not make".
- `/banana restart` works after the target repository or base branch changed in the configuration.
- `/banana start` restarts a queued or running task whose runner vanished, instead of being ignored.
- A stop or fail sent while the PR description is written wins over publication, and one sent after it is no longer overwritten when the run ends.
- Guidance sent while the PR description is written is applied before the PR opens.
- Unexpected errors and Git or Docker timeouts still save the work, release the task and report on the issue.
- State writes are retried after transient forge errors, including GitHub's secondary rate limits.
- Finding a task's PR on Forgejo no longer fails in repositories with more than 500 pull requests.
- A new run removes containers, networks and key files left by a hard-killed run of the same task.
- `limits.poll_seconds` must be at most a third of `limits.lease_seconds`, so the lease cannot lapse between heartbeats.
- Backups use the workflow's repository as the control repository, like the main command.

### Features
- `/banana answer` on a finished task revises the same branch and PR.
- Multi-line `/banana answer`, plus `revise`, `cancel` and `continue` aliases.
- A single status comment, edited in place.
- The sandbox image is tagged by content, so upgrades rebuild automatically. Release builds are published to GHCR.
- `bananavibe check-config`, `bananavibe migrate-state`, and Action outputs `outcome` and `issue`.
- Protected-path edits are reverted and explained to the agent instead of failing the task.

### Operations
- The state branch is an orphan, so its writes don't trigger push workflows. All records are listed with one Git fetch, with no 1,000-task limit.
- The PR description comes from one diff-only request instead of a second sandbox.
- Hourly watchdog and cheap `author_association` filtering in the example workflows.
- Provider errors reach OpenCode and the logs, with keys redacted.
- A test suite built on real Git, plus a Docker end-to-end test in CI.

## Preview release

First public release (September 2026, commit `7dde06e`). It reported its
version as 2.0.0.
