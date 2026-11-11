"""Offline integration checks; never sends Telegram messages or downloads media."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import zlib

os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123456:offline-test')
import main
from bubbles.service import BubbleService, RETAINED_FILES, item, load_design, locale, state_json, strings


class BubbleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(do_api_request=AsyncMock(return_value={'message_id': 42}),
            edit_message_caption=AsyncMock(), send_message=AsyncMock())
        self.tasks = []
        def create_task(coro):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task
        self.app = SimpleNamespace(create_task=create_task, user_data={123:{}}, bot_data={})
        self.context = SimpleNamespace(bot=self.bot, user_data={}, application=self.app)
        self.source = SimpleNamespace(chat_id=123, chat=SimpleNamespace(type='private'),
            from_user=SimpleNamespace(id=123, language_code='ru-RU', is_bot=False))
        self.download, self.send = AsyncMock(), AsyncMock()
        self.service = BubbleService(self.download, self.send)
        self.app.bot_data['bubbles'] = self.service
        self.items = [item('https://example.com/'+str(i), {'title':'Трек '+str(i)}) for i in range(8)]

    async def asyncTearDown(self):
        if self.tasks:
            await asyncio.gather(*self.tasks)
        for entry in self.service.entries.values():
            entry.forget_file(remove=True)

    async def card(self, view='format'):
        return await self.service.send(self.context,self.source,self.items if view=='search' else self.items[:1], 'Поиск', view)

    async def dispatch(self, data, owner=123):
        answer=AsyncMock()
        await self.service.dispatch(self.context,owner,{'id':123,'type':'private'},42,data,answer,'ru')
        return answer

    def test_language_fallback_and_template_budgets(self):
        self.assertEqual(locale(None),'en')
        self.assertEqual(locale('fr'),'en')
        self.assertEqual(locale('ru-RU'),'ru')
        for code in ('ru','en'):
            source=load_design(code)
            self.assertLessEqual(len(source.encode()),32768)
            self.assertLessEqual(len(zlib.compress(source.encode(),9)),4096)
            self.assertNotIn('__I18N__',source)
            self.assertIn(strings(code)['problems']['send'][2],source)

    async def test_private_chats_and_plain_opt_out(self):
        self.source.chat.type='supergroup'
        self.assertIsNone(await self.card())
        self.source.chat.type='private'; self.context.user_data['bubbles_enabled']=False
        self.assertIsNone(await self.card())
        self.bot.do_api_request.assert_not_awaited()

    async def test_transport_failure_falls_back(self):
        self.bot.do_api_request.side_effect=RuntimeError('Unsupported')
        self.assertIsNone(await self.card())
        self.assertFalse(self.service.entries)

    async def test_real_metadata_and_bounded_search_page(self):
        self.items=[item('https://example.com/'+str(i),{'title':'😀'*200,'uploader':'Я'*200}) for i in range(8)]
        entry=await self.card('search')
        self.assertIsNotNone(entry)
        self.assertLessEqual(len(state_json(entry.state).encode()),1024)
        await self.dispatch('page|2')
        self.assertEqual([r['i'] for r in entry.state['rows']],[6,7])
        self.assertNotIn('https://',state_json(entry.state))
        await self.dispatch('page|99')
        self.assertEqual(entry.state['page'],2)

    async def test_ownership_and_invalid_format(self):
        await self.card()
        await self.dispatch('dl|0|mp3',owner=999)
        await self.dispatch('dl|0|exe')
        await self.dispatch('dl|100|mp3')
        self.download.assert_not_awaited()
        self.assertFalse(self.tasks)

    async def test_duplicate_click_does_not_start_second_job(self):
        await self.card()
        await self.dispatch('dl|0|mp3')
        await self.dispatch('dl|0|mp3')
        await asyncio.gather(*self.tasks)
        self.download.assert_awaited_once()
        self.assertFalse(self.service.busy_users)

    async def test_query_answered_before_download(self):
        order=[]
        self.download.side_effect=lambda *a,**k:order.append('download')
        await self.card()
        async def answer(_):order.append('answer')
        await self.service.dispatch(self.context,123,{'id':123,'type':'private'},42,'dl|0|mp4',answer)
        await asyncio.gather(*self.tasks)
        self.assertEqual(order,['answer','download'])

    async def test_resend_uses_existing_file_and_cleans_up(self):
        entry=await self.card();await entry.begin(0,'mp3')
        with tempfile.NamedTemporaryFile(delete=False) as file:file.write(b'audio'); filename=file.name
        await entry.send_failed(filename,{'title':'Song'})
        self.assertIn(filename,RETAINED_FILES)
        await self.dispatch('resend')
        await asyncio.gather(*self.tasks)
        self.send.assert_awaited_once()
        self.download.assert_not_awaited()
        self.assertFalse(Path(filename).exists())
        self.assertNotIn(filename,RETAINED_FILES)
        self.assertEqual(entry.state['stage'],'done')

    async def test_expired_file_cannot_be_resent(self):
        entry=await self.card();await entry.begin(0,'mp3')
        with tempfile.NamedTemporaryFile(delete=False) as file:filename=file.name
        await entry.send_failed(filename,{})
        entry.file_deadline=0
        await self.dispatch('resend')
        self.assertEqual(entry.state['error'],'expired')
        self.assertFalse(Path(filename).exists())
        self.send.assert_not_awaited()

    async def test_background_cleanup_keeps_retry_file(self):
        entry=await self.card()
        with tempfile.TemporaryDirectory() as folder:
            filename=str(Path(folder)/'output_test.mp3');Path(filename).write_bytes(b'audio')
            os.utime(filename,(0,0));await entry.send_failed(filename,{})
            with patch.object(main,'DOWNLOAD_DIR',folder):main.cleanup_stale_downloads()
            self.assertTrue(Path(filename).exists())
            entry.expire_file()
            self.assertFalse(Path(filename).exists())

    async def test_raw_future_update_is_handled(self):
        from telegram import Update
        await self.card()
        update=Update.de_json({'update_id':1,'widget_query':{'id':'q1','from':{'id':123,'language_code':'ru'},
            'chat':{'id':123,'type':'private'},'message_id':42,'data':'input'}},None)
        await self.service.handle_widget(update,self.context)
        call=self.bot.do_api_request.await_args
        self.assertEqual(call.kwargs['endpoint'],'answerWidgetQuery')
        self.assertEqual(call.kwargs['api_kwargs']['data'],strings('ru')['input_prompt'])

    async def test_failed_delivery_retains_file_on_real_download_path(self):
        entry=await self.card();await entry.begin(0,'mp3')
        with tempfile.NamedTemporaryFile(delete=False) as file:file.write(b'audio');filename=file.name
        with patch.object(main,'download_media',AsyncMock(return_value=(filename,{'title':'Test'}))), \
             patch.object(main,'send_media_file',AsyncMock(side_effect=RuntimeError('delivery failed'))):
            await main.download_and_send(self.context,self.source,self.items[0]['url'],'mp3',msg=entry)
        self.assertEqual(entry.state['error'],'send')
        self.assertTrue(Path(filename).exists())


if __name__=='__main__':unittest.main()
