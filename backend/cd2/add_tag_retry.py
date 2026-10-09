"""Retry qB addTags from the plugin.

A reaped keepalive makes this POST raise httpx.ReadError with an empty
message. The other qB calls already retry that error. This one did not, so a
single dropped connection left the handoff tag unset after CD2 had accepted
the offline task. The wrapper lives here so an AutoBangumi update cannot
remove it.
"""


def wrap_add_tag(cls, decorator) -> bool:
    current = cls.__dict__.get("add_tag")
    if current is None or getattr(current, "_cd2_add_tag_retry", False):
        return False
    wrapped = decorator(current)
    wrapped._cd2_add_tag_retry = True
    cls.add_tag = wrapped
    return True


def install_add_tag_retry() -> None:
    from module.ab_decorator import qb_connect_failed_wait
    from module.downloader.client.qb_downloader import QbDownloader

    wrap_add_tag(QbDownloader, qb_connect_failed_wait)
