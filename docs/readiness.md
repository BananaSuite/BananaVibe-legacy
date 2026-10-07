# Release verification

This records the production behavior checked on 3 October 2026. The checks
exercise the advertised Action, command, sandbox, publication and backup paths.
They do not assess the correctness of arbitrary model-generated changes;
maintainer review remains part of the product's workflow.

## Feature and failure coverage

| Product requirement | Evidence |
| --- | --- |
| Issue-open, comment and manual starts; whole-comment command parsing; ignore bots, PRs, strangers and other repositories | `test_controller.py`, `test_forge.py`: event handling, repository identity, permission checks on both repositories and silent authorization refusal. |
| `start`, `status`, `answer`, `stop`, `resume`, `restart`, `models`, `model`, `fail`, `help` | `test_controller.py`, `test_runner.py`: recorded intent, deduplication, same-session guidance, model changes, revision, preserved stop/fail requests and status comments. |
| Compatible configuration and task records; clear setup errors | `test_config.py`, `test_cli.py`, `test_state.py`: preview-format normalization, both shipped examples, credentials, URL/port validation, limits and malformed saved-state rejection. |
| Durable checkpoints, exclusive leases and recovery after cancellation or runner failure | Real Git integration in `test_state.py`, `test_workspace.py`, `test_runner.py`: compare-and-swap races, transient API errors, leased pushes, interrupted pushes, expired leases, iteration/time limits and final checkpoints. |
| State storage and migration preserve the control default branch | `test_state.py`: creation, mutation and migration refuse a state branch that is the default branch, including when the target base is a different release branch. |
| Edited issues require approval; approved edits use current instructions | `test_runner.py`: edits before a run, during work, during summary generation and during active heartbeat; approving current text restarts the session on saved work. |
| Protected workflows/configuration, caches, report files and embedded repositories are handled safely | `test_workspace.py`, `test_runner.py`, `test_ignore_rules.py`: protected changes are reverted, caches are omitted, controller Git metadata/credentials stay outside the mounted tree, and report reads refuse links/FIFOs/oversized files. |
| Scoped inference and network access | `test_gateway.py`: real HTTP relay, task/control credential separation, provider authentication, model and call-budget enforcement, host allowlists, private/metadata/NAT64/mixed DNS refusal and credential-redacted provider errors. |
| IPv6-independent agent/checker isolation | `test_engine_network.py`: 13 cases for explicit IPv4-only creation, fail-closed inspection/reuse, agent/checker IPv6 disable and preserved gateway egress; real restricted-container before/after probes against emulated IPv6-enabled daemon defaults. |
| All supported provider adapters | `test_gateway.py`, `test_summary.py`: OpenAI, OpenAI-compatible, Anthropic, Google and Azure request/response mapping against scripted responses. |
| Forge/provider requests obey finite response budgets and refuse corrupt JSON | `test_httpclient.py`, `test_forge.py`: bounded stalled DNS, TCP/TLS budgets, verified certificates, slowly arriving headers, fixed-length/chunked/error bodies, explicit retries, socket/worker cleanup before backoff, no redirects, UTF-8, finite numbers and nesting limits. |
| Clean committed-tree acceptance checks; failed checks return to the agent | `test_runner.py`, `test_e2e_docker.py`: exact committed files checked in a new container with a fresh state directory, followed by correction and another check. |
| Draft PR creation, graceful draft fallback, updates and refusal of closed/merged PRs | `test_forge.py`, `test_runner.py`: both GitHub and Forgejo wire protocols, existing PR revision, moved-branch refusal before and after summary generation, and late commands preserved. |
| Actual Forgejo permission and publication behavior | `test_forgejo_docker.py`: pinned official Forgejo 16.0.5, write-only bot refusal, repository Admin bot, reader/unknown-user refusal, private repositories, issue/comments, authenticated orphan Git state, file CAS conflicts, draft PR creation/update and closed-PR refusal. |
| Public descriptions contain only diff context and inert forge text | `test_summary.py`, `test_e2e_docker.py`: provider requests receive only the diff; mentions/closing keywords and unusual fallback filenames are neutralized. |
| Pinned sandbox setup and cleanup | `test_fetch_opencode.py`, `test_engine.py`, `test_e2e_docker.py`: checksummed downloads, transient retries, bounded downloads, atomic replacement, failed-shutdown credential cleanup, real OpenCode startup and zero leftover task containers. |
| Encrypted backups, verification, retention and recovery | `test_git_backups.py`, `test_backups.py`: real age encryption and Git branches, wrong-key/tamper/replay refusal, multipart archives, verification before retention, privacy and key permissions, checkpoint-pinned bundles, exclusive review restores and paused state replay that refuses active leases or newer work. |
| Distributable source and CLI | Build of wheel and source distribution; unpacked-source tests; configuration examples and CLI version; `compileall`, Ruff and `scripts/sync_backups.py --check`. |

