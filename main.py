import os
import asyncio
import time
import aiohttp
import uuid
import re
import html
import shlex
import subprocess
import ipaddress
import socket
import shutil
import math
import logging
from functools import wraps
from metadata_cache import MetadataCache
import inspect
from runtime import MEDIA_JOBS, configure_logging, run_blocking
from routes import RouteSelector
from urllib.parse import urlparse
from yandex_music import is_yandex_music_url, yandex_to_youtube
from bubbles import BubbleService, RETAINED_FILES
from bubbles.delivery import AmbiguousDelivery, send_media
from bubbles.service import item as bubble_item, strings as bubble_strings, user_language

LOG = logging.getLogger(__name__)
ROUTES = RouteSelector(workers=MEDIA_JOBS.limit * 2 + 2)
ACTIVE_MEDIA_IDS = set()
METADATA = MetadataCache(ttl=90, limit=32)

# Runtime configuration. Secrets are supplied through the environment.
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
DOWNLOAD_DIR = "downloads"
DOWNLOAD_PROXY = os.environ.get("DOWNLOAD_PROXY", "")
MAX_UPLOAD_SIZE = 50 * 1024 * 1024
STALE_FILE_AGE = 600
MAX_CALLBACK_LINKS = 32
SEARCH_TIMEOUT = 25
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Filesystem and network safety
# ---------------------------------------------------------------------------
def cleanup_stale_downloads(max_age=STALE_FILE_AGE):
    now = time.time()
    prefixes = ("video_", "audio_", "input_", "output_", "thumb_", "final_")
    try:
        for entry in os.scandir(DOWNLOAD_DIR):
            if entry.is_file() and entry.name.startswith(prefixes) and entry.path not in RETAINED_FILES:
                if any(uid in entry.name for uid in tuple(ACTIVE_MEDIA_IDS)):
                    continue
                if now - entry.stat().st_mtime > max_age:
                    os.remove(entry.path)
    except OSError as error:
        LOG.warning(f"Download cleanup error: {error}")

# ---------------------------------------------------------------------------
# yt-dlp integration and proxy fallback
# ---------------------------------------------------------------------------
def get_yt_dlp():
    import yt_dlp
    return yt_dlp

class YDLLogger:
    def debug(self, message):
        if not message.startswith('[debug]'):
            LOG.info('%s', message)

    def warning(self, message):
        LOG.warning('%s', message)

    def error(self, message):
        LOG.warning('%s', message)

def ydl_options(options=None):
    bundled_deno = os.path.join(os.path.dirname(__file__), '.tools', 'deno')
    deno = os.environ.get('YTDLP_DENO') or (bundled_deno if os.path.isfile(bundled_deno) else shutil.which('deno'))
    result = {
        'js_runtimes': {'deno': {'path': deno}} if deno else {'node': {}},
        'socket_timeout': 15,
        'logger': YDLLogger(),
        'noprogress': True,
    }
    if options:
        result.update(options)
    return result

class ProxyRetryYoutubeDL:
    """Prefer a fast cached route and retain the alternative for failures."""

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def _run(self, method, *args, **kwargs):
        def attempt(proxy=None):
            options = dict(self.options)
            if proxy is not None:
                options['proxy'] = proxy
            with get_yt_dlp().YoutubeDL(options) as ydl:
                return getattr(ydl, method)(*args, **kwargs)

        url = args[0] if args else kwargs.get('url')
        if isinstance(url, (list, tuple)):
            url = url[0] if url else None
        if url and url.startswith(('http://', 'https://')):
            validate_external_url(url)

        key = ROUTES.key(url)
        if method == 'extract_info' and url and DOWNLOAD_PROXY and kwargs.get('download', True) is False:
            result, route = ROUTES.extract(url, lambda route: attempt(DOWNLOAD_PROXY if route == 'proxy' else ''))
            LOG.info('Metadata route=%s host=%s', route, key)
            return result

        route = ROUTES.preferred(key) if DOWNLOAD_PROXY else 'direct'
        try:
            return attempt(DOWNLOAD_PROXY if route == 'proxy' else '')
        except Exception as error:
            error_text = str(error).lower()
            http_error = re.search(r'\b(?:40[1-9]|4[1-9]\d|[5-9]\d{2})\b', error_text)
            network_error = any(token in error_text for token in (
                'timeout', 'timed out', 'connection reset', 'connection refused',
                'connection aborted', 'temporary failure', 'network is unreachable',
                'name or service not known',
            ))
            if not DOWNLOAD_PROXY or not (http_error or network_error):
                raise
            alternate = 'direct' if route == 'proxy' else 'proxy'
            LOG.warning('Media route failed; retry method=%s route=%s host=%s', method, alternate, key)
            result = attempt(DOWNLOAD_PROXY if alternate == 'proxy' else '')
            ROUTES.remember(key, alternate)
            return result

    def extract_info(self, *args, **kwargs):
        return self._run('extract_info', *args, **kwargs)

    def download(self, *args, **kwargs):
        return self._run('download', *args, **kwargs)


def validate_external_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Only HTTP(S) URLs with a hostname are allowed')
    hostname = parsed.hostname.lower().rstrip('.')
    if hostname == 'localhost' or hostname.endswith('.localhost') or hostname.endswith('.local'):
        raise ValueError('Local hostnames are not allowed')
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, None)}
    except socket.gaierror:
        addresses = set()
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            raise ValueError('Private or local network addresses are not allowed')

# ---------------------------------------------------------------------------
# URL normalization and Spotify lookup
# ---------------------------------------------------------------------------
def normalize_url(text: str):
    text = text.strip()
    if not text.startswith(("http://", "https://")):
        if not re.match(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:/|$)", text, re.IGNORECASE):
            return None
        text = f"https://{text}"

    tiktok_mobile = re.fullmatch(r"https?://m\.tiktok\.com/v/(\d+)/?", text, re.IGNORECASE)
    if tiktok_mobile:
        return f"https://www.tiktok.com/@_/video/{tiktok_mobile.group(1)}"
    return text

def is_tiktok_photo_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.netloc.lower().endswith('tiktok.com')
        and bool(re.match(r'^/@[^/]+/photo/\d+/?$', parsed.path, re.IGNORECASE))
    )

def is_instagram_photo_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.netloc.lower().endswith('instagram.com')
        and bool(re.match(r'^/p/[A-Za-z0-9_-]+/?$', parsed.path, re.IGNORECASE))
    )

