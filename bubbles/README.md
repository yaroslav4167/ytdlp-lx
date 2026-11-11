# HTMLBubbles

The UI is separate from download/extraction code. Python uses PTB's forward-compatible `do_api_request` and reads `widget_query` from `Update.api_kwargs` on PTB 22.5. PTB 22.5 is supported; Pillow handles covers, and yt-dlp[default] supplies the matching YouTube EJS solver.

## Editing design

Edit `designs/default/`:

- `bubble.html` — the shared document shell and view containers;
- `format.html`, `search.html`, `progress.html`, `error.html` — individual screens;
- `loading.html` — the small standalone animated processing placeholder;
- `styles.css` — typography, spacing, theme-aware colors;
- `widget.js` — rendering state, track selection, and direct download actions.

The header uses normal sentence case, system typography and a secondary color. The app's blue/navy fallback colors are retained; Telegram theme variables take precedence for readability in users' chat themes.

Templates are read on every **new** card, without a bot restart. A sent widget's HTML cannot be edited: old cards keep their design. Send a fresh link to see a design change. Python service changes require restarting the bot. Keep `__STYLES__`, view placeholders, `__SCRIPT__`, `__I18N__`, DOM IDs, and request action names intact when editing.

Copy the design directory and set `BUBBLES_DESIGN=other-name` to select another design. Do not put secrets or external assets into templates.

Before deploying:

```sh
python -m bubbles.build  # Node/npm on the development machine only
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
- Groups use the original UI. If `sendWidget` or design validation fails, the request falls back to text/inline buttons. Widget messages themselves have **no caption or inline keyboard**; older clients can opt out with `/plain`. All recording details and errors are drawn inside the widget.
- Search shows all eight real results as a compact flat list, rather than buttons or pages. Titles/artists retain their full labels when the state budget allows; unusually long labels are trimmed to fit. Full metadata and URLs remain on the server. Metadata is rendered with `textContent`. The widget has no inner scrollbar: Telegram reserves vertical dragging for the chat and clips widget content above 600 px, so the eight compact rows are laid out to fit.
- Format, progress and errors share one immutable HTML document, switching views through `chat_data`. A plain URL shows two direct download actions (audio/video), without an intermediate selector. After confirmed delivery, the loading widget is deleted and only the native file remains. The actual audio/video/document is sent through the existing native API.
- The problem states are expired card, empty search, unavailable source, failed delivery, and unconfirmed delivery (check the chat before manually resending). “Change search / Send another link” requests a native ForceReply; widgets cannot open a keyboard themselves.
- Query ownership is checked against private chat, sender, and original message. Formats and result indices are checked against server state. A query acknowledgement is attempted within four seconds; a failed or timed-out acknowledgement does not cancel the validated job. Concurrent widget jobs for one user are rejected. Widget control requests have short timeouts independent of long media uploads.
- No playlists, quality selector, cancellation or Mini App is added. Existing photo-post handlers keep their existing behavior.

## File and state lifecycle

Cards are kept in a bounded in-memory registry (512 total; new cards replace idle non-file cards after 32 per user). Idle cards expire after 30 minutes or restart; an old card click shows the expired view.

A file whose delivery failed is retained for **10 minutes** for “Send again”. The resend action calls the file sender directly, never extraction/download/conversion. Routine stale-file cleanup skips retained paths. Success deletes the file; expiration deletes it and disables resend. An in-flight resend is allowed to finish before cleanup. Files left by a restart use the existing stale-download cleanup. The registry is not durable and a restart expires resend actions as well as old search buttons.

## Reference

https://core.telegram.org/bots/widgets
https://core.telegram.org/bots/api#sendwidget

## Covers and delivery safety

`thumbnails.py` fetches a thumbnail from actual media metadata, bounds its body and pixel count, crops/resizes it with Pillow, and encodes WebP. Network destinations must be public HTTPS addresses; redirects are checked too. An unavailable image leaves a decorative placeholder without blocking download. All eight search miniatures share a small WebP atlas in a separate 1024-byte `public_data` object. This avoids inflating the 4096-byte compressed HTML or the 1024-byte `chat_data`. Larger individual covers are cached on the server and the selected cover is pushed when downloading begins. The progress card keeps the cover during download, conversion and upload, and the completed widget is removed after confirmed delivery.

`delivery.py` does not blindly retry media uploads after timeouts, connection-read errors, or bad-gateway responses: Telegram may already have delivered the file. Such outcomes show an unconfirmed-delivery screen and require an explicit manual retry. Only explicit flood-wait rejections and failures before connection establishment can retry automatically. A failure to remove a completed widget never restarts media delivery.

The loading card uses the same small square artwork to the left of the recording title as the search list. During upload the center reads “Отправка” / “Sending”, instead of an arrow.

## Response latency

A single widget is sent before search or URL inspection, with all view templates already included. Processing, search results or format selection, and download/conversion/upload update the state of that same message. An error also updates it in place. If widget editing fails, the placeholder falls back to one editable text message.

Search uses flat result metadata immediately. It does not fully extract all eight videos before showing the list. Source format validation happens only after download selection. Ordinary search keyboards also avoid per-result requests. A direct link reuses one probe for playlist detection, media types and card metadata.

Covers are fetched and pushed in a background application task after the results card is sent. Completion cannot overwrite a newer loading/error state or resurrect a deleted card. Search has a 25-second deadline and an explicit problem state, so an upstream failure does not leave an endless spinner.

## Long recording titles

Search always keeps all eight results in one widget. Titles wrap across lines; selection changes only highlighting and format buttons, preserving DOM nodes and artwork. No marquee or additional result messages are used.

The initial state stays within 1024 bytes. If full names cannot fit, the private-chat widget fetches the remaining names in bounded background replies (at least 5.2 seconds apart) and changes title text in place. Download taps wait for a pending background reply. A small font adjustment keeps long lists within the widget height where practical. The authoritative full titles remain in the owned server registry.

After editing `widget.js`, run `python -m bubbles.build` (Node/npm required locally). It uses Terser 5.51.2 to produce `widget.min.js` and `widget.build.json`; deploy those with the source. Inline HTML handler names are preserved. The server needs no npm runtime: it uses the compact file only when source and output hashes match. HTML/CSS/localization edits do not require rebuilding JavaScript.

## Cover quality

The initial eight-cover atlas is a fast preview. A private-chat background lookup starts a bounded replay of separately encoded square covers (32–64 px), at most one image update every 1.1 seconds, within the same widget. Each update stays below the 1024-byte public-data cap. The client merges cover fragments and retains unchanged image nodes; identical chat state does not rebuild the list. Replays stop on download, errors, deletion or eviction. A recreated widget can request the cached images again. Selected/download covers use the same square encoder, with a 738-byte image budget and higher WebP quality.

Missing artwork recovers in place: image fetches retry with public YouTube fallback sizes, and the client checks for individual covers missed during progressive updates. Bounded acknowledged replies return cached images or start a deduplicated fetch retry, without creating a new result message. Requests remain at least 5.2 seconds apart, stop after the missing covers are restored, and honor the existing download-request queue. Permanent source failures have a bounded retry count.
