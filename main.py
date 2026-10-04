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
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

# Runtime configuration. Secrets are supplied through the environment.
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
DOWNLOAD_DIR = "downloads"
DOWNLOAD_PROXY = os.environ.get("DOWNLOAD_PROXY", "")
MAX_UPLOAD_SIZE = 50 * 1024 * 1024
STALE_FILE_AGE = 600
MAX_CALLBACK_LINKS = 32
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Filesystem and network safety
# ---------------------------------------------------------------------------
def cleanup_stale_downloads(max_age=STALE_FILE_AGE):
    now = time.time()
    prefixes = ("video_", "audio_", "input_", "output_")
    try:
        for entry in os.scandir(DOWNLOAD_DIR):
            if entry.is_file() and entry.name.startswith(prefixes):
                if now - entry.stat().st_mtime > max_age:
                    os.remove(entry.path)
    except OSError as error:
        print(f"Download cleanup error: {error}")

cleanup_stale_downloads()

# ---------------------------------------------------------------------------
# yt-dlp integration and proxy fallback
# ---------------------------------------------------------------------------
def get_yt_dlp():
    import yt_dlp
    return yt_dlp

def ydl_options(options=None):
    result = {
        'js_runtimes': {'node': {}},
        'socket_timeout': 15,
    }
    if options:
        result.update(options)
    return result

class ProxyRetryYoutubeDL:
    """Retry yt-dlp through the proxy after HTTP or network failures."""

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def _run(self, method, *args, **kwargs):
        def attempt(proxy=None):
            options = dict(self.options)
            if proxy:
                options['proxy'] = proxy
            with get_yt_dlp().YoutubeDL(options) as ydl:
                return getattr(ydl, method)(*args, **kwargs)

        url = args[0] if args and isinstance(args[0], str) else kwargs.get('url')
        if url and url.startswith(('http://', 'https://')):
            validate_external_url(url)

        if method == 'extract_info' and url and DOWNLOAD_PROXY:
            executor = ThreadPoolExecutor(max_workers=2)
            futures = {
                executor.submit(attempt, route): route
                for route in (None, DOWNLOAD_PROXY)
            }
            last_error = None
            try:
                for future in as_completed(futures):
                    route = futures[future]
                    try:
                        result = future.result()
                        print(f'yt-dlp selected {"proxy" if route else "direct"} route for {url}')
                        return result
                    except Exception as error:
                        last_error = error
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            raise last_error

        try:
            return attempt()
        except Exception as error:
            error_text = str(error).lower()
            http_error = re.search(r'\b(?:40[1-9]|4[1-9]\d|[5-9]\d{2})\b', error_text)
            network_error = any(token in error_text for token in (
                'timeout', 'timed out', 'connection reset', 'connection refused',
                'connection aborted', 'temporary failure', 'network is unreachable',
                'name or service not known',
            ))
            if not http_error and not network_error:
                raise
            proxy = DOWNLOAD_PROXY
            if not proxy:
                raise
            print(f'yt-dlp network error: retrying {method} through configured proxy')
            return attempt(proxy)

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
        print(f"Spotify error: {exc}")
        return None, "Error while contacting Spotify."

def get_telegram_components():
    import telegram
    from telegram import InputFile, InlineKeyboardMarkup, InlineKeyboardButton
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters
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
    }

def make_progress_hook(msg, loop):
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
                    f"⬇️ Downloading...\n"
                    f"{progress_bar(percent)}\n"
                    f"Speed: {speed_str}\n"
                    f"Remaining: {eta_str}"
                )
                await edit_progress(text)
            elif status == 'finished':
                await edit_progress("✅ Downloaded, converting file...")
            elif status == 'converting':
                now = time.time()
                if now - last_update['time'] < 1:
                    return
                last_update['time'] = now
                percent = d.get('percent', 0)
                await edit_progress(
                    f"🔄 Converting...\n{progress_bar(percent)}"
                )
            elif status == 'error':
                await edit_progress("❌ Download error.")
        except Exception as e:
            print(f"Progress update error: {e}")

    def progress_hook(d):
        try:
            asyncio.run_coroutine_threadsafe(progress_hook_async(d), loop)
        except Exception as e:
            print(f"Progress task error: {e}")

    return progress_hook