async def resolve_tiktok_short_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.lower().rstrip('.').lower() != 'vt.tiktok.com':
        return url
    try:
        timeout = aiohttp.ClientTimeout(total=15, connect=10, sock_read=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as response:
                final_url = str(response.url)
        validate_external_url(final_url)
        return final_url
    except Exception as error:
        LOG.warning(f'TikTok short URL resolution error: {error}')
        return url

def is_spotify_url(url: str) -> bool:
    return urlparse(url).netloc.lower().endswith('spotify.com')

def spotify_credentials():
    client_id = os.getenv('SPOTIFY_CLIENT_ID')
    client_secret = os.getenv('SPOTIFY_CLIENT_SECRET')
    if client_id and client_secret:
        return client_id, client_secret

    credentials_path = os.path.join(os.path.dirname(__file__), '.spotify.env')
    try:
        values = {}
        with open(credentials_path, encoding='utf-8') as credentials_file:
            for line in credentials_file:
                key, value = line.strip().split('=', 1)
                values[key] = value
        return values.get('SPOTIFY_CLIENT_ID'), values.get('SPOTIFY_CLIENT_SECRET')
    except Exception:
        return None, None

async def spotify_to_youtube(url: str):
    client_id, client_secret = spotify_credentials()
    match = re.search(r'/track/([A-Za-z0-9]+)', urlparse(url).path)
    if not match:
        return None, "Only individual Spotify track links are supported."

    try:
        track = None
        async with aiohttp.ClientSession() as session:
            if client_id and client_secret:
                auth = aiohttp.BasicAuth(client_id, client_secret)
                async with session.post(
                    'https://accounts.spotify.com/api/token',
                    auth=auth,
                    data={'grant_type': 'client_credentials'},
                ) as response:
                    token_data = await response.json()
                token = token_data.get('access_token')
                if token:
                    async with session.get(
                        f'https://api.spotify.com/v1/tracks/{match.group(1)}',
                        headers={'Authorization': f'Bearer {token}'},
                    ) as response:
                        if response.status == 200:
                            track = await response.json()

            if not track:
                async with session.get(
                    url,
                    headers={'User-Agent': 'Mozilla/5.0'},
                ) as response:
                    page = await response.text() if response.status == 200 else ''
                title_match = re.search(
                    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
                    page,
                    re.IGNORECASE,
                )
                description = re.search(
                    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)',
                    page,
                    re.IGNORECASE,
                )
                title = html.unescape(title_match.group(1)).split(' - song and lyrics by ', 1)[0].strip() if title_match else None
                artists = html.unescape(description.group(1)).split(' · ', 1)[0].strip() if description else None
            else:
                artists = ', '.join(artist['name'] for artist in track.get('artists', []))
                title = track.get('name')

        if not title:
            return None, "Spotify did not return the track title."

        search_query = f'{artists} - {title} official audio' if artists else f'{title} official audio'
        results = await search_youtube(search_query)
        if not results or not results[0].get('id'):
            return None, "Could not find the track on YouTube."
        return f"https://www.youtube.com/watch?v={results[0]['id']}", None
    except Exception as exc:
        LOG.warning(f"Spotify error: {exc}")
        return None, "Error while contacting Spotify."

def get_telegram_components():
    import telegram
    from telegram import InputFile, InlineKeyboardMarkup, InlineKeyboardButton
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters
    from telegram.request import HTTPXRequest
    return {
        'telegram': telegram,
        'InputFile': InputFile,
        'InlineKeyboardMarkup': InlineKeyboardMarkup,
        'InlineKeyboardButton': InlineKeyboardButton,
        'ApplicationBuilder': ApplicationBuilder,
        'CommandHandler': CommandHandler,
        'MessageHandler': MessageHandler,
        'CallbackQueryHandler': CallbackQueryHandler,
        'filters': filters,
        'HTTPXRequest': HTTPXRequest,
    }

def make_progress_hook(msg, loop, words=None):
    words = words or bubble_strings()
    last_update = {'time': 0, 'text': None}
    bar_length = 20

    def progress_bar(percent):
        filled_len = int(bar_length * percent // 100)
        bar = '▓' * filled_len + '░' * (bar_length - filled_len)
        return f"[{bar}] {percent:.1f}%"

    async def edit_progress(text):
        # yt-dlp/ffmpeg may report the same rounded progress many times.
        # Telegram rejects editing a message with identical content.
        if text == last_update['text']:
            return
        last_update['text'] = text
        try:
            await edit_text_retry(msg, text)
        except Exception as error:
            if 'Message is not modified' not in str(error):
                last_update['text'] = None
                raise

    async def progress_hook_async(d):
        try:
            if getattr(msg, 'is_bubble', False):
                await msg.update_progress(d)
                return
            status = d.get('status')
            if status == 'downloading':
                now = time.time()
                if now - last_update['time'] < 1:
                    return
                last_update['time'] = now

                downloaded = d.get('downloaded_bytes', 0)
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 1
                percent = downloaded / total * 100 if total else 0
                speed = d.get('speed')
                eta = d.get('eta')

                speed_str = f"{speed/1024:.1f} KiB/s" if speed else "N/A"
                eta_str = f"{eta}s" if eta else "N/A"

                text = (
                    f"{words['download_plain']}\n"
                    f"{progress_bar(percent)}\n"
                    f"{words['speed_plain']}: {speed_str}\n"
                    f"{words['remaining_plain']}: {eta_str}"
                )
                await edit_progress(text)
            elif status == 'finished':
                await edit_progress(words["converted_plain"])
            elif status == 'converting':
                now = time.time()
                if now - last_update['time'] < 1:
                    return
                last_update['time'] = now
                percent = d.get('percent', 0)
                await edit_progress(
                    f"{words['convert_plain']}" + (f"\n{progress_bar(percent)}" if percent is not None else "")
                )
            elif status == 'error':
                await edit_progress(words["download_error_plain"])
        except Exception as e:
            LOG.warning(f"Progress update error: {e}")

    pending = {'latest': None}

    def progress_hook(d):
        try:
            pending['latest'] = asyncio.run_coroutine_threadsafe(progress_hook_async(dict(d)), loop)
        except Exception as e:
            LOG.warning(f"Progress task error: {e}")

    async def flush():
        if pending['latest'] is not None:
            await asyncio.wrap_future(pending['latest'])

    progress_hook.flush = flush
    return progress_hook

def run_ffmpeg(command, duration, progress_hook=None):
    process = subprocess.Popen(
        shlex.split(command) + ['-progress', 'pipe:1', '-nostats'],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    duration_us = duration * 1_000_000 if duration and duration > 0 else None
    if progress_hook:
        progress_hook({'status': 'converting', 'percent': None})
    for line in process.stdout:
        if line.startswith('out_time_us=') and progress_hook:
            try:
                current_us = int(line.split('=', 1)[1])
                progress_hook({
                    'status': 'converting',
                    'percent': min(max(0, current_us / duration_us * 100), 100) if duration_us else None,
                })
            except ValueError:
                pass
    return process.wait()

async def probe_url(url: str):
    cached = METADATA.get(url)
    if cached is not None:
        return cached
    loop = asyncio.get_running_loop()

    def _probe():
        with ProxyRetryYoutubeDL(ydl_options({
            'skip_download': True,
            'quiet': True,
            'no_warnings': True,
            'extract_flat': 'in_playlist',
        })) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await run_blocking(_probe)
    except Exception:
        return None
    METADATA.put(url, info)
    return info

async def edit_text_retry(message, text, **kwargs):
    for delay in (0, 3, 8):
        if delay:
            await asyncio.sleep(delay)
        try:
            await message.edit_text(text, **kwargs)
            return True
        except Exception as error:
            if delay == 8:
                LOG.warning(f"Message edit error after retries: {error}")
    return False

async def telegram_retry(operation):
    last_error = None
    for delay in (0, 3, 8):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await operation()
        except Exception as error:
            last_error = error
            if delay == 8:
                LOG.warning(f"Telegram request error after retries: {error}")
    raise last_error

async def download_tiktok_photos(url: str):
    """Resolve a TikTok photo post and download its images through TikWM."""
    import mimetypes

    api_url = 'https://www.tikwm.com/api/'
    timeout = aiohttp.ClientTimeout(total=60, connect=15, sock_read=45)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(api_url, params={'url': url}) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)

        data = payload.get('data') or {}
        image_urls = data.get('images') or []
        if payload.get('code') != 0 or not image_urls:
            raise Exception('TikTok photo post did not return any images')

        uid = uuid.uuid4().hex
        files = []
        try:
            for index, image_url in enumerate(image_urls, start=1):
                validate_external_url(image_url)
                async with session.get(image_url) as image_response:
                    image_response.raise_for_status()
                    content_type = image_response.headers.get('Content-Type', '').split(';', 1)[0]
                    extension = mimetypes.guess_extension(content_type) or '.jpg'
                    path = os.path.join(DOWNLOAD_DIR, f'photo_{uid}_{index}{extension}')
                    with open(path, 'wb') as image_file:
                        image_file.write(await image_response.read())
                if downloaded_size(path) > MAX_UPLOAD_SIZE:
                    raise Exception('A TikTok photo exceeds the 50 MB limit')
                files.append(path)
        except Exception:
            for path in files:
                try:
                    os.remove(path)
                except OSError:
                    pass
            raise

    return files, {
        'title': data.get('title') or 'TikTok photo',
        'uploader': (data.get('author') or {}).get('unique_id', ''),
    }

async def download_instagram_photo(url: str):
    """Download the public preview image from an Instagram photo post."""
    timeout = aiohttp.ClientTimeout(total=60, connect=15, sock_read=45)
    headers = {'User-Agent': 'Mozilla/5.0'}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            page = await response.text()

        match = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            page,
            re.IGNORECASE,
        )
        if not match:
            raise Exception('Instagram did not return a public photo')

        image_url = html.unescape(match.group(1))
        validate_external_url(image_url)
        async with session.get(image_url) as image_response:
            image_response.raise_for_status()
            path = os.path.join(DOWNLOAD_DIR, f'photo_{uuid.uuid4().hex}.jpg')
            with open(path, 'wb') as image_file:
                image_file.write(await image_response.read())

    if downloaded_size(path) > MAX_UPLOAD_SIZE:
        os.remove(path)
        raise Exception('The Instagram photo exceeds the 50 MB limit')
    return [path], {'title': 'Instagram photo', 'uploader': ''}

