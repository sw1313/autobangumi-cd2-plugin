import asyncio
import logging
import posixpath
import threading
import time
from collections.abc import Iterator
from typing import Callable

import grpc
from google.protobuf import empty_pb2

from clouddrive2_client import CloudDriveClient
from clouddrive2_client.proto import clouddrive_pb2

from cd2.sync import completed_copy_task_keys, copy_before_backup

logger = logging.getLogger(__name__)

OFFLINE_INIT = 0
OFFLINE_DOWNLOADING = 1
OFFLINE_FINISHED = 2
OFFLINE_ERROR = 3
CD2_CONNECT_TIMEOUT_SEC = 15
CD2_RPC_TIMEOUT_SEC = 60


class OfflineLocalFsNotSupportedError(Exception):
    """CD2 offline APIs are unavailable for LocalFs paths."""


class CD2ConnectionError(ConnectionError):
    """CloudDrive2 gRPC service is unreachable or authentication failed."""


OfflineListNotSupportedError = OfflineLocalFsNotSupportedError


def is_duplicate_offline_error(message: str) -> bool:
    text = message or ""
    lower = text.lower()
    return (
        "10008" in text
        or "任务已存在" in text
        or "重复的链接" in text
        or "already exist" in lower
        or "duplicate" in lower
    )


def parse_grpc_address(host: str) -> str:
    host = host.strip().rstrip("/")
    if host.startswith("http://") or host.startswith("https://"):
        from urllib.parse import urlparse

        parsed = urlparse(host)
        port = parsed.port or 19798
        return f"{parsed.hostname}:{port}"
    return host


def normalize_info_hash(value: str) -> str:
    return value.lower().replace("0x", "").strip()


_TRANSIENT_RPC_CODES = {
    grpc.StatusCode.INTERNAL,
    grpc.StatusCode.UNAVAILABLE,
    grpc.StatusCode.DEADLINE_EXCEEDED,
    grpc.StatusCode.RESOURCE_EXHAUSTED,
}
_LIST_OFFLINE_RETRY_DELAYS = (0.4, 0.8)
_CACHE_MISS = object()


def brief_rpc_error(exc: BaseException) -> str:
    """One line for logs. RpcError's default text is a multi-line dump."""
    code = getattr(exc, "code", None)
    details = getattr(exc, "details", None)
    if callable(code) and callable(details):
        try:
            return f"{code().name}: {details()}"
        except Exception:
            pass
    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    return text.splitlines()[0]


def _file_has_content(file_info: object) -> bool:
    return getattr(file_info, "isDirectory", False) or (
        getattr(file_info, "size", 0) or 0
    ) > 0


def _normalize_cloud_path(path: str) -> str:
    normalized = (path or "").replace("\\", "/").strip()
    if len(normalized) > 1:
        normalized = normalized.rstrip("/")
    return normalized


def _is_exact_cloud_path(requested: str, returned: str) -> bool:
    return _normalize_cloud_path(requested) == _normalize_cloud_path(returned)


def _split_cloud_path(path: str) -> tuple[str, str]:
    normalized = _normalize_cloud_path(path)
    if normalized in {"", "/"}:
        return "/", ""
    parent, name = posixpath.split(normalized)
    return parent or "/", name


def iter_sub_files(
    client: CloudDriveClient,
    path: str,
    *,
    force_refresh: bool = False,
    timeout: float | None = CD2_RPC_TIMEOUT_SEC,
    cancel_event: threading.Event | None = None,
) -> Iterator:
    """Stream GetSubFiles results incrementally instead of buffering the full list."""
    request = clouddrive_pb2.ListSubFileRequest(
        path=path, forceRefresh=force_refresh
    )
    metadata = client._create_authorized_metadata()
    call = client.stub.GetSubFiles(request, metadata=metadata, timeout=timeout)
    try:
        for response in call:
            if cancel_event and cancel_event.is_set():
                call.cancel()
                return
            yield from response.subFiles
    except grpc.RpcError:
        if cancel_event and cancel_event.is_set():
            return
        raise


