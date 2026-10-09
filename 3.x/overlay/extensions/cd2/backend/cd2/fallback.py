import asyncio
import logging
import re
import time
from pathlib import Path

from cd2.client import (
    CD2Session,
    OFFLINE_DOWNLOADING,
    OFFLINE_INIT,
    OfflineLocalFsNotSupportedError,
    brief_rpc_error,
    normalize_info_hash,
    torrent_to_magnet,
)
from cd2.sync import (
    is_local_download_complete,
    join_cloud_path,
    move_content_to_qb,
    resolve_cd2_content,
    resolve_cd2_local_dest,
    resolve_offline_target,
    resolve_sync_local_base,
)
from module.conf import settings
from module.downloader import DownloadClient
from module.downloader.path import check_files, path_to_bangumi
from module.manager.renamer import Renamer

logger = logging.getLogger(__name__)

STALL_STATES = {"stalledDL", "missingFiles", "error"}
MAX_AUTO_SUBMISSIONS_PER_CYCLE = 5
MAX_RESUME_COPY_UPLOADS_PER_CYCLE = 5
CD2_SUBMITTED_TAG = "cd2:submitted"
CD2_FORCE_REPAIR_TAG = "cd2:force-repair"
MANUAL_REPAIR_COOLDOWN_SECONDS = 5
_cd2_submit_lock = asyncio.Lock()
_manual_repair_recent: dict[str, float] = {}
_manual_repair_inflight: set[str] = set()
DOWNLOADING_STATES = {"downloading", "metaDL", "queuedDL", "checkingDL"}
VERSIONED_RELEASE_RE = re.compile(r"(?i)(?:^|[\s._\-\]])\d{1,3}v\d+(?:\D|$)")

OFFLINE_DIR_HINT_EN = (
    "Set CD2 cloud offline path (offline_dir), e.g. /115/Anime/cd2-offline. "
    "Local NAS paths belong in local_path."
)
OFFLINE_DIR_HINT_ZH = (
    "请配置 CD2 115 云盘离线下载目录（offline_dir），"
    "例如 /115/动漫/cd2-offline；群晖本地路径请填在「本地同步目录」。"
)


def is_complete_torrent(torrent: dict) -> bool:
    return torrent.get("progress", 0) >= 1.0


def is_dead_torrent(
    torrent: dict,
    stall_seconds: int,
    min_speed_bps: int = 0,
) -> bool:
    """Treat incomplete torrents as dead when stalled or effectively not downloading."""
    if is_complete_torrent(torrent):
        return False

    now = __import__("time").time()
    last_activity = torrent.get("last_activity") or torrent.get("added_on") or now
    added_on = torrent.get("added_on") or last_activity
    inactive_for = now - last_activity
    age_for = now - added_on

    state = torrent.get("state", "")
    dlspeed = torrent.get("dlspeed", 0) or 0

    if inactive_for >= stall_seconds:
        if state in STALL_STATES:
            return True
        if state in DOWNLOADING_STATES and dlspeed == 0:
            return True

    # Slow but non-zero speed still bumps last_activity; use task age instead.
    if min_speed_bps > 0 and age_for >= stall_seconds:
        if state in STALL_STATES:
            return True
        if state in DOWNLOADING_STATES and dlspeed < min_speed_bps:
            return True

    return False


def claim_manual_repairs(
    hashes: set[str], now: float | None = None
) -> tuple[set[str], set[str]]:
    """Split hashes into ones to send and ones still cooling down.

    Accepted hashes are recorded before any CD2 call. A hash stays blocked
    while that repair is in flight, and for a few seconds after it was sent.
    """
    current = time.monotonic() if now is None else now
    accepted: set[str] = set()
    cooled: set[str] = set()
    for raw in hashes:
        if not raw:
            continue
        if raw in _manual_repair_inflight:
            cooled.add(raw)
            continue
        seen = _manual_repair_recent.get(raw)
        if seen is not None and current - seen < MANUAL_REPAIR_COOLDOWN_SECONDS:
            cooled.add(raw)
            continue
        _manual_repair_recent[raw] = current
        _manual_repair_inflight.add(raw)
        accepted.add(raw)
    cutoff = current - MANUAL_REPAIR_COOLDOWN_SECONDS
    for key, seen in list(_manual_repair_recent.items()):
        if key not in _manual_repair_inflight and seen < cutoff:
            del _manual_repair_recent[key]
    return accepted, cooled


def finish_manual_repairs(hashes: set[str]) -> None:
    """Repair finished. The cooldown timestamp stays so a quick retry waits."""
    for raw in hashes:
        _manual_repair_inflight.discard(raw)


def release_manual_repairs(hashes: set[str]) -> None:
    """Repair failed. Allow the same file to be sent again immediately."""
    for raw in hashes:
        _manual_repair_inflight.discard(raw)
        _manual_repair_recent.pop(raw, None)


def offline_task_should_be_replaced(status: int | None) -> bool:
    """True when restarting the task will not recreate a deleted 115 file.

    Init and downloading tasks can be restarted in place. An error or finished
    task stays as a record after its file is deleted, and RestartOfflineTask
    does not start a new download.
    """
    return status not in {OFFLINE_INIT, OFFLINE_DOWNLOADING}


