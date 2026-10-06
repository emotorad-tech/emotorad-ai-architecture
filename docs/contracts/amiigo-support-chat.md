# Amiigo Support Chat API Contract

Oct 6, 2026 · @Sagnik Mukherjee · v1, **Proposed**. Replaces the draft of 30 September 2026 and the chat history draft of 6 October 2026.

## What changed from the earlier drafts

- **The chat runs over a WebSocket.** The app sends the rider's messages and receives the bot's replies, a typing state and ticket updates on one connection. There is no HTTP endpoint for sending a message: the proposed `POST /amiigo/v1/message` is withdrawn, and messages are sent only as `message` frames on the socket.
- **The server keeps the history.** When the connection has closed (the rider left the chat, the app went to the background, the network dropped), the app restores the chat with a `GET` when the rider comes back. Uninstalling the app or changing phone loses nothing.
- **Ticket closure comes from Zoho Desk.** When the support team closes a ticket in Zoho Desk, Zoho tells our server; the app gets a `ticket_update` on the socket, and the chat's history shows the ticket as closed.
- **Every app call carries the rider's Amiigo access token** in the `Authorization` header, including the socket's opening handshake. There is no test session in this contract.
- **Photos and videos are uploaded first** and sent by `upload_id`; nothing is sent inline on the socket.

## Status and scope

| Part | Today | v1 (this contract) |
| --- | --- | --- |
| Chat engine (routing, answers, safety check, one step per reply) | Built, website only, over HTTP | The same engine, behind the socket |
| Who the rider is | A test session only | The rider's real Amiigo access token, checked by our server |
| Real-time chat | None | `wss://…/amiigo/v1/chat` |
| History | Kept on our server; no way for the app to read it | `GET /amiigo/v1/conversations` and `GET /amiigo/v1/conversations/{conversation_id}/messages` |
| Tickets | Recorded with our `EM-` reference and sent to a Zoho Desk test department on staging | The same, plus the closed status from Zoho Desk (see "Tickets and Zoho Desk") |
| Photos and videos | Stored in our S3 bucket | The same, by upload |
| Bike, warranty and service data | Test data in the app's test session | The rider's bikes and warranty from the OMS, and their Amiigo bikes, service status and recent rides |

None of v1 is built yet. We build it next and tell you when it is on staging. Until it ships, anything here can change; once it ships, v1 only gains things (see "Versioning").

The app must not go to real riders until two things are done: engineering signs off sending chat text to our model provider (it runs outside AWS today), and the token check is finished and reviewed.

## How the app uses it

1. **The chat screen opens.** Call `GET /amiigo/v1/conversations?limit=1`. If the latest chat has `can_continue: true`, load its messages with `GET /amiigo/v1/conversations/{id}/messages` and carry it on. Otherwise start a new chat (or show the list of past chats).
2. **Open the socket** to `/amiigo/v1/chat` with the token in the handshake, and wait for `ready`.
3. **The rider sends a message.** Send a `message` frame. The server answers with `ack` (the message as stored), then `bot_typing`, then `reply`.
4. **The socket closes.** Nothing is lost: a reply the bot was still working on is finished and saved on the server.
5. **The rider comes back.** Call `GET …/messages?after=<id of the last message the app holds>` to fetch everything that arrived while the socket was closed, then open the socket again. After a reinstall, step 1 restores the chat from scratch.
6. **Support closes the ticket in Zoho Desk.** If the socket is open, the app gets `ticket_update`. Either way the chat's history shows the ticket as closed, with a notice message.

If the socket cannot be opened, keep retrying with the back-off in "Close codes" and show the rider that the chat is reconnecting; the history stays readable with `GET` meanwhile.

## What the bot knows about the rider

- From the token, the bot knows the rider's verified phone number. From that number it looks up their bikes and warranty in EMotorad's order system, and their bikes, service status and recent rides in the Amiigo app.
- So the bot never asks a signed-in rider for their bike model, frame number or purchase date, and never asks for a one-time code.
- A rider with several bikes is asked which one the chat is about.
- A rider with no registered bike can still chat; the bot offers to help register the warranty.

