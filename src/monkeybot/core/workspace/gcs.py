"""Google Cloud Storage :class:`WorkspaceStorage` (``google-cloud-storage``)."""

from __future__ import annotations

import asyncio
import logging

from google.cloud.exceptions import NotFound

from monkeybot.core.workspace.protocol import WorkspaceStorage

_log = logging.getLogger(__name__)


class GCSWorkspaceStorage:
    """Object-prefix layout: ``gs://bucket/{prefix}/{path}``."""

    def __init__(self, bucket: str, prefix: str = "") -> None:
        from google.cloud import storage  # type: ignore[attr-defined]

        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket)
        self._prefix = prefix.strip().strip("/").replace("\\", "/")
        if self._prefix:
            self._prefix = self._prefix + "/"

    def _key(self, path: str) -> str:
        key = path.strip().replace("\\", "/").lstrip("/")
        return self._prefix + key if self._prefix else key

    async def read_text(self, path: str) -> str:
        key = self._key(path)

        def _read() -> str:
            blob = self._bucket.blob(key)
            data = blob.download_as_bytes()
            text: str = data.decode("utf-8")
            return text

        try:
            return await asyncio.to_thread(_read)
        except NotFound as exc:
            raise FileNotFoundError(key) from exc

    async def write_text(self, path: str, content: str) -> None:
        key = self._key(path)

        def _write() -> None:
            blob = self._bucket.blob(key)
            blob.upload_from_string(content, content_type="text/plain; charset=utf-8")

        await asyncio.to_thread(_write)

    async def append_text(self, path: str, content: str) -> None:
        key = self._key(path)
        blob = self._bucket.blob(key)

        def _append() -> None:
            try:
                cur = blob.download_as_text(encoding="utf-8")
            except NotFound:
                cur = ""
            blob.upload_from_string(cur + content, content_type="text/plain; charset=utf-8")

        await asyncio.to_thread(_append)

    async def exists(self, path: str) -> bool:
        key = self._key(path)

        def _exists() -> bool:
            return bool(self._bucket.blob(key).exists())

        return await asyncio.to_thread(_exists)

    async def list_files(self, prefix: str = "") -> list[str]:
        pfx = self._key(prefix).rstrip("/")
        if pfx:
            pfx = pfx + "/"

        def _list() -> list[str]:
            out: list[str] = []
            for blob in self._client.list_blobs(self._bucket.name, prefix=pfx):
                name = blob.name
                if name.endswith("/"):
                    continue
                rel = name[len(self._prefix) :] if self._prefix else name
                out.append(rel.lstrip("/"))
            return sorted(out)

        return await asyncio.to_thread(_list)

    async def delete(self, path: str) -> None:
        key = self._key(path)

        def _del() -> None:
            self._bucket.blob(key).delete()

        try:
            await asyncio.to_thread(_del)
        except NotFound:
            return

    async def move(self, src: str, dest: str) -> None:
        sk = self._key(src)
        dk = self._key(dest)
        sb = self._bucket.blob(sk)
        db = self._bucket.blob(dk)

        def _copy() -> None:
            rewrite_token = None
            while True:
                rewrite_token, _, _ = db.rewrite(sb, token=rewrite_token)
                if rewrite_token is None:
                    break

        await asyncio.to_thread(_copy)
        try:
            await asyncio.to_thread(sb.delete)
        except Exception as exc:
            _log.warning("gcs move: delete source failed after copy src=%s dest=%s: %r", sk, dk, exc)

    async def gc_prefix(self, prefix: str, max_age_sec: float) -> dict[str, int]:
        del prefix, max_age_sec
        _log.info(
            "gcs gc_prefix: skipped — configure object lifecycle rules on the bucket for prefix cleanup"
        )
        return {"scanned": 0, "deleted": 0, "errors": 0}


__all__ = ["GCSWorkspaceStorage"]
