"""Factory for :class:`~monkeybot.core.workspace.protocol.WorkspaceStorage` from a URI string."""

from __future__ import annotations

from pathlib import Path

from monkeybot.core.workspace.local import LocalWorkspaceStorage
from monkeybot.core.workspace.protocol import WorkspaceStorage


def _split_bucket_and_prefix(scheme_body: str) -> tuple[str, str]:
    """Parse ``bucket`` or ``bucket/prefix/parts`` after the ``scheme://`` portion."""
    body = scheme_body.strip().lstrip("/")
    if not body:
        raise ValueError("URI must include a bucket name")
    if "/" in body:
        bucket, prefix = body.split("/", 1)
        return bucket.strip(), prefix.strip().strip("/")
    return body.strip(), ""


def create_workspace_storage(uri: str) -> WorkspaceStorage:
    """Return storage for ``local://``, ``gcs://``, ``s3://``, or a bare filesystem path.

    Lazy-imports optional cloud SDKs so local-only installs pay no import cost.
    """
    u = uri.strip()
    if not u:
        raise ValueError("workspace storage URI is empty")

    if u.startswith("gcs://"):
        from monkeybot.core.workspace.gcs import GCSWorkspaceStorage

        bucket, prefix = _split_bucket_and_prefix(u[len("gcs://") :])
        return GCSWorkspaceStorage(bucket=bucket, prefix=prefix)

    if u.startswith("s3://"):
        from monkeybot.core.workspace.s3 import S3WorkspaceStorage

        bucket, prefix = _split_bucket_and_prefix(u[len("s3://") :])
        return S3WorkspaceStorage(bucket=bucket, prefix=prefix)

    path_str = u.removeprefix("local://").strip()
    root = Path(path_str).expanduser()
    return LocalWorkspaceStorage(root)


__all__ = ["create_workspace_storage"]
