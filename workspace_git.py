"""Git for the coding workspace's Changes view (Conduit for desktop, issue 452).

Conduit's workbench shows what changed in the open chat's folder, the diff of
each file, and the steps a person takes on it: stage, unstage, discard,
commit, switch or make a branch, pull, push, fetch, and start a repository.
The folder is a root the Folder tab already reaches (workspace_files.py): the
caller resolves it with that workspace's own rules (a Docker profile's paths,
a hosted install's one folder, an SSH profile's machine) and hands this the
real path and a runner for that machine.

Git runs with no terminal to ask on (GIT_TERMINAL_PROMPT=0, ssh in batch
mode), under a time limit, one change at a time per repository. A file Hermes'
own file routes refuse to show (``.env``, keys) is listed by name and marked
secret, and its diff isn't read. Paths come back relative to the repository's
top folder, with that folder, so a client can open them in the Folder's tabs.
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
import subprocess
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

READ_TIMEOUT_S = 20
CHANGE_TIMEOUT_S = 60
NETWORK_TIMEOUT_S = 90
MAX_FILES = 2000
MAX_DIFF_BYTES = 512 * 1024
MAX_LOG = 200
MAX_BRANCHES = 300
MAX_MESSAGE_CHARS = 10000
MAX_PATHS = 500
ERROR_CHARS = 400

# What every git call carries: nothing waits for a password, nothing paints
# colours or pages, and messages stay in English so they read the same in
# Conduit as in its strings.
_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "LC_ALL": "C",
    "LANG": "C",
    "GIT_OPTIONAL_LOCKS": "0",
}
_BATCH_SSH = "ssh -o BatchMode=yes"
# --literal-pathspecs: a path a client sends is one path, never a pattern (`*`, `:(glob)`).
_CONFIG = ("--literal-pathspecs", "-c", "core.quotepath=false", "-c", "color.ui=false", "-c", "core.fsmonitor=false")

STATUS_NAMES = {
    "M": "modified", "T": "modified", "A": "added", "D": "deleted", "R": "renamed",
    "C": "copied", "U": "conflict", "?": "untracked",
}


class GitError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Result:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.out = out
        self.err = err


def _message(result: Result, fallback: str) -> str:
    """Git's own words for what went wrong, without its hints."""
    lines = [line.strip() for line in (result.err or result.out or "").splitlines()]
    lines = [line for line in lines if line and not line.startswith("hint:")]
    text = " ".join(lines[-4:]).strip()
    if text.startswith("fatal: "):
        text = text[len("fatal: "):]
    elif text.startswith("error: "):
        text = text[len("error: "):]
    if not text:
        return fallback
    return text if len(text) <= ERROR_CHARS else text[:ERROR_CHARS - 1] + "…"


# --- Runners: where git runs ----------------------------------------------------


