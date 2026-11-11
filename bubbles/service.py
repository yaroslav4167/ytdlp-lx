"""Widget transport, private-chat ownership, jobs and bounded send retries.

No design markup lives here. Templates and locales are read for every new card.
"""
import asyncio
from collections import OrderedDict
import json
import logging
import math
import os
import re
from pathlib import Path
import time
import zlib

ROOT = Path(__file__).parent
LOG = logging.getLogger(__name__)
RETAINED_FILES = set()
CARD_TTL = 1800
FILE_TTL = 600
MAX_CARDS = 512


def locale(language=None):
    code = str(language or 'en').lower().replace('_', '-').split('-')[0]
    if not code.isalpha() or not (ROOT / 'locales' / f'{code}.json').is_file():
        code = 'en'
    return code


def strings(language=None):
    return json.loads((ROOT / 'locales' / f'{locale(language)}.json').read_text())


def user_language(user, preferences=None):
    return locale((preferences or {}).get('language') or getattr(user, 'language_code', None))


def shorten(value, size):
    return str(value or '').encode('utf-8')[:size].decode('utf-8', errors='ignore')


def state_json(state):
    result = json.dumps(state, ensure_ascii=False, separators=(',', ':'))
    if len(result.encode()) > 1024:
        raise ValueError('Widget state exceeds 1024 bytes')
    return result


def load_design(language):
    name = os.environ.get('BUBBLES_DESIGN', 'default')
    if not name.replace('-', '').replace('_', '').isalnum():
        raise ValueError('Invalid BUBBLES_DESIGN')
    folder = ROOT / 'designs' / name
    source = (folder / 'bubble.html').read_text()
    for key, filename in [('STYLES', 'styles.css'), ('FORMAT', 'format.html'),
                          ('SEARCH', 'search.html'), ('PROGRESS', 'progress.html'),
                          ('ERROR', 'error.html'), ('SCRIPT', 'widget.js')]:
        source = source.replace(f'__{key}__', (folder / filename).read_text())
    keys = set(re.findall(r'\bI\.([a-z_]+)', source) + re.findall(r'data-i18n="([a-z_]+)"', source))
    translations = {k: v for k, v in strings(language).items() if k in keys}
    source = source.replace('__I18N__', json.dumps(translations, ensure_ascii=False,
                                                  separators=(',', ':')).replace('<', '\\u003c'))
    source = source.replace('lang="ru"', f'lang="{locale(language)}"')
    raw = source.encode()
    if len(raw) > 32768 or len(zlib.compress(raw, 9)) > 4096:
        raise ValueError(f'Widget HTML exceeds Telegram size limits: {len(raw)} raw / {len(zlib.compress(raw, 9))} compressed')
    return source


def duration(value):
    try:
        seconds = max(0, int(value or 0))
        return f'{seconds // 60}:{seconds % 60:02d}' if seconds else ''
    except (TypeError, ValueError):
        return ''


def item(url, info=None, audio=True, video=True):
    info = info or {}
    return dict(url=url, title=shorten(info.get('title'), 160),
                artist=shorten(info.get('uploader') or info.get('artist'), 80),
                duration=duration(info.get('duration')), audio=audio, video=video)