## Conversation lifecycle

- A chat is one `conversation_id`, a UUID the app makes when the rider starts a new chat. The app sends it with every message of that chat.
- The bot reads the last 12 exchanges of the chat when it answers.
- For each rider, the bot also remembers a one-line summary of their last 3 chats (topic, bike, outcome, ticket), so it can say "last time we spoke about…".
- The server keeps a chat's working state for 48 hours after its last message, and the messages themselves permanently (until the rider asks for them to be deleted). After 48 hours of silence the same `conversation_id` still works, but the bot starts that chat afresh, so offer the rider a new chat instead (`can_continue` says which).
- One message at a time per chat: send the next message only after the reply to the previous one has arrived.

## Base URLs

| Environment | HTTP | WebSocket |
| --- | --- | --- |
| Staging | `https://ai-release-stage.emotorad.com` | `wss://ai-release-stage.emotorad.com/amiigo/v1/chat` |
| Production | Not decided | Not decided |
| A developer laptop | `http://localhost:8000` | `ws://localhost:8000/amiigo/v1/chat` (test data only) |

## Authentication

Every HTTP request, and the socket's opening handshake, carries the rider's normal Amiigo access token:

```
Authorization: Bearer <Amiigo access token>
```

- Our server checks the token's signature with Amiigo's public key, checks it has not expired, and reads the rider's verified phone number from it. There is no extra sign-in or code step in the app.
- Only an **access** token is accepted. A refresh token, an OTP token or any other type is refused.
- Never put the token in a URL or a query string. The server never stores it.
- Native WebSocket clients on Android and iOS can send headers on the handshake (in Flutter, `IOWebSocketChannel.connect(uri, headers: {...})`).
- A socket lives no longer than its token: when the token expires, the server closes the socket with code `4401` and reason `token_expired`. Refresh the token with Amiigo and reconnect.
- A rider's chats are the Amiigo app chats they had while signed in with this phone number (see "Which chats are in the history").

## The chat socket: `/amiigo/v1/chat`

### Connecting

- One socket per signed-in rider on a device. It carries all of that rider's chats: every frame names its `conversation_id`.
- Frames are JSON text frames, UTF-8, at most 64 KB each. Binary frames are refused.
- Within 2 seconds of the handshake the server sends `ready`. Send nothing before it.
- While the chat screen is open, send `{"type": "ping"}` every 30 seconds; the server answers `{"type": "pong"}`. The server closes a socket that sends nothing for 10 minutes (code `4408`).
- Close the socket when the chat screen closes or the app goes to the background. Restore with `GET` when the rider comes back (see "How the app uses it").

### Frames the app sends

**`message`**: one rider message.

