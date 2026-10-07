"""Git with the forge credential kept out of argv, config files and errors.

The repository metadata lives outside any directory mounted into the
sandbox, and every option that could execute repository-controlled code
(hooks, fsmonitor, external diff, filters from global config) is disabled.
"""

import os
from pathlib import Path
import subprocess
import sys

HARDENING = ("-c", "safe.directory=*", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "credential.helper=",
             "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=user", "-c", "http.followRedirects=false",
             "-c", "core.attributesFile=/dev/null", "-c", "diff.external=", "-c", "core.autocrlf=false",
             "-c", "core.symlinks=true", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
             "-c", "advice.detachedHead=false", "-c", "init.defaultBranch=main", "-c", "commit.gpgSign=false",
             "-c", "tag.gpgSign=false", "-c", "push.gpgSign=false")
IDENTITY = {"GIT_AUTHOR_NAME": "BananaVibe", "GIT_AUTHOR_EMAIL": "bananavibe@users.noreply.local",
            "GIT_COMMITTER_NAME": "BananaVibe", "GIT_COMMITTER_EMAIL": "bananavibe@users.noreply.local"}


class GitError(RuntimeError):
    pass


class Git:
    def __init__(self, git_dir, *, work_tree=None, token="", username="", control_dir=None):
        self.git_dir = Path(git_dir)
        self.work_tree = Path(work_tree) if work_tree else None
        control = Path(control_dir or self.git_dir.parent / "git-control")
        control.mkdir(mode=0o700, parents=True, exist_ok=True)
        helper = control / "askpass.py"
        helper.write_text(f"#!{sys.executable}\nimport os, sys\n"
                          "print(os.environ['BANANAVIBE_GIT_USER'] if 'sername' in sys.argv[-1] "
                          "else os.environ['BANANAVIBE_GIT_PASSWORD'])\n")
        helper.chmod(0o700)
        self.environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": str(control),
                            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
                            "GIT_ASKPASS": str(helper), "GIT_LFS_SKIP_SMUDGE": "1", "GIT_NO_REPLACE_OBJECTS": "1",
                            "BANANAVIBE_GIT_USER": username or "git", "BANANAVIBE_GIT_PASSWORD": token, **IDENTITY}
        if not (self.git_dir / "HEAD").exists():
            self.run("init", "--quiet", "--bare", str(self.git_dir), repository=False)
            if self.work_tree:
                self.run("config", "core.bare", "false")
                self.run("config", "core.worktree", str(self.work_tree))

    def run(self, *args, check=True, text=True, timeout=600, env=None, input=None, repository=True):
        location = ["--git-dir", str(self.git_dir)] if repository else []
        if repository and self.work_tree:
            location += ["--work-tree", str(self.work_tree)]
        result = subprocess.run(["git", *HARDENING, *location, *map(str, args)], cwd=self.work_tree or self.git_dir.parent,
                                env={**self.environment, **(env or {})}, capture_output=True, text=text,
                                timeout=timeout, input=input)
        if check and result.returncode:
            # Git output can echo remote responses; keep it out of messages.
            raise GitError(f"git {args[0]} failed with exit status {result.returncode}.")
        return result

    def out(self, *args, **kwargs):
        return self.run(*args, **kwargs).stdout.strip()

    def fetch(self, url, refspec, *, depth=None, timeout=900):
        return self.run("fetch", "--quiet", "--no-tags", *(["--depth", str(depth)] if depth else []),
                        url, refspec, timeout=timeout)

    def read_files(self, revision, prefix, *, limit=512 * 1024, strict=False):
        """Return {path: bytes} for regular files under prefix at revision."""
        listing = self.run("ls-tree", "-r", "-z", "--full-tree", "-l", revision, "--", prefix, text=False).stdout
        wanted = []
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            info, path = entry.split(b"\t", 1)
            mode, kind, oid, size = info.split()
            if strict and (mode != b"100644" or kind != b"blob" or int(size) > limit):
                raise GitError("The task state includes an oversized or nonregular file; inspect the state branch.")
            if mode == b"100644" and kind == b"blob" and int(size) <= limit:
                wanted.append((path.decode(), oid.decode()))
        if not wanted:
            return {}
        raw = self.run("cat-file", "--batch", input="".join(oid + "\n" for _, oid in wanted).encode(),
                       text=False).stdout
        files, offset = {}, 0
        for path, _ in wanted:
            header_end = raw.index(b"\n", offset)
            size = int(raw[offset:header_end].split()[2])
            files[path] = raw[header_end + 1:header_end + 1 + size]
            offset = header_end + 1 + size + 1
        return files
