# Task lifecycle

## A run, step by step

1. A maintainer opens an issue (with `open_issues = true`) or comments `/banana start`. BananaVibe checks they can write to both repositories, records the request and a fingerprint of the issue text they approved, and starts working in the same job.
2. The runner takes a lease on the task record (compare-and-swap on the state branch). A second runner for the same issue sees the lease and exits as *busy*. A background heartbeat renews the lease and picks up new commands.
3. The first run creates `bananavibe/<task-id>-<generation>` from the base branch. Later runs check out that branch and refuse to continue if someone else pushed to it.
4. OpenCode starts in the sandbox, `prepare` runs, and the agent works through up to `max_iterations` turns. After every turn the controller pauses the container, commits the tree (reverting protected paths, embedded repositories and huge files) and pushes a checkpoint.
5. When the agent reports completion, BananaVibe exports the committed tree, runs `git diff --check`, then runs `prepare` and every check in a fresh container. Failures go back to the agent verbatim.
6. With everything passing, a separate request that sees only the diff writes the PR title and description. BananaVibe opens a draft PR (or updates the task's open one), links it on the issue and closes the issue.

A status comment on the issue is edited as the run progresses. Each run ends with one new comment saying what happened and what you can do next.

## States

| State | Meaning | What you can do |
| --- | --- | --- |
| queued | Recorded; a runner will pick it up. | Wait. If no job ever started, `start` again. |
| running | A runner holds the lease. | `stop`, `answer`, `model`, `status`. |
| blocked | The agent asked a question, completion had no changes, or the issue text changed after approval. | `answer TEXT`, or `resume` to approve edited text. |
| paused | Time or iteration allowance used up, or the run was cancelled. | `resume`. |
| interrupted | The runner vanished without finishing (found by the watchdog or `status`). | Check the job log, then `resume`. `start` also restarts a task whose runner vanished. |
| stopped | A maintainer stopped it. | `resume` or `restart`. |
| failed | An error, such as a model key, a preparation command or a moved branch, or `/banana fail`. | Fix the cause, then `resume` or `restart`. |
| complete | Checks passed and a draft PR is open. | Review and merge or close it, or `answer` to revise. |

Work is saved on the task branch in every state. `resume` always continues from the last checkpoint. `restart` starts a new generation from the base branch and keeps the old branch; it also picks up a target repository or base branch changed in the configuration.

## Changing a running task

- `/banana answer TEXT` while running is delivered to the agent in the same session at its next safe point.
- `/banana model ALIAS` switches models at the next safe point with a fresh session on the saved work.
- Issue edits are detected by the heartbeat and checked again before publication. An unapproved edit blocks the run; approving edited text with `resume` starts a fresh session using the current text and saved work.
- `/banana stop` stops at the next safe point, within `poll_seconds` plus the current operation, and saves a checkpoint.
- A command recorded while a run is finishing is not lost: the runner continues, or tells you to `resume` if its time is up.

## Revising a finished task

When review turns up something to change, comment `/banana answer TEXT` on the (closed) issue. BananaVibe reopens it, continues on the same branch, reruns the checks and updates the same PR. If the PR was merged or closed, use `/banana restart` for a new contribution instead.

## What "complete" means

The agent claimed completion, `git diff --check` passed, and every configured check passed on a clean checkout of the exact commit in the PR. It does not mean the change is correct, complete or safe. The checks are only as good as you configure them, and a person must review the code.