## Reproduce the checks

The earlier release verification passed **311 cases on each of Python 3.11,
3.13 and 3.14**. The independent recheck found and fixed a real host-reachability
gap when a Docker daemon enables IPv6 by default. Current source passes **324
ordinary cases on Python 3.11 and 3.14**; the 13 added cases cover the network
policy. The earlier Python 3.13 result predates this repair. All three current
Docker cases passed: both real OpenCode workflows, including
a failed first acceptance check followed by correction, and the actual pinned
Forgejo permission, state and publication checks.
Ruff, shared-backup source verification and whitespace checks passed.

The restricted agent reached a disposable IPv6 host listener before the repair.
After the repair, actual network inspection reports IPv6 disabled, and the
agent/checker have no IPv6 addresses with all/default/loopback IPv6 disabled.
A deliberately IPv6-enabled reused bridge is refused before checker launch.
These disposable fixtures emulate daemon defaults at network creation; they
do not change the runner's daemon configuration or contact external services.
Docker's [bridge documentation](https://docs.docker.com/engine/network/drivers/bridge/)
distinguishes IPv4 gateway inhibition from IPv6 configuration.

An initial suite run concurrent with Docker builds exposed a test scheduling
assumption: only seven of eight stalled DNS worker threads had entered their
lookup by the assertion. All request deadline checks passed. The test now waits
separately, with a finite budget, for worker entry while preserving the request
deadline, eight-worker ceiling, cleanup and reuse checks. The initial failure
and the subsequent verification logs are retained in the recheck evidence.

```sh
python -m pip install pytest==9.1.1 ruff==0.15.8 build
# Install Git, Docker Engine and age/age-keygen using the runner's package manager.
python -m ruff check .
python -m pytest -q
python -m pytest -q -m docker tests/test_e2e_docker.py
python -m pytest -q -m docker tests/test_forgejo_docker.py
python scripts/sync_backups.py --check
python -m build
python -m bananavibe check-config --config examples/bananawiki.toml
python -m bananavibe check-config --config examples/bananachat.toml
```

The Docker cases use the real pinned OpenCode binary and sandbox image, with a
scripted local model and Git-backed forge double. They cover both immediate
success and a deliberately incorrect first change that must fail checks and be
corrected. They also verify metadata proxy access returns HTTP 403, direct
egress has no route, private issue text is absent from the summary request and
task containers are removed.

## Deployment evidence boundaries

GitHub is exercised through its wire protocol on a local HTTP server. Forgejo
also passed nine acceptance groups against the actual official **16.0.5**
release in a disposable container with private fixture repositories, synthetic
users and locally generated credentials. These verify actual API and Git
behavior without modifying an external forge. The test confirmed that Forgejo
requires repository Admin access for the bot to query command authors'
permissions; a write-only bot is now refused with a clear setup error.
Model adapters use scripted provider responses, and the real Docker
workflow uses an OpenAI-compatible scripted provider. This audit does not use
live forge credentials, perform live billed model calls, publish a release or
deploy an Action into another repository. A deployment still needs the bot
permissions, provider endpoint/model access, host allowlist and repository
checks described in [deployment](deployment.md) and [configuration](configuration.md).

An approved task branch remains editable by maintainers after publication;
the PR description names the exact checked checkpoint. Repository branch
protection and review govern subsequent commits and merging. The remaining
trust boundaries are described in [security-model.md](security-model.md).
