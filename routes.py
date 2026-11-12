"""Remember fast metadata routes; hedge only slow cached requests."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
import threading
import time
from urllib.parse import urlparse


class RouteSelector:
    def __init__(self, workers=6, hedge_delay=.25, ttl=600):
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='media-route')
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        self.hedge_delay, self.ttl = hedge_delay, ttl

    @staticmethod
    def key(url):
        if isinstance(url, (list, tuple)):
            url = url[0] if url else ''
        host = urlparse(url or '').hostname or ''
        return 'youtube.com' if host in ('youtu.be', 'www.youtube.com', 'm.youtube.com', 'youtube.com') or str(url).startswith('ytsearch') else host

    def preferred(self, key):
        with self.lock:
            entry = self.cache.get(key)
            if entry and time.monotonic() - entry[1] < self.ttl:
                self.cache.move_to_end(key)
                return entry[0]
        return None

    def remember(self, key, route):
        with self.lock:
            self.cache[key] = route, time.monotonic()
            self.cache.move_to_end(key)
            while len(self.cache) > 64:
                self.cache.popitem(last=False)

    def extract(self, url, attempt):
        key = self.key(url)
        preferred = self.preferred(key)
        futures = {}

        def submit(route):
            future = self.pool.submit(copy_context().run, attempt, route)
            futures[future] = route
            return future

        try:
            if preferred is not None:
                first = submit(preferred)
                try:
                    result = first.result(timeout=self.hedge_delay)
                except Exception:
                    pass
                else:
                    self.remember(key, preferred)
                    return result, preferred
                submit('proxy' if preferred == 'direct' else 'direct')
            else:
                submit('direct')
                submit('proxy')
            last_error = None
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as error:
                    last_error = error
                else:
                    route = futures[future]
                    self.remember(key, route)
                    return result, route
            raise last_error
        finally:
            # Running library calls cannot be cancelled. Bound their total threads,
            # discard their result, and never let a late loser change the cache.
            for future in futures:
                future.cancel()