def run_ffmpeg(command, duration, progress_hook=None):
    process = subprocess.Popen(
        shlex.split(command) + ['-progress', 'pipe:1', '-nostats'],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    duration_us = max((duration or 0) * 1_000_000, 1)
    for line in process.stdout:
        if line.startswith('out_time_us=') and progress_hook:
            try:
                current_us = int(line.split('=', 1)[1])
                progress_hook({
                    'status': 'converting',
                    'percent': min(current_us / duration_us * 100, 100),
                })
            except ValueError:
                pass
    return process.wait()

async def probe_url(url: str):
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
        info = await loop.run_in_executor(None, _probe)
    except Exception:
        return None
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
                print(f"Message edit error after retries: {error}")
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
                print(f"Telegram request error after retries: {error}")
    raise last_error

async def url_is_playlist(url: str) -> bool:
    """True, если ссылка ведёт на плейлист (несколько роликов)."""
    info = await probe_url(url)
    if not info:
        return False
    if info.get('_type') == 'playlist':
        return True
    entries = info.get('entries')
    return bool(entries and len(entries) > 1)

async def url_media_types(url: str):
    info = await probe_url(url)
    if not info:
        return False, False
    formats = info.get('formats', [])
    has_audio = any(format_has_audio(f) for f in formats)
    has_video = any(format_has_video(f) for f in formats)
    direct_exts = {f.get('ext') for f in formats if f.get('url')}
    has_audio = has_audio or bool(direct_exts & {'mp3', 'm4a', 'aac', 'ogg', 'opus', 'wav'})
    has_video = has_video or bool(direct_exts & {'mp4', 'webm', 'mkv', 'mov', 'avi'})
    return has_audio, has_video

def format_has_audio(fmt):
    return fmt.get('acodec') not in (None, 'none') or fmt.get('audio_ext') not in (None, 'none')

def format_has_video(fmt):
    return fmt.get('vcodec') not in (None, 'none') or fmt.get('video_ext') not in (None, 'none')

# ---------------------------------------------------------------------------
# Media download, conversion and Telegram delivery
# ---------------------------------------------------------------------------
async def download_media(url: str, format_choice: str, progress_cb=None):
    yt_dlp = get_yt_dlp()
    loop = asyncio.get_running_loop()

    def _hook(d):
        if progress_cb is not None:
            progress_cb(d)

    def _download():
        # Базовые настройки
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s'),
            'progress_hooks': [_hook],
            'noplaylist': True,
            'writethumbnail': True,
            'embed_thumbnail': True,
            'add_metadata': True,
        }

        # Получаем список форматов
        ydl_probe = ProxyRetryYoutubeDL(ydl_options({'quiet': True}))
        info = ydl_probe.extract_info(url, download=False)

        formats = info.get("formats", [])
        max_size = MAX_UPLOAD_SIZE

        # --- MP3 ---
        if format_choice == "mp3":
            uid = uuid.uuid4().hex

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
                    raise Exception("File is too large: no audio under 50 MB")

            # скачиваем thumbnail
            thumb_url = info.get("thumbnail")
            if thumb_url:
                try:
                    import requests
                    r = requests.get(thumb_url)
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
                    ydl_a.download([url])

                # добавляем только метаданные
                title = safe_meta(info.get("title", ""))
                artist = safe_meta(info.get("uploader", ""))

                final_m4a = os.path.join(DOWNLOAD_DIR, f"final_{uid}.m4a")

                subprocess.run(
                    [
                        'ffmpeg', '-y', '-i', m4a_path,
                        '-metadata', f'title={title}',
                        '-metadata', f'artist={artist}',
                        '-c:a', 'copy', final_m4a,
                    ],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )

                try: os.remove(m4a_path)
                except: pass

                if downloaded_size(final_m4a) > max_size:
                    os.remove(final_m4a)
                    raise Exception("The prepared file exceeds the 50 MB limit")

                # возвращаем M4A
                return final_m4a, info

            # ---------------------------------------------------------
            # 🔥 Иначе — твой старый MP3‑код
            # ---------------------------------------------------------

            best_audio = best["format_id"]

            # скачиваем аудио
            with ProxyRetryYoutubeDL(ydl_options({
                'outtmpl': audio_path,
                'format': best_audio,
                'progress_hooks': [_hook],
                'max_filesize': max_size,
            })) as ydl_a:
                ydl_a.download([url])

            # создаём MP3 с обложкой
            if thumb_path:
                title = safe_meta(info.get("title", ""))
                artist = safe_meta(info.get("uploader", ""))
                cmd = shlex.join([
                    'ffmpeg', '-y', '-i', audio_path, '-i', thumb_path,
                    '-metadata', f'title={title}',
                    '-metadata', f'artist={artist}',
                    '-metadata:s:v', 'title=Album cover',
                    '-metadata:s:v', 'comment=Cover (front)', output_path,
                ])
            else:
                cmd = f'ffmpeg -y -i "{audio_path}" -c:a libmp3lame -b:a 192k "{output_path}"'

            run_ffmpeg(cmd, info.get('duration'), _hook)

            if downloaded_size(output_path) > max_size:
                os.remove(output_path)
                raise Exception("The prepared file exceeds the 50 MB limit")

            try: os.remove(audio_path)
            except: pass
            try: os.remove(thumb_path)
            except: pass

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
                uid = uuid.uuid4().hex
                direct_ext = direct_format.get('ext') or 'mp4'
                output_path = os.path.join(DOWNLOAD_DIR, f"output_{uid}.mp4")
                direct_path = os.path.join(DOWNLOAD_DIR, f"input_{uid}.{direct_ext}")
                with ProxyRetryYoutubeDL(ydl_options({
                    'outtmpl': direct_path,
                    'format': direct_format["format_id"],
                    'progress_hooks': [_hook],
                    'max_filesize': max_size,
                })) as ydl_direct:
                    ydl_direct.download([url])

                if direct_ext == 'mp4':
                    subprocess.run(
                        ['ffmpeg', '-y', '-i', direct_path, '-c', 'copy',
                         '-movflags', '+faststart', output_path],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                else:
                    subprocess.run(
                        ['ffmpeg', '-y', '-i', direct_path, '-c:v', 'libx264',
                         '-vf', 'scale=-2:480', '-preset', 'ultrafast', '-crf', '32',
                         '-c:a', 'aac', '-movflags', '+faststart', output_path],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )

                try: os.remove(direct_path)
                except: pass

                if downloaded_size(output_path) > max_size:
                    os.remove(output_path)
                    raise Exception("The prepared file exceeds the 50 MB limit")
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
            uid = uuid.uuid4().hex
            video_path = os.path.join(DOWNLOAD_DIR, f"video_{uid}.{video_ext}")
            audio_path = os.path.join(DOWNLOAD_DIR, f"audio_{uid}.{audio_ext or 'webm'}")
            output_path = os.path.join(DOWNLOAD_DIR, f"output_{uid}.mp4")

            # скачиваем видео
            with ProxyRetryYoutubeDL(ydl_options({
                'outtmpl': video_path,
                'format': best_video,
                'progress_hooks': [_hook],
                'max_filesize': max_size,
            })) as ydl_v:
                ydl_v.download([url])

            # скачиваем аудио
            with ProxyRetryYoutubeDL(ydl_options({
                'outtmpl': audio_path,
                'format': best_audio,
                'progress_hooks': [_hook],
                'max_filesize': max_size,
            })) as ydl_a:
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
            run_ffmpeg(cmd, info.get('duration'), _hook)

            if downloaded_size(output_path) > max_size:
                os.remove(output_path)
                raise Exception("The prepared file exceeds the 50 MB limit")

            # удаляем временные файлы
            try: os.remove(video_path)
            except: pass

            try: os.remove(audio_path)
            except: pass

            # возвращаем итоговый файл
            return output_path, info

    return await loop.run_in_executor(None, _download)

def safe_meta(text: str) -> str:
    if not text:
        return ""
    text = text.replace('"', "'")  # убираем двойные кавычки
    return text

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
async def download_and_send(context, source_message, url, format_choice, msg=None, delete_source=False):
    cleanup_stale_downloads()
    chat_id = source_message.chat_id
    if msg is None:
        msg = await telegram_retry(
            lambda: context.bot.send_message(chat_id=chat_id, text="⬇️ Downloading..."))
    if delete_source:
        try:
            await source_message.delete()
        except Exception:
            pass

    loop = asyncio.get_running_loop()
    progress_hook = make_progress_hook(msg, loop)
    try:
        filename, info = await download_media(url, format_choice=format_choice, progress_cb=progress_hook)
    except Exception as e:
        await edit_text_retry(msg, f"Download error: {e}")
        return

    if not filename or not os.path.exists(filename):
        await edit_text_retry(msg, "Could not prepare the file for sending.")
        return

    comps = get_telegram_components()
    InputFile = comps['InputFile']
    await edit_text_retry(msg, "📤 Sending file...")
    ext = os.path.splitext(filename)[1].lower()
    audio_exts = {'.mp3', '.m4a', '.flac', '.ogg', '.opus'}
    document_exts = {'.webm', '.mkv'}
    sent = False

    async def send_file():
        with open(filename, 'rb') as stream:
            file_to_send = InputFile(stream, filename=os.path.basename(filename))
            if ext in audio_exts:
                return await context.bot.send_audio(chat_id=chat_id, audio=file_to_send)
            elif ext in document_exts:
                return await context.bot.send_document(chat_id=chat_id, document=file_to_send)
            else:
                return await context.bot.send_video(chat_id=chat_id, video=file_to_send)

    try:
        await telegram_retry(send_file)
        sent = True
    except Exception as e:
        print(f"File sending error: {e}")
        await telegram_retry(lambda: context.bot.send_message(
            chat_id=chat_id, text=f"File sending error.\n{e}"))

    try:
        os.remove(filename)
    except Exception as e:
        if sent or os.path.exists(filename):
            print(f"File deletion error: {e}")

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
            'extract_flat': 'in_playlist'
        }
        with ProxyRetryYoutubeDL(ydl_options(ydl_opts)) as ydl:
            return ydl.extract_info(f"ytsearch8:{query}", download=False)

    info = await loop.run_in_executor(None, _search)
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

    url_id = store_callback_url(context, url)

    buttons = []
    if include_audio:
        buttons.append(InlineKeyboardButton("🎵 Audio", callback_data=f"dl|{url_id}|mp3"))
    if include_video:
        buttons.append(InlineKeyboardButton("🎬 Video", callback_data=f"dl|{url_id}|mp4"))

    keyboard = InlineKeyboardMarkup([buttons])
    return keyboard