def info_is_playlist(info):
    return bool(info and (info.get('_type') == 'playlist' or len(info.get('entries') or []) > 1))


def info_media_types(info):
    formats = (info or {}).get('formats', [])
    has_audio = any(format_has_audio(f) for f in formats)
    has_video = any(format_has_video(f) for f in formats)
    direct_exts = {f.get('ext') for f in formats if f.get('url')}
    return (has_audio or bool(direct_exts & {'mp3','m4a','aac','ogg','opus','wav'}),
            has_video or bool(direct_exts & {'mp4','webm','mkv','mov','avi'}))


async def url_is_playlist(url: str) -> bool:
    return info_is_playlist(await probe_url(url))


async def url_media_types(url: str):
    return info_media_types(await probe_url(url))


def format_has_audio(fmt):
    return fmt.get('acodec') not in (None, 'none') or fmt.get('audio_ext') not in (None, 'none')

def format_has_video(fmt):
    return fmt.get('vcodec') not in (None, 'none') or fmt.get('video_ext') not in (None, 'none')

# ---------------------------------------------------------------------------
# Media download, conversion and Telegram delivery
# ---------------------------------------------------------------------------
class MediaTooLarge(Exception):
    """Prepared media cannot fit Telegram's upload limit."""


def mp3_bitrate(duration, max_size=MAX_UPLOAD_SIZE, artwork_size=0):
    if not duration or not math.isfinite(duration) or duration <= 0:
        return 192
    # Reserve space for artwork, tags, MP3 headers and duration rounding.
    budget = max(0, max_size - artwork_size - 256 * 1024) * .97
    for rate in (192,160,128,112,96,80,64,56,48,40,32):
        if duration * rate * 1000 / 8 <= budget:
            return rate
    raise MediaTooLarge('Audio is too long for the upload limit')


def prepare_mp3(audio_path, thumb_path, output_path, info, progress_cb=None, max_size=MAX_UPLOAD_SIZE):
    duration = media_duration(audio_path) or info.get('duration')
    thumb_path = thumb_path if thumb_path and os.path.isfile(thumb_path) else None
    bitrate = mp3_bitrate(duration, max_size, os.path.getsize(thumb_path) if thumb_path else 0)
    command = ['ffmpeg','-y','-i',audio_path]
    if thumb_path:
        command += ['-i',thumb_path]
    command += ['-map','0:a:0','-c:a','libmp3lame','-b:a',f'{bitrate}k']
    if thumb_path:
        command += ['-map','1:v:0','-c:v','mjpeg','-disposition:v','attached_pic']
    else:
        command += ['-vn']
    command += ['-metadata',f"title={safe_meta(info.get('title',''))}",
                '-metadata',f"artist={safe_meta(info.get('uploader',''))}",output_path]
    try:
        if run_ffmpeg(shlex.join(command),duration,progress_cb):
            raise RuntimeError('Could not prepare media')
        if downloaded_size(output_path) > max_size:
            raise MediaTooLarge('The prepared file exceeds the upload limit')
    except BaseException:
        if os.path.exists(output_path):
            os.remove(output_path)
        raise


