import asyncio
import os
import shutil


class DiskUsage:
    NAME = 'disk_usage'

    def __init__(self, paths):
        self.paths = paths
        # created lazily inside the running event loop: instances are built at
        # module import time, before the loop (and the daemon fork) exists
        self.lock = None
        self.cached = None

    @staticmethod
    def has_changed(old, new):
        return old['free'] != new['free'] or old['used'] != new['used'] or old['total'] != new['total']

    def name(self):
        return DiskUsage.NAME

    async def get(self, changed_only=True):
        if self.lock is None:
            self.lock = asyncio.Lock()
        async with self.lock:
            total, used, free = 0, 0, 0
            for path in self.paths.split(':'):
                if os.path.isdir(path):
                    usage = shutil.disk_usage(path)
                    used += usage.used
                    total += usage.total
                    free += usage.free
            usage = {'total': total, 'used': used, 'free': free}
            if changed_only:
                # updater path: report only real changes, remember what was reported
                if self.cached is not None and not DiskUsage.has_changed(self.cached, usage):
                    return None
                self.cached = usage
                return usage
            # register path: always report the current state, but never touch the
            # updater's change tracking - a register between two updater ticks must
            # not swallow a change broadcast for everyone else
            return usage
