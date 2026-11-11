"""Widget transport, private-chat ownership, jobs and bounded send retries.

No design markup lives here. Templates and locales are read for every new card.
"""
import asyncio
from collections import OrderedDict
from .thumbnails import thumbnail, search_thumbnails
from .delivery import AmbiguousDelivery
import json
import html
import hashlib
import logging
import math
import os
import re
from pathlib import Path
import time
import zlib
import unicodedata

ROOT = Path(__file__).parent
LOG = logging.getLogger(__name__)
RETAINED_FILES = set()
CARD_TTL = 1800
FILE_TTL = 600
MAX_CARDS = 512
ACK_TIMEOUT = 4


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
        fragment = (folder / filename).read_text()
        if key == 'SCRIPT':
            try:
                build = json.loads((folder / 'widget.build.json').read_text())
                compact = (folder / 'widget.min.js').read_bytes()
                if (build['source_sha256'] == hashlib.sha256((folder / filename).read_bytes()).hexdigest()
                        and build['output_sha256'] == hashlib.sha256(compact).hexdigest()):
                    fragment = compact.decode()
            except (OSError, ValueError, KeyError):
                pass
        source = source.replace(f'__{key}__', fragment)
    keys = set(re.findall(r'\bI\.([a-z_]+)', source) + re.findall(r'data-i18n="([a-z_]+)"', source))
    translations = {k: v for k, v in strings(language).items() if k in keys}
    source = source.replace('__I18N__', json.dumps(translations, ensure_ascii=True,
                                                  separators=(',', ':')).replace('<', '\\u003c'))
    source = source.replace('lang="ru"', f'lang="{locale(language)}"')
    raw = source.encode()
    if len(raw) > 32768 or len(zlib.compress(raw, 9)) > 4096:
        raise ValueError(f'Widget HTML exceeds Telegram size limits: {len(raw)} raw / {len(zlib.compress(raw, 9))} compressed')
    return source


def load_loading_design(language):
    name = os.environ.get('BUBBLES_DESIGN', 'default')
    if not name.replace('-', '').replace('_', '').isalnum():
        raise ValueError('Invalid BUBBLES_DESIGN')
    source = (ROOT / 'designs' / name / 'loading.html').read_text()
    words = strings(language)
    for key, value in [('LANG',locale(language)), ('TITLE',words['processing_title']), ('WAITING',words['waiting'])]:
        source = source.replace('__'+key+'__',html.escape(value))
    raw = source.encode()
    if len(raw)>32768 or len(zlib.compress(raw,9))>4096:
        raise ValueError('Loading widget exceeds Telegram size limits')
    return source


class ProcessingMessage:
    """Animated placeholder that can fall back to the existing text-message interface."""
    def __init__(self, service, context, source, message_id):
        self.service, self.context, self.source = service, context, source
        self.chat_id, self.message_id = source.chat_id, message_id
        self.loader_deleted = False
        self.fallback = None

    async def delete_loader(self):
        if not self.loader_deleted:
            try:
                await self.context.bot.delete_message(chat_id=self.chat_id,message_id=self.message_id,
                    connect_timeout=3,read_timeout=4,write_timeout=4,pool_timeout=1)
                self.loader_deleted = True
            except Exception as exception:
                if 'message to delete not found' in str(exception).lower():
                    self.loader_deleted = True
                else:
                    LOG.warning('Could not remove processing widget (%s)',type(exception).__name__)

    async def edit_text(self, text, **kwargs):
        if self.fallback is not None:
            return await self.fallback.edit_text(text,**kwargs)
        if getattr(self.source,'message_thread_id',None):
            kwargs['message_thread_id'] = self.source.message_thread_id
        self.fallback = await self.context.bot.send_message(chat_id=self.chat_id,text=text,**kwargs)
        await self.delete_loader()
        return self.fallback

    async def delete(self):
        await self.delete_loader()
        if self.fallback is not None:
            await self.fallback.delete()
            self.fallback = None


def duration(value):
    try:
        seconds = max(0, int(value or 0))
        return f'{seconds // 60}:{seconds % 60:02d}' if seconds else ''
    except (TypeError, ValueError):
        return ''