class CD2Client:
    def __init__(self, host: str, username: str, password: str):
        self.address = parse_grpc_address(host)
        self.username = username
        self.password = password
        self._client: CloudDriveClient | None = None

    def connect(self) -> None:
        client = CloudDriveClient(self.address)
        try:
            authenticated = client.authenticate(self.username, self.password)
        except grpc.RpcError as e:
            client.close()
            details = e.details() or str(e)
            raise CD2ConnectionError(
                f"CloudDrive2 gRPC unavailable at {self.address}: {details}"
            ) from e
        if not authenticated:
            client.close()
            raise CD2ConnectionError("CloudDrive2 authentication failed")
        self._client = client

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def add_offline_files(self, urls: str, target_folder: str) -> str:
        """Return 'ok', 'duplicate', or 'failed'."""
        if not self._client:
            self.connect()
        assert self._client is not None
        logger.info("[CD2] AddOfflineFiles request: target=%s urls=%s", target_folder, urls)
        request = clouddrive_pb2.AddOfflineFileRequest(
            urls=urls,
            toFolder=target_folder,
            checkFolderAfterSecs=10,
        )
        try:
            response = self._client.stub.AddOfflineFiles(
                request, metadata=self._client._create_authorized_metadata()
            )
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.UNIMPLEMENTED:
                raise OfflineLocalFsNotSupportedError(str(e.details())) from e
            details = str(e.details() or "")
            if is_duplicate_offline_error(details):
                logger.info("[CD2] Offline task already exists: %s", details)
                return "duplicate"
            logger.error("[CD2] AddOfflineFiles rpc error: %s", details)
            return "failed"
        if not response.success:
            error = response.errorMessage or ""
            if is_duplicate_offline_error(error):
                logger.info("[CD2] Offline task already exists: %s", error)
                return "duplicate"
            logger.error("[CD2] AddOfflineFiles failed: %s", error)
            return "failed"
        logger.info("[CD2] AddOfflineFiles accepted: target=%s", target_folder)
        return "ok"

    def restart_offline_task(
        self,
        target_folder: str,
        info_hash: str,
        magnet: str,
        offline_item: object | None = None,
    ) -> bool:
        if not self._client:
            self.connect()
        assert self._client is not None
        request = clouddrive_pb2.RestartOfflineFileRequest(
            path=target_folder,
            infoHash=info_hash,
            url=magnet,
            parentId=str(getattr(offline_item, "parentId", "") or ""),
        )
        try:
            self._client.stub.RestartOfflineTask(
                request, metadata=self._client._create_authorized_metadata()
            )
            return True
        except grpc.RpcError as e:
            logger.error("[CD2] RestartOfflineTask failed: %s", e.details())
            return False

    def remove_offline_tasks(
        self,
        target_folder: str,
        info_hashes: list[str],
        delete_files: bool = False,
    ) -> bool:
        if not self._client:
            self.connect()
        assert self._client is not None
        request = clouddrive_pb2.RemoveOfflineFilesRequest(
            path=target_folder,
            infoHashes=info_hashes,
            deleteFiles=delete_files,
        )
        try:
            response = self._client.stub.RemoveOfflineFiles(
                request, metadata=self._client._create_authorized_metadata()
            )
            if not response.success:
                logger.error("[CD2] RemoveOfflineFiles failed: %s", response.errorMessage)
                return False
            return True
        except grpc.RpcError as e:
            logger.error("[CD2] RemoveOfflineFiles rpc error: %s", e.details())
            return False

    def path_has_content(self, path: str) -> bool:
        if not self._client:
            self.connect()
        assert self._client is not None
        parent, name = _split_cloud_path(path)
        if not name:
            return False

        parent_dir = parent if parent.endswith("/") else f"{parent}/"
        info = None
        try:
            for child in iter_sub_files(self._client, parent_dir, force_refresh=True):
                child_name = getattr(child, "name", "") or posixpath.basename(
                    _normalize_cloud_path(getattr(child, "fullPathName", ""))
                )
                if child_name == name and _is_exact_cloud_path(path, child.fullPathName):
                    info = child
                    break
        except Exception as e:
            logger.debug("[CD2] GetSubFiles failed for %s: %s", parent_dir, e)
            return False

        if info is None:
            logger.debug("[CD2] Cloud path not found under %s: %s", parent_dir, name)
            return False
        if getattr(info, "isDirectory", False):
            folder = info.fullPathName
            if not folder.endswith("/"):
                folder += "/"
            for child in iter_sub_files(self._client, folder):
                if _file_has_content(child):
                    return True
            return False
        return (getattr(info, "size", 0) or 0) > 0

    def list_offline_files(self, folder: str) -> list:
        if not self._client:
            self.connect()
        assert self._client is not None
        cache: dict[str, list | BaseException] = self.__dict__.setdefault(
            "_offline_list_cache", {}
        )
        cached = cache.get(folder, _CACHE_MISS)
        if cached is not _CACHE_MISS:
            if isinstance(cached, BaseException):
                raise cached
            return cached
        try:
            files = self._list_offline_files(folder)
        except Exception as e:
            cache[folder] = e
            raise
        cache[folder] = files
        return files

    def _list_offline_files(self, folder: str) -> list:
        assert self._client is not None
        request = clouddrive_pb2.FileRequest(path=folder)
        delays = _LIST_OFFLINE_RETRY_DELAYS
        for attempt in range(len(delays) + 1):
            try:
                response = self._client.stub.ListOfflineFilesByPath(
                    request,
                    metadata=self._client._create_authorized_metadata(),
                    timeout=CD2_RPC_TIMEOUT_SEC,
                )
            except grpc.RpcError as e:
                if e.code() == grpc.StatusCode.UNIMPLEMENTED:
                    raise OfflineLocalFsNotSupportedError(str(e.details())) from e
                if e.code() not in _TRANSIENT_RPC_CODES or attempt == len(delays):
                    raise
                logger.debug(
                    "[CD2] Offline list interrupted, retrying: %s",
                    brief_rpc_error(e),
                )
                time.sleep(delays[attempt])
                continue
            return list(response.offlineFiles)
        return []

    def paused_copy_upload_keys(self, dest_prefix: str, limit: int) -> list[str]:
        """Keys of paused copy-uploads whose destination is under dest_prefix."""
        if not self._client:
            self.connect()
        assert self._client is not None
        prefix = (dest_prefix or "").replace("\\", "/").rstrip("/")
        if not prefix or limit <= 0:
            return []

        keys: list[str] = []
        for page in range(0, 20):
            request = clouddrive_pb2.GetUploadFileListRequest(
                getAll=False,
                itemsPerPage=100,
                pageNumber=page,
                operatorTypeFilter=clouddrive_pb2.UploadFileInfo.Copy,
                statusFilter=clouddrive_pb2.UploadFileInfo.Pause,
            )
            response = self._client.stub.GetUploadFileList(
                request,
                metadata=self._client._create_authorized_metadata(),
                timeout=CD2_RPC_TIMEOUT_SEC,
            )
            files = list(response.uploadFiles)
            if not files:
                break
            for item in files:
                dest = (item.destPath or "").replace("\\", "/").rstrip("/")
                if dest != prefix and not dest.startswith(prefix + "/"):
                    continue
                if item.key:
                    keys.append(item.key)
                    if len(keys) >= limit:
                        return keys
            if len(files) < 100:
                break
        return keys

    def remove_completed_copy_tasks(self, dest_prefix: str) -> int:
        """Drop finished copy tasks that landed in the local staging folder."""
        if not self._client:
            self.connect()
        assert self._client is not None
        result = self._client.stub.GetCopyTasks(
            empty_pb2.Empty(),
            metadata=self._client._create_authorized_metadata(),
            timeout=CD2_RPC_TIMEOUT_SEC,
        )
        keys = completed_copy_task_keys(list(result.copyTasks), dest_prefix)
        if not keys:
            return 0
        removed = 0
        metadata = self._client._create_authorized_metadata()
        for start in range(0, len(keys), 100):
            chunk = keys[start : start + 100]
            response = self._client.stub.RemoveCopyTasks(
                clouddrive_pb2.CopyTaskBatchRequest(taskKeys=chunk),
                metadata=metadata,
                timeout=CD2_RPC_TIMEOUT_SEC,
            )
            if not response.success:
                logger.error("[CD2] RemoveCopyTasks failed: %s", response.errorMessage)
                break
            removed += response.affectedCount or len(chunk)
        return removed

    def prefer_copy_over_backup(self) -> list[str] | None:
        """Schedule copy uploads ahead of backup uploads.

        Only ``operatorPriorityOrder`` is sent. Backup jobs are not paused
        or removed, and download tasks are left alone. Returns the new
        order, or None when no change was needed.
        """
        if not self._client:
            self.connect()
        assert self._client is not None
        metadata = self._client._create_authorized_metadata()
        current = self._client.stub.GetSystemSettings(
            empty_pb2.Empty(),
            metadata=metadata,
            timeout=CD2_RPC_TIMEOUT_SEC,
        )
        if current.HasField("operatorPriorityOrder"):
            order = list(current.operatorPriorityOrder.values)
        else:
            order = []
        updated = copy_before_backup(order)
        if updated is None:
            return None
        settings_msg = clouddrive_pb2.SystemSettings()
        settings_msg.operatorPriorityOrder.values.extend(updated)
        self._client.stub.SetSystemSettings(
            settings_msg,
            metadata=metadata,
            timeout=CD2_RPC_TIMEOUT_SEC,
        )
        return updated

    def resume_upload_files(self, keys: list[str]) -> int:
        if not keys:
            return 0
        if not self._client:
            self.connect()
        assert self._client is not None
        self._client.stub.ResumeUploadFiles(
            clouddrive_pb2.MultpleUploadFileKeyRequest(keys=keys),
            metadata=self._client._create_authorized_metadata(),
            timeout=CD2_RPC_TIMEOUT_SEC,
        )
        return len(keys)

    def copy_files(self, source_paths: list[str], dest_path: str) -> bool:
        if not self._client:
            self.connect()
        assert self._client is not None
        try:
            response = self._client.copy_file(source_paths, dest_path)
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.NOT_FOUND:
                raise
            # CopyFile rejects the whole batch when any one path is gone.
            if len(source_paths) > 1:
                started = False
                for path in source_paths:
                    if self.copy_files([path], dest_path):
                        started = True
                return started
            logger.info(
                "[CD2] Cloud path is gone, skip copy: %s",
                brief_rpc_error(e),
            )
            return False
        if not response.success:
            logger.error("[CD2] CopyFile failed: %s", response.errorMessage)
            return False
        return True


