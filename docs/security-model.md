# Security model

BananaVibe runs code written by a language model on your runner and pushes it
to your repository. These are the boundaries it relies on, and the ones it
cannot provide.

## Who can make it act

- Configuration comes only from the control repository's default branch, as checked out by the workflow. The workflow's own repository overrides any `control_repository` in the file.
- Commands must be a whole comment, from someone with **write access to both** repositories. Bots, pull-request comments and edited comments are ignored. Unauthorized commands get no reply, so the bot can't be used to spam.
- Forgejo requires the dedicated bot to have repository Admin access to query other people's permissions. This grants the controller additional repository-management authority; the token stays outside the sandbox on a trusted runner. It does not require instance administrator access or grant command authors additional rights. See [deployment](deployment.md#credentials).
- The agent's instructions are the issue title and body **as approved** by a maintainer, plus guidance given with `/banana answer`. Other comments never reach it. If the issue text changes after approval, the task stops until a maintainer resumes it.

## What the sandbox can reach

```text
runner (controller)      holds BANANAVIBE_TOKEN, model keys, Git metadata
  └─ per-task bridge ─── gateway container   real model key, control secret
       └─ internal net ─ agent container      workspace + /state, task token only
                       └ checker container    clean export + fresh /state
```

- The agent and checker use an explicitly IPv4-only internal Docker bridge, with its IPv4 host gateway inhibited. BananaVibe inspects the bridge before starting the agent and before each checker, and refuses unreadable or unsafe network settings. Both untrusted containers also disable IPv6 on all existing and future interfaces, including loopback and link-local addresses. Their only reachable peer is the gateway. The trusted gateway retains ordinary provider egress, including IPv6.
- The gateway (`sandbox/gateway.py`) accepts the task token for inference with the configured model only, up to `max_calls`, until the run's deadline. It relays provider responses, with the real key redacted from errors.
- Its egress proxy allows public hosts in `network.allow` on ports 443 (CONNECT) and 80. It resolves DNS once and refuses private, loopback, link-local, carrier-grade NAT and metadata addresses.
- The controller reaches OpenCode's API only through the gateway's control port, bound to `127.0.0.1` and protected by a separate secret.
- The containers run as an unprivileged user with a read-only root filesystem, all capabilities dropped, `no-new-privileges`, and memory, CPU, process, file-size and open-file limits.
- Git metadata, the askpass helper and the forge token are outside the mounted directory. Git runs with hooks, fsmonitor, external diff and global configuration disabled.

## What is checked before publishing

- Changes to `forbidden_paths` (workflows, local actions, `.bananavibe.toml` and `.gitmodules` by default), embedded Git repositories and files over 50 MiB are reverted, never committed.
- Checks run on a clean export of the committed tree in a fresh container with a fresh `/state`. Nothing the agent changed in its own environment can influence them.
- Each push uses a lease on the last known checkpoint, so BananaVibe never overwrites commits it did not make. It re-checks the branch before opening the PR.
- The PR description is generated from the diff alone, in a request with no tools. Mentions and closing keywords (`Fixes #12`) are neutralized so opening or merging the PR cannot ping people or close unrelated issues.

## What it does not protect against

Task branches are ordinary branches of the target repository, so its `pull_request` workflows run the agent's code (tests, build scripts) with whatever secrets same-repository branches receive. Protected paths stop edits to workflow files, not to the code those workflows run. Keep deployment secrets in environments that require a reviewer.

The agent can also weaken or delete a test in plain sight in its diff, and it sees the private issue and guidance. It is told not to copy them into files, but review test changes and look for disclosure as carefully as you review the code. Don't put secrets in issues.

Prompts and source code go to the model provider you configure, so choose one whose data handling fits your project.

Containers share the runner's kernel. Use GitHub-hosted runners or a dedicated, disposable, patched VM, never a runner that holds other secrets.

Finally, anyone who can push to the control repository can edit task records. BananaVibe validates what it reads (for example, it only ever works on `bananavibe/*` branches), but treat write access to that repository as maintainer access.

Report vulnerabilities as described in [SECURITY.md](../SECURITY.md).
