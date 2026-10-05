# Amiigo Support Chat API Contract

Sep 30, 2026 · @Sagnik Mukherjee

## Status and scope

The support chat is built and tested for the website; the Amiigo endpoint and the Amiigo token check are not built yet. Zoho ticketing is built and sends to a test department on staging only, until engineering signs off real tickets. Build the app against this contract; anything marked **Proposed** can still change before the endpoint ships, and we will tell you before it does.

| Part | Today | Planned |
| --- | --- | --- |
| Chat engine (routing, answers, safety check, one step per reply) | Built, website only | Same engine serves the app |
| Endpoint for the app | Not built | **Proposed:** `POST /amiigo/v1/message` |
| Who the rider is | Test session `sess-amiigo-test` only | **Proposed:** the rider's Amiigo access token, checked by the bot |
| Photos and videos | Built: photos stored in S3; videos by presigned upload | Same |
| Conversations and memory | Built, in MongoDB | Same |
| Support tickets | Recorded with our own `EM-` reference and sent to a Zoho Desk test department on staging | Zoho Desk's real department, after sign-off (see "What changes with the Zoho integration") |
| Bike, warranty and service data | Test data | OMS warranty data, and the rider's Amiigo bikes, rides and service history |

Staging gets this build once the current branch is merged and deployed; the chat team will say when.

The app must not go to real riders until two things are done: engineering signs off sending chat text to our model provider (it runs outside AWS today), and the Amiigo token work is finished and reviewed.

## How the app connects

The app sends each rider message to one HTTPS endpoint and gets the bot's reply in the same response. There are no websockets or push messages yet.

| Environment | Base URL | Notes |
| --- | --- | --- |
| Staging | `https://ai-release-stage.emotorad.com` | The host that already serves the web chat |
| Production | Not decided | Not before the sign-offs in "Status and scope" |
| A developer laptop | `http://localhost:8000` | Started with `python scripts/chat_local.py`; test data only |

**Authentication (Proposed).** Every request carries the rider's normal Amiigo access token, the one the app already sends to the Amiigo backend:

```
Authorization: Bearer <Amiigo access token>
```

- The bot checks the token itself with Amiigo's public key, and reads the rider's verified phone from it. There is no extra login or code step in the app.
- Send the current token on every request. The bot never stores it.
- Only an access token is accepted, not a refresh token or an OTP token.

**What the bot knows from the token.** From the rider's phone it looks up their bikes, warranty and, later, rides and service history. So the bot never asks a signed-in rider for their bike model, frame number or purchase date. A rider with several bikes is asked which one the chat is about.

**Until the endpoint exists.** Prototype against the live `POST /message` with `"session_token": "sess-amiigo-test"` in the body instead of the header. That is a test rider (Kabir Sharma, two bikes: an EMX Plus bought February 2026 and a Doodle V3 bought May 2023). The request and response shapes below are the same.

## POST /amiigo/v1/message (Proposed)

One call per rider message: the app sends what the rider typed or picked, and the response is the bot's whole reply. Allow up to 60 seconds for a reply (150 seconds when the message carries a video); most arrive in 2 to 11 seconds.

