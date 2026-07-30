import logging
import re
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


def _normalize_match_name(name: str) -> str:
    stem = Path(name).stem if "." in name else name
    return re.sub(r"[\s._\-+\[\]()（）【】]+", "", stem.lower())


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


def find_video_in_src(src: Path, dst_name: str = "") -> Path | None:
    """Pick the video file inside a CD2 offline wrapper folder."""
    if src.is_file():
        return src

    videos = _list_video_files(src)
    if not videos:
        return None

    if dst_name:
        for video in videos:
            if video.name == dst_name:
                return video
        lower = dst_name.lower()
        for video in videos:
            if video.name.lower() == lower:
                return video

    if len(videos) == 1:
        return videos[0]

    if dst_name:
        dst_norm = _normalize_match_name(dst_name)
        for video in videos:
            if _normalize_match_name(video.name) == dst_norm:
                return video

    return max(videos, key=lambda item: item.stat().st_size)


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


def join_cloud_path(base: str, name: str) -> str:
    return f"{base.rstrip('/')}/{name.lstrip('/')}"


def resolve_cd2_content(local_base: Path, torrent_name: str, info_hash: str) -> Path | None:
    """Find downloaded content directory/file under CD2 local mount."""
    if not local_base.exists():
        return None

    candidates = [
        local_base / torrent_name,
        local_base / info_hash,
        local_base / info_hash.upper(),
    ]
    for path in candidates:
        if path.exists():
            return path

    needle = info_hash.lower()
    norm_torrent = _normalize_match_name(torrent_name)
    for child in local_base.iterdir():
        if needle in child.name.lower():
            return child
        if norm_torrent and _normalize_match_name(child.name) == norm_torrent:
            return child
        if child.is_dir() and child.suffix.lower() in _VIDEO_SUFFIXES:
            inner = find_video_in_src(child, torrent_name)
            if inner:
                return child
    return None


def local_content_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def is_local_download_complete(src: Path, expected_size: int) -> bool:
    """Heuristic: local folder/file size matches qB torrent total size."""
    actual = local_content_size(src)
    if actual <= 0:
        return False
    if expected_size <= 0:
        return True
    return actual >= expected_size * 0.98


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