async def make_results_keyboard(results, context):
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
    import hashlib

    buttons = []
    urls = [
        f"https://www.youtube.com/watch?v={info.get('id')}"
        for info in results
        if info.get('id')
    ]
    media_types = await asyncio.gather(*(url_media_types(url) for url in urls))
    type_index = 0

    for index, info in enumerate(results, start=1):
        video_id = info.get('id')
        if not video_id:
            continue

        url = f"https://www.youtube.com/watch?v={video_id}"
        url_id = store_callback_url(context, url)
        has_audio, has_video = media_types[type_index]
        type_index += 1
        row = []
        if has_audio:
            row.append(InlineKeyboardButton(f"{index}. Audio", callback_data=f"dl|{url_id}|mp3"))
        if has_video:
            row.append(InlineKeyboardButton(f"{index}. Video", callback_data=f"dl|{url_id}|mp4"))
        if not row:
            row.append(InlineKeyboardButton(f"{index}. Video", callback_data=f"dl|{url_id}|mp4"))
        if row:
            buttons.append(row)

    return InlineKeyboardMarkup(buttons)

async def start(update, context):
    await telegram_retry(lambda: update.message.reply_text(
        "Send a link or a track/video name.\n"
        "After searching or opening a link, choose audio or video."))

async def process_message(update, context):
    text = (update.message.text or "").strip()
    msg = await telegram_retry(lambda: update.message.reply_text("⏳ Processing..."))

    parts = text.split()
    if parts and parts[-1].lower() in ("mp3", "mp4"):
        query = " ".join(parts[:-1])
    else:
        query = text

    url = normalize_url(query)
    if url:
        if is_spotify_url(url):
            spotify_url, spotify_error = await spotify_to_youtube(url)
            if not spotify_url:
                await edit_text_retry(msg, spotify_error)
                return
            await download_and_send(context, update.message, spotify_url, "mp3", msg=msg)
            return
        if await url_is_playlist(url):
            await edit_text_retry(msg, "Playlists are not supported. Send a link to a single video.")
            return
        has_audio, has_video = await url_media_types(url)
        if not has_audio and not has_video:
            has_audio = has_video = True
        if has_audio != has_video:
            format_choice = "mp3" if has_audio else "mp4"
            await download_and_send(context, update.message, url, format_choice, msg=msg)
            return
        keyboard = make_format_keyboard(url, context, include_audio=has_audio, include_video=has_video)
        # Проверяем тип клавиатуры
        from telegram import InlineKeyboardMarkup
        if not isinstance(keyboard, InlineKeyboardMarkup):
            await edit_text_retry(msg, "Error: invalid keyboard format")
            return
        try:
            await edit_text_retry(msg, "Choose a download format:", reply_markup=keyboard)
        except Exception as e:
            print(f"Message edit error: {e}")
        return

    results = await search_youtube(query)
    if not results:
        await edit_text_retry(msg, "Nothing found.")
        return

    keyboard = await make_results_keyboard(results, context)
    lines = ["🔎 Found tracks:"]
    for index, info in enumerate(results, start=1):
        title = (info.get('title') or 'Untitled').strip()
        uploader = (info.get('uploader') or 'Unknown artist').strip()
        lines.append(f"{index}. {title}\n   {uploader}")

    await edit_text_retry(msg, "\n".join(lines), reply_markup=keyboard)

