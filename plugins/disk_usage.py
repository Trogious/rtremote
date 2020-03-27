import asyncio
import os
import shutil
from collections import namedtuple


class DiskUsage:
    NAME = 'disk_usage'
    lock = asyncio.Lock()
    Usage = namedtuple('Usage', ['total', 'used', 'free'])
    cached = None

    @staticmethod
    def has_chaged(old, new):
        return old['free'] != new['free'] or old['used'] != new['used'] or old['total'] != new['total']

    def __init__(self, paths):
        self.paths = paths

    def name(self):
        return DiskUsage.NAME

    async def get(self, changed_only=True):
        async with DiskUsage.lock:
            total, used, free = 0, 0, 0
            for path in self.paths.split(':'):
                if os.path.isdir(path):
                    usage = shutil.disk_usage(path)
                    used += usage.used
                    total += usage.total
                    free += usage.free
            usage = {'total': total, 'used': used, 'free': free}
            if DiskUsage.cached is None or DiskUsage.has_chaged(DiskUsage.cached, usage):
                DiskUsage.cached = usage
                return DiskUsage.cached
            if changed_only:
                return None  # no changes in usage, no need to report to listening cliebnts
            else:
                return DiskUsage.cached
