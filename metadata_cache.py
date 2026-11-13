from collections import OrderedDict
from copy import deepcopy
from threading import Lock
from time import monotonic


class MetadataCache:
    def __init__(self, ttl=90, limit=32, clock=monotonic):
        self.ttl = ttl
        self.limit = limit
        self.clock = clock
        self.entries = OrderedDict()
        self.lock = Lock()

    def get(self, url):
        with self.lock:
            entry = self.entries.get(url)
            if entry is None:
                return None
            expires, info = entry
            if self.clock() >= expires:
                del self.entries[url]
                return None
            self.entries.move_to_end(url)
            return deepcopy(info)

    def put(self, url, info):
        if not isinstance(info, dict) or not info.get('formats') or info.get('_type') in ('playlist', 'multi_video'):
            return
        if info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming', 'post_live'):
            return
        with self.lock:
            self.entries[url] = self.clock() + self.ttl, deepcopy(info)
            self.entries.move_to_end(url)
            while len(self.entries) > self.limit:
                self.entries.popitem(last=False)