async def callback(update, context):
    query = update.callback_query
    await query.answer()

    data = query.data.split('|')
    links = context.user_data.get('media_links', {})
    if data[0] == "pick":
        if len(data) != 2 or not links.get(data[1]):
            await telegram_retry(lambda: query.message.reply_text("The link has expired or is invalid."))
            return

        has_audio, has_video = await url_media_types(links[data[1]])
        if not has_audio and not has_video:
            has_audio = has_video = True
        keyboard = make_format_keyboard(
            links[data[1]], context,
            include_audio=has_audio,
            include_video=has_video,
        )
        await edit_text_retry(query.message, "Choose a download format:", reply_markup=keyboard)
        return

    if len(data) < 3 or data[0] != "dl":
        await telegram_retry(lambda: query.message.reply_text("Invalid data."))
        return
    url_id = data[1]
    format_choice = data[2]

    url = links.get(url_id)

    if not url:
        await telegram_retry(lambda: query.message.reply_text("The link has expired or is invalid."))
        return

    if not url or format_choice not in ("mp3", "mp4"):
        await telegram_retry(lambda: query.message.reply_text("Invalid data."))
        return

    if await url_is_playlist(url):
        await telegram_retry(lambda: query.message.reply_text(
            "Playlists are not supported. Send a link to a single video."))
        return

    await download_and_send(context, query.message, url, format_choice, delete_source=True)

def build_app():
    comps = get_telegram_components()
    ApplicationBuilder = comps['ApplicationBuilder']
    CommandHandler = comps['CommandHandler']
    MessageHandler = comps['MessageHandler']
    CallbackQueryHandler = comps['CallbackQueryHandler']
    filters = comps['filters']

    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_message))
    return app

def run():
    app = build_app()
    app.run_polling()

if __name__ == "__main__":
    run()
