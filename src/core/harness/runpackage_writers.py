"""RunPackage writers: Local / GCS / S3. Layout:

    {sink}/{yyyy}/{mm}/{dd}/{run_id}.json

Writers refuse to overwrite an existing run_id (enforces immutability).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

from .events import Principal
from .runpackage import RunPackage, RunPackageRef


@runtime_checkable
class RunPackageWriter(Protocol):
    async def write(self, pkg: RunPackage) -> str: ...
    async def read(self, run_id: str) -> RunPackage | None: ...
    async def index(
        self, *, principal: Principal | None = None, limit: int = 100
    ) -> list[RunPackageRef]: ...


def _layout_path(base: str, pkg: RunPackage) -> str:
    d = pkg.started_at
    return f"{base.rstrip('/')}/{d.year:04d}/{d.month:02d}/{d.day:02d}/{pkg.run_id}.json"


@dataclass
class LocalRunPackageWriter(RunPackageWriter):
    sink_uri: str

    def __post_init__(self) -> None:
        self._base = Path(self.sink_uri)

    async def write(self, pkg: RunPackage) -> str:
        path = Path(_layout_path(str(self._base), pkg))
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"RunPackage already exists: {path}")
        await asyncio.to_thread(
            path.write_text, pkg.model_dump_json(indent=2)
        )
        return str(path)

    async def read(self, run_id: str) -> RunPackage | None:
        matches = list(self._base.rglob(f"{run_id}.json"))
        if not matches:
            return None
        raw = await asyncio.to_thread(matches[0].read_text)
        return RunPackage.model_validate_json(raw)

    async def index(
        self, *, principal: Principal | None = None, limit: int = 100
    ) -> list[RunPackageRef]:
        out: list[RunPackageRef] = []
        if not self._base.exists():
            return out
        for path in sorted(self._base.rglob("*.json")):
            try:
                raw = await asyncio.to_thread(path.read_text)
                pkg = RunPackage.model_validate_json(raw)
            except Exception:
                continue
            if principal is not None and pkg.principal.id != principal.id:
                continue
            out.append(
                RunPackageRef(
                    run_id=pkg.run_id,
                    session_id=pkg.session_id,
                    principal_id=pkg.principal.id,
                    uri=str(path),
                    created_at=pkg.started_at,
                )
            )
            if len(out) >= limit:
                break
        return out


@dataclass
class GCSRunPackageWriter(RunPackageWriter):
    """Lazy GCS writer. sink_uri like ``gs://bucket/prefix``."""

    sink_uri: str
    _client: object | None = None

    def _get_client(self):  # type: ignore[no-untyped-def]
        if self._client is None:
            from google.cloud import storage  # type: ignore

            self._client = storage.Client()
        return self._client

    async def write(self, pkg: RunPackage) -> str:
        parsed = urlparse(self.sink_uri)
        bucket = self._get_client().bucket(parsed.netloc)  # type: ignore[attr-defined]
        blob_path = _layout_path(parsed.path.strip("/"), pkg).lstrip("/")
        blob = bucket.blob(blob_path)
        if await asyncio.to_thread(blob.exists):
            raise FileExistsError(f"gs://{parsed.netloc}/{blob_path} already exists")
        await asyncio.to_thread(
            blob.upload_from_string,
            pkg.model_dump_json(),
            content_type="application/json",
        )
        return f"gs://{parsed.netloc}/{blob_path}"

    async def read(self, run_id: str) -> RunPackage | None:
        parsed = urlparse(self.sink_uri)
        client = self._get_client()
        blobs = await asyncio.to_thread(
            lambda: list(client.list_blobs(parsed.netloc, prefix=parsed.path.strip("/")))  # type: ignore[attr-defined]
        )
        for b in blobs:
            if b.name.endswith(f"{run_id}.json"):
                raw = await asyncio.to_thread(b.download_as_text)
                return RunPackage.model_validate_json(raw)
        return None

    async def index(
        self, *, principal: Principal | None = None, limit: int = 100
    ) -> list[RunPackageRef]:
        # Minimal impl: lists, loads, filters. Consumer should front this with a DB index in prod.
        parsed = urlparse(self.sink_uri)
        client = self._get_client()
        blobs = await asyncio.to_thread(
            lambda: list(client.list_blobs(parsed.netloc, prefix=parsed.path.strip("/")))  # type: ignore[attr-defined]
        )
        out: list[RunPackageRef] = []
        for b in blobs[:limit]:
            if not b.name.endswith(".json"):
                continue
            try:
                raw = await asyncio.to_thread(b.download_as_text)
                pkg = RunPackage.model_validate_json(raw)
            except Exception:
                continue
            if principal is not None and pkg.principal.id != principal.id:
                continue
            out.append(
                RunPackageRef(
                    run_id=pkg.run_id,
                    session_id=pkg.session_id,
                    principal_id=pkg.principal.id,
                    uri=f"gs://{parsed.netloc}/{b.name}",
                    created_at=pkg.started_at,
                )
            )
        return out


@dataclass
class S3RunPackageWriter(RunPackageWriter):
    """Lazy S3 writer. sink_uri like ``s3://bucket/prefix``."""

    sink_uri: str
    _client: object | None = None

    def _get_client(self):  # type: ignore[no-untyped-def]
        if self._client is None:
            import boto3  # type: ignore

            self._client = boto3.client("s3")
        return self._client

    async def write(self, pkg: RunPackage) -> str:
        parsed = urlparse(self.sink_uri)
        bucket = parsed.netloc
        key = _layout_path(parsed.path.strip("/"), pkg).lstrip("/")

        client = self._get_client()

        def _put() -> None:
            try:
                client.head_object(Bucket=bucket, Key=key)  # type: ignore[attr-defined]
                raise FileExistsError(f"s3://{bucket}/{key} already exists")
            except Exception as exc:  # ClientError 404 means not found → OK to put
                if "404" not in str(exc) and "Not Found" not in str(exc):
                    if isinstance(exc, FileExistsError):
                        raise
            client.put_object(  # type: ignore[attr-defined]
                Bucket=bucket, Key=key, Body=pkg.model_dump_json(), ContentType="application/json"
            )

        await asyncio.to_thread(_put)
        return f"s3://{bucket}/{key}"

    async def read(self, run_id: str) -> RunPackage | None:
        parsed = urlparse(self.sink_uri)
        bucket = parsed.netloc
        client = self._get_client()

        def _scan() -> RunPackage | None:
            paginator = client.get_paginator("list_objects_v2")  # type: ignore[attr-defined]
            for page in paginator.paginate(Bucket=bucket, Prefix=parsed.path.strip("/")):
                for obj in page.get("Contents", []):
                    if obj["Key"].endswith(f"{run_id}.json"):
                        body = client.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()  # type: ignore[attr-defined]
                        return RunPackage.model_validate_json(body)
            return None

        return await asyncio.to_thread(_scan)

    async def index(
        self, *, principal: Principal | None = None, limit: int = 100
    ) -> list[RunPackageRef]:
        return []  # prod consumers should index via DynamoDB/RDS


class DisabledRunPackageWriter(RunPackageWriter):
    async def write(self, pkg: RunPackage) -> str:
        return ""

    async def read(self, run_id: str) -> RunPackage | None:
        return None

    async def index(
        self, *, principal: Principal | None = None, limit: int = 100
    ) -> list[RunPackageRef]:
        return []