class CD2Session:
    """Long-lived CD2 gRPC connection reused across multiple calls in one scan cycle.

    Reduces connection churn vs. the standalone async functions which each
    create and destroy their own gRPC client.
    """

    def __init__(self, host: str, username: str, password: str):
        self._host = host
        self._username = username
        self._password = password
        self._client: CD2Client | None = None

    @classmethod
    def from_cfg(cls, cfg):
        return cls(cfg.host, cfg.username, cfg.password)

    async def __aenter__(self):
        self._client = await asyncio.to_thread(self._connect)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await asyncio.to_thread(self._client.close)
            self._client = None

    def _connect(self) -> CD2Client:
        c = CD2Client(self._host, self._username, self._password)
        c.connect()
        return c

    async def cloud_content_exists(self, paths: list[str]) -> bool:
        """Check whether any of *paths* has content on CD2 cloud."""
        if not paths:
            return False
        cli = self._client

        def _check():
            for path in paths:
                if cli.path_has_content(path):
                    return True
            return False

        return await asyncio.to_thread(_check)

    async def add_offline_magnet(self, magnet: str, target_folder: str) -> str:
        """Return 'ok', 'duplicate', or 'failed'."""
        cli = self._client
        return await asyncio.to_thread(
            cli.add_offline_files, magnet, target_folder
        )

    async def restart_offline_magnet(
        self,
        target_folder: str,
        info_hash: str,
        magnet: str,
        offline_item: object | None = None,
    ) -> bool:
        cli = self._client
        return await asyncio.to_thread(
            cli.restart_offline_task,
            target_folder, info_hash, magnet, offline_item,
        )

    async def remove_offline_tasks(
        self,
        target_folder: str,
        info_hashes: list[str],
        delete_files: bool = False,
    ) -> bool:
        cli = self._client
        return await asyncio.to_thread(
            cli.remove_offline_tasks,
            target_folder, info_hashes, delete_files,
        )

    async def list_all_offline_by_hash(
        self, target_folder: str
    ) -> dict[str, object]:
        cli = self._client

        def _list():
            result = {}
            for item in cli.list_offline_files(target_folder):
                info_hash = normalize_info_hash(getattr(item, "infoHash", "") or "")
                if info_hash:
                    result[info_hash] = item
            return result

        return await asyncio.to_thread(_list)

    async def list_finished_offline_by_hash(
        self, target_folder: str
    ) -> dict[str, object]:
        cli = self._client

        def _list():
            result = {}
            for item in cli.list_offline_files(target_folder):
                if item.status != OFFLINE_FINISHED:
                    continue
                info_hash = normalize_info_hash(getattr(item, "infoHash", "") or "")
                if info_hash:
                    result[info_hash] = item
            return result

        return await asyncio.to_thread(_list)

    async def copy_cloud_files_to_local(
        self, source_paths: list[str], dest_path: str
    ) -> bool:
        if not source_paths:
            return False
        cli = self._client
        return await asyncio.to_thread(cli.copy_files, source_paths, dest_path)

    async def prefer_copy_over_backup(self) -> list[str] | None:
        cli = self._client
        return await asyncio.to_thread(cli.prefer_copy_over_backup)

    async def resume_paused_copy_uploads(self, dest_prefix: str, limit: int) -> int:
        cli = self._client

        def _resume() -> int:
            keys = cli.paused_copy_upload_keys(dest_prefix, limit)
            return cli.resume_upload_files(keys)

        return await asyncio.to_thread(_resume)

    async def remove_completed_copy_tasks(self, dest_prefix: str) -> int:
        cli = self._client
        return await asyncio.to_thread(cli.remove_completed_copy_tasks, dest_prefix)