def _has_cd2_submitted_tag(torrent: dict) -> bool:
    tags = torrent.get("tags", "") or ""
    return CD2_SUBMITTED_TAG in {tag.strip() for tag in tags.split(",") if tag.strip()}


def _has_cd2_force_repair_tag(torrent: dict) -> bool:
    tags = torrent.get("tags", "") or ""
    return CD2_FORCE_REPAIR_TAG in {
        tag.strip() for tag in tags.split(",") if tag.strip()
    }


def _is_versioned_release(torrent: dict) -> bool:
    """Detect releases like 02v2/04v2.

    AB renames 02 and 02v2 to the same SxxEyy target, so local/cloud existence
    checks can falsely treat a v2 repair as already satisfied by the old file.
    """
    return VERSIONED_RELEASE_RE.search(torrent.get("name", "") or "") is not None


class CD2FallbackManager:
    """CD2 offline fallback with qBittorrent reseed workflow."""

    @staticmethod
    def _offline_target(cfg) -> str | None:
        return resolve_offline_target(cfg.offline_dir, cfg.target_dir)

    @staticmethod
    def _sync_local_base(cfg) -> Path | None:
        return resolve_sync_local_base(cfg.local_path, cfg.target_dir)

    @staticmethod
    def _cd2_local_dest(cfg) -> str | None:
        return resolve_cd2_local_dest(cfg.local_path, cfg.target_dir)

    @staticmethod
    def _qb_content_path(torrent: dict) -> Path | None:
        content_path = torrent.get("content_path")
        if content_path:
            return Path(content_path)
        save_path = (torrent.get("save_path") or "").strip()
        name = torrent.get("name", "")
        if save_path and name:
            return Path(save_path) / name
        if save_path:
            return Path(save_path)
        return None

    def _content_complete_at(self, path: Path, torrent: dict) -> bool:
        if not path.exists():
            return False
        expected_size = torrent.get("total_size") or torrent.get("size") or 0
        return is_local_download_complete(path, expected_size)

    def _qb_content_complete(self, torrent: dict) -> bool:
        # qB can preallocate/sparsely create the final file size while pieces
        # are still missing. Only trust filesystem size after qB itself reports
        # the torrent complete.
        if not is_complete_torrent(torrent):
            return False
        dst = self._qb_content_path(torrent)
        return dst is not None and self._content_complete_at(dst, torrent)

    def _staging_content_missing(self, torrent: dict, cfg) -> bool:
        local_base = self._sync_local_base(cfg)
        if local_base is None:
            return True
        if not local_base.exists():
            return True
        name = torrent.get("name", "")
        norm_hash = normalize_info_hash(torrent.get("hash", ""))
        src = resolve_cd2_content(local_base, name, norm_hash)
        if not src:
            return True
        return not self._content_complete_at(src, torrent)

    @staticmethod
    def _cloud_candidate_paths(
        offline_target: str,
        torrent: dict,
        offline_item: object | None = None,
    ) -> list[str]:
        name = torrent.get("name", "")
        offline_name = getattr(offline_item, "name", "") or name if offline_item else name
        paths: list[str] = []
        for candidate in {offline_name, name}:
            if candidate:
                paths.append(join_cloud_path(offline_target, candidate))
                # CloudDrive2 offline downloads commonly materialize as:
                # /115/anime/<release-or-file-name>/<release-or-file-name>
                # Check both the wrapper directory and the inner file path.
                paths.append(
                    join_cloud_path(
                        join_cloud_path(offline_target, candidate),
                        candidate,
                    )
                )
        return list(dict.fromkeys(paths))

    async def _existing_cloud_paths(
        self,
        offline_target: str,
        torrent: dict,
        offline_item: object | None = None,
    ) -> list[str]:
        """Return copyable cloud paths that actually exist for this torrent.

        Prefer the CD2 wrapper directory over the inner file when both exist,
        because copying the wrapper preserves the on-disk shape that the local
        resolver already understands.
        """
        name = torrent.get("name", "")
        offline_name = getattr(offline_item, "name", "") or name if offline_item else name
        existing: list[str] = []
        seen: set[str] = set()
        for candidate in {offline_name, name}:
            if not candidate:
                continue
            wrapper = join_cloud_path(offline_target, candidate)
            inner = join_cloud_path(wrapper, candidate)
            for path in (wrapper, inner):
                if path in seen:
                    continue
                if await self._session.cloud_content_exists([path]):
                    existing.append(path)
                    seen.add(path)
                    break
        return existing

    async def _copy_cloud_only(
        self,
        cfg,
        name: str,
        existing_paths: list[str],
    ) -> str:
        """Copy 115 files that are already there. Do not touch the offline task."""
        cd2_local_dest = self._cd2_local_dest(cfg)
        if not cd2_local_dest or self._sync_local_base(cfg) is None:
            logger.error(
                "[CD2] Cloud files exist but local copy target is not mapped: %s",
                name,
            )
            return "failed"
        if await self._session.copy_cloud_files_to_local(
            existing_paths,
            cd2_local_dest,
        ):
            logger.info(
                "[CD2] 115 files exist, copy only: %s -> %s",
                name,
                cd2_local_dest,
            )
            return "copy_started"
        logger.error("[CD2] Failed to start cloud-to-local copy: %s", name)
        return "failed"

    async def _replace_offline_task(
        self,
        offline_target: str,
        norm_hash: str,
        magnet: str,
        name: str,
    ) -> bool:
        """Drop a task whose file is already gone, then submit that magnet once.

        delete_files stays false. The cloud file is already missing, and removing
        the task record is what lets AddOffline use the original name again.
        """
        removed = await self._session.remove_offline_tasks(
            offline_target,
            [norm_hash],
            delete_files=False,
        )
        if not removed:
            logger.error(
                "[CD2] Failed to remove offline task before resubmit: %s",
                name,
            )
            return False
        result = await self._session.add_offline_magnet(magnet, offline_target)
        if result == "ok":
            logger.info("[CD2] Replaced offline task whose 115 file is gone: %s", name)
            return True
        logger.error(
            "[CD2] Offline task removed but resubmit returned %s: %s",
            result,
            name,
        )
        return False

    async def _needs_redownload(
        self,
        torrent: dict,
        cfg,
        offline_item: object | None = None,
    ) -> bool:
        """True only when a new 115 offline download is required.

        Do not re-offline when qB already has the files, the CD2 staging folder
        is ready, or 115 cloud already holds the content (copy/sync is enough).
        """
        if self._qb_content_complete(torrent):
            return False
        if not self._staging_content_missing(torrent, cfg):
            return False

        offline_target = self._offline_target(cfg)
        if not offline_target:
            return True

        if await self._existing_cloud_paths(offline_target, torrent, offline_item):
            return False

        logger.info(
            "[CD2] 115 cloud content missing for %s",
            torrent.get("name", ""),
        )
        return True

    async def _ensure_local_copy(
        self,
        cfg,
        pending: list[dict],
        offline_target: str,
        cd2_local_dest: str,
        local_base: Path,
    ) -> int:
        """Start cloud-to-local copies for finished offline tasks whose local files
        don't yet exist or are incomplete.  Returns how many items were submitted."""
        try:
            finished = await self._session.list_finished_offline_by_hash(
                offline_target,
            )
        except OfflineLocalFsNotSupportedError as e:
            logger.warning("[CD2] Cannot list offline tasks (%s)", e)
            return 0
        except Exception as e:
            self._warn_offline_list_once(
                "[CD2] Offline list failed, keep syncing files already in %s: %s",
                local_base,
                brief_rpc_error(e),
            )
            return 0

        if not finished:
            return 0

        cloud_paths: list[str] = []
        for torrent in pending:
            torrent_hash = torrent.get("hash", "")
            norm_hash = normalize_info_hash(torrent_hash)
            offline = finished.get(norm_hash)
            if not offline:
                continue

            name = torrent.get("name", "")
            src = resolve_cd2_content(local_base, name, norm_hash)
            if src and self._content_complete_at(src, torrent):
                continue

            # Copy only a path CD2 can still see. A finished task whose 115
            # file was deleted otherwise makes CopyFile fail the whole batch.
            existing = await self._existing_cloud_paths(
                offline_target,
                torrent,
                offline,
            )
            if not existing:
                logger.debug(
                    "[CD2] Finished offline task has no cloud file to copy: %s",
                    name,
                )
                continue
            cloud_paths.extend(existing)

        unique_paths = list(dict.fromkeys(cloud_paths))
        if not unique_paths:
            return 0

        try:
            ok = await self._session.copy_cloud_files_to_local(
                unique_paths,
                cd2_local_dest,
            )
        except Exception as e:
            logger.warning(
                "[CD2] Cloud copy request failed, keep syncing local files: %s",
                brief_rpc_error(e),
            )
            return 0
        if ok:
            logger.info(
                "[CD2] Started copy from cloud to local: %d item(s) -> %s",
                len(unique_paths),
                cd2_local_dest,
            )
            return len(unique_paths)
        else:
            logger.error(
                "[CD2] Failed to start cloud-to-local copy for %d item(s)",
                len(unique_paths),
            )
            return 0

    async def _sync_selected_torrents(
        self,
        client: DownloadClient,
        cfg,
        torrents: list[dict],
    ) -> int:
        offline_target = self._offline_target(cfg)
        local_base = self._sync_local_base(cfg)
        cd2_local_dest = self._cd2_local_dest(cfg)

        if offline_target and cd2_local_dest and local_base is not None:
            copies_initiated = await self._ensure_local_copy(
                cfg, torrents, offline_target, cd2_local_dest, local_base
            )
        else:
            copies_initiated = 0
            if offline_target and local_base is not None and not cd2_local_dest:
                logger.warning(
                    "[CD2] Cannot map local_path to CD2 absolute path; "
                    "use /cd2-offline or a /volume1/... path"
                )

        if local_base is None:
            logger.warning("[CD2] local_path is not configured for sync")
            return 0
        if not local_base.exists():
            logger.warning("[CD2] local path does not exist: %s", local_base)
            return 0

        return copies_initiated + await self._sync_pending_from_local(
            client, torrents, local_base, cfg
        )

    async def submit_torrents_by_hash(self, hashes: list[str]) -> dict:
        """Manually submit selected qB torrents to CD2 offline download."""
        cfg = settings.cd2
        if not cfg.enable:
            return {
                "success": False,
                "submitted": 0,
                "skipped": 0,
                "failed": 0,
                "msg_en": "CD2 fallback is disabled. Enable it in settings first.",
                "msg_zh": "CD2 回退未启用，请先在设置中开启。",
            }
        if not cfg.host or not cfg.username:
            return {
                "success": False,
                "submitted": 0,
                "skipped": 0,
                "failed": 0,
                "msg_en": "CD2 host or username is not configured.",
                "msg_zh": "CD2 地址或用户名未配置。",
            }

        offline_target = self._offline_target(cfg)
        if not offline_target:
            return {
                "success": False,
                "submitted": 0,
                "skipped": 0,
                "failed": 0,
                "msg_en": OFFLINE_DIR_HINT_EN,
                "msg_zh": OFFLINE_DIR_HINT_ZH,
            }

        wanted = {normalize_info_hash(h) for h in hashes if h}
        accepted, cooled = claim_manual_repairs(wanted)
        if cooled:
            logger.info(
                "[CD2] Manual repair cooling down, skip: %s", sorted(cooled)
            )
        if not accepted:
            return {
                "success": True,
                "submitted": 0,
                "synced": 0,
                "skipped": len(cooled),
                "failed": 0,
                "msg_en": (
                    "These torrents were just sent. Wait a few seconds "
                    "before trying again."
                ),
                "msg_zh": "这些任务刚刚发送过，请几秒后再试。",
            }
        logger.info("[CD2] Manual repair requested hashes: %s", sorted(accepted))
        wanted = accepted
        submitted = failed = 0
        skipped = len(cooled)
        copies_started = 0
        pending_sync: list[dict] = []
        keep_cooldown: set[str] = set()

        try:
            async with CD2Session.from_cfg(cfg) as session:
                self._session = session
                try:
                    offline_tasks = await self._load_offline_tasks(cfg)

                    async with _cd2_submit_lock, DownloadClient() as client:
                        torrents = await client.get_torrent_info(
                            category="Bangumi", status_filter=None
                        )
                        for torrent in torrents:
                            torrent_hash = torrent.get("hash", "")
                            norm_hash = normalize_info_hash(torrent_hash)
                            if norm_hash not in wanted:
                                continue

                            logger.info(
                                "[CD2] Manual repair matched torrent: %s %s",
                                norm_hash,
                                torrent.get("name", ""),
                            )
                            pending_sync.append(torrent)
                            try:
                                result = await self._submit_torrent(
                                    client,
                                    torrent,
                                    cfg,
                                    force=True,
                                    offline_tasks=offline_tasks,
                                )
                            except Exception as e:
                                detail = str(e).strip() or type(e).__name__
                                logger.exception(
                                    "[CD2] Submit error for %s: %s",
                                    torrent.get("name", norm_hash[:8]),
                                    detail,
                                )
                                failed += 1
                                continue

                            if result == "submitted":
                                submitted += 1
                                keep_cooldown.add(norm_hash)
                                await client.add_tag(torrent_hash, CD2_SUBMITTED_TAG)
                                await client.add_tag(
                                    torrent_hash, CD2_FORCE_REPAIR_TAG
                                )
                                offline_tasks = await self._load_offline_tasks(cfg)
                            elif result == "copy_started":
                                copies_started += 1
                                keep_cooldown.add(norm_hash)
                                await client.add_tag(torrent_hash, CD2_SUBMITTED_TAG)
                                await client.add_tag(
                                    torrent_hash, CD2_FORCE_REPAIR_TAG
                                )
                            elif result == "synced":
                                copies_started += 1
                                keep_cooldown.add(norm_hash)
                            elif result == "pending_sync":
                                copies_started += 1
                                keep_cooldown.add(norm_hash)
                            elif result.startswith("skipped_"):
                                skipped += 1
                                keep_cooldown.add(norm_hash)
                            elif result == "failed":
                                failed += 1

                        synced = copies_started
                        if pending_sync:
                            # Copy still runs, but its path count is not another
                            # batch of tasks. The toast already counted each
                            # selected torrent once above.
                            await self._sync_selected_torrents(
                                client, cfg, pending_sync
                            )
                finally:
                    self._session = None

            ok = submitted > 0 or synced > 0
            msg_en = (
                f"Submitted {submitted}, synced {synced}, "
                f"skipped {skipped}, failed {failed}."
            )
            msg_zh = (
                f"已提交 {submitted} 个，同步 {synced} 个，"
                f"跳过 {skipped} 个，失败 {failed} 个。"
            )
            if cooled:
                msg_en += f" {len(cooled)} were just sent."
                msg_zh += f"其中 {len(cooled)} 个刚刚发送过。"
            return {
                "success": ok,
                "submitted": submitted,
                "synced": synced,
                "skipped": skipped,
                "failed": failed,
                "msg_en": msg_en,
                "msg_zh": msg_zh,
            }
        finally:
            release_manual_repairs(set(accepted) - keep_cooldown)
            finish_manual_repairs(keep_cooldown)

    async def _submit_torrent(
        self,
        client: DownloadClient,
        torrent: dict,
        cfg,
        force: bool = False,
        offline_tasks: dict[str, object] | None = None,
    ) -> str:
        torrent_hash = torrent.get("hash", "")
        name = torrent.get("name", torrent_hash[:8])
        norm_hash = normalize_info_hash(torrent_hash)
        offline_item = (offline_tasks or {}).get(norm_hash)

        if force and self._qb_content_complete(torrent):
            logger.info("[CD2] qB local content is ready, force recheck: %s", name)
            await self._recheck_and_resume(client, torrent_hash, cfg)
            await self._clear_cd2_submitted(client, torrent_hash)
            return "synced"

        if is_complete_torrent(torrent) and not force:
            logger.info(
                "[CD2] Skip completed torrent with complete local content: %s",
                name,
            )
            return "skipped_complete"

        if not force:
            stall_seconds = max(cfg.stall_time, 1) * 60
            min_speed_bps = max(cfg.stall_min_speed, 0) * 1024
            if not is_dead_torrent(torrent, stall_seconds, min_speed_bps):
                return "skipped_active"

        offline_target = self._offline_target(cfg)
        if not offline_target:
            logger.error("[CD2] offline_dir is not configured: %s", name)
            return "failed"

        if not self._staging_content_missing(torrent, cfg):
            logger.info(
                "[CD2] Local staging already has %s, skip offline",
                name,
            )
            return "pending_sync" if force else "skipped_offline"

        # 115 的离线任务和网盘文件是分开的：删掉文件，任务还在。
        # 文件还在就只复制。再 AddOffline 会在同一文件夹里生成「视频A（1）」，
        # 随后的复制会把整个文件夹拉下来。
        existing_paths = await self._existing_cloud_paths(
            offline_target,
            torrent,
            offline_item,
        )
        if existing_paths:
            return await self._copy_cloud_only(cfg, name, existing_paths)

        try:
            magnet = torrent_to_magnet(torrent)
        except ValueError:
            logger.warning("[CD2] Skip torrent without hash: %s", name)
            return "failed"

        if cfg.pause_qb_torrent and torrent_hash:
            await client.pause_torrent(torrent_hash)

        if offline_item:
            status = getattr(offline_item, "status", None)
            if offline_task_should_be_replaced(status):
                ok = await self._replace_offline_task(
                    offline_target,
                    norm_hash,
                    magnet,
                    name,
                )
            elif await self._session.restart_offline_magnet(
                offline_target,
                norm_hash,
                magnet,
                offline_item,
            ):
                logger.info(
                    "[CD2] Restarted in-progress offline task with no file yet: %s",
                    name,
                )
                ok = True
            else:
                logger.error("[CD2] Failed to restart existing offline task: %s", name)
                ok = False
            if not ok:
                if cfg.pause_qb_torrent and torrent_hash:
                    await client.resume_torrent(torrent_hash)
                return "failed"
            await client.add_tag(torrent_hash, CD2_SUBMITTED_TAG)
            return "submitted"

        try:
            result = await self._session.add_offline_magnet(
                magnet,
                offline_target,
            )
        except OfflineLocalFsNotSupportedError as e:
            logger.error("[CD2] Offline submit rejected for local path: %s", e)
            if cfg.pause_qb_torrent and torrent_hash:
                await client.resume_torrent(torrent_hash)
            return "failed"

        if result == "duplicate":
            # 列表没对上，但 115 说任务已存在。再按文件有没有决定复制还是重启。
            duplicate_paths = await self._existing_cloud_paths(
                offline_target,
                torrent,
                offline_item,
            )
            if duplicate_paths:
                return await self._copy_cloud_only(cfg, name, duplicate_paths)
            if await self._replace_offline_task(
                offline_target,
                norm_hash,
                magnet,
                name,
            ):
                await client.add_tag(torrent_hash, CD2_SUBMITTED_TAG)
                return "submitted"
            logger.error(
                "[CD2] Offline task already exists and replace failed: %s",
                name,
            )
            if cfg.pause_qb_torrent and torrent_hash:
                await client.resume_torrent(torrent_hash)
            return "failed"

        if result != "ok":
            logger.error("[CD2] Failed to submit offline task: %s", name)
            if cfg.pause_qb_torrent and torrent_hash:
                await client.resume_torrent(torrent_hash)
            return "failed"

        await client.add_tag(torrent_hash, CD2_SUBMITTED_TAG)
        logger.info("[CD2] Submitted offline download (qB kept): %s", name)
        return "submitted"

    async def _sync_torrent_to_qb(
        self,
        client: DownloadClient,
        torrent: dict,
        src: Path,
        cfg,
    ) -> bool:
        torrent_hash = torrent.get("hash", "")
        name = torrent.get("name", "")
        save_path = Path(torrent.get("save_path", ""))
        content_path = torrent.get("content_path")
        dst = Path(content_path) if content_path else save_path
        if not dst:
            logger.warning("[CD2] Torrent has no save_path: %s", name)
            return False

        try:
            await client.pause_torrent(torrent_hash)
            move_content_to_qb(src, dst)
            if _is_versioned_release(torrent):
                await self._force_versioned_jellyfin_rename(client, torrent)
            await self._recheck_and_resume(client, torrent_hash, cfg)
            await self._clear_cd2_submitted(client, torrent_hash)
            logger.info("[CD2] Synced + recheck for reseed: %s -> %s", src, dst)
            return True
        except Exception as e:
            logger.error("[CD2] Sync/recheck failed for %s: %s", name, e)
            return False

    async def _force_versioned_jellyfin_rename(
        self,
        client: DownloadClient,
        torrent: dict,
    ) -> None:
        """Ensure v2/v3 repaired content lands on AB's final Jellyfin filename.

        qB keeps each torrent's own file path. If a non-v2 release later filled
        the same AB target filename, qB's renameFile can 409 on the v2 repair.
        For manual versioned repairs we intentionally replace that target so
        Jellyfin indexes the fixed release.
        """
        torrent_hash = torrent.get("hash", "")
        if not torrent_hash:
            return

        files = await client.get_torrent_files(torrent_hash)
        media_list, _subtitle_list = check_files(files)
        if len(media_list) != 1:
            return

        save_path_text = torrent.get("save_path", "")
        torrent_name = torrent.get("name", "")
        save_path = Path(save_path_text)
        if not save_path_text or not torrent_name:
            return

        renamer = Renamer(client)
        offset_map = await renamer._batch_lookup_offsets([torrent])
        if torrent_hash not in offset_map:
            return
        episode_offset, season_offset, episode_type = offset_map[torrent_hash]
        bangumi_name, season = path_to_bangumi(save_path_text, torrent_name)
        ep = renamer._parser.torrent_parser(
            torrent_name=torrent_name,
            torrent_path=media_list[0],
            season=season,
            episode_type=episode_type,
        )
        if not ep:
            return

        new_path = renamer.gen_path(
            ep,
            bangumi_name,
            method=settings.bangumi_manage.rename_method,
            episode_offset=episode_offset,
            season_offset=season_offset,
        )
        old_path = media_list[0]
        if old_path == new_path:
            return

        target = save_path / new_path
        current = save_path / old_path
        try:
            same_file = target.exists() and current.exists() and target.samefile(current)
        except OSError:
            same_file = False

        if target.exists() and not same_file:
            target.unlink()

        if await client.rename_torrent_file(
            _hash=torrent_hash,
            old_path=old_path,
            new_path=new_path,
        ):
            logger.info(
                "[CD2] Versioned repair renamed for Jellyfin: %s -> %s",
                old_path,
                new_path,
            )

    async def _force_recheck(self, client: DownloadClient, torrent_hash: str) -> None:
        """Force qB recheck without requiring upstream DownloadClient changes."""
        concrete = getattr(client, "client", None)
        if concrete is None or not hasattr(concrete, "_post"):
            raise RuntimeError("Downloader does not expose qB recheck API")
        resp = await concrete._post(
            "torrents/recheck",
            data={"hashes": torrent_hash},
        )
        if resp.status_code >= 300:
            raise RuntimeError(f"qB recheck failed: HTTP {resp.status_code}")

    async def _clear_cd2_submitted(
        self,
        client: DownloadClient,
        torrent_hash: str,
    ) -> None:
        """Clear the CD2 handoff tag after qB recheck/resume completes."""
        concrete = getattr(client, "client", None)
        if concrete is None or not hasattr(concrete, "_post"):
            return
        resp = await concrete._post(
            "torrents/removeTags",
            data={
                "hashes": torrent_hash,
                "tags": f"{CD2_SUBMITTED_TAG},{CD2_FORCE_REPAIR_TAG}",
            },
        )
        if resp.status_code >= 300:
            logger.warning(
                "[CD2] Failed to clear handoff tag for %s: HTTP %s",
                torrent_hash,
                resp.status_code,
            )

    async def _wait_until_recheck_complete(
        self,
        client: DownloadClient,
        torrent_hash: str,
        *,
        timeout: int = 300,
    ) -> str:
        """Wait until qB reports the torrent complete after force-recheck."""
        deadline = __import__("time").time() + timeout
        last_state = ""
        last_progress = 0.0
        last_amount_left = -1
        while __import__("time").time() < deadline:
            torrents = await client.get_torrent_info(
                category="Bangumi",
                status_filter=None,
            )
            for torrent in torrents:
                if normalize_info_hash(torrent.get("hash", "")) == normalize_info_hash(
                    torrent_hash
                ):
                    last_state = torrent.get("state", "")
                    last_progress = torrent.get("progress", 0) or 0
                    last_amount_left = torrent.get("amount_left", -1)
                    state_lower = last_state.lower()
                    if (
                        "checking" not in state_lower
                        and (last_progress >= 1.0 or last_amount_left == 0)
                    ):
                        return last_state
                    break
            await asyncio.sleep(2)
        logger.warning(
            "[CD2] qB recheck wait timed out: %s state=%s progress=%.4f amount_left=%s",
            torrent_hash,
            last_state,
            last_progress,
            last_amount_left,
        )
        return last_state

    async def _recheck_and_resume(self, client: DownloadClient, torrent_hash: str, cfg) -> None:
        await self._force_recheck(client, torrent_hash)
        if not cfg.resume_after_recheck:
            return
        # qB may briefly keep the torrent stopped after recheck. Wait until
        # qB itself reports 100% before starting, matching the WebUI resume path.
        state = await self._wait_until_recheck_complete(client, torrent_hash)
        await client.resume_torrent(torrent_hash)
        logger.info("[CD2] qB recheck finished/resumed: %s state=%s", torrent_hash, state)

    async def _sync_pending_from_local(
        self,
        client: DownloadClient,
        pending: list[dict],
        local_base: Path,
        cfg,
    ) -> int:
        synced = 0
        for torrent in pending:
            torrent_hash = torrent.get("hash", "")
            norm_hash = normalize_info_hash(torrent_hash)
            name = torrent.get("name", "")
            src = resolve_cd2_content(local_base, name, norm_hash)
            if not src:
                continue
            expected_size = torrent.get("total_size") or torrent.get("size") or 0
            if not is_local_download_complete(src, expected_size):
                logger.debug("[CD2] Local download not complete yet: %s", name)
                continue
            if await self._sync_torrent_to_qb(client, torrent, src, cfg):
                synced += 1
        return synced

    async def _sync_pending_from_api(
        self,
        client: DownloadClient,
        pending: list[dict],
        finished: dict[str, object],
        local_base: Path | None,
        cfg,
    ) -> int:
        synced = 0
        for torrent in pending:
            torrent_hash = torrent.get("hash", "")
            norm_hash = normalize_info_hash(torrent_hash)
            offline = finished.get(norm_hash)
            if not offline:
                continue

            name = torrent.get("name", "")
            if not local_base:
                logger.warning(
                    "[CD2] Offline finished but local_path not configured: %s",
                    name,
                )
                continue

            offline_name = getattr(offline, "name", "") or ""
            src = None
            for exact_name in (offline_name, name):
                if not exact_name:
                    continue
                src = resolve_cd2_content(local_base, exact_name, norm_hash)
                if src:
                    break
            if not src:
                logger.warning(
                    "[CD2] Finished offline but local files not found: %s",
                    name,
                )
                continue

            if await self._sync_torrent_to_qb(client, torrent, src, cfg):
                synced += 1
        return synced

    async def _recheck_ready_qb_content(
        self,
        client: DownloadClient,
        pending: list[dict],
        cfg,
    ) -> tuple[int, list[dict]]:
        """Recheck torrents whose qB target already contains complete files."""
        synced = 0
        remaining: list[dict] = []
        for torrent in pending:
            if _has_cd2_force_repair_tag(torrent):
                remaining.append(torrent)
                continue
            if not self._qb_content_complete(torrent):
                remaining.append(torrent)
                continue
            name = torrent.get("name", "")
            torrent_hash = torrent.get("hash", "")
            try:
                logger.info(
                    "[CD2] qB content already complete; recheck only: %s",
                    name,
                )
                await self._recheck_and_resume(client, torrent_hash, cfg)
                await self._clear_cd2_submitted(client, torrent_hash)
                synced += 1
            except Exception as e:
                logger.error("[CD2] Recheck-ready torrent failed for %s: %s", name, e)
                remaining.append(torrent)
        return synced, remaining

    def _warn_offline_list_once(self, message: str, *args) -> None:
        """115 often fails the same way for every list in one scan."""
        if getattr(self, "_offline_list_warned", False):
            logger.debug(message, *args)
            return
        self._offline_list_warned = True
        logger.warning(message, *args)

    async def process(self) -> tuple[int, int]:
        self._offline_list_warned = False
        cfg = settings.cd2
        if not cfg.enable:
            return 0, 0

        async with CD2Session.from_cfg(cfg) as session:
            self._session = session
            try:
                try:
                    updated = await session.prefer_copy_over_backup()
                except Exception as e:
                    logger.warning("[CD2] Could not prefer copy tasks over backup: %s", e)
                else:
                    if updated:
                        logger.info(
                            "[CD2] Upload scheduling runs copy tasks before backup: %s",
                            ", ".join(updated),
                        )
                try:
                    submitted = await self.process_stalled_torrents()
                except Exception as e:
                    logger.error("[CD2] Stalled scan failed: %s", e)
                    submitted = 0
                try:
                    synced = await self.process_cd2_completed()
                except Exception as e:
                    logger.error("[CD2] Completed sync failed: %s", e)
                    synced = 0
            finally:
                self._session = None
        return submitted, synced

    async def process_stalled_torrents(self) -> int:
        cfg = settings.cd2
        if not cfg.enable:
            return 0
        if not cfg.host or not cfg.username:
            logger.warning("[CD2] Enabled but host/username is empty, skip scan")
            return 0
        if not self._offline_target(cfg):
            logger.warning("[CD2] offline_dir is not configured, skip stalled scan")
            return 0

        stall_seconds = max(cfg.stall_time, 1) * 60
        min_speed_bps = max(cfg.stall_min_speed, 0) * 1024
        submitted = 0
        offline_tasks = await self._load_offline_tasks(cfg)

        async with _cd2_submit_lock, DownloadClient() as client:
            torrents = await client.get_torrent_info(
                category="Bangumi", status_filter=None
            )
            for torrent in torrents:
                if submitted >= MAX_AUTO_SUBMISSIONS_PER_CYCLE:
                    logger.info(
                        "[CD2] Auto submission limit reached (%d/cycle)",
                        MAX_AUTO_SUBMISSIONS_PER_CYCLE,
                    )
                    break
                if not is_dead_torrent(torrent, stall_seconds, min_speed_bps):
                    continue
                if await self._submit_torrent(
                    client,
                    torrent,
                    cfg,
                    offline_tasks=offline_tasks,
                ) == "submitted":
                    submitted += 1
                    offline_tasks = await self._load_offline_tasks(cfg)

        return submitted

    async def _load_offline_tasks(self, cfg) -> dict[str, object]:
        offline_target = self._offline_target(cfg)
        if not offline_target:
            return {}
        try:
            return await self._session.list_all_offline_by_hash(
                offline_target,
            )
        except OfflineLocalFsNotSupportedError:
            return {}
        except Exception as e:
            self._warn_offline_list_once(
                "[CD2] Offline list failed, new submits will not reuse existing tasks: %s",
                brief_rpc_error(e),
            )
            return {}

    async def _incomplete_bangumi_torrents(self, client: DownloadClient) -> list[dict]:
        torrents = await client.get_torrent_info(
            category="Bangumi", status_filter=None
        )
        return [
            torrent
            for torrent in torrents
            if _has_cd2_submitted_tag(torrent)
            and (not is_complete_torrent(torrent) or _has_cd2_force_repair_tag(torrent))
        ]

    async def process_cd2_completed(self) -> int:
        """When CD2 offline completes, move files to qB path and force recheck."""
        cfg = settings.cd2
        if not cfg.enable:
            return 0

        local_base = self._sync_local_base(cfg)
        offline_target = self._offline_target(cfg)
        cd2_local_dest = self._cd2_local_dest(cfg)

        if cd2_local_dest and self._session is not None:
            try:
                removed = await self._session.remove_completed_copy_tasks(cd2_local_dest)
            except Exception as e:
                logger.warning("[CD2] Remove completed copy tasks failed: %s", e)
            else:
                if removed:
                    logger.info("[CD2] Removed %d completed copy task(s)", removed)

        async with DownloadClient() as client:
            pending = await self._incomplete_bangumi_torrents(client)
            if not pending:
                return 0

            synced, pending = await self._recheck_ready_qb_content(
                client,
                pending,
                cfg,
            )
            if not pending:
                return synced

            if offline_target and cd2_local_dest and local_base is not None:
                await self._ensure_local_copy(
                    cfg, pending, offline_target, cd2_local_dest, local_base
                )
            elif offline_target and local_base is not None and not cd2_local_dest:
                logger.warning(
                    "[CD2] Cannot map local_path to CD2 absolute path; "
                    "use /cd2-offline or a /volume1/... path"
                )
            if cd2_local_dest and self._session is not None:
                try:
                    resumed = await self._session.resume_paused_copy_uploads(
                        cd2_local_dest,
                        MAX_RESUME_COPY_UPLOADS_PER_CYCLE,
                    )
                except Exception as e:
                    logger.warning("[CD2] Resume paused copies failed: %s", e)
                else:
                    if resumed:
                        logger.info(
                            "[CD2] Resumed %d paused copy upload(s) -> %s",
                            resumed,
                            cd2_local_dest,
                        )

            if local_base is not None:
                if not local_base.exists():
                    logger.warning("[CD2] local path does not exist: %s", local_base)
                    return 0
                finished: dict = {}
                if offline_target and self._session is not None:
                    try:
                        finished = await self._session.list_finished_offline_by_hash(
                            offline_target,
                        )
                    except Exception as e:
                        self._warn_offline_list_once(
                            "[CD2] Offline list failed, will not pair files by name: %s",
                            brief_rpc_error(e),
                        )
                        finished = {}
                if finished:
                    logger.debug(
                        "[CD2] Syncing %d finished offline hash(es) from %s",
                        len(finished),
                        local_base,
                    )
                    return synced + await self._sync_pending_from_api(
                        client, pending, finished, local_base, cfg
                    )
                if not self._offline_list_warned:
                    logger.warning(
                        "[CD2] No offline hash map; only this torrent's exact folder "
                        "under %s can be moved",
                        local_base,
                    )
                return synced + await self._sync_pending_from_local(
                    client, pending, local_base, cfg
                )

            if not offline_target:
                logger.warning("[CD2] local_path is not configured for sync")
                return synced

            try:
                finished = await self._session.list_finished_offline_by_hash(
                    offline_target,
                )
            except OfflineLocalFsNotSupportedError as e:
                logger.warning("[CD2] Offline list API unavailable (%s)", e)
                return synced
            except Exception as e:
                self._warn_offline_list_once(
                    "[CD2] Offline list failed (%s)",
                    brief_rpc_error(e),
                )
                return synced

            if not finished:
                return synced

            return synced + await self._sync_pending_from_api(
                client, pending, finished, local_base, cfg
            )
