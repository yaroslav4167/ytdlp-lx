# HTMLBubbles

The UI is separate from download/extraction code. Python uses PTB's forward-compatible `do_api_request` and reads `widget_query` from `Update.api_kwargs` on PTB 22.5. No framework upgrade or new dependency is required.

## Editing design

Edit `designs/default/`:

- `bubble.html` — the shared document shell and view containers;
- `format.html`, `search.html`, `progress.html`, `error.html` — individual screens;
- `styles.css` — typography, spacing, theme-aware colors;
- `widget.js` — rendering state, local format selection, and button actions.

The header uses normal sentence case, system typography and a secondary color. The app's blue/navy fallback colors are retained; Telegram theme variables take precedence for readability in users' chat themes.

Templates are read on every **new** card, without a bot restart. A sent widget's HTML cannot be edited: old cards keep their design. Send a fresh link to see a design change. Python service changes require restarting the bot. Keep `__STYLES__`, view placeholders, `__SCRIPT__`, `__I18N__`, DOM IDs, and request action names intact when editing.

Copy the design directory and set `BUBBLES_DESIGN=other-name` to select another design. Do not put secrets or external assets into templates.

Before deploying:

```sh
python -m bubbles.check
TELEGRAM_BOT_TOKEN=123456:offline-test python -m unittest -v test_bubbles test_yandex_music
```

`check` enforces both Telegram HTML limits: 32768 UTF-8 bytes and 4096 bytes after zlib level 9. Edit carefully: translations count towards the limit. No scripts/styles/fonts/images are fetched by the widget. Rendering still requires validation in Telegram clients; passing byte limits is not a sandbox certification.

## Language

`locales/ru.json` and `locales/en.json` hold user-facing copy. The sender's Telegram `language_code` is normalized (`ru-RU` → `ru`). Missing or unsupported language **always falls back to English**. Only the chosen frontend dictionary is embedded. Metadata such as recording titles is not translated.

`/language ru`, `/language en`, `/language auto` override or restore detection. A new locale can be added as `<language>.json` with the same keys; its native name and translations must fit the template budget. Existing cards keep the language in which they were sent. Preferences and card state are in memory and reset on restart.

## Behavior and fallback

- Widgets are enabled by default in private chats. Set `BUBBLES_ENABLED=0` to make them opt-in.
- `/bubbles` enables cards for the user; `/plain` switches to the original text/keyboard path.
- Groups use the original UI. If `sendWidget` or design validation fails, the request falls back to text/inline buttons. Older clients can use each widget's caption and inline keyboard or opt out with `/plain`.
- Search uses the bot's real eight results; only three compact rows are placed in state at once. All full URLs remain on the server. Metadata is rendered with `textContent`.
- Format, progress and errors share one immutable HTML document, switching views through `chat_data`. A caption and fallback buttons are updated alongside status transitions. The actual audio/video/document is sent through the existing native API.
- The four problem states are expired card, empty search, unavailable source, and failed file delivery. “Change search / Send another link” requests a native ForceReply; widgets cannot open a keyboard themselves.
- Query ownership is checked against private chat, sender, and original message. Formats and result indices are checked against server state. A query is acknowledged before a background job starts; concurrent widget jobs for one user are rejected.
- No playlists, quality selector, cancellation or Mini App is added. Existing photo-post handlers keep their existing behavior.

## File and state lifecycle

Cards are kept in a bounded in-memory registry (512 total; new cards replace idle non-file cards after 32 per user). Idle cards expire after 30 minutes or restart; an old card click shows the expired view.

A file whose delivery failed is retained for **10 minutes** for “Send again”. The resend action calls the file sender directly, never extraction/download/conversion. Routine stale-file cleanup skips retained paths. Success deletes the file; expiration deletes it and disables resend. An in-flight resend is allowed to finish before cleanup. Files left by a restart use the existing stale-download cleanup. The registry is not durable and a restart expires resend actions as well as old search buttons.

## Reference

https://core.telegram.org/bots/widgets
https://core.telegram.org/bots/api#sendwidget