class Bubble:
    is_bubble = True

    def __init__(self, service, context, source, language, items, title, view):
        self.service, self.context, self.source = service, context, source
        self.language, self.words = language, strings(language)
        self.chat_id, self.owner = source.chat_id, source.from_user.id
        self.message_id = None
        self.items, self.selected, self.format = items, 0, 'mp3'
        self.state = {'view': view, 'title': shorten(title, 120)}
        if view == 'search':
            self.state.update(page=0, total=len(items))
            self.search_page(0)
        elif items:
            self.state.update({k: v for k, v in items[0].items() if k != 'url'})
            self.state['title'] = items[0]['title'] or shorten(title, 120) or self.words['recording']
        self.created = time.monotonic()
        self.filename = None
        self.info = {}
        self.file_deadline = 0
        self.file_timer = None
        self.busy = False
        self.last_progress = 0
        self.lock = asyncio.Lock()
        self.published = None

    def search_page(self, page):
        self.state['page'] = page
        self.state['rows'] = [dict(i=i, t=shorten(v['title'], 90),
                                  a=shorten(v['artist'], 40), d=v['duration'],
                                  audio=v['audio'], video=v['video'])
                              for i, v in enumerate(self.items) if page*3 <= i < page*3+3]

    def caption(self):
        if self.state.get('error'):
            return self.words['problems'][self.state['error']][2] + '\n' + self.words['problems'][self.state['error']][3]
        key = 'caption_' + (self.state.get('stage', 'downloading') if self.state['view'] == 'progress'
                            else self.state['view'])
        return self.state.get('title', '') + '\n' + self.words[key].format(total=len(self.items))

    def keyboard(self):
        def button(text, action):
            return {'text': text, 'callback_data': 'bubble|' + action}
        error = self.state.get('error')
        if error:
            problem = self.words['problems'][error]
            rows = [[button(problem[4], problem[5])]]
            if error in ('source', 'send'):
                rows.append([button(self.words['other_link'], 'input')])
        elif self.state['view'] == 'progress':
            rows = []
        else:
            rows = []
            indices = range(self.state.get('page', 0)*3, min(self.state.get('page', 0)*3+3, len(self.items))) if self.state['view'] == 'search' else range(min(1, len(self.items)))
            for i in indices:
                media = self.items[i]
                prefix = f'{i+1}. ' if self.state['view'] == 'search' else ''
                row = [button(prefix+self.words[label], f'dl|{i}|{fmt}') for label, fmt in [('audio','mp3'),('video','mp4')] if media[label]]
                rows.append(row)
            if self.state['view'] == 'search':
                page = self.state['page']
                nav = []
                if page:
                    nav.append(button('←', f'page|{page-1}'))
                if (page+1)*3 < len(self.items):
                    nav.append(button('→', f'page|{page+1}'))
                if nav:
                    rows.append(nav)
        return {'inline_keyboard': rows}

    async def publish(self):
        """Serialize updates so late progress cannot overwrite terminal states."""
        async with self.lock:
            serialized = state_json(self.state)
            if serialized == self.published:
                return
            try:
                await self.service.api(self.context.bot, 'editMessageWidgetData',
                                       chat_id=self.chat_id, message_id=self.message_id,
                                       chat_data=serialized)
                self.published = serialized
                caption = self.caption()
                if getattr(self, 'last_caption', None) != caption:
                    await self.context.bot.edit_message_caption(chat_id=self.chat_id,
                        message_id=self.message_id, caption=caption, reply_markup=self.keyboard())
                    self.last_caption = caption
            except Exception as error:
                if 'message is not modified' not in str(error).lower():
                    LOG.warning('Widget update failed (%s)', type(error).__name__)

    async def error(self, kind):
        self.state['error'] = kind
        await self.publish()

    async def begin(self, index, fmt):
        self.selected, self.format = index, fmt
        self.created = time.monotonic()
        media = self.items[index]
        self.state = {'view': 'progress', 'title': media['title'] or self.words['recording'],
                      'artist': media['artist'], 'duration': media['duration'],
                      'format': fmt, 'stage': 'downloading', 'percent': 0}
        self.last_progress = 0
        await self.publish()

    async def update_progress(self, data):
        if self.state.get('error') or self.state.get('stage') in ('sending', 'done'):
            return
        now = time.monotonic()
        if now-self.last_progress < 1 and data.get('status') in ('downloading', 'converting'):
            return
        self.last_progress = now
        stage = data.get('status')
        if stage == 'error':
            await self.error('source')
            return
        if stage == 'finished':
            self.state.update(stage='converting', percent=0, eta='', speed='')
        elif stage == 'converting':
            self.state.update(stage='converting', percent=round(data.get('percent', 0)), eta='', speed='')
        elif stage == 'downloading':
            total = data.get('total_bytes') or data.get('total_bytes_estimate') or 0
            downloaded = data.get('downloaded_bytes') or 0
            percent = min(100, max(0, downloaded/total*100)) if total else 0
            if not math.isfinite(percent):
                percent = 0
            speed = data.get('speed') or 0
            eta = data.get('eta')
            self.state.update(stage='downloading', percent=round(percent),
                size=f'{downloaded/1048576:.1f} / {total/1048576:.1f} MiB' if total else '',
                speed=f'{speed/1048576:.1f} MiB/s' if speed else '',
                eta=self.words['eta'].format(seconds=max(0, int(eta))) if eta else '')
        await self.publish()

    async def edit_text(self, text, **kwargs):
        if text.startswith(('Download error:', 'Could not prepare')) or text in (self.words['source_failed'], self.words['prepare_failed']):
            await self.error('source')
        elif 'Sending file' in text or text == self.words['sending_plain']:
            self.state.pop('error', None)
            self.state.update(stage='sending', percent=100, eta='', speed='')
            await self.publish()
        return self

    async def delete(self):
        # Keep completed widgets visible; send_audio/video remains a native file.
        return None

    def forget_file(self, remove=False):
        if self.file_timer:
            self.file_timer.cancel()
            self.file_timer = None
        if self.filename:
            RETAINED_FILES.discard(self.filename)
            if remove:
                Path(self.filename).unlink(missing_ok=True)
        self.filename = None
        self.info = {}

    async def send_failed(self, filename, info):
        self.filename = filename
        self.info = {key: info.get(key) for key in ('title', 'uploader')}
        self.created = time.monotonic()
        self.file_deadline = self.created + FILE_TTL
        RETAINED_FILES.add(filename)
        if self.file_timer:
            self.file_timer.cancel()
        self.file_timer = asyncio.get_running_loop().call_later(FILE_TTL, self.expire_file)
        await self.error('send')

    def expire_file(self):
        if self.busy:
            self.file_timer = asyncio.get_running_loop().call_later(30, self.expire_file)
            return
        self.forget_file(remove=True)
        self.state['error'] = 'expired'

    async def complete(self):
        self.forget_file()
        self.state.pop('error', None)
        self.state.update(stage='done', percent=100, eta='', speed='')
        await self.publish()


