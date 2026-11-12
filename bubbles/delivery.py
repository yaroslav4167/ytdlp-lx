"""Retry only deliveries Telegram definitely rejected before accepting media."""
import asyncio
from datetime import timedelta

import httpx
from telegram.error import BadRequest, NetworkError, RetryAfter


class AmbiguousDelivery(Exception):
    """The upload may have succeeded; blindly retrying can deliver two files."""


async def send_media(operation):
    for attempt in range(3):
        try:
            return await operation()
        except RetryAfter as error:
            if attempt == 2:
                raise
            delay = error.retry_after
            await asyncio.sleep(delay.total_seconds() if isinstance(delay, timedelta) else delay)
        except BadRequest:
            raise
        except NetworkError as error:
            # No bytes can have been accepted when connection establishment failed.
            if isinstance(error.__cause__, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
                if attempt == 2:
                    raise
                await asyncio.sleep((3, 8)[attempt])
                continue
            raise AmbiguousDelivery('Telegram did not confirm the media upload') from error
