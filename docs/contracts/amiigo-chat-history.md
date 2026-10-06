# Amiigo Chat History API Contract

Oct 6, 2026 · @Sagnik Mukherjee · v1, **Proposed**

## Status and scope

The app can show a signed-in rider their past support chats, and the messages in each, on any device. The rider is identified by the Amiigo access token the app already holds; there is no extra sign-in.

| Part | Today | This contract |
| --- | --- | --- |
| Rider identity | The chat server knows only the test session `sess-amiigo-test` | The chat server checks the rider's real Amiigo access token itself |
| History for the app | None: the app keeps chats on the device | `GET /amiigo/v1/conversations` and `GET /amiigo/v1/conversations/{conversation_id}/messages` |
| Where history comes from | Chats are already saved permanently on the chat server | Same store, read back for the rider whose token it is |

Not built yet. We build it next on staging and tell you when it is live. Until then, anything here can still change; once it ships, v1 only gains things (see "Versioning"). This contract sits beside the main chat contract, `docs/contracts/amiigo-support-chat.md`, and uses the same base URLs, error style and versioning.

## Base URLs

| Environment | Base URL |
| --- | --- |
| Staging | `https://ai-release-stage.emotorad.com` |
| Production | Not decided |
| A developer laptop | `http://localhost:8000` (test data only) |

## Authentication

Every request carries the rider's normal Amiigo access token:

```
Authorization: Bearer <Amiigo access token>
```

- The chat server checks the token's signature with Amiigo's public key, checks it has not expired, and reads the rider's verified phone number from it.
- Only an **access** token is accepted. A refresh token, an OTP token or any other token type is refused with `401 token_type_not_allowed`.
- Never put the token in a URL or a query string. The server never stores it.
- A rider's history is every chat in which **this phone number** was verified (see "Which chats are returned").

## Which chats are returned

- Chats in the Amiigo app made with this rider's token, and chats on the EMotorad website where the rider proved this number with a one-time code. Each chat says where it happened in `channel`: `amiigo_app` or `website_chat` (later also `whatsapp`).
- To show only chats from the app, pass `channel=amiigo_app`.
- Not returned: a website chat where this number was never verified, and anything deleted through "Delete my conversation data".
- History starts with the first chat sent with a real Amiigo token, which becomes possible when this ships (see "Related change").

## GET /amiigo/v1/conversations

The rider's chats, most recent activity first.

**Query parameters**

| Name | Type | Default | Meaning |
| --- | --- | --- | --- |
| `limit` | integer, 1 to 50 | 20 | Chats per page |
| `cursor` | string | none | `next_cursor` from the previous page. Leave out for the first page. |
| `channel` | `amiigo_app` or `website_chat` | all | Only chats from that place |

**Response 200**