```json
{
  "type": "message",
  "client_message_id": "6c0b3f6a-1d2e-4f4b-9a7e-2c5d8e9f0a11",
  "conversation_id": "3f2c9a1e-5b7d-4e8a-9c21-7d4e5f6a8b90",
  "text": "my battery isn't charging",
  "attachments": [{"upload_id": "upl_7Hq2k9"}],
  "screen": "battery_health"
}
```

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `client_message_id` | string (UUID) | Yes | Made by the app, new for each message. A message resent with the same id is never handled twice (see "Resending"). |
| `conversation_id` | string (UUID) | Yes | The chat this message belongs to. For a new chat, the app makes a new UUID and uses it from the first message on (and for that chat's uploads). |
| `text` | string | Yes | What the rider typed, up to 4,000 characters. `""` when they send only a photo or tap a chip. |
| `attachments` | array, up to 3 | No | `{"upload_id": "..."}` for each photo or video, from "Photos and videos". |
| `screen` | string | No | The app screen the chat was opened from, for example `battery_health`. It helps the bot guess the topic. |
| `pill` | string | No | A quick-reply chip the rider tapped. Today only `battery` and `motor` are handled. |
| `location` | object | No | `{"latitude": 18.52, "longitude": 73.85}`, sent only after the rider taps "Share my location". The bot turns it into a pincode and keeps no coordinates. |

**`ping`**: `{"type": "ping"}`, to keep the socket open.

### Frames the server sends

**`ready`**: the socket is open and the rider is known.

```json
{"type": "ready", "protocol": 1, "server_time": "2026-10-06T09:12:40Z"}
```

**`ack`**: the rider's message was accepted, as the server stored it.

```json
{
  "type": "ack",
  "client_message_id": "6c0b3f6a-1d2e-4f4b-9a7e-2c5d8e9f0a11",
  "conversation_id": "3f2c9a1e-5b7d-4e8a-9c21-7d4e5f6a8b90",
  "message": {
    "id": "3f2c9a1e-5b7d-4e8a-9c21-7d4e5f6a8b90#00003",
    "sender": "rider",
    "text": "my battery isn't charging",
    "sent_at": "2026-10-06T09:12:44Z",
    "attachments": [{"kind": "image", "url": "https://…signed link…", "url_expires_at": "2026-10-06T09:27:44Z"}]
  }
}
```

Replace the app's local copy of the message with `message`: its `id` is the one history uses, and its `text` is masked the way history shows it (see "What is masked").

**`bot_typing`**: the bot is working on a reply.

```json
{"type": "bot_typing", "conversation_id": "3f2c9a1e-5b7d-4e8a-9c21-7d4e5f6a8b90", "state": "thinking"}
```

`state` is `thinking`, or `looking_at_video` while a video is being described (that can take a minute or two).

**`reply`**: the bot's whole answer to one message. Each message gets exactly one reply.

```json
{
  "type": "reply",
  "conversation_id": "3f2c9a1e-5b7d-4e8a-9c21-7d4e5f6a8b90",
  "in_reply_to": "6c0b3f6a-1d2e-4f4b-9a7e-2c5d8e9f0a11",
  "message": {
    "id": "3f2c9a1e-5b7d-4e8a-9c21-7d4e5f6a8b90#00004",
    "sender": "bot",
    "text": "Thanks for the photo. Is the key on the battery turned to **ON**?",
    "sent_at": "2026-10-06T09:12:51Z",
    "attachments": [
      {"kind": "image", "url": "https://…signed link…", "url_expires_at": "2026-10-06T09:27:51Z", "mime_type": "image/png", "caption": "The battery On/Off switch", "poster": null}
    ]
  },
  "actions": [],
  "escalated": false,
  "ticket": null,
  "handled_by": "narrow_support"
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `message` | object | The reply as history stores it (see "Messages") |
| `actions` | array | Buttons for under this reply, for example `{"kind": "request_location", "label": "Share my location"}`. Shown live only: history does not keep them. |
| `escalated` | boolean | `true` when the bot handed the chat to EMotorad's support team, for example for a safety issue |
| `ticket` | object or `null` | Set on the reply that raised a ticket: `{"reference": "EM-1000042", "status": "open", "closed_at": null}` |
| `handled_by` | string | Which part of the bot answered. For logs and support; do not build UI on it. |

Replies arrive whole, never word by word. Every reply is checked (the safety check, warranty claims, one step at a time) before the rider sees any of it, so show `bot_typing` until the reply arrives. Most replies take 2 to 11 seconds; allow 60 seconds, and 150 seconds when the message carries a video. If the socket closes first, the reply is saved: fetch it with `GET …/messages?after=…`.

**`ticket_update`**: a ticket from one of the rider's chats changed status in Zoho Desk. Sent to every open socket of that rider.

```json
{
  "type": "ticket_update",
  "conversation_id": "3f2c9a1e-5b7d-4e8a-9c21-7d4e5f6a8b90",
  "ticket": {"reference": "EM-1000042", "status": "closed", "closed_at": "2026-10-07T11:02:10Z"},
  "message": {
    "id": "3f2c9a1e-5b7d-4e8a-9c21-7d4e5f6a8b90#00011",
    "sender": "system",
    "text": "Your support request EM-1000042 was closed by our support team.",
    "sent_at": "2026-10-07T11:02:12Z",
    "attachments": []
  }
}
```

The notice in `message` is also saved in the chat's history. Its wording is a draft until the support lead approves it.

**`error`**: a frame the server could not act on. The socket stays open.

```json
{"type": "error", "client_message_id": "6c0b3f6a-1d2e-4f4b-9a7e-2c5d8e9f0a11", "detail": "conversation_busy"}
```

| `detail` | When | What the app should do |
| --- | --- | --- |
| `conversation_busy` | A message for a chat whose previous message has no reply yet | Wait for the reply, then send it again with the same `client_message_id` |
| `conversation_not_found` | `conversation_id` belongs to another rider | Start a new chat with a new UUID |
| `upload_not_found` | An `upload_id` that is unknown, another rider's, or older than an hour | Upload the file again |
| `upload_not_finished` | The `PUT` to S3 has not completed, or the file does not match what was requested | Finish the `PUT`, or upload again |
| `too_many_attachments` | More than 3 attachments | Send at most 3 |
| `text_too_long` | `text` over 4,000 characters | Shorten it |
| `bad_frame` | Not JSON, an unknown `type`, a missing required field, or over 64 KB | A bug in the app: log it |
| `rate_limited` | More than 20 messages a minute from this rider | Wait a few seconds, then send again |

### Resending

If the socket closes before an `ack` or a `reply` arrives, reconnect, then send the same `message` frame again with the same `client_message_id`. The server remembers ids for 24 hours: a message it has already accepted is not handled again; it sends the same `ack`, and the same `reply` once that exists. So resending is always safe.

### Close codes

| Code | Reason | What the app should do |
| --- | --- | --- |
| `4401` | `token_missing`, `token_invalid`, `token_expired` or `token_type_not_allowed` | Expired: refresh the token and reconnect. The others: ask the rider to sign in again. |
| `4408` | `idle` | Nothing: reconnect when the rider is in the chat again |
| `1001` | The server is restarting | Reconnect after 1 to 2 seconds |
| `1008` | Repeated bad frames | A bug in the app: log it |
| `1011` | A fault on our side | Reconnect with back-off: 1, 2, 4, 8, then every 30 seconds |

After any reconnect, call `GET …/messages?after=…` before showing the chat as up to date.

## Photos and videos

Upload each photo or video first, then send its `upload_id` in a `message` frame.

1. Ask for an upload slot: `POST /amiigo/v1/uploads` with the token in the header and `{"conversation_id": "...", "mime_type": "video/mp4", "size_bytes": 18350081}`. Use the chat's `conversation_id` (for a new chat, the UUID the app made for it).
2. The response is `{"upload_id": "upl_...", "url": "...", "headers": {...}, "expires_in": 300}`. Send an HTTP `PUT` of the file's bytes to `url` with exactly those `headers`, within 5 minutes. The `PUT` goes straight to S3 and carries no token. Show progress; a 100 MB clip takes a while on mobile data.
3. Send the `message` frame with `{"upload_id": "upl_..."}` in `attachments`, within an hour of the upload.

Shrink photos on the phone before uploading: at most 1280 px on the long edge, JPEG at quality 0.8 (usually 150 to 300 KB).

| Limit | Value |
| --- | --- |
| Photo types | `image/jpeg`, `image/png`, `image/webp`, up to 10 MB |
| Video types | `video/mp4`, `video/quicktime` (.mov), `video/3gpp` (.3gp), up to 100 MB |
| Per message | 3 attachments, photos and videos together |
| Upload slots | 20 a minute per rider |

A photo or video counts as the evidence the bot needs before it raises a fault ticket. The bot asks for a short video first, and a photo if the rider can't take one.

## History

### Which chats are in the history

- The rider's chats in the Amiigo app, while signed in with this phone number. Each chat says where it happened in `channel`, which in v1 is always `amiigo_app`; later versions may add other places.
- Not included in v1: chats on the EMotorad website, even ones in which the rider proved this phone number with a one-time code, and anything deleted through "Delete my conversation data".

### GET /amiigo/v1/conversations

The rider's chats, most recent activity first.

| Query | Type | Default | Meaning |
| --- | --- | --- | --- |
| `limit` | integer, 1 to 50 | 20 | Chats per page |
| `cursor` | string | none | `next_cursor` from the previous page. Leave out for the first page. |
| `channel` | `amiigo_app` | all | Only chats from that place. In v1 every chat is from the app, so this changes nothing; any other value is a `422` |

**Response 200**

```json
{
  "conversations": [
    {
      "conversation_id": "3f2c9a1e-5b7d-4e8a-9c21-7d4e5f6a8b90",
      "channel": "amiigo_app",
      "title": "Battery charges very slowly",
      "started_at": "2026-10-06T09:12:44Z",
      "last_message_at": "2026-10-06T09:31:02Z",
      "bike": {"product_name": "EMX Plus", "frame_number": "EMXP2025004417"},
      "status": "handed_to_support",
      "ticket": {"reference": "EM-1000042", "status": "closed", "closed_at": "2026-10-07T11:02:10Z"},
      "message_count": 14,
      "can_continue": false
    }
  ],
  "next_cursor": null
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `conversation_id` | string | The chat's id |
| `channel` | string | Where the chat happened: `amiigo_app` in v1 |
| `title` | string | What the chat was about, written by the server: the problem the bot worked through, for example "Battery charges very slowly" or "Range has dropped", or a general one: "Battery issue", "Motor issue", "Warranty registration" or "General question". Show as it is. |
| `started_at`, `last_message_at` | string | ISO 8601 times in UTC |
| `bike` | object or `null` | The bike the chat was about; `null` when no bike was chosen |
| `status` | string | `open`, or `handed_to_support` when the chat was passed to EMotorad's support team |
| `ticket` | object or `null` | The chat's ticket: `reference`, `status` (`open` or `closed`) and `closed_at` |
| `message_count` | integer | Messages in the chat, of every sender |
| `can_continue` | boolean | `true` when the last message is under 48 hours old. After that the bot starts the chat afresh, so start a new chat instead. |
| `next_cursor` | string or `null` | Pass as `cursor` for the next page; `null` on the last page |

An empty history is `{"conversations": [], "next_cursor": null}`.

### GET /amiigo/v1/conversations/{conversation_id}/messages

One chat's messages. Three ways to call it:

| Query | Returns |
| --- | --- |
| (none) | The newest `limit` messages, oldest first, and `older_cursor` for the page before them |
| `before=<older_cursor>` | The page before, oldest first, and the next `older_cursor` |
| `after=<message id>` | The messages after that one, oldest first, up to `limit`, and `more_after: true` when more remain (call again with the last id) |

`limit` is 1 to 100, default 50. Use `after` when the rider comes back: pass the `id` of the last message the app holds.

**Response 200**

```json
{
  "conversation_id": "3f2c9a1e-5b7d-4e8a-9c21-7d4e5f6a8b90",
  "messages": [
    {
      "id": "3f2c9a1e-5b7d-4e8a-9c21-7d4e5f6a8b90#00001",
      "sender": "rider",
      "text": "My battery is not charging",
      "sent_at": "2026-10-06T09:12:44Z",
      "attachments": []
    },
    {
      "id": "3f2c9a1e-5b7d-4e8a-9c21-7d4e5f6a8b90#00002",
      "sender": "bot",
      "text": "Hi, I'm EMotorad's virtual assistant, an AI, not a person. Is the battery switched on?",
      "sent_at": "2026-10-06T09:12:51Z",
      "attachments": [{"kind": "image", "url": "https://…signed link…", "url_expires_at": "2026-10-06T09:27:51Z"}]
    }
  ],
  "older_cursor": null,
  "more_after": false
}
```

### Messages

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string | Stable. Use it to de-duplicate, and as `after` |
| `sender` | string | `rider`, `bot`, or `system` (a notice from EMotorad, such as a closed ticket) |
| `text` | string | As stored (see "What is masked"). `""` for a message that was only a photo or video. |
| `sent_at` | string | ISO 8601 time in UTC |
| `attachments[].kind` | string | `image` or `video` |
| `attachments[].url` | string or `null` | A temporary link to the file. `null` when the file cannot be shown (a photo sent while photo storage was unavailable, so it was not kept, or one that has been deleted): show a placeholder such as "Photo". |
| `attachments[].url_expires_at` | string or `null` | When the link stops working, about 15 minutes after the response. Load the image when it arrives and keep the image, not the link; fetch again for fresh links. |
| `attachments[].mime_type`, `caption`, `poster` | string or `null` | Only on a live `reply` frame. History keeps the kind and the file, not the caption. |

### What is masked

Text comes back as the server stored it, which is not always exactly what the rider typed.

- Phone numbers and email addresses typed in a message show as `[phone]` and `[email]`.
- The bot's first reply in a chat starts with the line saying it is EMotorad's virtual assistant, as the rider saw it.
- Replies written by a person on the support team are not part of the chat yet.

## Rendering

| Part | What to show | Notes |
| --- | --- | --- |
| `text` | The bubble | Formatting used: `**bold**`, `*italic*`, `` `code` ``, bullet lines starting ` -  `, numbered lines ` 1.  `, and line breaks. Links and headings are not used: show any `[...]` or `#` as plain text. Escape the text before formatting it. |
| First reply of a chat | As sent | It opens with a line saying the bot is an AI, not a person. That line is required; keep it. Do not add your own "I'm an AI" greeting in the app: the server's line is the disclosure. A plain greeting such as "Hi! How can I help with your EMotorad bike today?" is fine. |
| Languages | As sent | The bot answers in the rider's language: English, Hindi (Devanagari script) or Hinglish. Fonts must render Devanagari. |
| `sender: "system"` | A centred notice, not a bubble | For example a closed ticket |
| `attachments[]` | An image or a video player under the bubble | Use `caption` as the caption and alt text and `poster` as a video's still, when present |
| `actions[]` | A button under the live reply | Today only `{"kind": "request_location", "label": "Share my location"}`. On tap, ask for location permission and send the next message with `location`. If the rider refuses, let them type their pincode. Ignore any `kind` you do not know. |
| `escalated: true` | A clear "handed to our team" state | The bot has stopped and a person will contact the rider; say "our team will contact you", with no promised channel or time. For a safety report (smoke, swelling, heat) this arrives with stop-using instructions: show them prominently. |
| `ticket` | A reference chip the rider can copy, with its status | `reference` is opaque: do not parse it or assume its length |

The server sends no quick-reply chips of its own. Chips the app offers at the start (for example Battery, Motor) are sent back as `pill`.

## Tickets and Zoho Desk

- A ticket gets our own reference while the bot replies: `EM-` and seven digits, from `EM-1000001`. Never the Zoho Desk ticket number: the Zoho Desk ticket carries our reference, so support finds it either way. The reply carries the reference in `ticket` at once, because it never waits for Zoho Desk, and the ticket reaches Zoho Desk shortly after. On staging it goes to a test department until engineering signs off real tickets.
- A server run without Zoho Desk, such as one on a developer laptop, issues short test references like `EM-00001` instead. One more reason to treat the reference as an opaque string: do not parse it or assume a prefix or length.
- When the support team closes the ticket in Zoho Desk, Zoho Desk calls our server (see "For the server team"). Our server marks the ticket closed, saves a `system` notice in the chat, and sends `ticket_update` to the rider's open sockets. A rider with the app closed sees it the next time the app loads the chat.
- v1 reports two statuses, `open` and `closed`. Zoho's other states (on hold, escalated) show as `open`.
- The rider can always write again after a ticket closes; the bot answers as usual.

**When the bot hands a chat to the support team.** Both wordings below are drafts until the support lead confirms them.

| Area | What the server sends | What the app should do |
| --- | --- | --- |
| Handover wording | A signed-in rider always has a number on record, so the reply is: "I've passed this conversation to our support team, so you won't need to repeat yourself. They will be in touch. Your reference is EM-…", with `escalated: true` and the reference in `ticket`. A chat with no number on record first gets "I can pass you to our support team. What mobile number can they reach you on?" with `escalated: false`; a signed-in rider is never asked, so this is written down only because the same engine serves the website chat. | Show the text as sent. Treat `escalated: false` as a chat still open. |
| Who contacts the rider | Support, from Zoho Desk; by phone or email is not decided | Promise no channel or time: say "our team will contact you" |

**Evidence before a fault ticket.** For a fault with the bike (battery or motor), the bot raises a ticket, or hands the chat to a person, only after a video or photo shows the fault. It asks for a short video first, and a photo if the rider can't take one. After three asks with nothing that shows the fault, it raises no ticket and gives EMotorad's customer care contact instead. Safety reports, delivery and order questions, and warranty registration never wait for evidence.

**Safety reports.** Smoke, fire, swelling, a burning smell, brakes that do not work and similar reports skip everything else: the bot raises a top-priority ticket at once, with no evidence needed, and the reply carries stop-using instructions. Show them prominently.

## Deleting conversation data

The app's "Delete my conversation data" button lets a signed-in rider ask for everything the support chat holds about them to be deleted: their past chats, the photos and videos they sent, and the record of where they chatted from. It does not delete their warranty registration, orders, invoices or service tickets. Our team checks each request and deletes the data within 30 days. A rider can also ask in the chat ("delete my data"); both make the same request, and a rider has at most one pending request. Once the deletion is done, history returns nothing for those chats.

**The dialog.** Show it before calling anything.

- Title: "Delete my conversation data?"
- Body: "This deletes everything this chat holds about you: your past chats with me, the photos and videos you sent, and the record of where you chatted from. It does not delete your warranty registration, orders, invoices or service tickets, which EMotorad keeps for your warranty and by law. It's done within 30 days."
- Buttons: "Delete" (calls `POST /amiigo/v1/erasure-requests` with `"confirm": true`) and "Keep my data" (closes the dialog).

| Endpoint (token in the header) | Body | Answer |
| --- | --- | --- |
| `POST /amiigo/v1/erasure-requests` | `{"confirm": true, "conversation_id": "..."}` (`conversation_id` optional) | 201 `{"reference": "DEL-7K3P9Q", "status": "pending", "text": "..."}`; 200 with the same shape when one was already pending; 400 without `"confirm": true` |
| `POST /amiigo/v1/erasure-requests/status` | `{}` | 200 `{"reference": "DEL-7K3P9Q", "status": "pending", "requested_at": "..."}`, or `{"reference": null, "status": "none"}` |
| `POST /amiigo/v1/erasure-requests/cancel` | `{}` | 200 `{"reference": "DEL-7K3P9Q", "status": "cancelled", "text": "..."}`; 404 when nothing is pending |

Show `text` to the rider as it comes. After a request, show the reference and a "Cancel deletion" action until the request is done.

## Errors on the HTTP endpoints

Errors come back as an HTTP status with `{"detail": "<code>"}` (a `422` carries a list of field problems instead).

| Status | `detail` | When | What the app should do |
| --- | --- | --- | --- |
| 401 | `token_missing` | No `Authorization` header | Send the access token |
| 401 | `token_expired` | The access token has expired | Refresh it with Amiigo and retry once |
| 401 | `token_invalid` | The signature does not check out, or the token cannot be read | Ask the rider to sign in again |
| 401 | `token_type_not_allowed` | A refresh, OTP or other non-access token | Send the access token |
| 400 | `cursor_invalid` | A `cursor`, `before` or `after` the server did not issue | Load the first page again |
| 404 | `conversation_not_found` | The chat does not exist, or it is not this rider's. The server does not say which. | Drop it from the list |
| 404 | `nothing_pending` | Cancelling a deletion when none is pending | Refresh the deletion status |
| 413 | `file_too_large` | An upload slot asked for more than the size limit | Tell the rider the limit |
| 415 | `file_type_not_accepted` | An upload of a type not listed | Tell the rider which types work |
| 422 | (field list) | A malformed body or query, for example `limit` out of range | A bug in the app: log it |
| 429 | `rate_limited` | More than 60 history requests or 20 upload slots a minute for one rider | Wait a few seconds, then retry |
| 503 | `history_unavailable` or `storage_unavailable` | Storage is briefly unavailable | Retry after a few seconds |

Retrying a `GET` is always safe. A problem inside the bot itself, such as the AI model being down, is not an error: it arrives as a normal `reply` that hands the chat to a person.

## Caching and privacy

- HTTP responses carry `Cache-Control: no-store`.
- Keep chats in memory for the screen that shows them. Do not write them to disk; the server is the copy that survives. Clear anything held when the rider signs out or switches account.
- Photo and video links are temporary and work for anyone holding them: do not log or share them.

## Testing on staging

1. Sign in to the Amiigo app on its staging environment with a test number, and take the access token it gets.
2. Open the socket with that token, send a `message`, and see `ack`, `bot_typing` and `reply`.
3. Close the socket, then call `GET /amiigo/v1/conversations` and `GET …/messages`: the chat comes back whole.

Use test numbers only, never a real rider's. There is no test session in v1: every call needs a real staging token.

## Versioning

The version is in the path, `/amiigo/v1/`, and in `ready.protocol`. Within v1, changes only add things: new optional fields, new frame `type`s, new `channel`, `sender`, `actions` and `attachments` kinds. Removing or renaming anything goes to `/v2/`, announced in advance. So the app must ignore fields, frame types and values it does not know.

## Open questions

| Question | Who answers | Our proposal |
| --- | --- | --- |
| Should website chats, where the rider verified their number with a code, appear in the app later? | App and product | Yes, once each message records whether the verified rider wrote it |
| Should a closed ticket also send a push notification when the app is closed? | App and product | Not in v1: the app sees it on its next `GET` |
| Should replies from the support team appear in the chat? | Support and product | Later, with Zoho two-way chat; they would arrive as a new `sender` |
| When a rider writes again after their ticket closed and needs help again, does the same ticket reopen or a new one start? | Support lead | A new ticket, with the old reference noted on it |
| Wording of the closed-ticket notice | Support lead | The draft in `ticket_update` |
| When a rider changes their number, should history follow the account? | Both | Not in v1: history belongs to the number in the token |
| Which `screen` names will the app send? | App team | A short fixed list, for example `battery_health`, `bike_home`, `service`, `help` |
| Should a chat opened from one bike's screen name that bike? | Both | An optional `vin` field on `message`, so a rider with several bikes is not asked which one |
| Are the page sizes right for the app's screens? | App team | 20 chats and 50 messages a page |

## For the server team: the Zoho Desk webhook

Not for the app. Recorded here so the ticket statuses above have a source.

- Zoho Desk calls `POST /webhooks/zoho/tickets` on our API when a ticket's status changes. It is set up in Zoho Desk by a person, for the department our tickets go to, and authenticated with a shared secret we give Zoho Desk when it is set up; requests without it are refused. The exact payload and how the secret is carried are confirmed against Zoho Desk when the webhook is configured, and captured in `docs/api-shapes/` before any code reads them.
- Support finds a chatbot ticket by its subject, which contains `[AI chat]` and ends with our environment and reference in square brackets, for example `[AI chat] Battery: charging - EMX Plus [stage:EM-1000001]`. A ticket for a number nobody verified starts `[Unverified] [AI chat]`. There is no custom field for it, so a Zoho Desk rule or webhook filter on the subject must test that it contains `[AI chat]`: a filter on how the subject starts misses the unverified tickets.
- Our server finds the ticket by its Zoho Desk ticket id in our `tickets` records, ignores tickets that are not the chatbot's, records the new status once (a repeat of the same event changes nothing), saves the `system` notice in the chat, and sends `ticket_update` to the rider's open sockets.
- Open sockets are held by the API process. Staging runs one container; running more than one needs a shared channel between them (for example Redis) so a webhook reaches the container holding the rider's socket.