class BubbleService:
    def __init__(self, download, send_file):
        self.download, self.send_file = download, send_file
        self.entries = OrderedDict()
        self.busy_users = set()

    @staticmethod
    async def api(bot, method, **kwargs):
        return await bot.do_api_request(endpoint=method, api_kwargs=kwargs)

    def enabled(self, source, context):
        default = os.environ.get('BUBBLES_ENABLED', '1').lower() not in ('0', 'false', 'off')
        return (getattr(getattr(source, 'chat', None), 'type', None) == 'private'
                and context.user_data.get('bubbles_enabled', default))

    def prune(self):
        now = time.monotonic()
        for key, entry in list(self.entries.items()):
            if not entry.busy and (now-entry.created > CARD_TTL or len(self.entries) >= MAX_CARDS):
                entry.forget_file(remove=True)
                del self.entries[key]

    async def send(self, context, source, items, title='', view='format', error=None):
        if not self.enabled(source, context):
            return None
        self.prune()
        if len(self.entries) >= MAX_CARDS:
            return None
        owned = [k for k, v in self.entries.items() if v.owner == source.from_user.id and not v.busy and not v.filename]
        if len(owned) >= 32:
            del self.entries[owned[0]]
        language = user_language(source.from_user, context.user_data)
        entry = Bubble(self, context, source, language, items, title, view)
        if error:
            entry.state['error'] = error
        try:
            payload = dict(chat_id=source.chat_id, html=load_design(language),
                           chat_data=state_json(entry.state), caption=entry.caption(),
                           allow_queries=True, reply_markup=entry.keyboard())
            if getattr(source, 'message_thread_id', None):
                payload['message_thread_id'] = source.message_thread_id
            result = await self.api(context.bot, 'sendWidget', **payload)
            entry.message_id = result['message_id']
            entry.published = payload['chat_data']
            entry.last_caption = payload['caption']
        except Exception as exception:
            LOG.warning('Widget unavailable; using plain UI (%s)', type(exception).__name__)
            return None
        self.entries[(entry.chat_id, entry.message_id)] = entry
        return entry

    async def handle_widget(self, update, context):
        query = getattr(update, 'widget_query', None)
        query = query.to_dict() if query is not None else update.api_kwargs.get('widget_query')
        if not query:
            return
        async def answer(text):
            await self.api(context.bot, 'answerWidgetQuery', widget_query_id=query['id'], data=text)
        await self.dispatch(context, query.get('from', {}).get('id'), query.get('chat', {}),
                            query.get('message_id'), query.get('data', ''), answer, query.get('from', {}).get('language_code'))

    async def handle_callback(self, update, context):
        query = update.callback_query
        async def answer(text):
            await query.answer(text[:180] or None)
        await self.dispatch(context, query.from_user.id, query.message.chat.to_dict(),
                            query.message.message_id, query.data.removeprefix('bubble|'), answer, query.from_user.language_code)

    async def dispatch(self, context, owner, chat, message_id, data, answer, language=None):
        words = strings(language)
        if chat.get('type') != 'private' or owner != chat.get('id') or not message_id:
            await answer(words['unavailable'])
            return
        self.prune()
        entry = self.entries.get((owner, message_id))
        preferences = context.application.user_data.get(owner, {})
        language = entry.language if entry else user_language(type('User', (), {'language_code': language})(), preferences)
        words = entry.words if entry else strings(language)
        if not isinstance(data, str) or len(data.encode()) > 64:
            await answer(words['unavailable']); return
        if data == 'input':
            await answer(words['input_prompt'])
            from telegram import ForceReply
            await context.bot.send_message(chat_id=owner, text=words['input_prompt'],
                reply_markup=ForceReply(selective=True, input_field_placeholder=words['input_prompt'][:64]))
            return
        if not entry:
            await answer(words['expired_hint'])
            try:
                await self.api(context.bot, 'editMessageWidgetData', chat_id=owner,
                               message_id=message_id, chat_data=state_json({'error':'expired'}))
                await context.bot.edit_message_caption(chat_id=owner, message_id=message_id,
                    caption=words['expired_hint'], reply_markup={'inline_keyboard': [[{
                        'text':words['problems']['expired'][4], 'callback_data':'bubble|input'}]]})
            except Exception:
                pass
            return
        if owner in self.busy_users:
            await answer(words['busy']); return
        parts = data.split('|')
        if len(parts) == 2 and parts[0] == 'page' and parts[1].isascii() and parts[1].isdigit() and entry.state.get('view') == 'search' and not entry.state.get('error'):
            page = int(parts[1])
            if page*3 >= len(entry.items):
                await answer(words['unavailable']); return
            await answer('')
            entry.search_page(page)
            await entry.publish()
            return
        if data == 'resend':
            if entry.state.get('error') != 'send' or not entry.filename or time.monotonic() >= entry.file_deadline or not Path(entry.filename).is_file():
                entry.forget_file(remove=True)
                await answer(words['expired_hint']); await entry.error('expired'); return
            action = 'resend'
        else:
            if data == 'retry' and entry.state.get('error') == 'source':
                index, fmt = entry.selected, entry.format
            elif len(parts) == 3 and parts[0] == 'dl' and parts[1].isascii() and parts[1].isdigit() and entry.state.get('view') in ('format', 'search') and not entry.state.get('error'):
                index, fmt = int(parts[1]), parts[2]
            else:
                await answer(words['unavailable']); return
            if index >= len(entry.items) or fmt not in ('mp3','mp4') or not entry.items[index]['audio' if fmt == 'mp3' else 'video']:
                await answer(words['unavailable']); return
            entry.selected, entry.format = index, fmt
            action = 'download'
        entry.busy = True
        self.busy_users.add(owner)
        try:
            await answer(words['resending' if action == 'resend' else 'accepted'])
        except Exception:
            entry.busy = False
            self.busy_users.discard(owner)
            raise
        context.application.create_task(self.run(entry, action))

    async def run(self, entry, action):
        try:
            if action == 'resend':
                await entry.edit_text('📤 Sending file...')
                try:
                    await self.send_file(entry.context, entry.chat_id, entry.filename, entry.info)
                except Exception:
                    await entry.error('send')
                else:
                    filename = entry.filename
                    await entry.complete()
                    Path(filename).unlink(missing_ok=True)
            else:
                await entry.begin(entry.selected, entry.format)
                await self.download(entry.context, entry.source, entry.items[entry.selected]['url'],
                                    entry.format, msg=entry)
        except Exception as exception:
            LOG.warning('Widget job failed (%s)', type(exception).__name__)
            await entry.error('send' if entry.filename else 'source')
        finally:
            entry.busy = False
            self.busy_users.discard(entry.owner)