```json
{
  "conversations": [
    {
      "conversation_id": "9b1f0c6e-2d4a-4c51-9a0e-3f7d2b8e41c2",
      "channel": "amiigo_app",
      "title": "Battery charges very slowly",
      "started_at": "2026-10-05T09:12:44Z",
      "last_message_at": "2026-10-05T09:31:02Z",
      "bike": { "product_name": "EMX Plus", "frame_number": "EMXP2025004417" },
      "status": "handed_to_support",
      "ticket_reference": "EM-1000042",
      "message_count": 14,
      "can_continue": true
    }
  ],
  "next_cursor": "c2Vjb25kLXBhZ2U"
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `conversation_id` | string | The chat's id, the same one `POST /message` returned |
| `channel` | string | `amiigo_app` or `website_chat` |
| `title` | string | What the chat was about, written by the server: the title of the problem the bot worked through, for example "Battery charges very slowly" or "Range has dropped", or a general one: "Battery issue", "Motor issue", "Warranty registration" or "General question". Show as it is. |
| `started_at`, `last_message_at` | string | ISO 8601 times in UTC |
| `bike` | object or `null` | The bike the chat was about. `null` when no bike was chosen. |
| `status` | string | `open`, or `handed_to_support` when the chat was passed to EMotorad's support team |
| `ticket_reference` | string or `null` | The support ticket's `EM-` reference, when one was raised |
| `message_count` | integer | Messages in the chat, rider's and bot's together |
| `can_continue` | boolean | `true` when the last message is under 48 hours old, so the chat can be carried on (see "Continuing a chat") |
| `next_cursor` | string or `null` | Pass as `cursor` for the next page. `null` on the last page. |

An empty history is `{"conversations": [], "next_cursor": null}`.

## GET /amiigo/v1/conversations/{conversation_id}/messages

The messages of one chat. The first call returns the newest messages; scroll back for older ones with `before`.

**Query parameters**

| Name | Type | Default | Meaning |
| --- | --- | --- | --- |
| `limit` | integer, 1 to 100 | 50 | Messages per page |
| `before` | string | none | `older_cursor` from the previous page. Leave out for the newest messages. |

**Response 200**

```json
{
  "conversation_id": "9b1f0c6e-2d4a-4c51-9a0e-3f7d2b8e41c2",
  "messages": [
    {
      "id": "9b1f0c6e-2d4a-4c51-9a0e-3f7d2b8e41c2#00001",
      "sender": "rider",
      "text": "My battery is not charging",
      "sent_at": "2026-10-05T09:12:44Z",
      "attachments": []
    },
    {
      "id": "9b1f0c6e-2d4a-4c51-9a0e-3f7d2b8e41c2#00002",
      "sender": "bot",
      "text": "Hi, I'm EMotorad's virtual assistant, an AI, not a person. Is the battery switched on? ...",
      "sent_at": "2026-10-05T09:12:51Z",
      "attachments": [
        {
          "kind": "image",
          "url": "https://emotorad-ai-stage-media.s3.ap-south-1.amazonaws.com/...",
          "url_expires_at": "2026-10-06T10:15:00Z"
        }
      ]
    }
  ],
  "older_cursor": null
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `messages` | array | Oldest first within the page |
| `id` | string | Stable id of the message. Use it to de-duplicate when pages overlap. |
| `sender` | string | `rider` or `bot` |
| `text` | string | As it was sent. See "What is masked". `""` for a message that was only a photo or video. |
| `sent_at` | string | ISO 8601 time in UTC |
| `attachments` | array | Photos and videos in that message, in order |
| `attachments[].kind` | string | `image` or `video` |
| `attachments[].url` | string or `null` | A temporary link to the file. `null` when the file cannot be shown (a photo sent while photo storage was unavailable, so it was not kept, or one that has been deleted): show a placeholder such as "Photo". |
| `attachments[].url_expires_at` | string or `null` | When the link stops working, about 15 minutes after the response. Fetch the page again for fresh links. |
| `older_cursor` | string or `null` | Pass as `before` to load older messages. `null` when there are none. |

**What is masked.** Text comes back as the server stored it, which is not always exactly what the rider typed:

- Phone numbers and email addresses typed in a message show as `[phone]` and `[email]`.
- A one-time code or an order number typed while signing in on the website shows as `[code]` or `[order number]`.
- The bot's first reply in a chat starts with the line saying it is EMotorad's virtual assistant, as the rider saw it.

Replies written by a person on the support team are not part of the history yet.

## Continuing a chat

History is for reading. To carry on a chat, send the next message to `POST /message` (later `POST /amiigo/v1/message`) with the same `conversation_id`, and only while `can_continue` is `true`. After 48 hours of silence the bot starts that chat afresh anyway, so offer "Start a new chat" instead.

## Errors

Errors come back as an HTTP status with `{"detail": "<code>"}`. On these two endpoints `detail` is a fixed code the app can check (a `422` carries a list of field problems instead).

| Status | `detail` | When | What the app should do |
| --- | --- | --- | --- |
| 401 | `token_missing` | No `Authorization` header | Send the access token |
| 401 | `token_expired` | The access token has expired | Refresh it with Amiigo and retry once |
| 401 | `token_invalid` | The token's signature does not check out, or it cannot be read | Ask the rider to sign in again |
| 401 | `token_type_not_allowed` | A refresh, OTP or other non-access token | Send the access token |
| 400 | `cursor_invalid` | A `cursor` or `before` the server did not issue | Load the first page again |
| 404 | `conversation_not_found` | The chat does not exist, or it is not this rider's. The server does not say which. | Drop it from the list |
| 422 | (field list) | `limit` out of range, or an unknown `channel` | A bug in the app: log it |
| 429 | `rate_limited` | More than 60 history requests a minute for one rider | Wait a few seconds, then retry |
| 503 | `history_unavailable` | Chat storage is briefly unavailable | Retry after a few seconds |

Retrying any of these requests is safe: they only read.

## Caching and privacy

- Responses carry `Cache-Control: no-store`.
- Keep history in memory for the screen that shows it. Do not write it to disk, and clear anything held when the rider signs out or switches account.
- Photo and video links are temporary and work for anyone holding them: do not log or share them.

## Related change: the real token works in the chat too

The same token check replaces the test-session table on the chat server. When this ships, the existing endpoints accept a real Amiigo access token where they take `session_token` today:

- `POST /message`: `"session_token": "<Amiigo access token>"` in the body identifies the rider, so their chats are saved under their number and appear in history, with `channel: "amiigo_app"`. (Today every chat through `POST /message` is recorded as `website_chat`; chats identified by an Amiigo token are recorded as `amiigo_app` from this release.)
- `POST /erasure-requests`, `/status` and `/cancel`: the same.

`sess-amiigo-test` keeps working on staging for prototyping, but it has no history: the history endpoints need a real token in the header.

## Testing on staging

1. Sign in to the Amiigo app on its staging environment with a test number, and take the access token it gets.
2. Send a few messages to `POST /message` with that token as `session_token`.
3. Call `GET /amiigo/v1/conversations` with the same token in the `Authorization` header. The chat appears, with `channel: "amiigo_app"`.

Use test numbers only, never a real rider's.

## Versioning

The version is in the path, `/amiigo/v1/`. Within v1, changes only add things: new optional fields, new `channel` values, new `attachments` kinds. Removing or renaming a field goes to `/v2/`, announced in advance. Ignore fields and values you do not know.

## Open questions

| Question | Who answers | Our proposal |
| --- | --- | --- |
| Should the app show website chats? | App team | They are returned with `channel`; filter with `channel=amiigo_app` if not |
| When a rider changes their number, should history follow the account? | Both | Not in v1: history belongs to the number in the token |
| Should replies from the support team appear in history? | Support and product | Later, with the Zoho two-way chat work |
| Are the page sizes right for the app's screens? | App team | 20 chats and 50 messages a page |
