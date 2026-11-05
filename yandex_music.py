"""Resolve public Yandex Music track metadata and find its YouTube counterpart."""
import logging
import re
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)
YANDEX_MUSIC_HOSTS = {
    f"music.yandex.{tld}" for tld in ("ru", "com", "kz", "by", "ua", "uz")
}


def is_yandex_music_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in ("http", "https")
        and (parsed.hostname or "").lower().rstrip(".") in YANDEX_MUSIC_HOSTS
    )


async def yandex_to_youtube(url: str, search_youtube):
    """Return (YouTube URL, error); no account, cookies or audio from Yandex."""
    match = re.fullmatch(r"/(?:album/\d+/)?track/(\d+)/?", urlparse(url).path)
    if not is_yandex_music_url(url) or not match:
        return None, "Поддерживаются только ссылки на отдельные треки Яндекс Музыки."

    try:
        timeout = aiohttp.ClientTimeout(total=20, connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"https://api.music.yandex.net/tracks/{match.group(1)}",
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    return None, "Не удалось получить название трека из Яндекс Музыки."
                data = await response.json()

        tracks = data.get("result")
        if not isinstance(tracks, list) or not tracks or not isinstance(tracks[0], dict):
            return None, "Яндекс Музыка не вернула данные трека."
        track = tracks[0]
        title = track.get("title")
        if not isinstance(title, str) or not title.strip():
            return None, "Яндекс Музыка не вернула название трека."
        version = track.get("version")
        if version:
            title = f"{title} ({version})"
        artists = ", ".join(
            artist["name"] for artist in track.get("artists", [])
            if isinstance(artist, dict) and isinstance(artist.get("name"), str)
        )
    except Exception:
        logger.exception("Yandex Music metadata lookup failed")
        return None, "Ошибка при получении данных из Яндекс Музыки. Попробуйте позже."

    query = f"{artists} - {title} official audio" if artists else f"{title} official audio"
    try:
        results = await search_youtube(query)
        if not results or not results[0].get("id"):
            return None, "Не удалось найти этот трек на YouTube."
        return f"https://www.youtube.com/watch?v={results[0]['id']}", None
    except Exception:
        logger.exception("YouTube search for Yandex Music track failed")
        return None, "Ошибка поиска трека на YouTube. Попробуйте позже."
