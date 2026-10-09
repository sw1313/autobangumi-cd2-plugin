"""CD2 periodic task tick."""

import logging

logger = logging.getLogger(__name__)


async def cd2_tick() -> None:
    """Scan stalled qBittorrent tasks and sync completed CD2 offline downloads."""
    from cd2.client import CD2ConnectionError
    from cd2.fallback import CD2FallbackManager

    try:
        submitted, synced = await CD2FallbackManager().process()
    except CD2ConnectionError as e:
        logger.warning("[CD2] Skip scan because CloudDrive2 is unavailable: %s", e)
        return
    except Exception:
        logger.exception("[CD2] Error during CD2 scan")
        return
    if submitted:
        logger.info("[CD2] Submitted %d offline task(s)", submitted)
    if synced:
        logger.info("[CD2] Synced + recheck %d torrent(s) for reseed", synced)
