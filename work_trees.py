"""Git worktrees for workstreams (designs/bot-mode-coordinator M4).

A coding workstream can work in a git worktree of its own, so two of a
bot's workstreams in one repo don't write over each other. The worktree is
Hermes' own subagent worktree (``tools/subagent_worktree.py``), reused, not
copied: it lives under ``<repo>/.worktrees/``, on a branch of its own, with
Hermes' hardened git and its worktree sweep looking after leftovers.

A worktree outlives the workstream's turns. Resolving the workstream
removes it only when it holds nothing (no commits, a clean tree); anything
else is kept for the user, and Hermes' sweep archives it once it's idle
(restorable with ``hermes worktree restore``). A workstream that's sent
words again after its worktree went gets a fresh one at the same path, on
the same branch name.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

NO_HERMES = "This Hermes can't make worktrees (it has no tools/subagent_worktree.py)."
NOT_LOCAL = ("Worktrees need Hermes' local terminal; on docker, ssh or modal the worktree isn't where the "
             "terminal runs.")
NOT_A_REPO = "That folder isn't in a git repository, so it can't have a worktree."
NO_FOLDER = "Name the repository's folder with cwd to work in a worktree."
FAILED = ("Git couldn't make a worktree there (a repository with no commits yet, or one whose filters Hermes can't "
          "run safely).")


class WorktreeError(Exception):
    pass


def _hermes() -> Any:
    try:
        from tools import subagent_worktree
    except Exception:  # noqa: BLE001 — an older Hermes, or none (tests)
        return None
    return subagent_worktree


def repo_root(cwd: Any) -> str:
    """The git toplevel of ``cwd``, or '' (not a repo, or no way to tell)."""
    hermes = _hermes()
    if hermes is None or not isinstance(cwd, str) or not cwd.strip():
        return ""
    try:
        return hermes.resolve_repo_root(cwd.strip()) or ""
    except Exception:  # noqa: BLE001
        logger.debug("conduit_push: couldn't read a workstream folder's repository", exc_info=True)
        return ""


def name_for(profile: str, ref: str, token: str) -> str:
    """The worktree's id: its folder is .worktrees/subagent-<id>, its branch
    hermes-subagent/subagent-<id>."""
    raw = f"work-{profile}-{ref}-{token}".lower()
    return re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-")[:80]


def create(cwd: str, name: str) -> dict:
    """A worktree of the repository ``cwd`` is in. Raises WorktreeError,
    whose message says why, for the coordinator."""
    hermes = _hermes()
    if hermes is None:
        raise WorktreeError(NO_HERMES)
    if not cwd:
        raise WorktreeError(NO_FOLDER)
    if not hermes.local_backend_active():
        raise WorktreeError(NOT_LOCAL)
    if not repo_root(cwd):
        raise WorktreeError(NOT_A_REPO)
    try:
        info = hermes.create_subagent_worktree(cwd, name)
    except Exception as error:  # noqa: BLE001
        logger.warning("conduit_push: making a workstream's worktree failed", exc_info=True)
        raise WorktreeError(FAILED) from error
    if not info or not info.get("path"):
        raise WorktreeError(FAILED)
    return {**info, "name": name}


def restore(info: dict) -> bool:
    """A worktree that was removed (resolved clean, or swept) made again at
    its path, for a workstream that's sent words again. True when it's
    there now."""
    path = str(info.get("path") or "")
    if not path or os.path.isdir(path):
        return bool(path)
    hermes = _hermes()
    if hermes is None or not info.get("repo_root") or not info.get("name"):
        return False
    try:
        made = hermes.create_subagent_worktree(info["repo_root"], info["name"])
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: making a workstream's worktree again failed", exc_info=True)
        return False
    return bool(made) and os.path.normpath(str(made.get("path"))) == os.path.normpath(path)


def tidy(info: dict) -> dict:
    """On resolve: removes the worktree only when it holds nothing; the
    answer says what's left (``commits``, ``dirty``, ``pruned``)."""
    hermes = _hermes()
    if hermes is None or not info.get("path"):
        return {}
    try:
        seen = hermes.finalize_subagent_worktree(info, prune=False)
        if seen.get("inspection_failed") or seen.get("commits") or seen.get("dirty"):
            return {key: seen.get(key) for key in ("path", "branch", "commits", "dirty", "pruned")}
        done = hermes.finalize_subagent_worktree(info, prune=True)
        return {key: done.get(key) for key in ("path", "branch", "commits", "dirty", "pruned")}
    except Exception:  # noqa: BLE001 — kept: Hermes' sweep looks after it
        logger.warning("conduit_push: tidying a workstream's worktree failed", exc_info=True)
        return {}


def brief(info: dict) -> str:
    """What the workstream's brief says about its worktree."""
    return (f"\n\nYou work in a git worktree of your own at {info.get('path')}, on the branch {info.get('branch')} "
            "(your terminal starts there). Make every edit and run every command inside it, never in the main "
            "checkout, and commit your changes to your branch when you're done; the user decides whether to merge "
            "it. Say the branch in your report.")