async def download_media(url: str, format_choice: str, progress_cb=None):
    yt_dlp = get_yt_dlp()
    loop = asyncio.get_running_loop()

    download_part = None
    active_ids = set()

    def new_id():
        uid = uuid.uuid4().hex
        active_ids.add(uid)
        ACTIVE_MEDIA_IDS.add(uid)
        return uid

    def _hook(d):
        if progress_cb is not None:
            progress_cb(dict(d, part=download_part if d.get('status') in ('downloading', 'finished') else None))

    def _download():
        nonlocal download_part
        # Базовые настройки
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s'),
            'progress_hooks': [_hook],
            'noplaylist': True,
            'writethumbnail': True,
            'embed_thumbnail': True,
            'add_metadata': True,
        }

        _hook({'status':'preparing', 'detail':'metadata'})
        # Получаем список форматов
        info = METADATA.get(url)
        if info is None:
            ydl_probe = ProxyRetryYoutubeDL(ydl_options({'quiet': True}))
            info = ydl_probe.extract_info(url, download=False)
            METADATA.put(url, info)

        _hook({'status':'preparing', 'detail':'formats'})
        formats = info.get("formats", [])
        max_size = MAX_UPLOAD_SIZE

        # --- MP3 ---
        if format_choice == "mp3":
            uid = new_id()

            audio_path = os.path.join(DOWNLOAD_DIR, f"audio_{uid}.webm")
            thumb_path = os.path.join(DOWNLOAD_DIR, f"thumb_{uid}.jpg")
            output_path = os.path.join(DOWNLOAD_DIR, f"output_{uid}.mp3")

            # Любой аудиоформат можно перекодировать в MP3, включая WebM/Opus.
            audio_formats = [
                f for f in formats
                if f.get("acodec") not in (None, "none")
            ]
            direct_format = next((f for f in formats if f.get("url")), None)

            sized = []
            unknown_size = []
            for f in audio_formats:
                size = f.get("filesize") or f.get("filesize_approx")
                if size and size <= max_size:
                    sized.append((size, f))
                elif not size:
                    unknown_size.append(f)

            if sized:
                sized.sort(key=lambda x: x[0], reverse=True)
                best = sized[0][1]
            elif unknown_size:
                # Some services expose the real size only while downloading.
                best = max(unknown_size, key=lambda f: f.get("abr") or f.get("tbr") or 0)
            elif direct_format:
                best = direct_format
            else:
                    raise MediaTooLarge("File is too large: no audio under 50 MB")

            # скачиваем thumbnail
            thumb_url = info.get("thumbnail")
            if thumb_url:
                _hook({'status':'preparing', 'detail':'artwork'})
                try:
                    import requests
                    r = requests.get(thumb_url, timeout=(3, 5))
                    if r.status_code == 200:
                        with open(thumb_path, "wb") as f:
                            f.write(r.content)
                except:
                    thumb_path = None

            # конвертируем thumbnail в JPG, если WEBP
            if thumb_path and thumb_path.endswith(".webp"):
                new_thumb = thumb_path.replace(".webp", ".jpg")
                subprocess.run(
                    ['ffmpeg', '-y', '-i', thumb_path, new_thumb],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                os.remove(thumb_path)
                thumb_path = new_thumb

            # ---------------------------------------------------------
            # 🔥 Если лучший формат — M4A → добавляем ТОЛЬКО метадату
            #    (обложку НЕ встраиваем — контейнер не поддерживает)
            # ---------------------------------------------------------
            size = best.get("filesize") or best.get("filesize_approx") or 0
            if best["ext"] == "m4a" and size > 15 * 1024 * 1024:
                m4a_path = os.path.join(DOWNLOAD_DIR, f"output_{uid}.m4a")

                # скачиваем m4a
                with ProxyRetryYoutubeDL(ydl_options({
                    'outtmpl': m4a_path,
                    'format': best["format_id"],
                    'progress_hooks': [_hook],
                    'max_filesize': max_size,
                })) as ydl_a:
                    _hook({'status':'preparing', 'detail':'transfer'})
                    ydl_a.download([url])

                # добавляем только метаданные
                title = safe_meta(info.get("title", ""))
                artist = safe_meta(info.get("uploader", ""))

                final_m4a = os.path.join(DOWNLOAD_DIR, f"final_{uid}.m4a")

                command = [
                        'ffmpeg', '-y', '-i', m4a_path,
                        '-metadata', f'title={title}',
                        '-metadata', f'artist={artist}',
                        '-c:a', 'copy', final_m4a,
                    ]
                if run_ffmpeg(shlex.join(command), info.get('duration'), _hook):
                    raise RuntimeError('Could not prepare audio')

                try: os.remove(m4a_path)
                except: pass

                if downloaded_size(final_m4a) > max_size:
                    os.remove(final_m4a)
                    raise MediaTooLarge("The prepared file exceeds the 50 MB limit")

                # возвращаем M4A
                return final_m4a, info

            # ---------------------------------------------------------
            # 🔥 Иначе — твой старый MP3‑код
            # ---------------------------------------------------------

            best_audio = best["format_id"]

            download_part = 'audio'
            # скачиваем аудио
            with ProxyRetryYoutubeDL(ydl_options({
                'outtmpl': audio_path,
                'format': best_audio,
                'progress_hooks': [_hook],
                'max_filesize': max_size,
            })) as ydl_a:
                _hook({'status':'preparing', 'detail':'transfer'})
                ydl_a.download([url])

            try:
                prepare_mp3(audio_path, thumb_path, output_path, info, _hook, max_size)
            finally:
                for temporary in (audio_path, thumb_path):
                    if temporary:
                        try: os.remove(temporary)
                        except OSError: pass

            return output_path, info


        # --- MP4 ---
        elif format_choice == "mp4":
            # TikTok and similar services expose ready-to-send muxed files.
            # Do not treat a muxed format as both a separate video and audio track.
            muxed_formats = [
                f for f in formats
                if f.get("url")
                and f.get("ext") in ("mp4", "webm", "mkv")
                and (
                    (
                        f.get("vcodec") not in (None, "none")
                        and f.get("acodec") not in (None, "none")
                    )
                    or (
                        f.get("height")
                        and f.get("video_ext") not in (None, "none")
                        and f.get("audio_ext") not in (None, "none")
                    )
                    or (
                        f.get("format_id") == "mp4"
                        and f.get("video_ext") == "mp4"
                    )
                    or (
                        re.fullmatch(r"\d+p", str(f.get("format_id")))
                        and f.get("video_ext") == "mp4"
                    )
                )
                and (not (f.get("filesize") or f.get("filesize_approx"))
                     or (f.get("filesize") or f.get("filesize_approx")) <= max_size)
            ]
            if muxed_formats:
                sized_muxed = [
                    f for f in muxed_formats
                    if (f.get("filesize") or f.get("filesize_approx") or 0) <= max_size
                ]
                if not sized_muxed:
                    # Pornhub HLS formats often have no size metadata; keep MP4 under Telegram's limit.
                    sized_muxed = [f for f in muxed_formats if format_height(f) <= 480] or muxed_formats
                direct_format = max(
                    sized_muxed,
                    key=lambda f: (format_height(f), f.get("tbr") or 0),
                )
                uid = new_id()
                direct_ext = direct_format.get('ext') or 'mp4'
                output_path = os.path.join(DOWNLOAD_DIR, f"output_{uid}.mp4")
                direct_path = os.path.join(DOWNLOAD_DIR, f"input_{uid}.{direct_ext}")
                with ProxyRetryYoutubeDL(ydl_options({
                    'outtmpl': direct_path,
                    'format': direct_format["format_id"],
                    'progress_hooks': [_hook],
                    'max_filesize': max_size,
                })) as ydl_direct:
                    _hook({'status':'preparing', 'detail':'transfer'})
                    ydl_direct.download([url])

                if direct_ext == 'mp4':
                    command = ['ffmpeg', '-y', '-i', direct_path, '-c', 'copy',
                               '-movflags', '+faststart', output_path]
                else:
                    command = ['ffmpeg', '-y', '-i', direct_path, '-c:v', 'libx264',
                               '-vf', 'scale=-2:480', '-preset', 'ultrafast', '-crf', '32',
                               '-c:a', 'aac', '-movflags', '+faststart', output_path]
                if run_ffmpeg(shlex.join(command), info.get('duration'), _hook):
                    raise RuntimeError('Could not prepare video')

                try: os.remove(direct_path)
                except: pass

                if downloaded_size(output_path) > max_size:
                    os.remove(output_path)
                    raise MediaTooLarge("The prepared file exceeds the 50 MB limit")
                return output_path, info

            videos = [
                f for f in formats
                if format_has_video(f)
                and f.get("acodec") in (None, "none")
            ]

            audios = [
                f for f in formats
                if format_has_audio(f)
                and f.get("vcodec") in (None, "none")
            ]

            pairs = []
            for v in videos:
                v_size = v.get("filesize") or v.get("filesize_approx")
                if not v_size:
                    continue

                for a in audios:
                    a_size = a.get("filesize") or a.get("filesize_approx")
                    if not a_size:
                        continue

                    total = v_size + a_size
                    if total <= max_size:
                        pairs.append((total, v, a))

            if not pairs:
                # A number of services do not publish file sizes in metadata.
                # Use a conservative fallback and verify the final file below.
                safe_videos = [f for f in videos if (f.get("height") or 0) <= 720]
                safe_videos = safe_videos or videos
                safe_audios = [f for f in audios if (f.get("abr") or f.get("tbr") or 0) <= 192]
                safe_audios = safe_audios or audios
                if not safe_videos or not safe_audios:
                    raise Exception("Could not find suitable video and audio formats")
                best_v = max(safe_videos, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
                best_a = max(safe_audios, key=lambda f: (f.get("abr") or f.get("tbr") or 0))
                pairs = [(0, best_v, best_a)]

            pairs.sort(key=lambda x: x[0], reverse=True)
            best_video = pairs[0][1]["format_id"]
            best_audio = pairs[0][2]["format_id"]
            video_ext = pairs[0][1].get("ext") or "mp4"
            audio_ext = pairs[0][2].get("ext") or ""
            is_coub = urlparse(url).netloc.lower().endswith("coub.com")
            video_bitrate = None
            video_needs_transcode = video_ext != "mp4" or is_coub

            # пути
            uid = new_id()
            video_path = os.path.join(DOWNLOAD_DIR, f"video_{uid}.{video_ext}")
            audio_path = os.path.join(DOWNLOAD_DIR, f"audio_{uid}.{audio_ext or 'webm'}")
            output_path = os.path.join(DOWNLOAD_DIR, f"output_{uid}.mp4")

            # Each stream has its own byte total; never present it as the whole job.
            download_part = 'video'
            # скачиваем видео
            with ProxyRetryYoutubeDL(ydl_options({
                'outtmpl': video_path,
                'format': best_video,
                'progress_hooks': [_hook],
                'max_filesize': max_size,
            })) as ydl_v:
                _hook({'status':'preparing', 'detail':'transfer'})
                ydl_v.download([url])

            download_part = 'audio'
            # скачиваем аудио
            with ProxyRetryYoutubeDL(ydl_options({
                'outtmpl': audio_path,
                'format': best_audio,
                'progress_hooks': [_hook],
                'max_filesize': max_size,
            })) as ydl_a:
                _hook({'status':'preparing', 'detail':'transfer'})
                ydl_a.download([url])

            # объединяем вручную
            if is_coub:
                audio_duration = media_duration(audio_path)
                if audio_duration:
                    # Reserve space for the container and audio stream.
                    total_bitrate = max_size * 8 * 0.92 / audio_duration
                    video_bitrate = max(160_000, int(total_bitrate - 96_000))
            video_loop = (
                "-stream_loop -1 "
                if is_coub
                else ""
            )
            if video_needs_transcode:
                bitrate = f'-b:v {video_bitrate}' if video_bitrate else '-crf 32'
                cmd = (
                    f'ffmpeg -y {video_loop}-i "{video_path}" -i "{audio_path}" '
                    f'-vf scale=-2:480 -c:v libx264 -preset ultrafast {bitrate} -c:a aac '
                    f'-shortest -movflags +faststart "{output_path}"'
                )
            else:
                cmd = (
                    f'ffmpeg -y {video_loop}-i "{video_path}" -i "{audio_path}" '
                    f'-c:v copy -c:a aac -shortest -movflags +faststart "{output_path}"'
                )
            if run_ffmpeg(cmd, info.get('duration'), _hook):
                raise RuntimeError('Could not prepare media')

            if downloaded_size(output_path) > max_size:
                os.remove(output_path)
                raise MediaTooLarge("The prepared file exceeds the 50 MB limit")

            # удаляем временные файлы
            try: os.remove(video_path)
            except: pass

            try: os.remove(audio_path)
            except: pass

            # возвращаем итоговый файл
            return output_path, info

    try:
        return await run_blocking(_download, wait_on_cancel=True)
    finally:
        ACTIVE_MEDIA_IDS.difference_update(active_ids)

def safe_meta(text: str) -> str:
    if not text:
        return ""
    text = text.replace('"', "'")  # убираем двойные кавычки
    return text

def safe_download_filename(info, extension):
    """Build a readable Telegram filename without unsafe path characters."""
    title = str(info.get('title') or 'download')
    uploader = str(info.get('uploader') or '').strip()
    name = f'{uploader} - {title}' if uploader else title
    name = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]+', '_', name)
    name = re.sub(r'\s+', ' ', name).strip(' ._') or 'download'
    return f'{name[:180]}.{extension.lstrip(".")}'

def media_duration(path: str):
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', path],
            capture_output=True, text=True, check=True,
        )
        return float(result.stdout.strip())
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None

