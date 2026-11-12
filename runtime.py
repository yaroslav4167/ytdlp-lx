"""Bounded FIFO media jobs and request-scoped, redacted logging."""
import asyncio
from collections import deque
from contextlib import asynccontextmanager
from contextvars import ContextVar, copy_context
import logging
import os
import re
import time
import uuid

LOG = logging.getLogger(__name__)
JOB_ID = ContextVar('media_job_id', default='-')


def parallel_downloads():
    value = int(os.environ.get('MAX_PARALLEL_DOWNLOADS', '2'))
    if not 1 <= value <= 8:
        raise ValueError('MAX_PARALLEL_DOWNLOADS must be between 1 and 8')
    return value


class JobFormatter(logging.Formatter):
    def format(self, record):
        record.job_id = JOB_ID.get()
        value = super().format(record)
        value = re.sub(r'\b\d{6,}:[A-Za-z0-9_-]+', '[BOT_TOKEN]', value)
        return re.sub(r'(https?|socks5h?)://[^\s/@]+:[^\s/@]+@', r'\1://[CREDENTIALS]@', value)


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JobFormatter('%(asctime)s %(levelname)s job=%(job_id)s %(name)s: %(message)s'))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    for name in ('httpx', 'httpcore'):
        logging.getLogger(name).setLevel(logging.WARNING)


async def run_blocking(function, wait_on_cancel=False):
    """Propagate the job identifier into executor threads."""
    future = asyncio.get_running_loop().run_in_executor(None, copy_context().run, function)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        if not wait_on_cancel:
            future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            raise
        # A running download/conversion cannot be stopped by cancelling its
        # asyncio Future. Keep its slot and file protection until it really ends.
        try:
            await asyncio.shield(future)
        except Exception:
            LOG.warning('Worker failed while its caller was cancelled', exc_info=True)
        raise


class MediaQueue:
    def __init__(self, limit):
        self.limit = limit
        self.active = 0
        self.waiting = deque()

    def schedule(self):
        while self.active < self.limit and self.waiting:
            ticket = self.waiting.popleft()
            ticket['active'] = True
            self.active += 1
            ticket['ready'].set_result(None)
        for position, ticket in enumerate(self.waiting, 1):
            if ticket['position'] != position:
                ticket['position'] = position
                ticket['changed'].set()

    @asynccontextmanager
    async def slot(self, on_position):
        token = JOB_ID.set(uuid.uuid4().hex[:10])
        queued_at = time.monotonic()
        ticket = dict(ready=asyncio.get_running_loop().create_future(), active=False,
                      position=None, changed=asyncio.Event())
        self.waiting.append(ticket)
        self.schedule()
        reporter = None

        async def report():
            while not ticket['ready'].done():
                await ticket['changed'].wait()
                ticket['changed'].clear()
                if ticket['ready'].done():
                    return
                try:
                    await on_position(ticket['position'])
                except Exception:
                    LOG.warning('Could not publish queue position', exc_info=True)

        try:
            if not ticket['active']:
                LOG.info('Queued position=%d active=%d limit=%d', ticket['position'], self.active, self.limit)
                reporter = asyncio.create_task(report())
            try:
                await asyncio.shield(ticket['ready'])
            finally:
                if reporter:
                    reporter.cancel()
                    await asyncio.gather(reporter, return_exceptions=True)
            if reporter:
                try:
                    await on_position(0)
                except Exception:
                    LOG.warning('Could not restore job status', exc_info=True)
            started = time.monotonic()
            LOG.info('Job started active=%d limit=%d waited=%.2fs', self.active, self.limit, started-queued_at)
            try:
                yield
            except BaseException:
                LOG.info('Job interrupted')
                raise
            else:
                LOG.info('Job finished elapsed=%.2fs', time.monotonic()-started)
        finally:
            if ticket['active']:
                self.active -= 1
            else:
                self.waiting.remove(ticket)
                ticket['ready'].cancel()
            self.schedule()
            JOB_ID.reset(token)


MEDIA_JOBS = MediaQueue(parallel_downloads())
