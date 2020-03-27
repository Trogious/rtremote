import asyncio

from plugins import DiskUsage


async def validate_paths(paths):
    DiskUsage.cached = None
    usage = await DiskUsage('/').get()
    assert usage['total'] > 0
    assert usage['total'] >= usage['used']


def test_disk_usage_root():
    asyncio.get_event_loop().run_until_complete(validate_paths('/'))


def test_disk_usage_multiple():
    asyncio.get_event_loop().run_until_complete(validate_paths('/:/tmp'))