def item(url, info=None, audio=True, video=True):
    info = info or {}
    thumbnails = info.get('thumbnails') or []
    cover = info.get('thumbnail') or (thumbnails[-1].get('url') if thumbnails else '')
    if not cover and re.fullmatch(r'[A-Za-z0-9_-]{11}', str(info.get('id') or '')) and 'youtube.com/' in url:
        cover = f"https://i.ytimg.com/vi/{info['id']}/default.jpg"
    return dict(url=url, thumbnail=cover, title=shorten(info.get('title'), 160),
                full_title=shorten(info.get('title'), 800),
                artist=shorten(info.get('uploader') or info.get('artist'), 80),
                duration=duration(info.get('duration')), audio=audio, video=video)


def search_rows(items):
    return [[i,v.get('full_title') or v['title'],v['artist'],v['duration'],
             int(v['audio']) | (int(v['video']) << 1)] for i,v in enumerate(items[:8])]


def search_groups(items, title):
    """Keep complete titles; split only when state bytes or estimated height require it."""
    groups, current, height = [], [], 0
    for media in items[:8]:
        label = media.get('full_title') or media['title']
        units = sum(2 if unicodedata.east_asian_width(c) in ('W','F') else 1 for c in label)
        row_height = max(46,26+17*math.ceil(max(1,units)/17))
        candidate = current+[media]
        state = {'view':'search','title':shorten(title,64),'total':len(candidate),
                 'range':'1–8 / 8','rows':search_rows(candidate)}
        size = len(json.dumps(state,ensure_ascii=False,separators=(',',':')).encode())
        if current and (size>1024 or height+row_height>400):
            groups.append(current);current=[];height=0
        current.append(media);height+=row_height
    if current:groups.append(current)
    return groups


