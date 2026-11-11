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
            edit_message_caption=AsyncMock(), send_message=AsyncMock(), delete_message=AsyncMock())
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
            self.assertIn(strings(code)['download_audio'],source)

    async def test_progress_keeps_latest_sample_and_stops_after_sending(self):
        entry = await self.card()
        await entry.begin(0, 'mp3')
        for percent in range(1, 100):
            await entry.update_progress({'status':'downloading', 'downloaded_bytes':percent, 'total_bytes':100})
        self.assertEqual(entry.state['percent'], 99)
        await entry.progress_task
        published = json.loads(self.bot.do_api_request.await_args.kwargs['api_kwargs']['chat_data'])
        self.assertEqual(published['percent'], 99)
        await entry.update_progress({'status':'downloading', 'downloaded_bytes':100, 'total_bytes':100})
        await entry.edit_text(entry.words['sending_plain'])
        await entry.update_progress({'status':'downloading', 'downloaded_bytes':1, 'total_bytes':100})
        await entry.progress_task
        self.assertEqual(entry.state['stage'], 'sending')
        self.assertLess(self.bot.do_api_request.await_count, 8)

    async def test_progress_unknown_and_estimated_totals(self):
        entry = await self.card()
        await entry.begin(0, 'mp3')
        self.assertIsNone(entry.state['percent'])
        await entry.update_progress({'status':'downloading', 'downloaded_bytes':1024})
        self.assertIsNone(entry.state['percent'])
        self.assertTrue(entry.state['size'])
        await entry.update_progress({'status':'downloading', 'downloaded_bytes':100, 'total_bytes_estimate':300})
        self.assertIsNone(entry.state['percent'])
        self.assertEqual(entry.state['stage'], 'downloading')
        await entry.progress_task

    async def test_stream_completion_does_not_start_preparation(self):
        entry = await self.card()
        await entry.begin(0, 'mp4')
        await entry.update_progress({'status':'finished', 'downloaded_bytes':100, 'total_bytes':100, 'part':'video'})
        self.assertEqual(entry.state['stage'], 'downloading')
        self.assertEqual(entry.state['part'], entry.words['video'])
        await entry.update_progress({'status':'downloading', 'downloaded_bytes':20, 'total_bytes':100, 'part':'audio'})
        self.assertEqual(entry.state['stage'], 'downloading')
        self.assertEqual(entry.state['part'], entry.words['audio'])
        await entry.update_progress({'status':'converting', 'percent':None})
        self.assertEqual(entry.state['stage'], 'converting')
        self.assertIsNone(entry.state['percent'])
        self.assertFalse(entry.state['part'])
        await entry.progress_task

    async def test_failed_progress_edit_retries_latest_state(self):
        entry = await self.card()
        await entry.begin(0, 'mp3')
        self.bot.do_api_request.side_effect = [TimeoutError(), {'ok':True}]
        await entry.update_progress({'status':'downloading', 'downloaded_bytes':30, 'total_bytes':100})
        await entry.progress_task
        self.assertEqual(json.loads(entry.published)['percent'], 30)

    def test_ffmpeg_without_duration_never_invents_percent(self):
        process = SimpleNamespace(stdout=['out_time_us=1000000\n'], wait=lambda:0)
        events = []
        with patch.object(main.subprocess, 'Popen', return_value=process):
            self.assertEqual(main.run_ffmpeg('ffmpeg -i input output', None, events.append), 0)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e['percent'] is None for e in events))

    async def test_fast_download_is_published_before_conversion_with_slow_transport(self):
        entry = await self.card()
        await entry.begin(0, 'mp3')
        self.assertEqual(entry.state['stage_title'], entry.words['waiting_source'])
        sent = []
        async def slow_request(endpoint, api_kwargs, **kwargs):
            sent.append(json.loads(api_kwargs['chat_data']))
            await asyncio.sleep(.05)
            return True
        self.bot.do_api_request.side_effect = slow_request
        await entry.update_progress({'status':'downloading', 'downloaded_bytes':1, 'total_bytes':100})
        await asyncio.sleep(.01)
        await entry.update_progress({'status':'finished', 'downloaded_bytes':100, 'total_bytes':100})
        await entry.update_progress({'status':'converting', 'percent':None})
        await entry.update_progress({'status':'converting', 'percent':30})
        await entry.progress_task
        phases = [(s['stage'], s.get('percent')) for s in sent]
        self.assertIn(('downloading',100), phases)
        self.assertLess(phases.index(('downloading',100)), phases.index(('converting',30)))
        self.assertNotIn('stage_title', entry.state)

    async def test_progress_hook_flush_waits_for_queued_phase_transition(self):
        entry = await self.card()
        await entry.begin(0, 'mp3')
        hook = main.make_progress_hook(entry, asyncio.get_running_loop())
        def worker():
            hook({'status':'finished', 'downloaded_bytes':100, 'total_bytes':100})
            hook({'status':'converting', 'percent':25})
        await asyncio.to_thread(worker)
        await hook.flush()
        self.assertEqual(entry.state['stage'], 'converting')
        self.assertEqual(entry.state['percent'], 25)
        await entry.progress_task

    def sequential_widget_ids(self):
        counter=0
        def response(endpoint,api_kwargs,**kwargs):
            nonlocal counter
            if endpoint=='sendWidget':
                counter+=1
                return {'message_id':counter}
            return True
        self.bot.do_api_request.side_effect=response

    async def test_search_does_not_wait_for_per_result_probes(self):
        self.sequential_widget_ids()
        self.source.text='Artist track'
        self.source.reply_text=AsyncMock()
        update=SimpleNamespace(message=self.source,effective_user=self.source.from_user)
        results=[{'id':f'v{i}','title':f'Track {i}'} for i in range(8)]
        with patch.object(main,'search_youtube',AsyncMock(return_value=results)), \
             patch.object(main,'probe_url',AsyncMock(side_effect=AssertionError('extra full probe'))), \
             patch.object(main,'url_media_types',AsyncMock(side_effect=AssertionError('per-result probe'))):
            await main.process_message(update,self.context)
        self.source.reply_text.assert_not_awaited()
        calls=[c for c in self.bot.do_api_request.await_args_list if c.kwargs['endpoint']=='sendWidget']
        self.assertEqual(len(calls),1)
        self.assertEqual(json.loads(calls[0].kwargs['api_kwargs']['chat_data'])['stage'],'processing')
        edit=self.bot.do_api_request.await_args.kwargs
        self.assertEqual(edit['endpoint'],'editMessageWidgetData')
        self.assertEqual(edit['api_kwargs']['message_id'],1)
        self.assertEqual(len(json.loads(edit['api_kwargs']['chat_data'])['rows']),8)
        self.bot.delete_message.assert_not_awaited()

    async def test_search_timeout_replaces_spinner_with_problem_card(self):
        self.sequential_widget_ids()
        self.source.text='Artist track';self.source.reply_text=AsyncMock()
        update=SimpleNamespace(message=self.source,effective_user=self.source.from_user)
        async def stalled(_):
            await asyncio.Event().wait()
        with patch.object(main,'search_youtube',side_effect=stalled), patch.object(main,'SEARCH_TIMEOUT',.01):
            await main.process_message(update,self.context)
        payload=self.bot.do_api_request.await_args.kwargs['api_kwargs']
        self.assertEqual(json.loads(payload['chat_data'])['error'],'search')
        self.assertEqual(payload['message_id'],1)
        self.bot.delete_message.assert_not_awaited()

    async def test_direct_link_reuses_single_probe(self):
        self.sequential_widget_ids()
        self.source.text='https://www.youtube.com/watch?v=example'
        self.source.reply_text=AsyncMock()
        update=SimpleNamespace(message=self.source,effective_user=self.source.from_user)
        probe=AsyncMock(return_value={'title':'Video','formats':[{'acodec':'aac','vcodec':'h264','ext':'mp4'}]})
        with patch.object(main,'probe_url',probe):
            await main.process_message(update,self.context)
        self.assertEqual(probe.await_count,1)
        self.source.reply_text.assert_not_awaited()

    async def test_artwork_does_not_block_card_or_revert_loading(self):
        gate=asyncio.Event()
        self.items[0]['thumbnail']='https://example.com/cover.jpg'
        async def fetch(*args,**kwargs):
            await gate.wait()
            return 'data:image/webp;base64,example'
        with patch('bubbles.service.thumbnail',side_effect=fetch):
            try:
                entry=await asyncio.wait_for(self.card(),timeout=.2)
                self.assertEqual(entry.covers,{})
                await entry.begin(0,'mp3')
            finally:
                gate.set()
            await asyncio.gather(*self.tasks)
        self.assertEqual(entry.state['view'],'progress')
        self.assertEqual(json.loads(entry.cover_data()),{'0':'data:image/webp;base64,example'})

    async def test_processing_selection_and_download_share_one_message(self):
        loader = await self.service.send_loading(self.context, self.source)
        initial = self.bot.do_api_request.await_args.kwargs['api_kwargs']
        self.assertEqual(json.loads(initial['chat_data'])['view'], 'processing')
        self.assertIn('class="spinner"', initial['html'])
        self.assertIn('@keyframes spin', initial['html'])
        card = await main.try_bubble(self.context, self.source, loader, self.items[:1], 'Track')
        self.assertEqual(card.message_id, loader.message_id)
        self.assertEqual(card.state['view'], 'format')
        await self.dispatch('dl|0|mp3')
        await asyncio.gather(*self.tasks)
        self.assertEqual(card.state['view'], 'progress')
        self.download.assert_awaited_once()
        self.assertIs(self.download.await_args.kwargs['msg'],card)
        sends = [c for c in self.bot.do_api_request.await_args_list if c.kwargs['endpoint']=='sendWidget']
        self.assertEqual(len(sends),1)
        edits = [c for c in self.bot.do_api_request.await_args_list if c.kwargs['endpoint']=='editMessageWidgetData']
        self.assertTrue(edits)
        self.assertTrue(all(c.kwargs['api_kwargs']['message_id']==loader.message_id for c in edits))
        self.bot.delete_message.assert_not_awaited()

    async def test_failed_processing_transition_uses_text_fallback(self):
        loader = await self.service.send_loading(self.context, self.source)
        self.bot.do_api_request.side_effect = RuntimeError('edit failed')
        card = await main.try_bubble(self.context, self.source, loader, self.items[:1])
        self.assertIsNone(card)
        await loader.edit_text('Choose format')
        self.bot.send_message.assert_awaited_once()
        self.bot.delete_message.assert_awaited_once()

    async def test_processing_widget_falls_back_to_one_editable_text_message(self):
        loader=await self.service.send_loading(self.context,self.source)
        fallback=SimpleNamespace(edit_text=AsyncMock(),delete=AsyncMock())
        self.bot.send_message.return_value=fallback
        await loader.edit_text('First')
        await loader.edit_text('Second')
        await loader.delete()
        self.bot.send_message.assert_awaited_once()
        self.bot.delete_message.assert_awaited_once()
        fallback.edit_text.assert_awaited_once_with('Second')
        fallback.delete.assert_awaited_once()

    async def test_caption_and_native_keyboard_are_absent(self):
        entry = await self.card()
        payload = self.bot.do_api_request.await_args.kwargs['api_kwargs']
        self.assertNotIn('caption', payload)
        self.assertNotIn('reply_markup', payload)
        self.assertEqual(json.loads(payload['public_data']), {})
        await entry.error('source')
        self.bot.edit_message_caption.assert_not_awaited()
        self.assertEqual(entry.state['problem'], strings('ru')['problems']['source'])
        self.assertLessEqual(len(state_json(entry.state).encode()), 1024)

    async def test_acknowledgement_failure_does_not_cancel_download(self):
        await self.card()
        answer = AsyncMock(side_effect=asyncio.TimeoutError())
        await self.service.dispatch(self.context,123,{'id':123,'type':'private'},42,'dl|0|mp3',answer)
        await asyncio.gather(*self.tasks)
        self.download.assert_awaited_once()
        self.assertFalse(self.service.busy_users)

    async def test_hung_acknowledgement_is_bounded(self):
        await self.card()
        async def hung(_):
            await asyncio.Event().wait()
        with patch('bubbles.service.ACK_TIMEOUT', 0.01):
            await self.service.dispatch(self.context,123,{'id':123,'type':'private'},42,'dl|0|mp3',hung)
        await asyncio.gather(*self.tasks)
        self.download.assert_awaited_once()

    async def test_thumbnail_public_state_has_a_separate_budget(self):
        from PIL import Image
        from bubbles.thumbnails import encode_image
        import io
        image = Image.new('RGB', (160,90), (20,100,200))
        buffer = io.BytesIO();image.save(buffer,format='PNG')
        cover = encode_image(buffer.getvalue())
        self.assertTrue(cover.startswith('data:image/webp;base64,'))
        entry = await self.card('search')
        from bubbles.thumbnails import search_artwork
        atlas, heroes = search_artwork([buffer.getvalue()]*8)
        entry.cover_atlas=atlas;entry.covers.update(heroes)
        self.assertLessEqual(len(entry.cover_data().encode()),1024)
        self.assertNotIn('thumbnail',state_json(entry.state))
        self.assertEqual(set(json.loads(entry.cover_data())),{'atlas'})
        self.assertEqual(len(heroes),8)

    async def test_delete_failure_does_not_restart_delivery(self):
        entry=await self.card();await entry.begin(0,'mp3')
        self.bot.delete_message.side_effect=RuntimeError('temporary delete error')
        with patch('bubbles.service.asyncio.sleep', AsyncMock()):
            await entry.complete()
        self.assertEqual(entry.state['stage'],'done')
        self.assertNotIn('error',entry.state)
        self.send.assert_not_awaited()
        self.assertEqual(self.bot.delete_message.await_count,2)

    def test_configured_deno_is_used_without_changing_other_options(self):
        with patch.dict(os.environ, {'YTDLP_DENO':'/app/deno'}):
            opts=main.ydl_options({'format':'bestaudio'})
        self.assertEqual(opts['js_runtimes'],{'deno':{'path':'/app/deno'}})
        self.assertEqual(opts['format'],'bestaudio')

    async def test_ambiguous_media_delivery_is_not_retried(self):
        from bubbles.delivery import send_media, AmbiguousDelivery
        from telegram.error import TimedOut
        send = AsyncMock(side_effect=TimedOut())
        with self.assertRaises(AmbiguousDelivery):
            await send_media(send)
        self.assertEqual(send.await_count, 1)

    async def test_definite_rate_limit_can_be_retried(self):
        from bubbles.delivery import send_media
        from telegram.error import RetryAfter
        send = AsyncMock(side_effect=[RetryAfter(0), 'sent'])
        self.assertEqual(await send_media(send), 'sent')
        self.assertEqual(send.await_count, 2)

    async def test_uncertain_resend_warns_instead_of_uploading_again(self):
        from bubbles.delivery import AmbiguousDelivery
        entry = await self.card();await entry.begin(0, 'mp3')
        with tempfile.NamedTemporaryFile(suffix='.mp3',delete=False) as file:
            file.write(b'audio'); filename=file.name
        await entry.send_failed(filename, {'title':'Track'}, kind='uncertain')
        self.assertEqual(entry.state['error'], 'uncertain')
        self.send.side_effect=AmbiguousDelivery('unknown result')
        await self.dispatch('resend');await asyncio.gather(*self.tasks)
        self.assertEqual(entry.state['error'], 'uncertain')
        self.assertEqual(self.send.await_count,1)
        self.assertTrue(Path(filename).exists())

    async def test_completed_card_is_removed_once_without_summary(self):
        entry=await self.card();await entry.begin(0,'mp3')
        with tempfile.NamedTemporaryFile(suffix='.m4a') as file:
            file.write(b'audio');file.flush()
            entry.media_ready(file.name,{'title':'Actual title','uploader':'Artist','duration':245})
            self.assertEqual(entry.state['duration'],'4:05')
            self.assertEqual(entry.state['format'],'m4a')
            self.bot.do_api_request.reset_mock()
            await entry.complete();await entry.delete()
        self.assertEqual(entry.state['stage'],'done')
        self.assertTrue(entry.deleted)
        self.bot.delete_message.assert_awaited_once()
        self.bot.do_api_request.assert_not_awaited()
        await self.dispatch('dl|0|mp3')
        self.download.assert_not_awaited()

    def test_thumbnails_reject_private_and_unsupported_urls(self):
        from bubbles.thumbnails import validate_image_url
        for url in ('http://example.com/x','https://127.0.0.1/x','https://[::1]/x',
                    'https://user:password@example.com/x','https://localhost/x',
                    'https://169.254.169.254/x','https://example.com:8443/x'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_image_url(url)

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

    async def test_all_eight_results_fit_in_one_state(self):
        self.items=[item('https://example.com/'+str(i),{'title':'😀'*200,'uploader':'Я'*200,'duration':999999}) for i in range(8)]
        entry=await self.card('search')
        self.assertIsNotNone(entry)
        self.assertLessEqual(len(state_json(entry.state).encode()),1024)
        self.assertEqual([row[0] for row in entry.state['rows']],list(range(8)))
        self.assertEqual(entry.state['total'],8)
        self.assertNotIn('https://',state_json(entry.state))
        self.assertNotIn('page',entry.state)

    async def test_search_preserves_full_titles_when_budget_allows(self):
        self.items[0]['title']='A readable full recording title '+('x'*70)
        entry=await self.card('search')
        self.assertEqual(entry.state['rows'][0][1],self.items[0]['title'])

    async def test_loading_keeps_selected_recording_cover(self):
        entry=await self.card('search')
        entry.covers[6]='data:image/webp;base64,example'
        await entry.begin(6,'mp3')
        self.assertEqual(json.loads(entry.cover_data()),{'6':entry.covers[6]})
        self.assertEqual(entry.state['cover_index'],6)
        self.assertIn('data-cover', (Path('bubbles/designs/default/progress.html')).read_text())

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
