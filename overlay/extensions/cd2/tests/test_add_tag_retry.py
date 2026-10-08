import asyncio

from cd2.add_tag_retry import wrap_add_tag


def marker(func):
    async def wrapped(*args, **kwargs):
        return await func(*args, **kwargs)

    return wrapped


def test_wraps_add_tag_once():
    class Local:
        async def add_tag(self, info_hash, tag):
            return tag

    assert wrap_add_tag(Local, marker) is True
    assert Local.add_tag._cd2_add_tag_retry is True
    assert wrap_add_tag(Local, marker) is False


def test_wrapped_method_still_calls_the_original():
    class Local:
        async def add_tag(self, info_hash, tag):
            return tag

    wrap_add_tag(Local, marker)
    assert asyncio.run(Local().add_tag("abc", "cd2:submitted")) == "cd2:submitted"