async def run_cd2(
    host: str,
    username: str,
    password: str,
    fn: Callable[[CD2Client], object],
    *,
    timeout: float | None = CD2_RPC_TIMEOUT_SEC,
):
    def _run():
        client = CD2Client(host, username, password)
        try:
            client.connect()
            return fn(client)
        finally:
            client.close()

    coro = asyncio.to_thread(_run)
    if timeout is None:
        return await coro
    return await asyncio.wait_for(coro, timeout=timeout)


async def cloud_content_exists(
    host: str,
    username: str,
    password: str,
    paths: list[str],
) -> bool:
    if not paths:
        return False

    def _check(client: CD2Client) -> bool:
        for path in paths:
            if client.path_has_content(path):
                return True
        return False

    return bool(await run_cd2(host, username, password, _check))


async def add_offline_magnet(
    host: str,
    username: str,
    password: str,
    magnet: str,
    target_folder: str,
) -> str:
    return str(
        await run_cd2(
            host,
            username,
            password,
            lambda c: c.add_offline_files(magnet, target_folder),
        )
    )


async def restart_offline_magnet(
    host: str,
    username: str,
    password: str,
    target_folder: str,
    info_hash: str,
    magnet: str,
    offline_item: object | None = None,
) -> bool:
    return bool(
        await run_cd2(
            host,
            username,
            password,
            lambda c: c.restart_offline_task(
                target_folder, info_hash, magnet, offline_item
            ),
        )
    )