def format_height(fmt):
    if fmt.get('height'):
        return fmt['height']
    match = re.fullmatch(r'(\d+)p', str(fmt.get('format_id')))
    return int(match.group(1)) if match else 0

def downloaded_size(path: str) -> int:
    if not os.path.isfile(path):
        raise Exception("Download stopped: file is missing or exceeds the 50 MB limit")
    size = os.path.getsize(path)
    if size <= 0:
        raise Exception("Could not prepare a non-empty file for sending")
    return size

# ---------------------------------------------------------------------------
# Telegram handlers and per-user callback state
# ---------------------------------------------------------------------------
async def send_media_file(context, chat_id, filename, info):
    """Send an existing file; used by initial delivery and widget resend."""
    downloaded_size(filename)
    InputFile = get_telegram_components()['InputFile']
    ext = os.path.splitext(filename)[1].lower()

    async def send_file():
        with open(filename, 'rb') as stream:
            file = InputFile(stream, filename=safe_download_filename(info, ext))
            if ext in {'.mp3', '.m4a', '.flac', '.ogg', '.opus'}:
                return await context.bot.send_audio(chat_id=chat_id, audio=file)
            if ext in {'.webm', '.mkv'}:
                return await context.bot.send_document(chat_id=chat_id, document=file)
            return await context.bot.send_video(chat_id=chat_id, video=file)

    return await send_media(send_file)


