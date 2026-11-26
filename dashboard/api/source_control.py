"""Source control: Git in a chat's folder, for the coding workspace's Changes view."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request, Response

from . import core
from . import workspace

logger = core.logger
router = core.router


# --- Git -------------------------------------------------------------------------
#
# Conduit for desktop's workbench (issue 452) shows what changed in the open
# chat's folder and lets a person stage, commit, switch branches, pull and
# push there (workspace_git.py in the plugin's folder). The folder is a root
# the Folder tab reaches: workspace._workspace(profile) checks it with the
# same rules (a hosted install's one folder, an SSH profile's machine, a
# Docker profile's mount), and Git runs where those files are.
#
# GET  /workspace/git/status    ?root=            the branch and every changed file
# GET  /workspace/git/diff      ?root=&path=&staged=  one file's diff
# GET  /workspace/git/log       ?root=&limit=&path=   commits, newest first
# GET  /workspace/git/branches  ?root=            local and remote branches
# POST /workspace/git/{action}  {root, ...}       init, stage, unstage, discard,
#                                                 commit, switch, pull, push, fetch
#
# A change answers with the repository's status after it, so the view
# redraws from one answer.

GIT_LIMIT = 240
GIT_WINDOW_S = 60.0
# Above every step's own limit (pull and push have the longest), so the
# step's own message reaches the client first.
GIT_TIMEOUT_S = 100.0
GIT_MAX_BODY_BYTES = 64 * 1024
GIT_ACTIONS = frozenset({"init", "stage", "unstage", "discard", "commit", "switch", "pull", "push", "fetch"})
_git_limiter = core._MintLimiter(GIT_LIMIT, GIT_WINDOW_S, message="Too many Git requests; try again shortly")
_git_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="conduit-git")
NO_STORE = {"Cache-Control": "no-store"}


def _git_module() -> Any:
    return core._plugin_package_module("workspace_git")


def _sensitive() -> Callable[[str], bool]:
    module = workspace._workspace_files()
    try:
        from hermes_cli.web_routers.files import _is_sensitive_path
    except Exception:
        _is_sensitive_path = None

    def sensitive(path: str) -> bool:
        if module.default_sensitive(path):
            return True
        return bool(_is_sensitive_path(Path(path))) if _is_sensitive_path is not None else False

    return sensitive


def _repo(profile: Optional[str], root: str) -> Any:
    """The Git repository at ``root``, as this profile's workspace reaches it."""
    return _git_module().for_workspace(workspace._workspace(profile), root, _sensitive())


async def _git_call(profile: Optional[str], root: Optional[str], op: Callable[[Any], Dict[str, Any]],
                    name: str) -> Dict[str, Any]:
    if not isinstance(root, str) or not root.strip():
        raise HTTPException(status_code=400, detail="root is required", headers=NO_STORE)
    await core._scoped_acquire(_git_limiter, profile)
    started = threading.Event()

    def job() -> Dict[str, Any]:
        started.set()
        return op(_repo(profile, root))

    try:
        result = await asyncio.wait_for(core._run_scoped(profile, job, _git_executor), timeout=GIT_TIMEOUT_S)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **NO_STORE}
        raise
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=NO_STORE)
    except asyncio.TimeoutError:
        detail = ("Git is taking too long and may still finish; check the changes before trying again"
                  if started.is_set() and name not in {"status", "diff", "log", "branches"}
                  else "Git took too long; try again")
        raise HTTPException(status_code=504, detail=detail, headers=NO_STORE)
    except Exception as exc:
        module = core._plugin_package_module("workspace_git")
        workspace_error = getattr(workspace._workspace_module, "WorkspaceError", None)
        if isinstance(exc, module.GitError) or (workspace_error is not None and isinstance(exc, workspace_error)):
            raise HTTPException(status_code=exc.status, detail=str(exc), headers=NO_STORE)
        raise core._unexpected(f"git-{name}", exc, feature="Source control")
    return {"ok": True, **result}


@router.get("/workspace/git/status")
async def get_git_status(response: Response, root: Optional[str] = None,
                         profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await _git_call(profile, root, lambda git: git.status(), "status")


@router.get("/workspace/git/diff")
async def get_git_diff(response: Response, root: Optional[str] = None, path: Optional[str] = None,
                       staged: bool = False, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await _git_call(profile, root, lambda git: git.diff(path, staged), "diff")


@router.get("/workspace/git/log")
async def get_git_log(response: Response, root: Optional[str] = None, limit: int = 50, path: Optional[str] = None,
                      profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await _git_call(profile, root, lambda git: git.log(limit, path), "log")


@router.get("/workspace/git/branches")
async def get_git_branches(response: Response, root: Optional[str] = None,
                           profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await _git_call(profile, root, lambda git: git.branches(), "branches")


@router.post("/workspace/git/{action}")
async def post_git(action: str, request: Request, response: Response,
                   profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    if action not in GIT_ACTIONS:
        raise HTTPException(status_code=404, detail="Not found", headers=NO_STORE)
    body = await core._read_json_body(request, GIT_MAX_BODY_BYTES)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be an object", headers=NO_STORE)
    root = body.get("root")
    return await _git_call(profile, root if isinstance(root, str) else None,
                           lambda git: git.change(action, body), action)