**Request** (`Content-Type: application/json`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `conversation_id` | string | No | Leave out on a chat's first message. Send back the one the server returned on every later message. |
| `text` | string | Yes | What the rider typed. `""` when they send only a photo or tap a chip. |
| `attachments` | array, up to 3 | No | Photos inline, or an `upload_id` for a video (see "Photos and videos"). |
| `screen` | string | No | The app screen the chat was opened from, for example `battery_health`. It helps the bot guess the topic. |
| `pill` | string | No | A quick-reply chip the rider tapped. Today only `battery` and `motor` are handled. |
| `location` | object | No | `{"latitude": 18.52, "longitude": 73.85}`, sent only after the rider taps "Share my location". The bot turns it into a pincode and keeps no coordinates. |

```json
{
  "conversation_id": "3f2c9a1e-5b7d-4e8a-9c21-7d4e5f6a8b90",
  "text": "my battery isn't charging",
  "screen": "battery_health",
  "attachments": [{"kind": "image", "url": "data:image/jpeg;base64,/9j/4AAQ..."}]
}
```

**Response 200**

| Field | Type | Meaning |
| --- | --- | --- |
| `conversation_id` | string | Keep it for the next message in this chat. |
| `text` | string | The reply to show, with light formatting (see "Rendering a reply"). |
| `escalated` | boolean | `true` when the bot handed the chat to a person, for example for a safety issue. |
| `ticket_id` | string or null | Set in the reply that raised a ticket. `null` on every other reply. |
| `handled_by` | string | Which part of the bot answered. For logs and support, not for UI logic. |
| `attachments` | array | Guide pictures or clips the bot chose to show. |
| `actions` | array | Buttons the bot asks the app to show, for example `request_location`. |

```json
{
  "conversation_id": "3f2c9a1e-5b7d-4e8a-9c21-7d4e5f6a8b90",
  "text": "Thanks for the photo. Is the key on the battery turned to **ON**?",
  "escalated": false,
  "ticket_id": null,
  "handled_by": "narrow_support",
  "attachments": [
    {"kind": "image", "url": "https://...signed link...", "mime_type": "image/png", "caption": "The battery On/Off switch", "poster": null}
  ],
  "actions": []
}
```

Ignore any field you do not recognise: new optional fields will be added without warning (see "Open questions and versioning").

## Photos and videos

Send photos inside the message; send videos by uploading them to S3 first and then sending the upload's id. The server keeps both permanently in the EMotorad media bucket, with a record of where each one is stored. A photo or video counts as the evidence the bot needs before it will raise a fault ticket. The bot asks for a short video first, and a photo if the rider can't take one.

**Photos: inline in the message.** Shrink the photo on the phone before sending, as the web chat does: at most 1280 px on the long edge, JPEG at quality 0.8 (usually 150 to 300 KB).

```json
{"kind": "image", "url": "data:image/jpeg;base64,<the JPEG bytes, base64>"}
```

| Limit | Value |
| --- | --- |
| Types | `image/jpeg`, `image/png`, `image/webp` |
| Size | 4 MB per photo, after decoding |
| Count | 3 attachments per message, photos and videos together |
| Form | A `data:` URL only; a web link is refused |

**Videos: upload first, then send the id.**

1. Ask for an upload slot: `POST /uploads` with `{"tree": "customers", "conversation_id": "...", "mime_type": "video/mp4", "size_bytes": 18350081}`. For a chat's first message, make up the `conversation_id` yourself (a UUID) and use the same one in the message.
2. The response is `{"upload_id": "upl_...", "key": "...", "url": "...", "headers": {...}, "expires_in": 300}`. Send an HTTP `PUT` of the file's bytes to `url` with exactly those `headers`, within 5 minutes. Show upload progress; a 100 MB clip takes a while on mobile data.
3. Send the message with `{"upload_id": "upl_..."}` in `attachments`, within an hour of the upload.
4. The server has a video model describe the clip before the bot answers. That can take a minute or two, so show a "looking at your video" state rather than a spinner alone.

| Limit | Value |
| --- | --- |
| Video types | `video/mp4`, `video/quicktime` (.mov), `video/3gpp` (.3gp) |
| Video size | 100 MB |
| Photo size by upload | 10 MB, for a photo too big to send inline |
| Upload slots | 20 a minute from one address |

The rider's Amiigo token goes on `POST /uploads` as well (Proposed). A presigned `PUT` goes straight to S3 and carries no token. A native app is not subject to the browser CORS rule that blocks video uploads from a local web page.

## Rendering a reply

Show `text` as one chat bubble, then any `attachments`, then any `actions`; use `escalated` and `ticket_id` for the chat's state. Replies are short by design: one step or one question at a time, so plan for many small bubbles rather than long ones.

| Part | What to show | Notes |
| --- | --- | --- |
| `text` | The bubble | Formatting used: `**bold**`, `*italic*`, `` `code` ``, bullet lines starting ` -  `, numbered lines ` 1.  `, and line breaks. Links and headings are not used: show any `[...]` or `#` as plain text. Escape the text before formatting it. |
| First reply of a chat | As sent | It opens with a line saying the bot is an AI, not a person. That line is required; keep it. |
| Languages | As sent | The bot answers in the rider's language: English, Hindi (Devanagari script) or Hinglish. Fonts must render Devanagari. |
| `attachments[]` | An image or a video player under the bubble | `kind` is `image` or `video`. `url` is a signed link valid for 15 minutes: load it when the reply arrives and keep the image itself, not the link. Use `caption` as the caption and alt text, and `poster` as a video's still. |
| `actions[]` | A button under the bubble | Today only `{"kind": "request_location", "label": "Share my location"}`. On tap, ask for location permission and send the next message with `location`. If the rider refuses, let them type their pincode instead. Ignore any `kind` you do not know. |
| `escalated: true` | A clear "handed to our team" state | The bot has stopped and a person will contact the rider. The rider can still write. For a safety report (smoke, swelling, heat) this arrives with stop-using instructions: show them prominently. |
| `ticket_id` | A reference chip the rider can copy | Keep it with the chat. It appears only on the reply that raised the ticket; the bot quotes it again if asked. |
| `handled_by` | Nothing | Log it with the message for support. Do not build UI on its values: they change as the bot changes. |

The server sends no quick-reply chips of its own. Chips the app offers at the start (for example Battery, Motor) are sent back as `pill`.

## Conversation lifecycle

A chat is one `conversation_id`: the app keeps it and sends it with every message until the rider starts a new chat. The server keeps the working copy for 48 hours after the last message and the transcript permanently.

- **Starting a chat.** Leave `conversation_id` out; the reply carries a new one. To upload a video before the first message, generate a UUID and use it for both calls.
- **Continuing.** Send the same `conversation_id`. The bot reads the last 12 exchanges of that chat.
- **One message at a time.** Disable Send until the reply arrives. A second message sent meanwhile may get a short "one moment" reply (`handled_by: "conversation_busy"`) instead of an answer.
- **After 48 hours of silence.** The same `conversation_id` still works, but the bot starts that chat afresh. Offer the rider a new chat instead.
- **Memory across chats.** For a signed-in rider, the bot sees a one-line summary of their last 3 chats (topic, bike, outcome, ticket) and can say "last time we spoke about…".
- **Showing past messages.** The server has no endpoint that returns a chat's history to the app yet. Keep the chat on the device to show it again.

## Deleting conversation data

The app's "Delete my conversation data" button lets a signed-in rider ask for
everything the support chat holds about them to be deleted: their past chats,
the photos and videos they sent, and the record of where they chatted from. It
does not delete their warranty registration, orders, invoices or service
tickets. Our team checks each request and deletes the data within 30 days. A
rider can also ask in the chat ("delete my data"); both make the same request,
and a rider has at most one pending request.

**The dialog.** Show it before calling anything.

- Title: "Delete my conversation data?"
- Body: "This deletes everything this chat holds about you: your past chats with me, the photos and videos you sent, and the record of where you chatted from. It does not delete your warranty registration, orders, invoices or service tickets, which EMotorad keeps for your warranty and by law. It's done within 30 days."
- Buttons: "Delete" (calls `POST /erasure-requests` with `"confirm": true`) and "Keep my data" (closes the dialog).

**After a request**, show the reference and a "Cancel deletion" action until the request is done. `POST /erasure-requests/status` tells you whether one is pending.

| Endpoint | Body | Answer |
| --- | --- | --- |
| `POST /erasure-requests` | `{"session_token": "...", "confirm": true, "conversation_id": "..."}` (`conversation_id` optional) | 201 `{"reference": "DEL-7K3P9Q", "status": "pending", "text": "..."}`; 200 with the same shape when one was already pending; 400 without `"confirm": true` |
| `POST /erasure-requests/status` | `{"session_token": "..."}` | 200 `{"reference": "DEL-7K3P9Q", "status": "pending", "requested_at": "..."}`, or `{"reference": null, "status": "none"}` |
| `POST /erasure-requests/cancel` | `{"session_token": "..."}` | 200 `{"reference": "DEL-7K3P9Q", "status": "cancelled", "text": "..."}`; 404 when nothing is pending |

Show `text` to the rider as it comes. Other answers: 403 `{"detail": "Sign in to the app to delete your data."}` for a session that is not a signed-in rider; 429 over the message rate limit; 503 when the request could not be recorded (`detail` says to try again in a few minutes).

**Until the Amiigo token is wired**, send `session_token` in the body, as for `POST /message` (`sess-amiigo-test` on staging). The `/amiigo/v1/...` versions of these paths will take the rider's Amiigo token in the `Authorization` header instead; the bodies and answers stay the same. The token is never put in a URL.

## Errors, limits and retries

An error comes back as an HTTP status with `{"detail": "<reason>"}` (a `422` carries a list of field problems instead). A problem inside the bot itself, such as the AI model being down, is not an error: it arrives as a normal `200` reply that hands the chat to a person.

| Status | When | What the app should do |
| --- | --- | --- |
| 400 | A bad attachment (more than 3, not valid base64, over 4 MB, a web link instead of data), or a missing `conversation_id` on an upload | Tell the rider the photo could not be sent; do not resend it unchanged |
| 401 (Proposed) | The Amiigo token is missing, expired or not an access token | Refresh the token and retry once; if it fails again, ask the rider to sign in |
| 403 | An `upload_id` or `conversation_id` that belongs to another rider | Start the upload again in this chat |
| 404 | An `upload_id` that is unknown or older than an hour | Upload the file again |
| 409 | The upload has not finished, or the file does not match what was requested | Finish the `PUT`, or upload again |
| 413 | A file over its size limit | Tell the rider the limit; shrink photos before sending |
| 415 | A file type that is not accepted | Tell the rider which types work |
| 422 | The request body is malformed (for example no `text`) | A bug in the app: log it |
| 429 | More than 20 messages, or 20 upload slots, a minute from one address | Wait a few seconds, then retry |
| 503 | Storage for photos or chats is briefly unavailable | Retry after a few seconds; a photo can be sent inline instead of uploaded |

**Retries.** Do not resend a message automatically after a timeout: the server may already have answered it, and the rider would see the question twice. Show "Not sent, tap to retry" instead. Retrying a `429` or `503` is safe, because the server refused those messages before handling them.

## What changes with the Zoho integration

Tickets move from a test system to Zoho Desk, where the support team works them. The request side of this contract does not change; the response keeps every field and may gain new optional ones. The ticket part is built (5 October 2026): each ticket is recorded with our own reference during the reply, and a background worker sends it to Zoho straight after. It goes to a test department on staging until engineering signs off real tickets. The handover wording comes with the next part.

| Area | Today | After Zoho | What to build now |
| --- | --- | --- | --- |
| `ticket_id` value | Test ids like `EM-00001`, from a counter that restarts with the server | Our own reference: `EM-` and seven digits, from `EM-1000001`. Never the Zoho Desk ticket number; the Zoho ticket carries our reference, so support finds it either way | Treat it as an opaque string: do not parse it or assume a prefix or length |
| Where a ticket goes | A test store nobody works | Zoho Desk, with this chat's transcript, and the rider's photos and videos uploaded to the ticket as files. A file over Zoho's size limit (taken as 20 MB until it is tested) is not attached: the ticket says a photo or video was too large, and the AI team keeps it | Nothing |
| How support finds a chatbot ticket | Not applicable | The ticket's subject starts with `[AI chat]` and ends with our reference in square brackets, for example `[AI chat] Battery: charging - EMX Plus [stage:EM-1000001]`. There is no custom field for it: that prefix is what a Zoho rule or webhook filters on | Nothing |
| When the ticket reaches Zoho | Not applicable | Shortly after the reply. The reply never waits for Zoho, so `ticket_id` is in the reply at once. Later messages in the chat are added to the same ticket | Nothing: show the reference as soon as it arrives |
| After a handover (`escalated: true`) | The bot stops; no one follows up, as it is a test | Support contacts the rider from Zoho; by phone or email is not decided | Say "our team will contact you", with no promised channel or time |
| Ticket status | Not available | **Proposed:** `GET /amiigo/v1/tickets` returns the rider's tickets and their Zoho status, for a "My support requests" screen | Leave room for such a screen; do not build it yet |
| Agent replies inside the chat | Not possible | Undecided: replies in the app would need push notifications | Keep the handed-over state open to later messages from "our team" |
| Response fields | `ticket_id` only | May add an optional `ticket` object (id, status) beside `ticket_id`, which stays | Parse with unknown fields ignored |
| Safety reports | Raised at once as critical | The same, as a top-priority Zoho ticket | Nothing |

## Open questions and versioning

The version is in the path, `/amiigo/v1/`. Within v1, changes only add things: new optional fields, new `actions` kinds, new `attachments` kinds. Anything that removes or renames a field goes to `/v2/`, announced in advance. So the app must ignore unknown fields and unknown kinds.

| Question | Who answers | Our proposal |
| --- | --- | --- |
| Which `screen` names will the app send? | App team | A short fixed list, for example `battery_health`, `bike_home`, `service`, `help` |
| Should a chat opened from one bike's screen name that bike? | Both | An optional `vin` field, so a rider with several bikes is not asked which one |
| Should a message carry a client id, so a retry after a timeout is answered once? | Chat team | An optional `client_message_id`; the server answers a repeat with the first reply |
| The rate limit counts messages per network address, and mobile carriers put many riders behind one address. Should the app's limit count per rider? | Chat team | Yes: 20 messages a minute per rider, keyed on the token |
| After a handover, do agent replies come into the app chat? | Support and product | Decided with the Zoho work (see "What changes with Zoho") |
| Should the server return a chat's history? | Both | A `GET` of the transcript, if the app needs history across devices |
| Where do app developers get staging access and a test token? | Chat team | A staging test rider, plus the `sess-amiigo-test` session until tokens are wired |
