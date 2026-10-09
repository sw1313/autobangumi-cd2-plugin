"""CD2 extension bootstrap — patches upstream at runtime, no upstream file edits."""

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_INSTALLED = False


def _ensure_app_path() -> None:
    app_dir = Path(os.environ.get("AB_APP_DIR", "/app"))
    if app_dir.is_dir():
        app_str = str(app_dir)
        if app_str not in sys.path:
            sys.path.insert(0, app_str)


def _ensure_path() -> None:
    _ensure_app_path()
    roots = [Path("/extensions/cd2/backend")]
    try:
        app_root = Path(__file__).resolve().parents[3]
        roots.append(app_root / "extensions" / "cd2" / "backend")
    except IndexError:
        pass
    for root in roots:
        if (root / "cd2" / "__init__.py").is_file():
            root_str = str(root)
            if root_str not in sys.path:
                sys.path.insert(0, root_str)
            return
    logger.warning(
        "CD2 extension backend not found; checked: %s",
        ", ".join(str(r) for r in roots),
    )


def _patch_app_context_build() -> None:
    import module.core.context as ctx_mod

    original = ctx_mod.AppContext.build.__func__

    @classmethod
    def build(cls, settings_obj):
        from cd2 import get_scheduler_tasks
        from cd2.config import attach_cd2_settings

        attach_cd2_settings(settings_obj)
        ctx = original(cls, settings_obj)
        ctx.scheduler._tasks.extend(get_scheduler_tasks(settings_obj))
        return ctx

    ctx_mod.AppContext.build = build  # type: ignore[method-assign]


def _patch_settings_reload() -> None:
    import module.core.context as ctx_mod

    original = ctx_mod.AppContext._reload_settings_unlocked

    async def _reload_settings_unlocked(self):
        await original(self)
        from cd2.config import attach_cd2_settings

        attach_cd2_settings(self.settings)

    ctx_mod.AppContext._reload_settings_unlocked = _reload_settings_unlocked


def _register_api_routes() -> None:
    import module.api as api_mod
    from cd2.api import router

    api_mod.v1.include_router(router)


def is_installed() -> bool:
    return _INSTALLED


def install() -> None:
    """Patch upstream and register CD2 routes. Call before ``main`` starts."""
    global _INSTALLED
    if _INSTALLED:
        return
    _ensure_path()
    try:
        import cd2  # noqa: F401
    except ImportError as exc:
        logger.error("CD2 extension not loaded: %s", exc)
        return
    try:
        _patch_app_context_build()
        _patch_settings_reload()
    except Exception:
        logger.exception("CD2 bootstrap context patch failed")
        return
    try:
        _register_api_routes()
    except Exception:
        logger.exception("CD2 bootstrap API registration failed")
        return
    try:
        from cd2.config import attach_cd2_settings
        from module.conf import settings

        attach_cd2_settings(settings)
    except Exception:
        logger.exception("CD2 settings attach failed (API routes are still active)")
    try:
        from cd2.add_tag_retry import install_add_tag_retry

        install_add_tag_retry()
    except Exception:
        logger.exception("CD2 add_tag retry patch failed")
    _INSTALLED = True
    logger.info("CD2 extension installed via bootstrap")
