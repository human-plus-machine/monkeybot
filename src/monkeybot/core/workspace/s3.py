"""Amazon S3 :class:`WorkspaceStorage` (``boto3``)."""

from __future__ import annotations

import asyncio
import logging

from botocore.exceptions import ClientError

from monkeybot.core.workspace.protocol import WorkspaceStorage

_log = logging.getLogger(__name__)


class S3WorkspaceStorage:
    """Object-prefix layout: ``s3://bucket/{prefix}/{path}``."""

    def __init__(self, bucket: str, prefix: str = "") -> None:
        import boto3

        self._s3 = boto3.client("s3")
        self._bucket = bucket
        self._prefix = prefix.strip().strip("/").replace("\\", "/")
        if self._prefix:
            self._prefix = self._prefix + "/"

    def _key(self, path: str) -> str:
        key = path.strip().replace("\\", "/").lstrip("/")
        return self._prefix + key if self._prefix else key

    async def read_text(self, path: str) -> str:
        key = self._key(path)

        def _read() -> str:
            resp = self._s3.get_object(Bucket=self._bucket, Key=key)
            body: bytes = resp["Body"].read()
            return body.decode("utf-8")

        try:
            return await asyncio.to_thread(_read)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                raise FileNotFoundError(key) from exc
            raise

    async def write_text(self, path: str, content: str) -> None:
        key = self._key(path)

        def _write() -> None:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content.encode("utf-8"),
                ContentType="text/plain; charset=utf-8",
            )

        await asyncio.to_thread(_write)

    async def append_text(self, path: str, content: str) -> None:
        key = self._key(path)

        def _append() -> None:
            try:
                resp = self._s3.get_object(Bucket=self._bucket, Key=key)
                cur = resp["Body"].read().decode("utf-8")
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code not in ("404", "NoSuchKey", "NotFound"):
                    raise
                cur = ""
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=(cur + content).encode("utf-8"),
                ContentType="text/plain; charset=utf-8",
            )

        await asyncio.to_thread(_append)

    async def exists(self, path: str) -> bool:
        key = self._key(path)

        def _head() -> bool:
            try:
                self._s3.head_object(Bucket=self._bucket, Key=key)
                return True
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchKey", "NotFound"):
                    return False
                raise

        return await asyncio.to_thread(_head)

    async def list_files(self, prefix: str = "") -> list[str]:
        pfx = self._key(prefix)

        def _list() -> list[str]:
            out: list[str] = []
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=pfx):
                for obj in page.get("Contents", []):
                    name = obj["Key"]
                    if name.endswith("/"):
                        continue
                    rel = name[len(self._prefix) :] if self._prefix else name
                    out.append(rel.lstrip("/"))
            return sorted(out)

        return await asyncio.to_thread(_list)

    async def delete(self, path: str) -> None:
        key = self._key(path)

        def _del() -> None:
            self._s3.delete_object(Bucket=self._bucket, Key=key)

        try:
            await asyncio.to_thread(_del)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in ("404", "NoSuchKey", "NotFound"):
                raise

    async def move(self, src: str, dest: str) -> None:
        sk = self._key(src)
        dk = self._key(dest)

        def _copy_delete() -> None:
            self._s3.copy_object(
                Bucket=self._bucket,
                CopySource={"Bucket": self._bucket, "Key": sk},
                Key=dk,
            )
            try:
                self._s3.delete_object(Bucket=self._bucket, Key=sk)
            except Exception as exc:
                _log.warning("s3 move: delete source failed after copy src=%s dest=%s: %r", sk, dk, exc)

        await asyncio.to_thread(_copy_delete)

    async def gc_prefix(self, prefix: str, max_age_sec: float) -> dict[str, int]:
        del prefix, max_age_sec
        _log.info(
            "s3 gc_prefix: skipped — configure S3 lifecycle rules on the bucket for prefix cleanup"
        )
        return {"scanned": 0, "deleted": 0, "errors": 0}


__all__ = ["S3WorkspaceStorage"]