def queued_media(function):
    signature = inspect.signature(function)

    @wraps(function)
    async def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        context, source = bound.arguments['context'], bound.arguments['source_message']
        msg = bound.arguments.get('msg')
        words = context_words(context)
        queued = False

        async def position(number):
            nonlocal msg, queued
            if number == 0 and not queued:
                return
            queued = number > 0
            if getattr(msg, 'is_bubble', False):
                await msg.queue_position(number)
            else:
                value = words['queue'].format(position=number) if number else words['download_plain']
                if msg is None:
                    msg = await telegram_retry(lambda: context.bot.send_message(chat_id=source.chat_id, text=value))
                    bound.arguments['msg'] = msg
                else:
                    await edit_text_retry(msg, value)

        async with MEDIA_JOBS.slot(position):
            return await function(*bound.args, **bound.kwargs)
    return wrapped


async def download_and_send(context, source_message, url, format_choice, msg=None, delete_source=False):
    service = bubble_service(context)
    if (not getattr(msg, 'is_bubble', False) and service
            and not getattr(getattr(source_message, 'from_user', None), 'is_bot', True)
            and service.enabled(source_message, context)):
        owner = source_message.from_user.id
        if owner in service.busy_users:
            await source_message.reply_text(bubble_strings(user_language(
                source_message.from_user, context.user_data))['busy'])
            return
        service.busy_users.add(owner)
        try:
            info = await probe_url(url) or {}
            card = await service.send(context, source_message,
                [bubble_item(url, info)], title=info.get('title') or '', view='progress', reuse=msg)
        except BaseException:
            service.busy_users.discard(owner)
            raise
        if not card:
            service.busy_users.discard(owner)
        if card:
            if msg and getattr(msg, 'message_id', None) != card.message_id:
                try:
                    await msg.delete()
                except Exception:
                    pass
            card.format = format_choice
            card.busy = True
            service.busy_users.add(owner)
            await service.run(card, 'download')
            return
    return await _download_and_send(context, source_message, url, format_choice, msg, delete_source)


@queued_media
async def _download_and_send(context, source_message, url, format_choice, msg=None, delete_source=False):
    cleanup_stale_downloads()
    words = context_words(context)
    chat_id = source_message.chat_id
    if msg is None:
        msg = await telegram_retry(
            lambda: context.bot.send_message(chat_id=chat_id, text=words["download_plain"]))
    if delete_source:
        try:
            await source_message.delete()
        except Exception:
            pass

    loop = asyncio.get_running_loop()
    progress_hook = make_progress_hook(msg, loop, context_words(context))
    try:
        filename, info = await download_media(url, format_choice=format_choice, progress_cb=progress_hook)
        await progress_hook.flush()
    except Exception as e:
        LOG.warning(f"Download error: {e}")
        if isinstance(e, MediaTooLarge):
            if getattr(msg, 'is_bubble', False):
                await msg.error('too_large')
            else:
                await edit_text_retry(msg, words['too_large_plain'])
        elif getattr(msg, 'is_bubble', False) and 'video is not available' in str(e).lower():
            await msg.error('unavailable')
        else:
            await edit_text_retry(msg, words["source_failed"])
        return

    if not filename or not os.path.exists(filename):
        await edit_text_retry(msg, words["prepare_failed"])
        return

    if getattr(msg, 'is_bubble', False):
        msg.media_ready(filename, info)
    await edit_text_retry(msg, words["sending_plain"])
    sent = False

    try:
        await send_media_file(context, chat_id, filename, info)
        sent = True
        LOG.info('Media sent bytes=%d format=%s', os.path.getsize(filename), os.path.splitext(filename)[1])
        if getattr(msg, 'is_bubble', False):
            await msg.complete()
    except Exception as e:
        LOG.warning(f"File sending error: {e}")
        if getattr(msg, 'is_bubble', False):
            await msg.send_failed(filename, info, kind='uncertain' if isinstance(e, AmbiguousDelivery) else 'send')
            return
        await telegram_retry(lambda: context.bot.send_message(
            chat_id=chat_id, text=words["problems"]["uncertain"][3] if isinstance(e, AmbiguousDelivery) else words["sending_failed"]))

    try:
        os.remove(filename)
    except Exception as e:
        if sent or os.path.exists(filename):
            LOG.warning(f"File deletion error: {e}")

    cleanup_stale_downloads()

    try:
        await msg.delete()
    except Exception:
        pass

@queued_media
async def download_and_send_tiktok_photos(context, source_message, url, msg=None):
    cleanup_stale_downloads()
    chat_id = source_message.chat_id
    InputFile = get_telegram_components()['InputFile']
    if msg is None:
        msg = await telegram_retry(
            lambda: context.bot.send_message(chat_id=chat_id, text="⬇️ Downloading photos..."))

    try:
        files, info = await download_tiktok_photos(url)
    except Exception as error:
        await edit_text_retry(msg, f"Photo download error: {error}")
        return

    await edit_text_retry(msg, f"📤 Sending {len(files)} photo(s)...")
    sent = False
    try:
        for path in files:
            def send_photo():
                with open(path, 'rb') as stream:
                    photo = InputFile(stream, filename=safe_download_filename(info, 'jpg'))
                    return context.bot.send_photo(chat_id=chat_id, photo=photo)

            await telegram_retry(send_photo)
            sent = True
    except Exception as error:
        LOG.warning(f"Photo sending error: {error}")
        await telegram_retry(lambda: context.bot.send_message(
            chat_id=chat_id, text=f"Photo sending error.\n{error}"))
    finally:
        for path in files:
            try:
                os.remove(path)
            except OSError:
                pass
        cleanup_stale_downloads()

    if sent:
        try:
            await msg.delete()
        except Exception:
            pass

@queued_media
async def download_and_send_instagram_photo(context, source_message, url, msg=None):
    cleanup_stale_downloads()
    chat_id = source_message.chat_id
    InputFile = get_telegram_components()['InputFile']
    if msg is None:
        msg = await telegram_retry(
            lambda: context.bot.send_message(chat_id=chat_id, text="⬇️ Downloading photo..."))

    try:
        files, info = await download_instagram_photo(url)
        await edit_text_retry(msg, "📤 Sending photo...")
        path = files[0]

        def send_photo():
            with open(path, 'rb') as stream:
                photo = InputFile(stream, filename=safe_download_filename(info, 'jpg'))
                return context.bot.send_photo(chat_id=chat_id, photo=photo)

        await telegram_retry(send_photo)
    except Exception as error:
        await edit_text_retry(msg, f"Photo download error: {error}")
        return
    finally:
        for path in locals().get('files', []):
            try:
                os.remove(path)
            except OSError:
                pass
        cleanup_stale_downloads()

    try:
        await msg.delete()
    except Exception:
        pass

