"""CD2 configuration — stored independently in ``config/cd2.json``."""

import json
import logging
from os.path import expandvars
from pathlib import Path

from pydantic import BaseModel, Field

from cd2.sync import is_local_fs_path, resolve_local_base

logger = logging.getLogger(__name__)

CONFIG_ROOT = Path("config")
CD2_CONFIG_PATH = (CONFIG_ROOT / "cd2.json").resolve()

_LEGACY_CD2_LOCAL_PATHS = {
    "/anime/cd2-offline": "/cd2-offline",
    "/volume1/videos/anime/cd2-offline": "/cd2-offline",
}

DEFAULT_CD2: dict = {
    "enable": False,
    "host": "",
    "username": "",
    "password": "",
    "target_dir": "",
    "offline_dir": "",
    "stall_time": 60,
    "stall_min_speed": 1,
    "scan_interval": 300,
    "local_path": "/cd2-offline",
    "pause_qb_torrent": True,
    "resume_after_recheck": True,
}


def _expand(value: str | None) -> str:
    return expandvars(value) if value else ""


class CloudDrive2Settings(BaseModel):
    """CloudDrive2 offline download fallback for stalled qBittorrent tasks."""

    enable: bool = Field(False, description="Enable CD2 offline fallback")
    host_: str = Field(
        "",
        alias="host",
        description="CD2 address, e.g. http://clouddrive.local:19798",
    )
    username_: str = Field("", alias="username", description="CD2 username")
    password_: str = Field("", alias="password", description="CD2 password")
    target_dir: str = Field(
        "",
        description="Legacy offline path; prefer offline_dir",
    )
    offline_dir: str = Field(
        "",
        description="CD2 cloud path for offline download, e.g. /115/Anime/cd2-offline",
    )
    stall_time: int = Field(
        60,
        description="Minutes before treating torrent as dead (no activity or too slow)",
    )
    stall_min_speed: int = Field(
        1,
        description="Minimum download speed in KB/s; below this after stall_time is dead (0=disable)",
    )
    scan_interval: int = Field(
        300,
        description="Scan interval in seconds for stalled torrents",
    )
    local_path: str = Field(
        "/cd2-offline",
        description="AB container path for CD2 offline sync, e.g. /cd2-offline",
    )
    pause_qb_torrent: bool = Field(
        True,
        description="Pause qBittorrent task before CD2 offline download",
    )
    resume_after_recheck: bool = Field(
        True,
        description="Resume torrent after force recheck for seeding",
    )

    @property
    def host(self):
        return _expand(self.host_)

    @property
    def username(self):
        return _expand(self.username_)

    @property
    def password(self):
        return _expand(self.password_)

    def model_dump(self, *args, by_alias=True, **kwargs):
        return super().model_dump(*args, by_alias=by_alias, **kwargs)


def migrate_cd2_config(cd2: dict) -> dict:
    """Normalize legacy CD2 path fields."""
    result = dict(cd2 or {})
    target = (result.get("target_dir") or "").strip()
    offline = (result.get("offline_dir") or "").strip()
    local = (result.get("local_path") or "").strip()

    if target:
        if is_local_fs_path(target):
            if not local:
                mapped = resolve_local_base(target, "")
                result["local_path"] = str(mapped) if mapped else "/cd2-offline"
        elif not offline:
            result["offline_dir"] = target
        result["target_dir"] = ""

    local = (result.get("local_path") or "").strip()
    if local in _LEGACY_CD2_LOCAL_PATHS:
        result["local_path"] = _LEGACY_CD2_LOCAL_PATHS[local]

    return result


def _write_cd2_file(data: dict) -> None:
    CD2_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    normalized = migrate_cd2_config(data)
    with open(CD2_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=4, ensure_ascii=False)


def _read_cd2_file() -> dict | None:
    if not CD2_CONFIG_PATH.exists():
        return None
    with open(CD2_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _main_config_path() -> Path:
    from module.conf.config import CONFIG_PATH

    return CONFIG_PATH


def migrate_legacy_from_main_config() -> None:
    """Move ``cd2`` section from config.json into config/cd2.json (one-time, idempotent)."""
    main_path = _main_config_path()
    if not main_path.exists():
        return
    with open(main_path, "r", encoding="utf-8") as f:
        main = json.load(f)
    legacy = main.get("cd2")
    if legacy is None:
        return

    existing = _read_cd2_file()
    if existing is None:
        _write_cd2_file(legacy)
        logger.info("[CD2] Migrated cd2 settings from config.json to %s", CD2_CONFIG_PATH)
    elif _cd2_has_user_data(legacy) and not _cd2_has_user_data(existing):
        _write_cd2_file(legacy)
        logger.info("[CD2] Restored cd2 settings from legacy config.json section")

    main.pop("cd2", None)
    with open(main_path, "w", encoding="utf-8") as f:
        json.dump(main, f, indent=4, ensure_ascii=False)
    logger.info("[CD2] Removed cd2 section from %s", main_path)


def _cd2_has_user_data(cd2: dict) -> bool:
    for key in ("host", "username", "password", "offline_dir", "target_dir"):
        if (cd2.get(key) or "").strip():
            return True
    return False


def load_cd2_dict() -> dict:
    migrate_legacy_from_main_config()
    data = _read_cd2_file()
    if data is None:
        return dict(DEFAULT_CD2)
    return migrate_cd2_config(data)


def save_cd2_dict(data: dict) -> dict:
    normalized = migrate_cd2_config(data)
    _write_cd2_file(normalized)
    return normalized


def _bind_cd2_settings(settings_obj, cd2: CloudDrive2Settings) -> None:
    """Attach CD2 settings without tripping Pydantic extra-field guards."""
    inner = getattr(settings_obj, "__dict__", None)
    if isinstance(inner, dict):
        inner["cd2"] = cd2
    else:
        object.__setattr__(settings_obj, "cd2", cd2)


def attach_cd2_settings(settings_obj) -> None:
    _bind_cd2_settings(
        settings_obj, CloudDrive2Settings.model_validate(load_cd2_dict())
    )


def reload_cd2_settings(settings_obj=None) -> CloudDrive2Settings:
    cfg = CloudDrive2Settings.model_validate(load_cd2_dict())
    if settings_obj is not None:
        _bind_cd2_settings(settings_obj, cfg)
    else:
        try:
            from module.conf import settings

            _bind_cd2_settings(settings, cfg)
        except ImportError:
            pass
    return cfg


def get_cd2_settings() -> CloudDrive2Settings:
    try:
        from module.conf import settings

        cd2 = getattr(settings, "cd2", None)
        if cd2 is not None:
            return cd2
    except ImportError:
        pass
    return CloudDrive2Settings.model_validate(load_cd2_dict())


def dump_cd2_settings(settings_obj) -> dict:
    cd2 = getattr(settings_obj, "cd2", None)
    if cd2 is None:
        return load_cd2_dict()
    return cd2.model_dump(by_alias=True)
