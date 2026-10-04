import asyncio
import json
import os
import subprocess
import tempfile
import time
from types import SimpleNamespace
from pathlib import Path

import main


CASES = [
    ("tiktok", "https://www.tiktok.com/@travelwithjlc/video/7574474868276661526?q=example&t=1791012601117"),
    ("instagram", "https://www.instagram.com/reels/Db803R9upJ3/"),
    ("coub", "https://coub.com/view/uhazri571j"),
    ("xv-ru", "https://www.xv-ru.com/video.utdkuch1e6a/18-_-_"),
    ("pornhub", "https://rt.pornhub.com/view_video.php?viewkey=69de027156568"),
]


def probe_streams(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "stream=codec_type,codec_name", "-of", "json", path,
        ],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout).get("streams", [])


async def test_downloads():
    for name, url in CASES:
        try:
            path, _ = await main.download_media(url, "mp4")
        except Exception as error:
            if name == "tiktok" and "IP address is blocked" in str(error):
                print(f"SKIP download {name}: server IP is blocked by TikTok")
                continue
            raise
        try:
            size = os.path.getsize(path)
            streams = probe_streams(path)
            codecs = {(s.get("codec_type"), s.get("codec_name")) for s in streams}
            assert size <= 50 * 1024 * 1024, f"{name}: file is over 50 MiB"
            assert any(kind == "video" for kind, _ in codecs), f"{name}: no video stream"
            assert any(kind == "audio" for kind, _ in codecs), f"{name}: no audio stream"
            print(f"PASS download {name}: {size} bytes, {sorted(codecs)}")
        finally:
            if os.path.exists(path):
                os.remove(path)


class FakeMessage:
    chat_id = 123

    async def edit_text(self, *_args, **_kwargs):
        return self

    async def delete(self):
        return None


class FakeBot:
    def __init__(self):
        self.video_attempts = 0

    async def send_video(self, **kwargs):
        self.video_attempts += 1
        if self.video_attempts == 1:
            raise RuntimeError("simulated Telegram timeout")
        assert kwargs["video"] is not None
        return object()

    async def send_message(self, **_kwargs):
        return FakeMessage()


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()


async def test_telegram_retry():
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as file:
        file.write(b"test video payload")
        path = file.name

    original_download = main.download_media
    original_components = main.get_telegram_components
    try:
        async def fake_download(*_args, **_kwargs):
            return path, {}

        main.download_media = fake_download
        main.get_telegram_components = lambda: {"InputFile": __import__("telegram").InputFile}
        context = FakeContext()
        await main.download_and_send(context, FakeMessage(), "https://example.com/video", "mp4", FakeMessage())
        assert context.bot.video_attempts == 2, "Telegram retry did not make a second attempt"
        assert not os.path.exists(path), "sent file was not cleaned up"
        print("PASS Telegram send retry: 2 attempts, file cleaned")
    finally:
        main.download_media = original_download
        main.get_telegram_components = original_components
        if os.path.exists(path):
            os.remove(path)


async def test_text_search_output():
    results = await main.search_youtube("a-ha Take on Me official audio")
    assert results, "text search returned no results"
    assert len(results) <= 8, "search returned more than eight results"

    keyboard = await main.make_results_keyboard(results, SimpleNamespace(user_data={}))
    rows = keyboard.inline_keyboard
    lines = ["Found tracks:"]
    for index, info in enumerate(results, start=1):
        lines.append(f"{index}. {(info.get('title') or 'Untitled').strip()}")

    assert len(lines) == len(results) + 1, "search output lines do not match results"
    assert len(rows) == len(results), "search keyboard rows do not match results"
    print(f"PASS text search: {len(results)} results, {len(rows)} keyboard rows")


async def test_internal_url_blocking():
    for url in ("http://127.0.0.1/", "http://localhost/", "http://169.254.169.254/"):
        try:
            main.validate_external_url(url)
        except ValueError:
            continue
        raise AssertionError(f"internal URL was not blocked: {url}")
    print("PASS internal URL blocking")


class TimedMessage:
    chat_id = 123

    def __init__(self, text="search text"):
        self.text = text
        self.events = []

    async def reply_text(self, text, **_kwargs):
        self.events.append(("reply", text, time.monotonic()))
        return self

    async def edit_text(self, text, **_kwargs):
        self.events.append(("edit", text, time.monotonic()))
        return self


class TimedUpdate:
    def __init__(self, message):
        self.message = message


async def test_message_response_timing():
    message = TimedMessage()
    update = TimedUpdate(message)
    original_search = main.search_youtube
    original_keyboard = main.make_results_keyboard
    try:
        async def fake_search(_query):
            return [{"id": "test", "title": "Test result", "uploader": "Test uploader"}]

        async def fake_keyboard(_results, _context):
            return object()

        main.search_youtube = fake_search
        main.make_results_keyboard = fake_keyboard
        started = time.monotonic()
        await asyncio.wait_for(main.process_message(update, object()), timeout=5)
    finally:
        main.search_youtube = original_search
        main.make_results_keyboard = original_keyboard

    assert message.events[0][0:2] == ("reply", "⏳ Processing...")
    assert message.events[-1][0] == "edit"
    assert message.events[-1][1].startswith("🔎 Found tracks:")
    assert message.events[0][2] - started < 1, "Processing response was delayed"
    assert message.events[-1][2] - started < 5, "Final response timed out"
    print(f"PASS message timing: first={message.events[0][2] - started:.3f}s, total={message.events[-1][2] - started:.3f}s")


async def test_telegram_retry_bounded():
    attempts = 0
    delays = []
    original_sleep = main.asyncio.sleep

    async def fake_sleep(delay):
        delays.append(delay)

    async def always_fail():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("simulated Telegram timeout")

    main.asyncio.sleep = fake_sleep
    try:
        try:
            await main.telegram_retry(always_fail)
        except RuntimeError:
            pass
        else:
            raise AssertionError("telegram_retry did not propagate the final error")
    finally:
        main.asyncio.sleep = original_sleep

    assert attempts == 3, "Telegram retry count changed"
    assert delays == [3, 8], f"Unexpected retry delays: {delays}"
    print("PASS Telegram retry bounded: 3 attempts, delays 3s/8s")


async def main_test():
    await test_downloads()
    await test_telegram_retry()
    await test_text_search_output()
    await test_internal_url_blocking()
    await test_message_response_timing()
    await test_telegram_retry_bounded()


if __name__ == "__main__":
    asyncio.run(main_test())
