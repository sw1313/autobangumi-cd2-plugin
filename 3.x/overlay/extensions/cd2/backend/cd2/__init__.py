"""CloudDrive2 local extension for AutoBangumi."""


def register_api(v1_router) -> None:
    from cd2.api import router

    v1_router.include_router(router)


def get_scheduler_tasks(settings_obj):
    from cd2.config import get_cd2_settings
    from module.core.scheduler import PeriodicTask

    from cd2.loops import cd2_tick

    def _cfg():
        return get_cd2_settings()

    return [
        PeriodicTask(
            name="cd2",
            run=cd2_tick,
            interval=lambda: max(_cfg().scan_interval, 60),
            initial_delay=90,
            enabled=lambda: _cfg().enable,
        )
    ]