class LocalRunner:
    """Git on this host."""

    def __init__(self, run: Callable[..., Any] = subprocess.run, git: str = "git") -> None:
        self._run = run
        self._git = git

    def __call__(self, cwd: str, args: Sequence[str], timeout: int, stdin: Optional[str] = None) -> Result:
        env = {**os.environ, **_ENV}
        # Batch mode always, an inherited ssh command included, so a passphrase prompt never waits.
        inherited = env.get("GIT_SSH_COMMAND", "").strip()
        env["GIT_SSH_COMMAND"] = f"{inherited} -o BatchMode=yes" if inherited else _BATCH_SSH
        try:
            done = self._run([self._git, *_CONFIG, *args], cwd=cwd, env=env, input=stdin, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except FileNotFoundError:
            raise GitError(501, "Git isn't installed on the Hermes host")
        except subprocess.TimeoutExpired:
            raise GitError(504, "Git took too long; try again")
        except OSError as exc:
            raise GitError(500, f"Couldn't run Git ({exc.strerror or 'error'})")
        return Result(done.returncode, done.stdout or "", done.stderr or "")


_ERR_MARK = "\x1e-conduit-git-stderr-\x1e"


class SshRunner:
    """Git on an SSH profile's machine, through Hermes' SSH adapter."""

    def __init__(self, fs: Any) -> None:
        self._fs = fs

    def __call__(self, cwd: str, args: Sequence[str], timeout: int, stdin: Optional[str] = None) -> Result:
        exports = " ".join(f"{key}={shlex.quote(value)}" for key, value in _ENV.items())
        command = " ".join(shlex.quote(part) for part in ("git", *_CONFIG, *args))
        script = (
            f"cd {shlex.quote(cwd)} || exit 97\n"
            "command -v git >/dev/null 2>&1 || exit 98\n"
            f"e=$(mktemp) || exit 99\n"
            f"export {exports} GIT_SSH_COMMAND=\"${{GIT_SSH_COMMAND:-{_BATCH_SSH}}}\"\n"
            f"{command} 2>\"$e\"; c=$?\n"
            f"printf '{_ERR_MARK}'; cat \"$e\"; unlink \"$e\"; exit $c\n"
        )
        try:
            result = self._fs._execute(script, stdin_data=stdin, timeout=timeout)
        except Exception as exc:
            if "timed out" in str(exc).lower():
                raise GitError(504, "Git took too long; try again")
            raise GitError(502, "The SSH machine couldn't run Git")
        code = result.get("returncode")
        code = 0 if code is None else int(code)
        output = result.get("output", "") or ""
        if code == 97:
            raise GitError(404, "This folder is no longer there")
        if code == 98:
            raise GitError(501, "Git isn't installed on the SSH machine")
        out, _, err = output.partition(_ERR_MARK)
        return Result(code, out, err)


# --- The repository -------------------------------------------------------------


_repo_locks: Dict[str, threading.Lock] = {}
_repo_locks_guard = threading.Lock()


def _repo_lock(key: str) -> threading.Lock:
    with _repo_locks_guard:
        lock = _repo_locks.get(key)
        if lock is None:
            lock = _repo_locks[key] = threading.Lock()
        return lock


def _split_z(text: str) -> List[str]:
    parts = text.split("\0")
    if parts and parts[-1] == "":
        parts.pop()
    return parts


class Git:
    """One folder's repository. ``root`` is already resolved and checked by
    the caller; ``sensitive`` is Hermes' read-side refusal for a path;
    ``posix`` says the machine's paths are POSIX (an SSH machine)."""

    def __init__(self, root: str, run: Callable[..., Result], sensitive: Callable[[str], bool],
                 posix: bool = False, machine: str = "local") -> None:
        self._root = root
        self._run = run
        self._sensitive = sensitive
        self._path = posixpath if posix else os.path
        self._machine = machine

    # Helpers

    def _git(self, args: Sequence[str], timeout: int = READ_TIMEOUT_S, cwd: Optional[str] = None,
             stdin: Optional[str] = None) -> Result:
        return self._run(cwd or self._root, list(args), timeout, stdin)

    def _top(self) -> Optional[str]:
        result = self._git(["rev-parse", "--show-toplevel"])
        if result.code != 0:
            if "not a git repository" in (result.err or "").lower():
                return None
            if "dubious ownership" in (result.err or "").lower():
                raise GitError(409, "Git won't open this repository: its owner isn't the user Hermes runs as "
                                    "(safe.directory)")
            raise GitError(500, _message(result, "Git couldn't read this folder"))
        top = result.out.strip().splitlines()[0] if result.out.strip() else ""
        if not top:
            return None
        return self._path.normpath(top)

    def _require_top(self) -> str:
        top = self._top()
        if top is None:
            raise GitError(409, "This folder isn't a Git repository")
        return top

    def _full(self, top: str, rel: str) -> str:
        return self._path.join(top, *rel.split("/"))

    def _secret(self, top: str, rel: str) -> bool:
        return bool(self._sensitive(self._full(top, rel)))

    def _renamed_from(self, top: str, rel: str) -> Optional[str]:
        """The path a staged file was renamed or copied from, if it was. Read
        over the whole index, since a diff limited to the new path shows a
        rename as a new file with the old one's contents."""
        result = self._git(["diff", "--cached", "--name-status", "-z", "--no-ext-diff", "-M", "-C"], cwd=top)
        if result.code != 0:
            return None
        fields = _split_z(result.out)
        at = 0
        while at < len(fields):
            code = fields[at]
            if code[:1] in ("R", "C") and at + 2 < len(fields):
                if fields[at + 2] == rel:
                    return fields[at + 1]
                at += 3
            else:
                at += 2
        return None

    def _secret_moved_to(self, top: str, rel: str) -> bool:
        """Whether an untracked file is a secret moved there without Git (a
        plain mv): its contents match a sensitive tracked file deleted from
        the folder."""
        deleted = self._git(["ls-files", "-d", "-z"], cwd=top)
        secrets_gone = [path for path in _split_z(deleted.out) if self._secret(top, path)] if deleted.code == 0 else []
        if not secrets_gone:
            return False
        staged = self._git(["ls-files", "-s", "-z", "--", *secrets_gone], cwd=top)
        blobs = {row.split("\t", 1)[0].split(" ")[1] for row in _split_z(staged.out) if "\t" in row and " " in row}
        mine = self._git(["hash-object", "--", rel], cwd=top)
        return mine.code == 0 and mine.out.strip() in blobs

    def _rel(self, top: str, raw: Any) -> str:
        """A path the client sent, as a path inside the repository. It may be
        relative to the top folder or absolute under it."""
        if not isinstance(raw, str) or not raw.strip() or "\0" in raw or len(raw) > 4096:
            raise GitError(400, "A file's path is required")
        text = raw.strip()
        if self._path.isabs(text):
            text = self._path.relpath(self._path.normpath(text), top)
        text = text.replace("\\", "/") if self._path is os.path and os.sep == "\\" else text
        norm = posixpath.normpath(text)
        if norm in (".", "") or norm.startswith("../") or norm == ".." or norm.startswith("/"):
            raise GitError(403, "That's outside this repository")
        return norm

    def _rels(self, top: str, raw: Any) -> List[str]:
        if not isinstance(raw, list) or not raw:
            raise GitError(400, "paths must be a list of files")
        if len(raw) > MAX_PATHS:
            raise GitError(400, f"At most {MAX_PATHS} files at a time")
        return [self._rel(top, item) for item in raw]

    # Reading

    def status(self) -> Dict[str, Any]:
        top = self._top()
        if top is None:
            return {"repo": False, "root": self._root, "machine": self._machine}
        result = self._git(["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"])
        if result.code != 0:
            raise GitError(500, _message(result, "Git couldn't read what changed"))
        branch: Dict[str, Any] = {"head": None, "oid": None, "upstream": None, "ahead": 0, "behind": 0,
                                  "detached": False}
        files: List[Dict[str, Any]] = []
        truncated = False
        parts = _split_z(result.out)
        i = 0
        while i < len(parts):
            entry = parts[i]
            i += 1
            if entry.startswith("# "):
                key, _, value = entry[2:].partition(" ")
                if key == "branch.oid":
                    branch["oid"] = None if value == "(initial)" else value
                elif key == "branch.head":
                    branch["detached"] = value == "(detached)"
                    branch["head"] = None if value == "(detached)" else value
                elif key == "branch.upstream":
                    branch["upstream"] = value
                elif key == "branch.ab":
                    match = re.match(r"\+(\d+) -(\d+)", value)
                    if match:
                        branch["ahead"], branch["behind"] = int(match.group(1)), int(match.group(2))
                continue
            kind = entry[:1]
            orig = None
            if kind == "1":
                fields = entry.split(" ", 8)
                if len(fields) < 9:
                    continue
                xy, path = fields[1], fields[8]
            elif kind == "2":
                fields = entry.split(" ", 9)
                if len(fields) < 10:
                    continue
                xy, path = fields[1], fields[9]
                orig = parts[i] if i < len(parts) else None
                i += 1
            elif kind == "u":
                fields = entry.split(" ", 10)
                if len(fields) < 11:
                    continue
                xy, path = "UU", fields[10]
            elif kind == "?":
                xy, path = "??", entry[2:]
            else:
                continue
            if len(files) >= MAX_FILES:
                truncated = True
                continue
            index, worktree = xy[0], xy[1]
            conflict = kind == "u"
            files.append({
                "path": path,
                "from": orig,
                "staged": None if index in ".?" or conflict else STATUS_NAMES.get(index, "modified"),
                "unstaged": "conflict" if conflict else (
                    "untracked" if kind == "?" else (None if worktree == "." else STATUS_NAMES.get(worktree, "modified"))),
                "secret": self._secret(top, path) or bool(orig and self._secret(top, orig)),
                "added": None,
                "removed": None,
                "stagedAdded": None,
                "stagedRemoved": None,
            })
        if files:
            by_path = {item["path"]: item for item in files}
            for cached, (added_key, removed_key) in ((False, ("added", "removed")),
                                                     (True, ("stagedAdded", "stagedRemoved"))):
                args = ["diff", "--numstat", "-z", "--no-ext-diff", "--no-renames"]
                if cached:
                    args.append("--cached")
                # Paths relative to the top folder, as the status rows are.
                counts = self._git(args, cwd=top)
                if counts.code != 0:
                    continue
                for row in _split_z(counts.out):
                    fields = row.split("\t", 2)
                    if len(fields) != 3 or fields[2] not in by_path:
                        continue
                    added, removed = fields[0], fields[1]
                    item = by_path[fields[2]]
                    item[added_key] = int(added) if added.isdigit() else None
                    item[removed_key] = int(removed) if removed.isdigit() else None
        return {"repo": True, "root": self._root, "top": top, "machine": self._machine,
                "branch": branch, "files": files, "truncated": truncated}

    def diff(self, raw_path: Any, staged: bool = False) -> Dict[str, Any]:
        top = self._require_top()
        rel = self._rel(top, raw_path)
        source = self._renamed_from(top, rel) if staged else None
        if self._secret(top, rel) or (source is not None and self._secret(top, source)):
            raise GitError(403, "Conduit doesn't open files that hold keys or passwords")
        # One file: a folder's path would show every file under it, secrets included.
        names = self._git(["diff", "--name-only", "-z", *(["--cached"] if staged else []), "--", rel], cwd=top)
        if names.code == 0 and any(name != rel for name in _split_z(names.out)):
            raise GitError(400, "Pick one file to see its changes")
        base = ["--no-color", "--no-ext-diff", "-U3", "--find-renames"]
        if staged:
            result = self._git(["diff", "--cached", *base, "--", rel], cwd=top)
        else:
            result = self._git(["diff", *base, "--", rel], cwd=top)
            if result.code == 0 and not result.out.strip():
                tracked = self._git(["ls-files", "--error-unmatch", "--", rel], cwd=top)
                if tracked.code != 0:
                    if self._secret_moved_to(top, rel):
                        raise GitError(403, "Conduit doesn't open files that hold keys or passwords")
                    # Untracked: the whole file as added (exit 1 means it differs).
                    result = self._git(["diff", "--no-index", *base, "--", os.devnull if self._path is os.path
                                        else "/dev/null", rel], cwd=top)
                    if result.code == 1:
                        result = Result(0, result.out, result.err)
        if result.code != 0:
            raise GitError(500, _message(result, "Git couldn't read that file's changes"))
        text = result.out
        truncated = len(text.encode("utf-8", errors="replace")) > MAX_DIFF_BYTES
        if truncated:
            text = text.encode("utf-8", errors="replace")[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
            text = text[:text.rfind("\n") + 1] if "\n" in text else text
        binary = bool(re.search(r"^Binary files .* differ$", text, re.MULTILINE))
        return {"path": rel, "top": top, "staged": staged, "diff": text, "binary": binary, "truncated": truncated}

    def log(self, limit: Any = 50, path: Any = None) -> Dict[str, Any]:
        top = self._top()
        if top is None:
            return {"repo": False, "commits": []}
        try:
            count = max(1, min(MAX_LOG, int(limit)))
        except (TypeError, ValueError):
            count = 50
        args = ["log", f"-n{count}", "--format=%x1e%H%x1f%h%x1f%an%x1f%at%x1f%s%x1f%D", "--shortstat"]
        if path:
            args += ["--", self._rel(top, path)]
        result = self._git(args, cwd=top)
        if result.code != 0:
            if "does not have any commits" in (result.err or ""):
                return {"repo": True, "top": top, "commits": []}
            raise GitError(500, _message(result, "Git couldn't read the history"))
        commits = []
        # Each record starts at its separator: the fields, then its refs'
        # line's end, then the --shortstat line.
        for record in result.out.split("\x1e"):
            fields = record.split("\x1f")
            if len(fields) < 6:
                continue
            head, rest = fields[:5], fields[5]
            refs, _, stat = rest.partition("\n")
            commit = {"hash": head[0].strip(), "short": head[1], "author": head[2],
                      "time": int(head[3]) if head[3].isdigit() else None, "subject": head[4],
                      "refs": [ref.strip() for ref in refs.split(",") if ref.strip()],
                      "files": None, "added": None, "removed": None}
            commit.update(_shortstat(stat))
            commits.append(commit)
        return {"repo": True, "top": top, "commits": commits}

    def branches(self) -> Dict[str, Any]:
        top = self._require_top()
        fmt = "%(refname)%1f%(refname:short)%1f%(upstream:short)%1f%(HEAD)%1f%(committerdate:unix)"
        result = self._git(["for-each-ref", f"--format={fmt}", f"--count={MAX_BRANCHES}",
                            "--sort=-committerdate", "refs/heads", "refs/remotes"], cwd=top)
        if result.code != 0:
            raise GitError(500, _message(result, "Git couldn't list the branches"))
        local, remote = [], []
        for line in result.out.splitlines():
            fields = line.split("\x1f")
            if len(fields) != 5:
                continue
            full, short, upstream, head, when = fields
            item = {"name": short, "upstream": upstream or None, "current": head == "*",
                    "time": int(when) if when.isdigit() else None}
            if full.startswith("refs/heads/"):
                local.append(item)
            elif not full.endswith("/HEAD"):
                remote.append(item)
        return {"top": top, "local": local, "remote": remote}

    # Changes

    def change(self, action: str, body: Dict[str, Any]) -> Dict[str, Any]:
        if action == "init":
            return self._init()
        top = self._require_top()
        with _repo_lock(f"{self._machine}:{top}"):
            if action == "stage":
                self._stage(top, body)
            elif action == "unstage":
                self._unstage(top, body)
            elif action == "discard":
                self._discard(top, body)
            elif action == "commit":
                return {**self._commit(top, body), "status": self.status()}
            elif action == "switch":
                self._switch(top, body)
            elif action == "pull":
                self._network(top, ["pull", "--ff-only", "--no-rebase"], "Git couldn't pull")
            elif action == "push":
                self._push(top)
            elif action == "fetch":
                self._network(top, ["fetch", "--prune"], "Git couldn't fetch")
            else:
                raise GitError(404, "Not found")
        return {"status": self.status()}

    def _check(self, result: Result, fallback: str) -> Result:
        if result.code != 0:
            raise GitError(409 if "conflict" in (result.err or "").lower() else 500, _message(result, fallback))
        return result

    def _init(self) -> Dict[str, Any]:
        if self._top() is not None:
            raise GitError(409, "This folder is already in a Git repository")
        self._check(self._git(["init", "-q"], timeout=CHANGE_TIMEOUT_S), "Git couldn't start a repository here")
        return {"status": self.status()}

    def _stage(self, top: str, body: Dict[str, Any]) -> None:
        if body.get("all") is True:
            args = ["add", "-A"]
        else:
            args = ["add", "-A", "--", *self._rels(top, body.get("paths"))]
        self._check(self._git(args, timeout=CHANGE_TIMEOUT_S, cwd=top), "Git couldn't stage that")

    def _has_head(self, top: str) -> bool:
        return self._git(["rev-parse", "--verify", "-q", "HEAD"], cwd=top).code == 0

    def _unstage(self, top: str, body: Dict[str, Any]) -> None:
        paths = None if body.get("all") is True else self._rels(top, body.get("paths"))
        if self._has_head(top):
            args = ["reset", "-q", "HEAD", "--"] + (paths or [])
        else:
            args = ["rm", "--cached", "-r", "-q", "--"] + (paths or ["."])
        result = self._git(args, timeout=CHANGE_TIMEOUT_S, cwd=top)
        # reset exits 1 when files stay modified: that's what unstaging is.
        if result.code not in (0, 1) or (result.code == 1 and "error" in result.err.lower()):
            raise GitError(500, _message(result, "Git couldn't unstage that"))

    def _discard(self, top: str, body: Dict[str, Any]) -> None:
        paths = self._rels(top, body.get("paths"))
        has_head = self._has_head(top)
        # Back to the last commit: what HEAD has is checked out from it (a staged deletion included);
        # what only the index has (a staged addition, a rename's new name) is unstaged, then removed.
        in_head: set = set()
        if has_head:
            listed = self._git(["ls-tree", "-r", "--name-only", "-z", "HEAD", "--", *paths], cwd=top)
            in_head = set(_split_z(listed.out)) if listed.code == 0 else set()
        indexed = self._git(["ls-files", "-z", "--", *paths], cwd=top)
        in_index = set(_split_z(indexed.out)) if indexed.code == 0 else set()
        restore = [rel for rel in paths if rel in in_head]
        index_only = [rel for rel in paths if rel not in in_head and rel in in_index]
        untracked = [rel for rel in paths if rel not in in_head and rel not in in_index]
        # A staged rename comes back under its old name too.
        for rel in index_only:
            source = self._renamed_from(top, rel)
            if source is not None and source not in restore:
                restore.append(source)
        if index_only:
            self._check(self._git(["rm", "-q", "--cached", "-f", "--", *index_only], timeout=CHANGE_TIMEOUT_S, cwd=top),
                        "Git couldn't discard those changes")
            untracked += index_only
        if restore and has_head:
            self._check(self._git(["checkout", "-q", "HEAD", "--", *restore], timeout=CHANGE_TIMEOUT_S, cwd=top),
                        "Git couldn't discard those changes")
        elif restore:
            self._check(self._git(["checkout", "-q", "--", *restore], timeout=CHANGE_TIMEOUT_S, cwd=top),
                        "Git couldn't discard those changes")
        if untracked:
            self._check(self._git(["clean", "-f", "-q", "--", *untracked], timeout=CHANGE_TIMEOUT_S, cwd=top),
                        "Git couldn't remove those new files")

    def _commit(self, top: str, body: Dict[str, Any]) -> Dict[str, Any]:
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            raise GitError(400, "A commit message is required")
        if len(message) > MAX_MESSAGE_CHARS:
            raise GitError(400, "That commit message is too long")
        # Exit 0 means nothing is staged, a repository with no commit yet included.
        if self._git(["diff", "--cached", "--quiet"], cwd=top).code == 0:
            if body.get("all") is not True:
                raise GitError(409, "Nothing is staged to commit")
            self._check(self._git(["add", "-A"], timeout=CHANGE_TIMEOUT_S, cwd=top), "Git couldn't stage that")
            if self._git(["diff", "--cached", "--quiet"], cwd=top).code == 0:
                raise GitError(409, "Nothing changed to commit")
        result = self._git(["commit", "-q", "-F", "-"], timeout=CHANGE_TIMEOUT_S, cwd=top, stdin=message)
        if result.code != 0:
            text = (result.err + result.out).lower()
            if "please tell me who you are" in text or "author identity unknown" in text:
                raise GitError(409, "Git doesn't know who you are on this host: set user.name and user.email")
            raise GitError(500, _message(result, "Git couldn't commit"))
        head = self._git(["rev-parse", "--short", "HEAD"], cwd=top)
        return {"commit": head.out.strip() or None}

    def _switch(self, top: str, body: Dict[str, Any]) -> None:
        name = body.get("branch")
        if not isinstance(name, str) or not name.strip() or name.startswith("-"):
            raise GitError(400, "A branch name is required")
        name = name.strip()
        valid = self._git(["check-ref-format", "--branch", name], cwd=top)
        if valid.code != 0:
            raise GitError(400, "That isn't a valid branch name")
        if body.get("create") is True:
            args = ["checkout", "-q", "-b", name]
        else:
            args = ["checkout", "-q", name]
        result = self._git(args, timeout=CHANGE_TIMEOUT_S, cwd=top)
        if result.code != 0:
            text = result.err.lower()
            if "would be overwritten" in text:
                raise GitError(409, "Your changes to some files would be lost: commit or discard them first")
            if "already exists" in text:
                raise GitError(409, "A branch with that name is already there")
            raise GitError(500, _message(result, "Git couldn't switch branches"))

    def _network(self, top: str, args: List[str], fallback: str) -> None:
        result = self._git(args, timeout=NETWORK_TIMEOUT_S, cwd=top)
        if result.code != 0:
            text = (result.err or "").lower()
            if ("terminal prompts disabled" in text or "could not read username" in text
                    or "permission denied" in text or "authentication failed" in text):
                raise GitError(401, "Git needs a sign-in on the Hermes host for this remote: "
                                    "run it once in a terminal there")
            if ("not possible to fast-forward" in text or "diverging" in text or "non-fast-forward" in text
                    or "fetch first" in text or "[rejected]" in text):
                raise GitError(409, "This branch and its remote have both changed: pull in a terminal to merge them")
            if "no configured push destination" in text or "does not appear to be a git repository" in text:
                raise GitError(409, "This repository has no remote to use")
            raise GitError(502, _message(Result(result.code, result.out, result.err), fallback))

    def _push(self, top: str) -> None:
        upstream = self._git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=top)
        if upstream.code == 0:
            self._network(top, ["push"], "Git couldn't push")
            return
        remotes = self._git(["remote"], cwd=top)
        names = [line.strip() for line in remotes.out.splitlines() if line.strip()]
        if not names:
            raise GitError(409, "This repository has no remote to push to")
        remote = "origin" if "origin" in names else names[0]
        self._network(top, ["push", "-u", remote, "HEAD"], "Git couldn't push")


def _shortstat(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    files = re.search(r"(\d+) files? changed", text or "")
    added = re.search(r"(\d+) insertions?\(\+\)", text or "")
    removed = re.search(r"(\d+) deletions?\(-\)", text or "")
    if files:
        out["files"] = int(files.group(1))
        out["added"] = int(added.group(1)) if added else 0
        out["removed"] = int(removed.group(1)) if removed else 0
    return out


def for_workspace(workspace: Any, root_raw: Any, sensitive: Callable[[str], bool],
                  run: Optional[Callable[..., Result]] = None) -> Git:
    """The repository at a Folder root: the workspace checks the root with its
    own rules first (a hosted install's one folder, an SSH profile's paths)."""
    try:
        root = workspace.root(root_raw)
    except Exception as exc:
        status = getattr(exc, "status", None)
        if status is not None:
            raise GitError(status, str(exc))
        raise
    fs = getattr(workspace, "_fs", None)
    if fs is not None:
        return Git(root, run or SshRunner(fs), sensitive, posix=True, machine="ssh")
    return Git(root, run or LocalRunner(), sensitive)


__all__: Tuple[str, ...] = ("Git", "GitError", "LocalRunner", "SshRunner", "for_workspace")
