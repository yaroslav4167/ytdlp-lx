"""Fetch metadata thumbnails on the server and pack them into widget state."""
import asyncio
import base64
import io
import ipaddress
from urllib.parse import urljoin, urlparse

import aiohttp
from aiohttp.resolver import DefaultResolver
from PIL import Image, ImageOps

MAX_IMAGE_BYTES = 512 * 1024


def validate_image_url(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Thumbnail requires a public HTTPS URL')
    if parsed.port not in (None, 443):
        raise ValueError('Unexpected thumbnail port')
    host = parsed.hostname.lower().rstrip('.')
    if host == 'localhost' or host.endswith(('.localhost', '.local')):
        raise ValueError('Local thumbnail host')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError('Private thumbnail address')


class PublicResolver(DefaultResolver):
    async def resolve(self, host, port=0, family=0):
        records = await super().resolve(host, port, family)
        if not records or any(not ipaddress.ip_address(r['host']).is_global for r in records):
            raise OSError('Non-public thumbnail address')
        return records


async def fetch_image(url):
    """No cookies, private addresses, unbounded bodies, or unchecked redirects."""
    timeout = aiohttp.ClientTimeout(total=4, connect=2, sock_read=2)
    connector = aiohttp.TCPConnector(resolver=PublicResolver())
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for _ in range(3):
            validate_image_url(url)
            async with session.get(url, allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get('Location', ''))
                    continue
                if response.status != 200:
                    return None
                data = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    data.extend(chunk)
                    if len(data) > MAX_IMAGE_BYTES:
                        return None
                return bytes(data)
    return None


def encode_image(data, hero=False):
    """A dedicated 1024-byte public_data budget leaves HTML and chat_data free."""
    budget = 650 if hero else 210
    sizes = [(128,72),(96,54),(64,36),(48,27),(32,18)] if hero else [(40,40),(32,32),(24,24),(16,16)]
    with Image.open(io.BytesIO(data)) as source:
        if source.width * source.height > 16_000_000:
            return None
        source = source.convert('RGB')
        for size in sizes:
            thumb = ImageOps.fit(source, size, method=Image.Resampling.LANCZOS)
            for quality in (25, 12, 5):
                output = io.BytesIO()
                thumb.save(output, format='WEBP', quality=quality, method=6)
                if output.tell() <= budget:
                    return 'data:image/webp;base64,' + base64.b64encode(output.getvalue()).decode('ascii')
    return None


async def thumbnail(url, hero=False):
    if not url:
        return None
    try:
        data = await fetch_image(url)
        return await asyncio.to_thread(encode_image, data, hero) if data else None
    except (ValueError, OSError, Image.DecompressionBombError, aiohttp.ClientError, asyncio.TimeoutError):
        return None