class Bubble:
    is_bubble = True

    def __init__(self, service, context, source, language, items, title, view, range_label=None):
        self.service, self.context, self.source = service, context, source
        self.language, self.words = language, strings(language)
        self.chat_id, self.owner = source.chat_id, source.from_user.id
        self.message_id = None
        self.items, self.selected, self.format = items, 0, 'mp3'
        self.state = {'view': view, 'title': shorten(title, 120)}
        if range_label:
            self.state['range']=range_label
        if view == 'search':
            self.state.update(total=len(items))
            self.search_results()
        elif items:
            self.state.update({k: v for k, v in items[0].items() if k in ('title', 'artist', 'duration', 'audio', 'video')})
            self.state['title'] = items[0]['title'] or shorten(title, 120) or self.words['recording']
        self.created = time.monotonic()
        self.filename = None
        self.info = {}
        self.file_deadline = 0
        self.file_timer = None
        self.busy = False
        self.last_progress = 0
        self.progress_task = None
        self.progress_events_lock = asyncio.Lock()
        self.lock = asyncio.Lock()
        self.published = None
        self.covers = {}
        self.cover_atlas = None
        self.summary = {}
        self.deleted = False
        self.published_covers = None

    def search_results(self):
        self.state['title']=shorten(self.state.get('title'),64)
        self.state['rows']=search_rows(self.items)
        # Only secondary artist labels may shrink; recording titles stay complete.
        while len(json.dumps(self.state,ensure_ascii=False,separators=(',',':')).encode())>1024:
            row=max(self.state['rows'],key=lambda r:len(r[2].encode()))
            if row[2]:row[2]=shorten(row[2],max(0,len(row[2].encode())-8))
            elif self.state['title']:self.state['title']=''
            else:raise ValueError('Search title exceeds widget state limit')

    def cover_indices(self):
        return [self.selected] if self.items else []

    async def prepare_covers(self):
        if self.state['view'] == 'search' and not self.state.get('error'):
            self.cover_atlas, covers = await search_thumbnails([v.get('thumbnail') for v in self.items])
            self.covers.update(covers)
            return
        indices = [i for i in self.cover_indices() if i not in self.covers]
        values = await asyncio.gather(*(thumbnail(self.items[i].get('thumbnail'),
            hero=self.state['view'] != 'search') for i in indices))
        self.covers.update(zip(indices, values))

    def cover_data(self):
        if self.state['view'] == 'search':
            return state_json({'atlas':self.cover_atlas} if self.cover_atlas else {})
        return state_json({str(i):self.covers[i] for i in self.cover_indices() if self.covers.get(i)})

    async def publish(self):
        """Serialize updates so late progress cannot overwrite terminal states."""
        async with self.lock:
            serialized = state_json(self.state)
            covers = self.cover_data()
            if serialized == self.published and covers == self.published_covers:
                return
            try:
                await self.service.api(self.context.bot, 'editMessageWidgetData',
                                       chat_id=self.chat_id, message_id=self.message_id,
                                       chat_data=serialized, public_data=covers)
                self.published = serialized
                self.published_covers = covers
                # Older cards may still carry a caption/keyboard from the first rollout.
                if not getattr(self, 'clean_chrome', False):
                    await self.context.bot.edit_message_caption(chat_id=self.chat_id,
                        message_id=self.message_id, caption='', reply_markup={'inline_keyboard': []},
                        connect_timeout=3, read_timeout=4, write_timeout=4, pool_timeout=1)
                    self.clean_chrome = True
            except Exception as error:
                if 'message is not modified' not in str(error).lower():
                    LOG.warning('Widget update failed (%s)', type(error).__name__)

    def problem_state(self, kind):
        self.state = {'view':self.state['view'], 'title':self.state.get('title', ''),
                      'error':kind, 'problem':self.words['problems'][kind]}

    async def error(self, kind):
        self.problem_state(kind)
        await self.publish()

    async def begin(self, index, fmt):
        self.selected, self.format = index, fmt
        self.created = time.monotonic()
        media = self.items[index]
        self.state = {'view': 'progress', 'title': media['title'] or self.words['recording'],
                      'artist': media['artist'], 'duration': media['duration'],
                      'format': fmt, 'stage': 'downloading', 'stage_title': self.words['waiting_source'],
                      'percent': None, 'cover_index':index, 'detail':self.words['progress_details']['metadata']}
        self.last_progress = 0
        await self.publish()

    async def update_progress(self, data):
        async with self.progress_events_lock:
            await self.apply_progress(data)

    async def apply_progress(self, data):
        if self.state.get('error') or self.state.get('stage') in ('sending', 'done'):
            return
        stage = data.get('status')
        if stage == 'error':
            await self.error('source')
            return
        if stage == 'preparing':
            self.state.update(detail=self.words['progress_details'].get(data.get('detail'),
                              self.words['waiting_source']), percent=None, size='', speed='', eta='')
        elif stage == 'converting':
            if self.state.get('stage') == 'downloading':
                # Flush the real final byte count before replacing a short download
                # with conversion. Slow Telegram edits must not erase this phase.
                await self.publish()
            self.state.pop('stage_title', None)
            percent = data.get('percent')
            self.state.update(stage='converting', percent=round(percent, 1) if percent is not None else None,
                              eta='', speed='', size='', part='', detail=self.words['progress_details']['convert'])
        elif stage in ('downloading', 'finished'):
            self.state.pop('stage_title', None)
            # Estimates change between HLS fragments and can report 100% too early.
            total = data.get('total_bytes') or 0
            downloaded = data.get('downloaded_bytes') or 0
            percent = min(100, max(0, downloaded/total*100)) if total else 0
            if not math.isfinite(percent):
                percent = 0
            speed = data.get('speed') or 0
            eta = data.get('eta')
            detail = 'finished' if stage == 'finished' else data.get('part') or 'download'
            self.state.update(stage='downloading', percent=round(percent, 1) if total else None,
                detail=self.words['progress_details'].get(detail,self.words['progress_details']['download']),
                part=self.words.get(data.get('part'), '') if data.get('part') else '',
                size=f'{downloaded/1048576:.1f} / {total/1048576:.1f} MiB' if total else f'{downloaded/1048576:.1f} MiB',
                speed=f'{speed/1048576:.1f} MiB/s' if speed else '',
                eta=self.words['eta'].format(seconds=max(0, int(eta))) if eta else '')
        # Keep every latest sample, but only one publisher in flight.
        # In particular, never drop the final sample inside a throttle window.
        if not self.progress_task or self.progress_task.done():
            self.progress_task = asyncio.create_task(self.publish_progress())

    async def publish_progress(self):
        started = time.monotonic()
        while not self.deleted and not self.state.get('error') and self.state.get('stage') not in ('sending', 'done'):
            await asyncio.sleep(max(0, .5 - (time.monotonic() - self.last_progress)))
            if self.deleted or self.state.get('error') or self.state.get('stage') in ('sending', 'done'):
                return
            before = state_json(self.state)
            self.last_progress = time.monotonic()
            await self.publish()
            await asyncio.sleep(.5)
            if state_json(self.state) == before and self.published == before:
                return
            # Retry failed idempotent state edits, always using the newest state.
            # Stop after a bounded window if Telegram remains unavailable.
            if time.monotonic() - started > 20:
                return

    async def edit_text(self, text, **kwargs):
        if text.startswith(('Download error:', 'Could not prepare')) or text in (self.words['source_failed'], self.words['prepare_failed']):
            await self.error('source')
        elif 'Sending file' in text or text == self.words['sending_plain']:
            self.state.pop('error', None)
            self.state.pop('stage_title', None)
            self.state.update(stage='sending', percent=None, eta='', speed='', size='', part='',
                              detail=self.words['progress_details']['sending'])
            await self.publish()
        return self

    async def delete(self):
        if self.deleted:
            return
        for attempt in range(2):
            try:
                await self.context.bot.delete_message(chat_id=self.chat_id, message_id=self.message_id,
                    connect_timeout=3, read_timeout=4, write_timeout=4, pool_timeout=1)
                self.deleted = True
                return
            except Exception as exception:
                if 'message to delete not found' in str(exception).lower():
                    self.deleted = True
                    return
                if attempt == 0:
                    await asyncio.sleep(1)
                else:
                    LOG.warning('Could not remove completed widget (%s)', type(exception).__name__)

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

    async def send_failed(self, filename, info, kind='send'):
        self.media_ready(filename, info)
        self.filename = filename
        self.info = {key: info.get(key) for key in ('title', 'uploader')}
        self.created = time.monotonic()
        self.file_deadline = self.created + FILE_TTL
        RETAINED_FILES.add(filename)
        if self.file_timer:
            self.file_timer.cancel()
        self.file_timer = asyncio.get_running_loop().call_later(FILE_TTL, self.expire_file)
        await self.error(kind)

    def expire_file(self):
        if self.busy:
            self.file_timer = asyncio.get_running_loop().call_later(30, self.expire_file)
            return
        self.forget_file(remove=True)
        self.problem_state('expired')

    def media_ready(self, filename, info):
        media = self.items[self.selected]
        updated = item(media['url'], info, media['audio'], media['video'])
        self.items[self.selected] = {key:updated.get(key) or value for key,value in media.items()}
        self.summary = {
            'title':updated['title'] or media['title'] or self.words['recording'],
            'artist':updated['artist'] or media['artist'],
            'duration':updated['duration'] or media['duration'],
            'format':Path(filename).suffix.lstrip('.'),
            'size':f'{Path(filename).stat().st_size/1048576:.1f} MiB',
        }
        self.state.update(self.summary)

    async def complete(self):
        self.forget_file()
        self.state = {'view':'progress', 'stage':'done'}
        await self.delete()
        self.covers.clear()
        self.cover_atlas = None