async def search_youtube(query: str):
    yt_dlp = get_yt_dlp()
    loop = asyncio.get_running_loop()

    def _search():
        ydl_opts = {
#            'cookiefile': "cookies.txt",
#            'quiet': True,
            'skip_download': True,
            'extract_flat': 'in_playlist',
            'socket_timeout': 8,
            'retries': 1,
            'extractor_retries': 1,
        }
        with ProxyRetryYoutubeDL(ydl_options(ydl_opts)) as ydl:
            return ydl.extract_info(f"ytsearch8:{query}", download=False)

    info = await run_blocking(_search)
    return info.get('entries', [])[:8] if info and info.get('entries') else []

def store_callback_url(context, url):
    links = context.user_data.setdefault('media_links', {})
    if len(links) >= MAX_CALLBACK_LINKS:
        links.pop(next(iter(links)))
    import hashlib
    url_id = hashlib.md5(url.encode()).hexdigest()[:8]
    links[url_id] = url
    return url_id

def make_format_keyboard(url, context, include_audio=True, include_video=True):
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
    words = context_words(context)

    url_id = store_callback_url(context, url)

    buttons = []
    if include_audio:
        buttons.append(InlineKeyboardButton("♫ " + words["audio"], callback_data=f"dl|{url_id}|mp3"))
    if include_video:
        buttons.append(InlineKeyboardButton("▷ " + words["video"], callback_data=f"dl|{url_id}|mp4"))

    keyboard = InlineKeyboardMarkup([buttons])
    return keyboard

async def make_results_keyboard(results, context):
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
    import hashlib
    words = context_words(context)

    buttons = []
    for index, info in enumerate(results, start=1):
        video_id = info.get('id')
        if not video_id:
            continue

        url = f"https://www.youtube.com/watch?v={video_id}"
        url_id = store_callback_url(context, url)
        has_audio, has_video = info_media_types(info)
        if not has_audio and not has_video:
            has_audio = has_video = True
        row = []
        if has_audio:
            row.append(InlineKeyboardButton(f"{index}. {words['audio']}", callback_data=f"dl|{url_id}|mp3"))
        if has_video:
            row.append(InlineKeyboardButton(f"{index}. {words['video']}", callback_data=f"dl|{url_id}|mp4"))
        if not row:
            row.append(InlineKeyboardButton(f"{index}. {words['video']}", callback_data=f"dl|{url_id}|mp4"))
        if row:
            buttons.append(row)

    return InlineKeyboardMarkup(buttons)

def bubble_service(context):
    app = getattr(context, 'application', None)
    return app.bot_data.get('bubbles') if app is not None else None


def context_words(context):
    preferences = getattr(context, 'user_data', None) or {}
    return bubble_strings(preferences.get('language') or preferences.get('telegram_language'))


def ui_words(update, context):
    preferences = getattr(context, 'user_data', None)
    user = getattr(update, 'effective_user', None)
    if preferences is not None:
        preferences['telegram_language'] = getattr(user, 'language_code', None)
    return bubble_strings(user_language(user, preferences))


async def start(update, context):
    await telegram_retry(lambda: update.message.reply_text(ui_words(update, context)['start']))


async def bubble_mode(update, context):
    enabled = update.message.text.split()[0].split('@')[0] == '/bubbles'
    context.user_data['bubbles_enabled'] = enabled
    await update.message.reply_text(ui_words(update, context)['widget_on' if enabled else 'widget_off'])


def language_keyboard(owner, words):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('Русский', callback_data=f'lang|{owner}|ru'),
         InlineKeyboardButton('English', callback_data=f'lang|{owner}|en')],
        [InlineKeyboardButton(words['language_auto'], callback_data=f'lang|{owner}|auto')],
    ])


def apply_language(context, choice):
    from bubbles.service import locale
    if choice == 'auto':
        context.user_data.pop('language', None)
    elif choice and locale(choice) == choice:
        context.user_data['language'] = choice
    else:
        return False
    return True


def language_confirmation(update, context):
    words = ui_words(update, context)
    key = 'language_set' if context.user_data.get('language') else 'language_set_auto'
    return words[key].format(language=user_language(update.effective_user, context.user_data))


async def language_command(update, context):
    choice = context.args[0].lower() if context.args else ''
    if not apply_language(context, choice):
        words = ui_words(update, context)
        await update.message.reply_text(words['language_help'],
            reply_markup=language_keyboard(update.effective_user.id, words))
        return
    await update.message.reply_text(language_confirmation(update, context))


async def language_callback(update, context):
    query = update.callback_query
    data = (query.data or '').split('|')
    words = ui_words(update, context)
    if (len(data) != 3 or data[1] != str(update.effective_user.id)
            or data[2] not in ('ru', 'en', 'auto')):
        await query.answer(words['unavailable'])
        return
    apply_language(context, data[2])
    words = ui_words(update, context)
    try:
        await query.answer(connect_timeout=3, read_timeout=3, write_timeout=3, pool_timeout=1)
    except Exception as error:
        LOG.warning('Language selection acknowledgement failed (%s)', type(error).__name__)
    try:
        await query.edit_message_text(language_confirmation(update, context),
            reply_markup=language_keyboard(update.effective_user.id, words))
    except Exception as error:
        if 'message is not modified' not in str(error).lower():
            raise


async def try_bubble(context, source, progress_message, items, title='', view='format', error=None):
    service = bubble_service(context)
    if service:
        card = await service.send(context, source, items, title, view, error, reuse=progress_message)
        if card:
            if getattr(progress_message, 'message_id', None) != card.message_id:
                try:
                    await progress_message.delete()
                except Exception:
                    pass
            return card
    return None


async def search_command(update, context):
    text = " ".join(context.args or []).strip()
    if not text:
        await telegram_retry(lambda: update.message.reply_text(ui_words(update, context)["search_usage"]))
        return
    await process_message(update, context, text=text)


