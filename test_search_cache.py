import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import main
from metadata_cache import MetadataCache
from telegram import Bot, Message, Update
from telegram.ext import CommandHandler


class CacheTests(unittest.TestCase):
    def test_expiry_eviction_and_isolation(self):
        now = [0]
        cache = MetadataCache(ttl=90, limit=2, clock=lambda: now[0])
        info = {'formats': [{'url': 'https://example.org/media'}]}
        cache.put('a', info)
        cache.get('a')['formats'].clear()
        self.assertEqual(len(cache.get('a')['formats']), 1)
        cache.put('b', info)
        cache.put('c', info)
        self.assertIsNone(cache.get('a'))
        now[0] = 90
        self.assertIsNone(cache.get('b'))

    def test_incomplete_playlists_and_live_are_not_cached(self):
        cache = MetadataCache()
        for info in (None, {}, {'formats': [{}], '_type': 'playlist'}, {'formats': [{}], 'is_live': True}):
            cache.put('a', info)
            self.assertIsNone(cache.get('a'))


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_reuses_metadata_and_failed_probe_retries(self):
        extractor = Mock()
        extractor.extract_info.side_effect = [RuntimeError('temporary'), {'formats': [{'acodec': 'aac'}]}]
        extractor.__enter__ = Mock(return_value=extractor)
        extractor.__exit__ = Mock(return_value=False)
        with patch.object(main, 'METADATA', MetadataCache()), patch.object(main, 'ProxyRetryYoutubeDL', return_value=extractor):
            self.assertIsNone(await main.probe_url('https://example.org/a'))
            first = await main.probe_url('https://example.org/a')
            self.assertEqual(first, await main.probe_url('https://example.org/a'))
            self.assertEqual(extractor.extract_info.call_count, 2)

    async def test_download_uses_cached_probe(self):
        cache = MetadataCache()
        cache.put('https://example.org/a', {'formats': [{'acodec': 'aac', 'filesize': main.MAX_UPLOAD_SIZE + 1}]})
        with patch.object(main, 'METADATA', cache), patch.object(main, 'ProxyRetryYoutubeDL') as extractor:
            with self.assertRaises(main.MediaTooLarge):
                await main.download_media('https://example.org/a', 'mp3')
            extractor.assert_not_called()

    async def test_search_passes_arguments_and_keeps_source(self):
        update = SimpleNamespace(message=SimpleNamespace(text='/search@ytdlp_lx_bot Daft Punk'))
        context = SimpleNamespace(args=['Daft', 'Punk'])
        with patch.object(main, 'process_message', new_callable=AsyncMock) as process:
            await main.search_command(update, context)
            process.assert_awaited_once_with(update, context, text='Daft Punk')
        self.assertTrue(update.message.text.startswith('/search@'))

    async def test_empty_search_shows_help(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        with patch.object(main, 'ui_words', return_value={'search_usage': 'usage'}):
            await main.search_command(SimpleNamespace(message=message), SimpleNamespace(args=[]))
        message.reply_text.assert_awaited_once_with('usage')

    async def test_group_addressed_command(self):
        bot = Bot('123:TEST')
        bot._bot_user = __import__('telegram').User(123, 'Bot', True, username='ytdlp_lx_bot')
        update = Update.de_json({'update_id': 1, 'message': {'message_id': 1, 'date': 1,
            'chat': {'id': -100123, 'type': 'supergroup', 'title': 'Test'},
            'from': {'id': 42, 'is_bot': False, 'first_name': 'User'},
            'text': '/search@ytdlp_lx_bot Daft Punk',
            'entities': [{'type': 'bot_command', 'offset': 0, 'length': 20}]}}, bot)
        handler = CommandHandler('search', main.search_command)
        result = handler.check_update(update)
        self.assertIsNot(result, False)
        self.assertEqual(result[0], ['Daft', 'Punk'])


if __name__ == '__main__':
    unittest.main()
