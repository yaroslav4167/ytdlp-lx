import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from yandex_music import is_yandex_music_url, yandex_to_youtube


class YandexMusicTests(unittest.IsolatedAsyncioTestCase):
    def test_hosts(self):
        self.assertTrue(is_yandex_music_url("https://music.yandex.ru/track/123"))
        self.assertFalse(is_yandex_music_url("https://music.yandex.ru.evil.com/track/123"))
        self.assertFalse(is_yandex_music_url("https://evil.com/music.yandex.ru"))
        self.assertFalse(is_yandex_music_url("ftp://music.yandex.ru/track/123"))

    async def resolve(self, data, search, status=200, url=None):
        response = MagicMock()
        response.status = status
        response.json = AsyncMock(return_value=data)
        session = MagicMock()
        session.get.return_value.__aenter__ = AsyncMock(return_value=response)
        with patch("yandex_music.aiohttp.ClientSession") as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=session)
            return await yandex_to_youtube(
                url or "https://music.yandex.ru/album/44620083/track/157450762?utm_source=test",
                search,
            )

    async def test_track_search(self):
        search = AsyncMock(return_value=[{"id": "video123"}])
        result = await self.resolve({"result": [{
            "title": "Нас не догонят", "artists": [{"name": "Артем Пора Домой"}, {"name": "t.A.T.u."}],
        }]}, search)
        self.assertEqual(result, ("https://www.youtube.com/watch?v=video123", None))
        search.assert_awaited_once_with("Артем Пора Домой, t.A.T.u. - Нас не догонят official audio")

    async def test_version_and_short_link(self):
        search = AsyncMock(return_value=[{"id": "video123"}])
        await self.resolve({"result": [{"title": "Song", "version": "Remix"}]}, search,
                           url="https://music.yandex.ru/track/123")
        search.assert_awaited_once_with("Song (Remix) official audio")

    async def test_missing_metadata(self):
        for data in ({}, {"result": []}, {"result": [{}]}):
            search = AsyncMock()
            result, error = await self.resolve(data, search)
            self.assertIsNone(result)
            self.assertTrue(error)
            search.assert_not_awaited()

    async def test_api_error(self):
        search = AsyncMock()
        result, error = await self.resolve({}, search, status=403)
        self.assertIsNone(result)
        self.assertTrue(error)
        search.assert_not_awaited()

    async def test_no_youtube_match(self):
        result, error = await self.resolve({"result": [{"title": "Song"}]}, AsyncMock(return_value=[]))
        self.assertIsNone(result)
        self.assertTrue(error)

    async def test_album_rejected(self):
        search = AsyncMock()
        result, error = await yandex_to_youtube("https://music.yandex.ru/album/123", search)
        self.assertIsNone(result)
        self.assertTrue(error)
        search.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