async def process_message(update, context, text=None):
    text = (update.message.text or "").strip() if text is None else text
    words = ui_words(update, context)
    service = bubble_service(context)
    msg = await service.send_loading(context, update.message) if service else None
    if msg is None:
        msg = await telegram_retry(lambda: update.message.reply_text(words["processing"]))

    parts = text.split()
    if parts and parts[-1].lower() in ("mp3", "mp4"):
        query = " ".join(parts[:-1])
    else:
        query = text

    url = normalize_url(query)
    if url:
        url = await resolve_tiktok_short_url(url)
        if is_tiktok_photo_url(url):
            await download_and_send_tiktok_photos(context, update.message, url, msg=msg)
            return
        if is_instagram_photo_url(url):
            await download_and_send_instagram_photo(context, update.message, url, msg=msg)
            return
        if is_yandex_music_url(url):
            youtube_url, error = await yandex_to_youtube(url, search_youtube)
            if not youtube_url:
                await edit_text_retry(msg, error)
                return
            await download_and_send(context, update.message, youtube_url, "mp3", msg=msg)
            return
        if is_spotify_url(url):
            spotify_url, spotify_error = await spotify_to_youtube(url)
            if not spotify_url:
                await edit_text_retry(msg, spotify_error)
                return
            await download_and_send(context, update.message, spotify_url, "mp3", msg=msg)
            return
        info = await probe_url(url)
        if info_is_playlist(info):
            await edit_text_retry(msg, words["playlist"])
            return
        has_audio, has_video = info_media_types(info)
        if not has_audio and not has_video:
            has_audio = has_video = True
        if has_audio != has_video:
            format_choice = "mp3" if has_audio else "mp4"
            await download_and_send(context, update.message, url, format_choice, msg=msg)
            return
        service = bubble_service(context)
        if service and service.enabled(update.message, context):
            info = info or {}
            if await try_bubble(context, update.message, msg,
                    [bubble_item(url, info, has_audio, has_video)], title=info.get('title') or query):
                return
        keyboard = make_format_keyboard(url, context, include_audio=has_audio, include_video=has_video)
        # Проверяем тип клавиатуры
        from telegram import InlineKeyboardMarkup
        if not isinstance(keyboard, InlineKeyboardMarkup):
            await edit_text_retry(msg, "Error: invalid keyboard format")
            return
        try:
            await edit_text_retry(msg, words["choose_plain"], reply_markup=keyboard)
        except Exception as e:
            LOG.warning(f"Message edit error: {e}")
        return

    try:
        results = await asyncio.wait_for(search_youtube(query), timeout=SEARCH_TIMEOUT)
    except Exception as exception:
        LOG.warning(f'Search failed: {type(exception).__name__}')
        if not await try_bubble(context,update.message,msg,[],query,view='search',error='search'):
            await edit_text_retry(msg,words['search_failed'])
        return
    if not results:
        if await try_bubble(context, update.message, msg, [], query, view='search', error='empty'):
            return
        await edit_text_retry(msg, words["nothing"])
        return

    service = bubble_service(context)
    if service and service.enabled(update.message, context):
        items = []
        usable = [info for info in results if info.get('id')]
        urls = [f"https://www.youtube.com/watch?v={info['id']}" for info in usable]
        for url, info in zip(urls, usable):
            audio, video = info_media_types(info)
            if not audio and not video:
                audio = video = True
            items.append(bubble_item(url, info, audio, video))
        if await try_bubble(context, update.message, msg, items, query, view='search',
                            error='empty' if not items else None):
            return
    keyboard = await make_results_keyboard(results, context)
    lines = [words["found_plain"]]
    for index, info in enumerate(results, start=1):
        title = (info.get('title') or 'Untitled').strip()
        uploader = (info.get('uploader') or 'Unknown artist').strip()
        lines.append(f"{index}. {title}\n   {uploader}")

    await edit_text_retry(msg, "\n".join(lines), reply_markup=keyboard)

async def callback(update, context):
    query = update.callback_query
    words = ui_words(update, context)
    if (query.data or '').startswith('bubble|'):
        await bubble_service(context).handle_callback(update, context)
        return
    await query.answer()

    data = query.data.split('|')
    links = context.user_data.get('media_links', {})
    if data[0] == "pick":
        if len(data) != 2 or not links.get(data[1]):
            await telegram_retry(lambda: query.message.reply_text(words["expired_hint"]))
            return

        has_audio, has_video = await url_media_types(links[data[1]])
        if not has_audio and not has_video:
            has_audio = has_video = True
        keyboard = make_format_keyboard(
            links[data[1]], context,
            include_audio=has_audio,
            include_video=has_video,
        )
        await edit_text_retry(query.message, words["choose_plain"], reply_markup=keyboard)
        return

    if len(data) < 3 or data[0] != "dl":
        await telegram_retry(lambda: query.message.reply_text(words["invalid"]))
        return
    url_id = data[1]
    format_choice = data[2]

    url = links.get(url_id)

    if not url:
        await telegram_retry(lambda: query.message.reply_text(words["expired_hint"]))
        return

    if not url or format_choice not in ("mp3", "mp4"):
        await telegram_retry(lambda: query.message.reply_text(words["invalid"]))
        return

    if await url_is_playlist(url):
        await telegram_retry(lambda: query.message.reply_text(
            words["playlist"]))
        return

    await download_and_send(context, query.message, url, format_choice, delete_source=True)

async def handle_error(update, context):
    error = context.error
    from telegram.error import NetworkError, RetryAfter
    if isinstance(error, (NetworkError, RetryAfter)):
        LOG.warning('Telegram network/API error: %s: %s', type(error).__name__, error)
    else:
        LOG.error('Unhandled Telegram handler error', exc_info=(type(error), error, error.__traceback__))


async def register_commands(application):
    """Command handlers do not populate Telegram's slash-command menu."""
    from telegram import BotCommand, BotCommandScopeDefault
    for language in ('', 'en', 'ru'):
        commands = [BotCommand(command, description)
                    for command, description in bubble_strings(language)['commands'].items()]
        try:
            await telegram_retry(lambda: application.bot.set_my_commands(
                commands, scope=BotCommandScopeDefault(), language_code=language,
                connect_timeout=5, read_timeout=5, write_timeout=5, pool_timeout=2))
            LOG.info('Command menu registered language=%s count=%d', language or 'default', len(commands))
        except Exception:
            # A menu API outage must not prevent the bot from receiving messages.
            LOG.warning('Could not register command menu language=%s', language or 'default', exc_info=True)


def build_app():
    comps = get_telegram_components()
    ApplicationBuilder = comps['ApplicationBuilder']
    CommandHandler = comps['CommandHandler']
    MessageHandler = comps['MessageHandler']
    CallbackQueryHandler = comps['CallbackQueryHandler']
    filters = comps['filters']
    HTTPXRequest = comps['HTTPXRequest']

    request = HTTPXRequest(
        connection_pool_size=20,
        connect_timeout=20,
        read_timeout=45,
        write_timeout=90,
        pool_timeout=20,
        http_version='1.1',
    )
    get_updates_request = HTTPXRequest(
        connection_pool_size=4,
        connect_timeout=20,
        read_timeout=75,
        write_timeout=30,
        pool_timeout=20,
        http_version='1.1',
    )
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request)
        .get_updates_request(get_updates_request)
        .post_init(register_commands)
        .concurrent_updates(True)
        .build()
    )
    from telegram import Update
    from telegram.ext import TypeHandler
    service = BubbleService(download_and_send, send_media_file)
    app.bot_data['bubbles'] = service
    app.add_error_handler(handle_error)
    app.add_handler(TypeHandler(Update, service.handle_widget), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("bubbles", bubble_mode))
    app.add_handler(CommandHandler("plain", bubble_mode))
    app.add_handler(CommandHandler("language", language_command))
    app.add_handler(CallbackQueryHandler(language_callback, pattern=r'^lang\|'))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_message))
    return app

def run():
    configure_logging()
    cleanup_stale_downloads()
    LOG.info('Starting bot parallel_downloads=%d', MEDIA_JOBS.limit)
    app = build_app()
    from telegram import Update
    app.run_polling(allowed_updates=list(Update.ALL_TYPES) + ["widget_query"])

if __name__ == "__main__":
    run()
