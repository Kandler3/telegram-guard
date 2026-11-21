# Content in telegram_guard 2.2

Use the existing `telegram_send` tool. Required: `chat_id`, `request_id`, `authorization`.
`text` is optional when `content` is present. Examples below describe content only and grant no permission.

| Content | Shape / notes |
| --- | --- |
| Any local file | `{"type":"file","path":"C:\\work\\report.pdf","kind":"document"}` |
| Photo | File with `kind:"photo"`; optional `caption`, `spoiler` |
| Video | File with `kind:"video"`, actual `width`, `height`, `duration` in seconds |
| GIF / animation | File with `kind:"animation"`; MP4 animations also need actual dimensions/duration |
| Audio / voice | File with `kind:"audio"` or `"voice"`, actual `duration`; audio may include `title`, `performer` |
| Round video | File with `kind:"video_note"`, equal nonzero width/height, actual duration; no caption |
| Sticker | File with `kind:"sticker"`, optional `emoji`; WebP, TGS or WebM must already be a valid sticker; no caption |
| Album | `{"type":"album","items":[...file objects...]}`; 2–10 photos/videos, audio only, or documents only |
| Contact | `{"type":"contact","phone_number":"+1234567","first_name":"Name"}`; optional last_name, vcard |
| Location | `{"type":"location","latitude":55.7,"longitude":37.6}`; optional accuracy_radius, live_period, heading |
| Venue | `{"type":"venue","latitude":55.7,"longitude":37.6,"title":"Office","address":"Street"}` |
| Poll | `{"type":"poll","question":"Choose","answers":["A","B"]}`; anonymous and multiple_choice flags |
| Quiz | Poll with `quiz:true`, `correct_answers:[0]` (zero-based), optional solution |
| Dice | `{"type":"dice","emoticon":"🎲"}`; also 🎯, 🏀, ⚽, 🎳, 🎰 |
| Checklist | `{"type":"checklist","title":"Tasks","items":["One","Two"]}`; requires Telegram Premium |
| Forward | `{"type":"forward","from_chat_id":123,"message_ids":[10,11]}`; optional drop_author, drop_media_captions |
| Reuse existing media | `{"type":"existing_media","from_chat_id":123,"message_id":10}`; optional new caption in top-level text |
| Share story in chat | `{"type":"story","from_chat_id":123,"story_id":10}`; does not publish a new story |

Both destination and source chats for forwarding/reusing/sharing must already be whitelisted.
Use IDs returned by the allowed read tools. Do not invent references or resolve other chats through another API.

For a file, put the caption in `content.caption` or top-level `text`, never both.
Albums carry captions on individual items. Optional file fields include `filename` (basename only),
`mime_type`, `thumbnail` (absolute JPEG path, up to 200 KiB), and `ttl_seconds` (1–60 on supported media).
The tool uploads existing bytes; it does not transcode, record voice/video, or convert documents into photos.
Read actual media metadata before filling duration/dimensions. Telegram validates codecs and media constraints.

Optional `options`: `reply_to`, `top_msg_id` (forum topic), `silent`, `link_preview` (text only), `entities`.
Plain text stays literal. Formatting entities use UTF-16 offsets/lengths; supported kinds are bold,
italic, underline, strike, spoiler, code, pre, blockquote, text_url and custom_emoji.
File captions may supply their own `entities`; albums use entities on each item.

Each item consumes the configured message quota, including unknown attempts. An album larger than
the per-minute limit is refused; never change the whitelist/limits to force a send.
The local per-file ceiling is 4 GiB; the account/server may impose a lower ceiling and other restrictions.
Paths must be absolute local regular files. Remote URLs, network shares, alternate streams, links,
guard state/session/credentials and protected secret paths are refused. Do not relocate a refused secret to bypass this.

Bot-only keyboards, invoices, games creation, payments, paid posts, secret chats, publishing stories,
and arbitrary Telegram RPCs are outside this personal-account sender. Forwarding existing supported
messages is available within the same whitelist/authorization rules. A live location sends an initial point
and period; the tool does not track or continuously update the device's location.

Only `sent` confirms all `message_ids`. `pending` means the server may still be working;
use `telegram_send_status` with the retained UUID. An unknown result or incomplete batch receipt
must never be replaced automatically with a new request. Check status even after a client timeout.
