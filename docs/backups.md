# Encrypted repository backups

BananaVibe can archive its private task data to a dedicated private GitHub or Forgejo repository. This is optional; nothing enables it for you. There is no persistent application server and no local application database: the backup command reads the forge, packages what it finds, encrypts it with [age](https://age-encryption.org/), and pushes it. It does not run a model, start a task, or post comments.

One snapshot contains the trusted `.bananavibe.toml`, the saved task records, the issue and comment transcripts belonging to those tasks, the control repository's default and state branches as a Git bundle, and the available `bananavibe/…` task branches as a second bundle. Each unfinished task's bundle carries the last checkpoint recorded in its state, even when the runner has since pushed a newer commit.

Runner secrets, repository permissions, PR metadata, issues without task records, deleted completed-task branches, and unpushed runner files are not included and need separate recovery. [BananaWiki](https://github.com/BananaSuite/BananaWiki) and [BananaChat](https://github.com/BananaSuite/BananaChat) store their own server backups in the same encrypted format; follow the backup documentation in those repositories for those products.

## Set up a destination

Install Python 3.11+, Git, and age on the machine that will run the backup (BananaVibe itself has no Python dependencies). On Debian/Ubuntu, `sudo apt install age` supplies `age` and `age-keygen`.

Create a private backup repository, separate from the issue and target repositories. It may be empty, and it can hold several series if each has a distinct name. Create a backup token with Contents read/write and Metadata read on that GitHub repository, or repository read/write on the Forgejo repository, and keep it in a mode-`0600` file. The token that reads the issue and target repositories is separate: supply it privately in the environment as `BANANAVIBE_TOKEN`, with read Contents and Issues on the control repository and read Contents on the target.

Run the commands from a checkout of this repository. The store keeps its working copy, backup token, recovery identity, and series configuration in `~/.local/state/bananavibe-backups`; `--root` or `BANANAVIBE_BACKUP_ROOT` selects another directory. Options belonging to the command go before the action, as shown.

```sh
python3 -m bananavibe backups --root /private/bananavibe-backups keygen --output /private/bananavibe-recovery.agekey
python3 -m bananavibe backups --root /private/bananavibe-backups configure \
  --forge forgejo --repo https://forge.example.org/YOUR_TEAM/private-backups.git \
  --name bananavibe --username YOUR_BOT \
  --token-file /private/backup.token --key-file /private/bananavibe-recovery.agekey \
  --keep 7 --max-mib 512
python3 -m bananavibe backups --root /private/bananavibe-backups --config /private/.bananavibe.toml run
python3 -m bananavibe backups --root /private/bananavibe-backups list
python3 -m bananavibe backups --root /private/bananavibe-backups status
```

For GitHub, use `--forge github --repo https://github.com/YOUR_TEAM/private-backups.git`. Set `--username` to the account your forge requires; GitHub installation tokens commonly use `x-access-token`. `--config` defaults to `.bananavibe.toml` in the working directory.

**Save an offline copy of the recovery key before relying on this backup.** Repository access alone cannot restore a snapshot. `keygen` prints the public recipient and the key filename, never the secret identity. Keep the original key for older snapshots when rotating keys; use a new series name for the new key.

The tool checks repository privacy through the forge API before each transfer. It refuses public, archived, or mirrored destinations and does not follow credential-bearing redirects. A visibility or permission change stops future transfers. Encryption also protects the payload if the repository later becomes public.

## Schedule

`backups enable --interval 1440` and `backups disable` only record the flag that `backups run --automatic` honors. There is no timer or service to install; run the command from your own scheduler on a persistent operator machine.

The other way to schedule is a dedicated Actions workflow, supplied as [`examples/backup-github.yml`](../examples/backup-github.yml) and [`examples/backup-forgejo.yml`](../examples/backup-forgejo.yml). Copy one into the issue repository's workflow directory and set:

- `BANANAVIBE_REVISION` as a repository variable, the reviewed published commit of this repository that the job fetches its tooling from.
- `BACKUP_REPO`, `BACKUP_FORGE`, `BACKUP_NAME`, and `BACKUP_USERNAME` as repository variables.
- `BANANAVIBE_BACKUP_READ_TOKEN`, `BACKUP_REPO_TOKEN`, and `BACKUP_RECOVERY_KEY` as Actions secrets. The recovery key secret holds the complete saved native age identity.

`scripts/backup_action.py` also reads optional `BACKUP_KEEP` and `BACKUP_MAX_MIB`, defaulting to 7 snapshots and 512 MiB. On an isolated Forgejo runner, install Python 3.11+, Git, and age beforehand; mirror the tooling URL onto an accessible forge and pin the mirror to a reviewed commit if GitHub is unavailable. Enable the workflow only after saving the recovery key and testing a restore. Backup credentials never go into the OpenCode task container. A temporary runner removes its local package at job end, including on failure, so rerun the backup after fixing the cause. Keep the offline key independently of Actions secrets.

## Restore for review

```sh
python3 -m bananavibe backups --root /private/bananavibe-backups verify SNAPSHOT_ID
python3 -m bananavibe backups --root /private/bananavibe-backups restore SNAPSHOT_ID --output /private/bananavibe-recovery
```

Use an exact ID from `backups list`. `verify` checks age authentication, snapshot identity, and the full checksum without writing a package; `download SNAPSHOT_ID --output FILE` keeps the decrypted archive instead. `restore` creates a new private review directory and refuses an existing path. It does not restart work, overwrite a repository, recreate issue numbers, or publish transcripts. The recovered transcripts are there for manual recovery if the forge itself was lost; they are not a full forge disaster-recovery export.

## Apply reviewed state

When the original forge and issue numbers still exist, disable Actions and wait for active leases to expire. Review the recovered configuration, recreate runner secrets separately, then apply the state explicitly:

```sh
python3 -m bananavibe restore-state /private/bananavibe-recovery \
  --config /private/.bananavibe.toml --confirm-repository YOUR_TEAM/BananaWiki-prompts
```

`--confirm-repository` must name the control repository in the running configuration and in the archive's metadata. This saves a private copy of current state into the recovery directory, restores missing unfinished-task branches from the checkpoint bundle, and writes task records with optimistic updates. A task branch with different or newer work fails the command for human review, as does a task whose lease has not expired. Recovered unfinished tasks come back paused with expired leases. Re-enable Actions only after checking the result, then resume individual tasks with `/banana resume`.

## Retention and Git limits

Each snapshot is an independent commit on `banana-backups/bananavibe/SERIES/SNAPSHOT_ID`. A small permanent `identity` branch binds that series to its public recovery recipient. The tool never pushes to the issue or target repositories. It uploads encrypted parts of at most 32 MiB, downloads the result through a fresh Git repository, authenticates and decrypts it, and checks the full checksum before deleting old snapshot branches in that series. Updates to branches use leases to prevent overwriting concurrent changes. Other products, series, and unrelated branches are preserved.

The default maximum is 512 MiB per compressed package, configurable up to 1024 MiB, and the same limit caps the archived transcripts. A repository with many task branches or long issue histories fails the backup rather than truncating it; archive old tasks first. Encryption prevents efficient deduplication, so retained full snapshots consume storage, and forge repository and push quotas still apply. Retention removes branch references; the hosting provider controls when unreachable objects and reflogs are garbage-collected, so space may not shrink immediately.

Malformed, oversized or nonregular task-state records also stop the backup. Inspect and repair the state branch before retrying; corrupted tasks are never silently omitted from a snapshot. Invalid archive manifests fail before review files are published, and a failed review copy removes its incomplete new directory.

Allow staging space for the archive, encryption, and a verification download. Downloads reserve three times the configured limit plus 128 MiB; upload needs additional package-sized working space. A backup in the same forge or account does not replace an independent offline copy and a tested recovery key.
