import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_VIDEO_SUFFIXES = {".mkv", ".mp4", ".avi", ".wmv", ".flv", ".ts", ".m4v"}

_CLOUD_PATH_PREFIXES = (
    "/115/",
    "/189/",
    "/123/",
    "/aliyundrive/",
    "/onedrive/",
    "/googledrive/",
)


_PATH_MAPPINGS: tuple[tuple[str, str], ...] = (
    ("/volume1/videos/cd2-offline", "/cd2-offline"),
    ("/volume1/videos/anime/cd2-offline", "/cd2-offline"),
    ("/volume1/videos/anime", "/anime"),
)
_CONTAINER_TO_HOST: dict[str, str] = {}
for _host_prefix, _container_prefix in _PATH_MAPPINGS:
    # Keep the first host mapping per container path; later legacy aliases
    # must not override the canonical cd2-offline mount.
    _CONTAINER_TO_HOST.setdefault(_container_prefix, _host_prefix)


def is_local_fs_path(path: str) -> bool:
    """True for NAS/container paths; CD2 offline APIs require cloud paths instead."""
    normalized = (path or "").replace("\\", "/").rstrip("/")
    if not normalized:
        return False
    lower = normalized.lower()
    if any(lower.startswith(prefix) for prefix in _CLOUD_PATH_PREFIXES):
        return False
    if lower.startswith(("/volume", "/mnt/", "/home/", "/var/", "/tmp/")):
        return True
    if lower.startswith(("/anime", "/cd2-offline")):
        return True
    return False


def resolve_offline_target(offline_dir: str = "", target_dir: str = "") -> str | None:
    """CD2 cloud path for AddOfflineFiles (e.g. /115/Anime/cd2-offline)."""
    for path in (offline_dir, target_dir):
        candidate = (path or "").strip().rstrip("/")
        if candidate and not is_local_fs_path(candidate):
            return candidate
    return None


def resolve_sync_local_base(local_path: str = "", target_dir: str = "") -> Path | None:
    """AB-visible path where CD2 offline files appear locally."""
    explicit = (local_path or "").strip()
    if explicit:
        return Path(explicit)
    legacy = (target_dir or "").strip()
    if legacy and is_local_fs_path(legacy):
        return resolve_local_base(legacy, "")
    return None


def resolve_local_base(target_dir: str, local_path: str = "") -> Path | None:
    """Map CD2 target_dir to a path visible inside AutoBangumi container."""
    if local_path:
        return Path(local_path)

    normalized = target_dir.replace("\\", "/").rstrip("/")
    for host_prefix, container_prefix in _PATH_MAPPINGS:
        if normalized.startswith(host_prefix):
            suffix = normalized[len(host_prefix) :].lstrip("/")
            base = Path(container_prefix)
            return base / suffix if suffix else base

    if normalized.startswith(("/anime", "/cd2-offline")):
        return Path(normalized)
    return None


def _list_video_files(root: Path) -> list[Path]:
    if root.is_file() and root.suffix.lower() in _VIDEO_SUFFIXES:
        return [root]
    if not root.is_dir():
        return []
    return [
        item
        for item in root.rglob("*")
        if item.is_file() and item.suffix.lower() in _VIDEO_SUFFIXES
    ]


def _same_file_name(left: str, right: str) -> bool:
    return left == right or left.lower() == right.lower()


def find_video_in_src(src: Path, dst_name: str = "") -> Path | None:
    """Return the video that belongs to this path.

    A requested destination name must match. Another video in the same folder
    is a different file and must not be substituted.
    """
    if src.is_file():
        if dst_name and not _same_file_name(src.name, dst_name):
            return None
        return src

    videos = _list_video_files(src)
    if not videos:
        return None

    if dst_name:
        for video in videos:
            if _same_file_name(video.name, dst_name):
                return video
        return None

    if len(videos) == 1:
        return videos[0]
    return None


