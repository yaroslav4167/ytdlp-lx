"""Fetch metadata thumbnails on the server and pack them into widget state."""
import asyncio
import base64
import io
import ipaddress
import re
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


async def fetch_thumbnail_data(url):
    """Retry a transient image failure and try public YouTube fallback sizes."""
    if not url:return None
    candidates=[url,url]
    parsed=urlparse(url)
    match=re.match(r'^/(?:vi|vi_webp)/([A-Za-z0-9_-]{11})/',parsed.path)
    if parsed.hostname in ('i.ytimg.com','i1.ytimg.com','i2.ytimg.com','i3.ytimg.com','img.youtube.com') and match:
        fallbacks=[f'https://i.ytimg.com/vi/{match[1]}/{name}.jpg' for name in ('hqdefault','mqdefault')]
        candidates=[url]+[v for v in fallbacks if v!=url]+[url]
    async def fetch():
        for candidate in candidates:
            try:
                data=await fetch_image(candidate)
                if data:
                    with Image.open(io.BytesIO(data)) as image:
                        if image.width*image.height>16_000_000:continue
                        image.verify()
                    return data
            except (ValueError,OSError,Image.DecompressionBombError,aiohttp.ClientError,asyncio.TimeoutError):
                continue
        return None
    try:return await asyncio.wait_for(fetch(),timeout=7)
    except asyncio.TimeoutError:return None


def encode_image(data, hero=False):
    """A dedicated 1024-byte public_data budget leaves HTML and chat_data free."""
    budget = 738 if hero else 210
    sizes = [(64,64),(56,56),(48,48),(40,40),(32,32)] if hero else [(40,40),(32,32),(24,24),(16,16)]
    with Image.open(io.BytesIO(data)) as source:
        if source.width * source.height > 16_000_000:
            return None
        source = source.convert('RGB')
        thumbs={size:ImageOps.fit(source,size,method=Image.Resampling.LANCZOS) for size in sizes}
        for quality in ((80,65,50,35,25) if hero else (40,25,12)):
            for size in sizes:
                thumb = thumbs[size]
                output = io.BytesIO()
                thumb.save(output, format='WEBP', quality=quality, method=6)
                if output.tell() <= budget:
                    return 'data:image/webp;base64,' + base64.b64encode(output.getvalue()).decode('ascii')

    return None


async def thumbnail(url, hero=False):
    if not url:
        return None
    try:
        data = await fetch_thumbnail_data(url)
        return await asyncio.to_thread(encode_image, data, hero) if data else None
    except (ValueError, OSError, Image.DecompressionBombError, aiohttp.ClientError, asyncio.TimeoutError):
        return None


def search_artwork(images):
    """Eight compact covers share one WebP atlas in public_data; heroes stay server-side."""
    decoded = []
    heroes = {}
    for index, data in enumerate(images):
        image = None
        if data:
            try:
                with Image.open(io.BytesIO(data)) as source:
                    if source.width * source.height <= 16_000_000:
                        image = source.convert('RGB')
                heroes[index] = encode_image(data, hero=True) if image is not None else None
            except (ValueError, OSError, Image.DecompressionBombError):
                image = None
        decoded.append(image)
    if not any(image is not None for image in decoded):
        return None, heroes
    for size in (40, 32, 24, 20, 16, 12):
        atlas = Image.new('RGB', (size*4, size*2), '#0b2d5c')
        for index, image in enumerate(decoded):
            if image is not None:
                atlas.paste(ImageOps.fit(image, (size, size), method=Image.Resampling.LANCZOS),
                            ((index % 4)*size, (index // 4)*size))
        for quality in (25, 12, 5):
            output = io.BytesIO()
            atlas.save(output, format='WEBP', quality=quality, method=6)
            if output.tell() <= 738:
                return 'data:image/webp;base64,' + base64.b64encode(output.getvalue()).decode('ascii'), heroes
    return None, heroes


async def search_thumbnails(urls):
    semaphore = asyncio.Semaphore(4)
    async def fetch(url):
        if not url:
            return None
        async with semaphore:
            try:
                return await fetch_thumbnail_data(url)
            except (ValueError, OSError, aiohttp.ClientError, asyncio.TimeoutError):
                return None
    images = await asyncio.gather(*(fetch(url) for url in urls[:8]))
    return await asyncio.to_thread(search_artwork, images)
