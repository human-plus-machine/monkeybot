"""Google Drive-backed filesystem sync for agent persistent memory.

Uses google-api-python-client (already a core dependency) — no additional
packages required.

Strategy: startup pull + periodic push + close() flush on shutdown.
All sync failures are non-fatal — agent continues with local state.

Drive folder structure mirrors local directory structure:
    Drive root folder/
        campaigns/
            my-campaign/
                state.json
                content/
                    image.png
"""

import asyncio
import io
import logging
import mimetypes
import signal
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
_FIELDS_FILE = "id, name, mimeType, modifiedTime"
_FIELDS_LIST = f"files({_FIELDS_FILE})"


def _parse_drive_time(dt_str: str) -> float:
    """Convert a Drive ISO 8601 UTC timestamp string to a Unix timestamp float."""
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00")).timestamp()


class DriveFilesystemSync:
    """Syncs a local directory with a Google Drive folder.

    On startup: pulls latest from Drive → local.
    Periodically: pushes local → Drive every sync_interval seconds.
    On shutdown: call close() for final push (wire to FastAPI lifespan).

    Auth: Application Default Credentials (same service account as Vertex AI).
    The service account must have at least Editor access to the root folder.

    Example:
        >>> sync = DriveFilesystemSync("1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs", "./data/memory")
        >>> await sync.sync_from_drive()      # startup pull
        >>> await sync.start_periodic_sync()  # background task
        >>> # On shutdown (FastAPI lifespan):
        >>> await sync.close()
    """

    def __init__(
        self,
        folder_id: str,
        local_dir: str | Path = "./data/memory",
        sync_interval: int = 300,
        max_workers: int = 8,
    ) -> None:
        """
        Args:
            folder_id:      Google Drive folder ID (the root memory folder)
            local_dir:      Local directory to sync (created if missing)
            sync_interval:  Seconds between periodic background syncs (default: 300)
            max_workers:    Thread pool size for parallel file uploads/downloads (default: 8)
        """
        self.folder_id = folder_id
        self.local_dir = Path(local_dir)
        self.sync_interval = sync_interval
        self.max_workers = max_workers
        self._sync_task: asyncio.Task | None = None

    def _get_service(self):
        """Build and return an authenticated Drive v3 service."""
        from googleapiclient.discovery import build
        import google.auth

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive"])
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    # ------------------------------------------------------------------
    # Internal blocking helpers (run in executor)
    # ------------------------------------------------------------------

    def _list_drive_files(self, service, parent_id: str) -> list[dict]:
        """Recursively list all files under parent_id, returning flat list with relative paths."""
        results = []
        self._collect_files(service, parent_id, "", results)
        return results

    def _collect_files(
        self,
        service,
        parent_id: str,
        prefix: str,
        results: list[dict],
    ) -> None:
        """DFS traversal of a Drive folder, populating results with {id, path, mimeType}."""
        page_token = None
        while True:
            resp = (
                service.files()
                .list(
                    q=f"'{parent_id}' in parents and trashed = false",
                    fields=_FIELDS_LIST,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            for item in resp.get("files", []):
                rel_path = f"{prefix}{item['name']}" if prefix else item["name"]
                if item["mimeType"] == _DRIVE_FOLDER_MIME:
                    self._collect_files(service, item["id"], rel_path + "/", results)
                else:
                    results.append({
                        "id": item["id"],
                        "path": rel_path,
                        "mimeType": item["mimeType"],
                        "modifiedTime": item["modifiedTime"],
                    })
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    def _pull_from_drive(self) -> None:
        """Pull Drive folder → local_dir (blocking, runs in executor).

        Phase 1: list all Drive files (sequential, recursive).
        Phase 2: download all files in parallel via thread pool.
        """
        service = self._get_service()
        files = self._list_drive_files(service, self.folder_id)
        if not files:
            return

        def _download(file_info: dict) -> None:
            from googleapiclient.http import MediaIoBaseDownload  # noqa: PLC0415
            svc = self._get_service()
            local_path = self.local_dir / file_info["path"]
            # Newest-wins: skip if local copy is already up to date
            if local_path.exists():
                drive_mtime = _parse_drive_time(file_info["modifiedTime"])
                if drive_mtime <= local_path.stat().st_mtime:
                    logger.debug("Drive sync: skip pull %s (local is newer or equal)", file_info["path"])
                    return
            local_path.parent.mkdir(parents=True, exist_ok=True)
            request = svc.files().get_media(fileId=file_info["id"], supportsAllDrives=True)
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            local_path.write_bytes(buf.getvalue())
            logger.debug("Drive sync: pulled %s → %s", file_info["path"], local_path)

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(_download, f): f["path"] for f in files}
            for future in as_completed(futures):
                future.result()  # re-raises any exception into the calling thread

    def _get_or_create_folder(
        self,
        service,
        name: str,
        parent_id: str,
        cache: dict[tuple[str, str], str],
    ) -> str:
        """Return the Drive folder ID for name under parent_id, creating it if needed.

        Results are stored in cache (keyed by (name, parent_id)) so repeated calls
        for the same path segment within a single sync cycle make no API calls.
        """
        key = (name, parent_id)
        if key in cache:
            return cache[key]
        resp = (
            service.files()
            .list(
                q=(
                    f"name = '{name}' and '{parent_id}' in parents "
                    f"and mimeType = '{_DRIVE_FOLDER_MIME}' and trashed = false"
                ),
                fields="files(id)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        existing = resp.get("files", [])
        if existing:
            folder_id = existing[0]["id"]
        else:
            folder = (
                service.files()
                .create(
                    body={"name": name, "mimeType": _DRIVE_FOLDER_MIME, "parents": [parent_id]},
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            )
            folder_id = folder["id"]
        cache[key] = folder_id
        return folder_id

    def _find_file_id(self, service, name: str, parent_id: str) -> str | None:
        """Return the Drive file ID for name under parent_id, or None."""
        resp = (
            service.files()
            .list(
                q=(
                    f"name = '{name}' and '{parent_id}' in parents "
                    f"and mimeType != '{_DRIVE_FOLDER_MIME}' and trashed = false"
                ),
                fields="files(id)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = resp.get("files", [])
        return files[0]["id"] if files else None

    def _push_to_drive(self) -> None:
        """Push local_dir → Drive folder (blocking, runs in executor).

        Phase 1 (sequential): Walk local tree, resolve Drive folder IDs using the
        folder cache, and build a list of (local_path, file_name, parent_id) upload tasks.
        Folder creation is kept sequential to prevent duplicate folder creation from
        concurrent threads.

        Phase 2 (parallel): Upload/update all files via thread pool.
        Each thread creates its own Drive service instance (not thread-safe to share).
        """
        from googleapiclient.http import MediaFileUpload  # noqa: PLC0415

        service = self._get_service()

        # Build a Drive index once: relative path → {id, modifiedTime}
        # This replaces per-file _find_file_id calls and enables mtime comparison.
        drive_index: dict[str, dict] = {
            f["path"]: f for f in self._list_drive_files(service, self.folder_id)
        }

        folder_cache: dict[tuple[str, str], str] = {}

        # Phase 1: resolve folder IDs + delta check (sequential)
        # upload_tasks: (local_path, file_name, parent_id, existing_drive_id | None)
        upload_tasks: list[tuple[Path, str, str, str | None]] = []
        for local_path in sorted(self.local_dir.rglob("*")):
            if not local_path.is_file():
                continue
            relative = local_path.relative_to(self.local_dir)
            # Normalize to forward-slash path to match Drive index keys
            rel_str = "/".join(relative.parts)
            drive_entry = drive_index.get(rel_str)

            # Newest-wins: skip if Drive copy is already up to date
            if drive_entry:
                drive_mtime = _parse_drive_time(drive_entry["modifiedTime"])
                if local_path.stat().st_mtime <= drive_mtime:
                    logger.debug("Drive sync: skip push %s (Drive is newer or equal)", rel_str)
                    continue

            parts = relative.parts
            parent_id = self.folder_id
            for folder_name in parts[:-1]:
                parent_id = self._get_or_create_folder(service, folder_name, parent_id, folder_cache)
            existing_id = drive_entry["id"] if drive_entry else None
            upload_tasks.append((local_path, parts[-1], parent_id, existing_id))

        if not upload_tasks:
            return

        # Phase 2: upload files in parallel
        def _upload(local_path: Path, file_name: str, parent_id: str, existing_id: str | None) -> None:
            svc = self._get_service()
            mime_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
            media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=False)
            # Use the pre-resolved ID from drive_index; fall back to a live lookup only
            # for new files whose parent folder was just created in Phase 1.
            file_id = existing_id or self._find_file_id(svc, file_name, parent_id)
            if file_id:
                svc.files().update(
                    fileId=file_id, media_body=media, supportsAllDrives=True
                ).execute()
            else:
                svc.files().create(
                    body={"name": file_name, "parents": [parent_id]},
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                ).execute()
            logger.debug("Drive sync: pushed %s", local_path.relative_to(self.local_dir))

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(_upload, lp, fn, pid, eid): lp
                for lp, fn, pid, eid in upload_tasks
            }
            for future in as_completed(futures):
                future.result()  # re-raises any exception into the calling thread

    # ------------------------------------------------------------------
    # Public async interface (mirrors GCSFilesystemSync exactly)
    # ------------------------------------------------------------------

    async def sync_from_drive(self) -> None:
        """Pull Drive → local disk.

        Called once at startup before agent begins serving requests.
        Creates local_dir if it doesn't exist.
        Non-fatal: logs error and returns if Drive is unreachable.
        """
        self.local_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Drive sync: pulling from folder %s → %s", self.folder_id, self.local_dir)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._pull_from_drive)
            logger.info("Drive sync: pull complete")
        except Exception as e:
            logger.error("Drive sync: pull failed: %s", e)

    async def sync_to_drive(self) -> None:
        """Push local disk → Drive.

        Called periodically by the background task and on clean shutdown.
        Non-fatal: logs error and returns on failure.
        """
        self.local_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Drive sync: pushing %s → folder %s", self.local_dir, self.folder_id)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._push_to_drive)
            logger.info("Drive sync: push complete")
        except Exception as e:
            logger.error("Drive sync: push failed: %s", e)

    async def start_periodic_sync(self) -> asyncio.Task:
        """Start background asyncio task that calls sync_to_drive every sync_interval seconds.

        Returns:
            The background asyncio.Task (cancel it to stop periodic sync).
        """
        async def _loop() -> None:
            while True:
                await asyncio.sleep(self.sync_interval)
                try:
                    await self.sync_to_drive()
                except Exception as e:
                    logger.error("Drive sync: periodic push error: %s", e)

        self._sync_task = asyncio.create_task(_loop())
        logger.info(
            "Drive sync: periodic task started (interval=%ss, folder=%s)",
            self.sync_interval,
            self.folder_id,
        )
        return self._sync_task

    async def close(self) -> None:
        """Cancel the periodic task and do a final sync_to_drive.

        Wire this to FastAPI lifespan shutdown for clean container exit.
        """
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        await self.sync_to_drive()
        logger.info("Drive sync: closed")

    def register_sigterm_handler(self) -> None:
        """Register a SIGTERM signal handler that schedules a final sync on shutdown."""
        def _handler(signum, frame):
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self.close())

        signal.signal(signal.SIGTERM, _handler)
        logger.info("Drive sync: SIGTERM handler registered")
