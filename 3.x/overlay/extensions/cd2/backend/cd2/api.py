import asyncio
import logging

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from module.core import AppContext
from module.security.api import get_current_user

from cd2.config import load_cd2_dict, reload_cd2_settings, save_cd2_dict

from module.api.deps import get_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/extensions/cd2", tags=["cd2-extension"])
_MASK = "********"


class TestCD2Request(BaseModel):
    host: str
    username: str
    password: str = ""


class TorrentHashesRequest(BaseModel):
    hashes: list[str]


def _is_sensitive(key: str) -> bool:
    return "password" in key.lower() or "secret" in key.lower()


def _sanitize_cd2(data: dict) -> dict:
    result = dict(data)
    for key, value in result.items():
        if _is_sensitive(key) and isinstance(value, str) and value:
            result[key] = _MASK
    return result


def _restore_cd2_password(incoming: dict, current: dict) -> dict:
    result = dict(incoming)
    if result.get("password") == _MASK:
        result["password"] = current.get("password", "")
    return result


@router.get("/config", dependencies=[Depends(get_current_user)])
async def get_cd2_config():
    """Return CD2 settings from config/cd2.json."""
    return _sanitize_cd2(load_cd2_dict())


@router.patch("/config", dependencies=[Depends(get_current_user)])
async def update_cd2_config(
    body: dict = Body(...),
    ctx: AppContext = Depends(get_context),
):
    """Save CD2 settings to config/cd2.json and reload runtime state."""
    try:
        current = load_cd2_dict()
        payload = _restore_cd2_password(body, current)
        await asyncio.to_thread(save_cd2_dict, payload)
        reload_cd2_settings(ctx.settings)
        if ctx.scheduler.running:
            await ctx.scheduler.stop_all()
            ctx.scheduler.start_all()
        return JSONResponse(
            status_code=200,
            content={
                "msg_en": "CD2 config updated.",
                "msg_zh": "CD2 配置已更新。",
            },
        )
    except Exception as e:
        logger.warning("CD2 config update failed: %s", e)
        return JSONResponse(
            status_code=406,
            content={"msg_en": "CD2 config update failed.", "msg_zh": "CD2 配置更新失败。"},
        )


@router.post("/config/test", dependencies=[Depends(get_current_user)])
async def test_cd2(req: TestCD2Request):
    """Test CloudDrive2 gRPC connection."""
    from cd2.client import test_cd2_connection

    password = req.password
    if password == _MASK:
        # The UI only receives a masked password from GET /config. Resolve the
        # real value from disk instead of runtime settings, which can lag behind
        # after bootstrap/reload failures.
        password = load_cd2_dict().get("password", "")
    logger.info("[CD2] Testing connection to %s", req.host)
    try:
        await test_cd2_connection(req.host, req.username, password)
        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "msg_en": "CloudDrive2 connection successful.",
                "msg_zh": "CloudDrive2 连接成功。",
            },
        )
    except asyncio.TimeoutError:
        logger.warning("[CD2] Connection test timed out: %s", req.host)
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "msg_en": "CloudDrive2 connection timed out (15s). Check host/port and network.",
                "msg_zh": "CloudDrive2 连接超时（15 秒），请检查地址、端口及容器网络。",
            },
        )
    except Exception as e:
        logger.warning("[CD2] Connection test failed: %s", e)
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "msg_en": f"CloudDrive2 connection failed: {e}",
                "msg_zh": f"CloudDrive2 连接失败：{e}",
            },
        )


@router.post("/downloader/torrents/cd2-repair", dependencies=[Depends(get_current_user)])
async def cd2_repair(req: TorrentHashesRequest):
    """Submit selected qBittorrent torrents to CloudDrive2 offline download."""
    from cd2.fallback import CD2FallbackManager

    result = await CD2FallbackManager().submit_torrents_by_hash(req.hashes)
    status_code = 200 if result.get("success") else 400
    return JSONResponse(status_code=status_code, content=result)