class BubbleService:
    def __init__(self, download, send_file):
        self.download, self.send_file = download, send_file
        self.entries = OrderedDict()
        self.busy_users = set()

    @staticmethod
    async def api(bot, method, **kwargs):
        return await asyncio.wait_for(bot.do_api_request(endpoint=method, api_kwargs=kwargs,
            connect_timeout=3, read_timeout=4, write_timeout=4, pool_timeout=1), timeout=6)

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

    async def send_loading(self, context, source):
        if not self.enabled(source,context):
            return None
        try:
            language = user_language(source.from_user,context.user_data)
            words = strings(language)
            payload = {'chat_id':source.chat_id, 'html':load_design(language),
                       'allow_queries':True, 'chat_data':state_json({
                           'view':'processing', 'stage':'processing', 'percent':None,
                           'stage_title':words['processing_title'], 'title':words['waiting']})}
            if getattr(source,'message_thread_id',None):
                payload['message_thread_id']=source.message_thread_id
            result = await self.api(context.bot,'sendWidget',**payload)
            return ProcessingMessage(self,context,source,result['message_id'])
        except Exception as exception:
            LOG.warning('Processing widget unavailable (%s)',type(exception).__name__)
            return None

    async def send(self, context, source, items, title='', view='format', error=None, reuse=None,
                   _grouped=False, _range=None):
        if not self.enabled(source, context):
            return None
        if view=='search' and items and not error and not _grouped:
            groups=search_groups(items,title)
            if len(groups)>1:
                first=None;start=0
                for group in groups:
                    label=f'{start+1}–{start+len(group)} / {len(items[:8])}'
                    card=await self.send(context,source,group,title,view,reuse=reuse if first is None else None,
                                         _grouped=True,_range=label)
                    if card is None:
                        if first:await first.error('search')
                        return first
                    if first is None:first=card
                    start+=len(group)
                return first
        self.prune()
        if len(self.entries) >= MAX_CARDS:
            return None
        owned = [k for k, v in self.entries.items() if v.owner == source.from_user.id and not v.busy and not v.filename]
        if len(owned) >= 32:
            del self.entries[owned[0]]
        language = user_language(source.from_user, context.user_data)
        entry = Bubble(self, context, source, language, items, title, view, _range)
        if error:
            entry.problem_state(error)
        try:
            payload = dict(chat_id=source.chat_id, html=load_design(language),
                           chat_data=state_json(entry.state),
                           allow_queries=True, public_data=entry.cover_data())
            if getattr(source, 'message_thread_id', None):
                payload['message_thread_id'] = source.message_thread_id
            if (isinstance(reuse, ProcessingMessage) and reuse.service is self
                    and reuse.chat_id == source.chat_id and reuse.fallback is None
                    and not reuse.loader_deleted):
                entry.message_id = reuse.message_id
                await self.api(context.bot, 'editMessageWidgetData',
                    chat_id=entry.chat_id, message_id=entry.message_id,
                    chat_data=payload['chat_data'], public_data=payload['public_data'])
            else:
                result = await self.api(context.bot, 'sendWidget', **payload)
                entry.message_id = result['message_id']
            entry.published = payload['chat_data']
            entry.published_covers = payload['public_data']
            entry.clean_chrome = True
        except Exception as exception:
            LOG.warning('Widget unavailable; using plain UI (%s)', type(exception).__name__)
            return None
        self.entries[(entry.chat_id, entry.message_id)] = entry
        if not error and any(v.get('thumbnail') for v in items):
            context.application.create_task(self.refresh_covers(entry))
        return entry

    async def refresh_covers(self, entry):
        try:
            await entry.prepare_covers()
            if entry.deleted or entry.state.get('stage') == 'done':
                entry.covers.clear();entry.cover_atlas = None
                return
            if self.entries.get((entry.chat_id, entry.message_id)) is entry:
                await entry.publish()
        except Exception as exception:
            LOG.warning('Background artwork failed (%s)', type(exception).__name__)

    async def handle_widget(self, update, context):
        query = getattr(update, 'widget_query', None)
        query = query.to_dict() if query is not None else update.api_kwargs.get('widget_query')
        if not query:
            return
        async def answer(text):
            await context.bot.do_api_request(endpoint='answerWidgetQuery',
                api_kwargs={'widget_query_id': query['id'], 'data': text},
                connect_timeout=3, read_timeout=3, write_timeout=3, pool_timeout=1)
        await self.dispatch(context, query.get('from', {}).get('id'), query.get('chat', {}),
                            query.get('message_id'), query.get('data', ''), answer, query.get('from', {}).get('language_code'))

    async def handle_callback(self, update, context):
        query = update.callback_query
        async def answer(text):
            await query.answer(text[:180] or None)
        await self.dispatch(context, query.from_user.id, query.message.chat.to_dict(),
                            query.message.message_id, query.data.removeprefix('bubble|'), answer, query.from_user.language_code)

    async def dispatch(self, context, owner, chat, message_id, data, answer, language=None):
        transport_answer = answer
        async def answer(text):
            try:
                await asyncio.wait_for(transport_answer(text), timeout=ACK_TIMEOUT)
            except Exception as exception:
                LOG.warning('Widget acknowledgement failed; validated action will continue (%s)', type(exception).__name__)
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
                               message_id=message_id, chat_data=state_json({'error':'expired','problem':words['problems']['expired']}))
                await context.bot.edit_message_caption(chat_id=owner, message_id=message_id,
                    caption='', reply_markup={'inline_keyboard': []})
            except Exception:
                pass
            return
        if owner in self.busy_users:
            await answer(words['busy']); return
        parts = data.split('|')
        if data == 'resend':
            if entry.state.get('error') not in ('send', 'uncertain') or not entry.filename or time.monotonic() >= entry.file_deadline or not Path(entry.filename).is_file():
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
                except Exception as exception:
                    await entry.error('uncertain' if isinstance(exception, AmbiguousDelivery) else 'send')
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