async def remove_offline_tasks(
    host: str,
    username: str,
    password: str,
    target_folder: str,
    info_hashes: list[str],
    delete_files: bool = False,
) -> bool:
    return bool(
        await run_cd2(
            host,
            username,
            password,
            lambda c: c.remove_offline_tasks(
                target_folder, info_hashes, delete_files
            ),
        )
    )


async def list_all_offline_by_hash(
    host: str,
    username: str,
    password: str,
    target_folder: str,
) -> dict[str, object]:
    def _list(client: CD2Client):
        result = {}
        for item in client.list_offline_files(target_folder):
            info_hash = normalize_info_hash(getattr(item, "infoHash", "") or "")
            if info_hash:
                result[info_hash] = item
        return result

    return await run_cd2(host, username, password, _list)


async def list_finished_offline_by_hash(
    host: str,
    username: str,
    password: str,
    target_folder: str,
) -> dict[str, object]:
    def _list(client: CD2Client):
        result = {}
        for item in client.list_offline_files(target_folder):
            if item.status != OFFLINE_FINISHED:
                continue
            info_hash = normalize_info_hash(getattr(item, "infoHash", "") or "")
            if info_hash:
                result[info_hash] = item
        return result

    return await run_cd2(host, username, password, _list)


async def copy_cloud_files_to_local(
    host: str,
    username: str,
    password: str,
    source_paths: list[str],
    dest_path: str,
) -> bool:
    if not source_paths:
        return False

    def _copy(client: CD2Client) -> bool:
        return client.copy_files(source_paths, dest_path)

    return bool(
        await run_cd2(host, username, password, _copy)
    )


async def test_cd2_connection(host: str, username: str, password: str) -> None:
    await run_cd2(
        host,
        username,
        password,
        lambda c: c._client.get_system_info(),
        timeout=CD2_CONNECT_TIMEOUT_SEC,
    )


def torrent_to_magnet(torrent: dict) -> str:
    from urllib.parse import quote

    magnet = torrent.get("magnet_uri")
    if magnet:
        return magnet
    info_hash = torrent.get("hash", "")
    if not info_hash:
        raise ValueError("Torrent has no hash")
    name = torrent.get("name", "")
    if name:
        return f"magnet:?xt=urn:btih:{info_hash}&dn={quote(name)}"
    return f"magnet:?xt=urn:btih:{info_hash}"
