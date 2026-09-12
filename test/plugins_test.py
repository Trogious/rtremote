import asyncio

from plugins import DiskUsage


async def validate_paths(paths):
    usage = await DiskUsage(paths).get()
    assert usage['total'] > 0
    assert usage['total'] >= usage['used']


def test_disk_usage_root():
    asyncio.run(validate_paths('/'))


def test_disk_usage_multiple():
    asyncio.run(validate_paths('/:/tmp'))


def test_disk_usage_changed_only():
    async def validate():
        plugin = DiskUsage('/')
        first = await plugin.get()
        assert first is not None
        # unchanged usage is suppressed on the changed_only (updater) path
        second = await plugin.get()
        assert second is None or DiskUsage.has_changed(first, second)
        # the register path always reports and never clobbers change tracking
        assert await plugin.get(False) is not None
    asyncio.run(validate())
