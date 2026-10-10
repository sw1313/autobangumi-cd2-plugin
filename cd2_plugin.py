"""4.0 插件入口。功能仍在 backend/cd2，配置表单写回 config/cd2.json。"""

import sys
from pathlib import Path

from pydantic import BaseModel, Field

from ab_sdk import Plugin


class Cd2Options(BaseModel):
    enable: bool = Field(False, title="启用 CD2 回退")
    host: str = Field(
        "",
        title="CD2 地址",
        description="例如 http://clouddrive.local:19798",
    )
    username: str = Field("", title="用户名")
    password: str = Field(
        "",
        title="密码",
        description="留空表示不修改。保存后这里保持空白，已保存的密码不会显示。",
    )
    offline_dir: str = Field(
        "",
        title="115 云盘离线路径",
        description="例如 /115/动漫/cd2-offline",
    )
    local_path: str = Field(
        "/cd2-offline",
        title="本地同步目录",
        description="容器内路径，例如 /cd2-offline",
    )
    stall_time: int = Field(60, title="卡种判定（分钟）", ge=0)
    stall_min_speed: int = Field(
        1,
        title="最低速度（KB/s）",
        description="低于这个速度并持续到卡种时间后视为卡死，0 表示不看速度",
        ge=0,
    )
    scan_interval: int = Field(300, title="扫描间隔（秒）", ge=1)
    pause_qb_torrent: bool = Field(True, title="提交前暂停 qB 任务")
    resume_after_recheck: bool = Field(True, title="校验后恢复做种")


class Cd2Plugin(Plugin[Cd2Options]):
    config_model = Cd2Options

    async def setup(self) -> None:
        backend = str(Path(__file__).resolve().parent / "backend")
        if backend not in sys.path:
            sys.path.insert(0, backend)
        from cd2.runtime import ensure

        ensure()
        from cd2.bootstrap import _install_page_script, install
        from cd2.config import attach_cd2_settings, load_cd2_dict, save_cd2_dict
        from module.conf import settings

        install()
        _install_page_script()
        current = load_cd2_dict()
        data = self.config.model_dump()
        entered = str(data.get("password") or "").strip()
        data["password"] = entered or current.get("password", "")
        save_cd2_dict(data)
        attach_cd2_settings(settings)
        options = settings.plugins.options.setdefault("cd2", {})
        if options.get("password"):
            options["password"] = ""
            settings.save()