def resolve_cd2_local_dest(local_path: str = "", target_dir: str = "") -> str | None:
    """Map AB local_path to the CD2-visible absolute path (same folder, two views)."""
    lp = (local_path or "").strip().replace("\\", "/").rstrip("/")
    if lp.startswith("/volume"):
        return lp

    mappings = tuple(_CONTAINER_TO_HOST.items())
    for container_prefix, host_prefix in sorted(
        mappings, key=lambda item: len(item[0]), reverse=True
    ):
        if lp.startswith(container_prefix):
            suffix = lp[len(container_prefix) :].lstrip("/")
            base = host_prefix.rstrip("/")
            return f"{base}/{suffix}" if suffix else base

    legacy = (target_dir or "").strip().replace("\\", "/").rstrip("/")
    if legacy and is_local_fs_path(legacy):
        return legacy
    return None


def completed_copy_task_keys(
    tasks: list,
    dest_prefix: str,
    *,
    completed_status: int = 3,
) -> list[str]:
    """Copy-task keys CD2 accepts: ``sourcePath:destPath``, unchanged.

    Only completed tasks whose destination is the local staging folder are
    included. Other destinations and unfinished tasks stay in the list.
    """
    prefix = (dest_prefix or "").replace("\\", "/").rstrip("/")
    if not prefix:
        return []
    keys: list[str] = []
    for task in tasks:
        if getattr(task, "status", None) != completed_status:
            continue
        source = getattr(task, "sourcePath", "") or ""
        dest = getattr(task, "destPath", "") or ""
        normalized = dest.replace("\\", "/").rstrip("/")
        if normalized != prefix and not normalized.startswith(prefix + "/"):
            continue
        keys.append(f"{source}:{dest}")
    return keys


def join_cloud_path(base: str, name: str) -> str:
    return f"{base.rstrip('/')}/{name.lstrip('/')}"


def resolve_cd2_content(local_base: Path, torrent_name: str, info_hash: str) -> Path | None:
    """Return the file CD2 stored for this magnet.

    The info hash is the identity. ``torrent_name`` must be that hash's own
    name (the offline task name, or this qB torrent's name). Sibling files are
    not searched: a similar title is a different magnet.
    """
    if not local_base.exists():
        return None

    candidates: list[Path] = []
    if torrent_name:
        candidates.append(local_base / torrent_name)
    if info_hash:
        candidates.extend([local_base / info_hash, local_base / info_hash.upper()])
    for path in candidates:
        if path != local_base and path.exists():
            return path
    return None


def local_content_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def is_local_download_complete(src: Path, expected_size: int) -> bool:
    """True when local bytes match this torrent's size.

    A larger different file used to pass the old one-sided check and then
    replace the qB target.
    """
    actual = local_content_size(src)
    if actual <= 0 or expected_size <= 0:
        return False
    return abs(actual - expected_size) <= expected_size * 0.02


def _move_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    shutil.move(str(src), str(dst))
    logger.debug("[CD2] Moved %s -> %s", src, dst)


def move_content_to_qb(src: Path, dst: Path) -> None:
    """Move CD2 downloaded files into qBittorrent content path (replace partial)."""
    if not src.exists():
        raise FileNotFoundError(f"CD2 content not found: {src}")

    if dst.suffix and (not dst.exists() or dst.is_file()):
        dst.parent.mkdir(parents=True, exist_ok=True)
        video = find_video_in_src(src, dst.name)
        if not video:
            raise FileNotFoundError(f"No matching video file for {dst.name} under {src}")
        _move_file(video, dst)
        if src.is_dir() and src.exists():
            shutil.rmtree(src, ignore_errors=True)
        return

    dst.mkdir(parents=True, exist_ok=True)

    if src.is_file():
        _move_file(src, dst / src.name)
        return

    videos = _list_video_files(src)
    if len(videos) == 1 and src.is_dir() and src.suffix.lower() in _VIDEO_SUFFIXES:
        _move_file(videos[0], dst / videos[0].name)
        if src.exists():
            shutil.rmtree(src, ignore_errors=True)
        return

    for item in list(src.rglob("*")):
        if not item.is_file():
            continue
        rel = item.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        shutil.move(str(item), str(target))

    if src.is_dir() and src.exists():
        shutil.rmtree(src, ignore_errors=True)
