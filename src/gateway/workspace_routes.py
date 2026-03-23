"""FastAPI routes for workspace file API (read/write/replace/glob/grep)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..core.workspace_service import WorkspaceError, WorkspaceFileService

router = APIRouter(tags=["workspace"])


def get_workspace_service(request: Request) -> WorkspaceFileService:
    root: Path = request.app.state.workspace_repo_root
    settings = getattr(request.app.state, "workspace_settings", None)
    return WorkspaceFileService(root, settings=settings)


def _workspace_api_key() -> str:
    return (os.getenv("WORKSPACE_FILE_API_KEY") or "").strip()


def verify_workspace_api_key(request: Request) -> None:
    key = _workspace_api_key()
    if not key:
        return
    header = request.headers.get("X-Workspace-File-Key") or ""
    auth = request.headers.get("Authorization") or ""
    bearer = ""
    if auth.lower().startswith("bearer "):
        bearer = auth[7:].strip()
    if header == key or bearer == key:
        return
    raise HTTPException(status_code=401, detail="Invalid or missing workspace API key")


def _handle(exc: WorkspaceError) -> HTTPException:
    code = exc.code
    status = 400
    if code == "not_found":
        status = 404
    elif code == "payload_too_large":
        status = 413
    return HTTPException(status_code=status, detail={"message": str(exc), "code": code})


class ReadBody(BaseModel):
    path: str
    offset: int = Field(default=1, ge=1)
    limit: int | None = None


class WriteBody(BaseModel):
    path: str
    content: str = ""


class ReplaceBody(BaseModel):
    path: str
    old_string: str = ""
    new_string: str = ""


class GlobBody(BaseModel):
    pattern: str
    root: str | None = None


class GrepBody(BaseModel):
    pattern: str
    root: str | None = None
    ignore_case: bool = False
    glob: str | None = None
    max_matches: int | None = Field(default=None, ge=1)


@router.post("/read")
def workspace_read(
    body: ReadBody,
    _: Annotated[None, Depends(verify_workspace_api_key)],
    svc: Annotated[WorkspaceFileService, Depends(get_workspace_service)],
):
    try:
        return svc.read_file(body.path, offset=body.offset, limit=body.limit)
    except WorkspaceError as e:
        raise _handle(e) from e


@router.put("/write")
def workspace_write(
    body: WriteBody,
    _: Annotated[None, Depends(verify_workspace_api_key)],
    svc: Annotated[WorkspaceFileService, Depends(get_workspace_service)],
):
    try:
        return svc.write_file(body.path, body.content)
    except WorkspaceError as e:
        raise _handle(e) from e


@router.post("/replace")
def workspace_replace(
    body: ReplaceBody,
    _: Annotated[None, Depends(verify_workspace_api_key)],
    svc: Annotated[WorkspaceFileService, Depends(get_workspace_service)],
):
    try:
        return svc.replace_in_file(body.path, body.old_string, body.new_string)
    except WorkspaceError as e:
        raise _handle(e) from e


@router.post("/glob")
def workspace_glob(
    body: GlobBody,
    _: Annotated[None, Depends(verify_workspace_api_key)],
    svc: Annotated[WorkspaceFileService, Depends(get_workspace_service)],
):
    try:
        return svc.glob_paths(body.pattern, root=body.root)
    except WorkspaceError as e:
        raise _handle(e) from e


@router.post("/grep")
def workspace_grep_http(
    body: GrepBody,
    _: Annotated[None, Depends(verify_workspace_api_key)],
    svc: Annotated[WorkspaceFileService, Depends(get_workspace_service)],
):
    try:
        return svc.grep(
            body.pattern,
            root=body.root,
            ignore_case=body.ignore_case,
            file_glob=body.glob,
            max_matches=body.max_matches,
        )
    except WorkspaceError as e:
        raise _handle(e) from e


def mount_workspace_routes(app: object) -> None:
    """Include ``/workspace/v1/*`` when ``app.state.workspace_repo_root`` is set."""
    root = getattr(app.state, "workspace_repo_root", None)
    if root is None:
        return
    app.include_router(router, prefix="/workspace/v1")
