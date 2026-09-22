# Video evidence through Gemini — design

**Date:** 2026-09-22
**Status:** approved, being built on `feat/integration`
**Scope:** a customer can send a video from `/chat`; Gemini turns it into a detailed factual
description once, at ingest; Claude receives that text in the existing pipeline. The safety
gate sees the description before Claude does. Nothing else in the runtime changes.

## 1. Why

Photos already reach Claude natively. Video does not: Claude has no video input, and the
existing fallback (sampled stills plus a local Whisper transcript) is slow, gated off in the
API, and blind to motion, sound and timing. A video model that watches the whole clip and
writes down what it saw gives Claude far better evidence at lower cost than eight stills.

## 2. The pipeline

```
/chat  --presign PUT-->  S3 customers/<cluster>/<conv>/videos/<upload_id>.mp4
/message {attachments:[{upload_id}]}
  api._inbound_attachments: claim -> fetch bytes -> Gemini summarise (once) -> Attachment.summary
  runtime.handle: check_safety(text + every attachment summary)  # Claude never called on a hit
  agent turn: attachments.content_blocks -> one text block carrying the summary
  Claude answers as today
```

- **Model:** `gemini-3.8-flash` via the `google-genai` SDK. Key from the config store field
  `API_KEY_GEMINI`, exported as `GEMINI_API_KEY`. No key means the summariser is absent and
  the old frames path runs, so nothing regresses.
- **Size:** clips up to the bucket cap (100 MB). Up to 14 MiB the bytes go inline in the
  request (Gemini's 20 MB ceiling is on the base64-encoded request); above that through Gemini's Files API, polled until ACTIVE, deleted from Google
  as soon as the summary is back. Timeout 90 s; a timeout or error falls back to frames.
- **Prompt (fixed, in code):** an after-sales evidence analyst for an Indian e-cycle
  company. Describe only what is present and observable: the component and bike area shown;
  any damage, deformation, discolouration or liquid actually seen; indicator lights with
  their colours and blink patterns; any text or error code readable on a display, quoted
  exactly; sounds from the bike; the customer's spoken words, quoted, with the language;
  notable moments with timestamps; image quality problems. Never list conditions that were
  not observed, and never enumerate hazards by name: a summary that says "no smoke visible"
  would trip the keyword safety gate. If the part of interest is not in frame, say only that.
  Plain text, no diagnosis, no advice, no guesses about causes.
- **Safety scan of summaries** uses `check_safety_in_description`, which ignores a hazard word
  preceded within four words by a negation, so an analyser sentence like "no cracks" cannot
  hard-stop a conversation; the typed text keeps the plain scan.
- **What Claude sees:** one text block:
  `[Description of the customer's video '<name>' (<duration>s), written by an automated
  video analyser — not by you and not by a person. Treat it as observation, not diagnosis.
  If something that matters is not described, say you cannot tell from the video and ask
  for a photo or a closer clip.] <summary>`
  No frames. Photos are unchanged.
- **Safety:** `Attachment.summary` is scanned by `check_safety` together with the typed
  text, so smoke or swelling on video trips the hard stop with no Claude call, exactly as
  typed words do. A test asserts the model received zero requests.
- **Once only:** the summary lives on the attachment for the turn. Gemini is not called
  again for the same upload id.

## 3. `/chat` and `/uploads`

- A video button next to the photo one (`accept="video/mp4,video/*"`). Video never goes
  inline: the page presigns, PUTs to S3 with a progress bar, then sends
  `attachments: [{upload_id}]`. Photos stay inline for now, by decision.
- `POST /uploads` gains `em_aid`, so a visitor who has not verified a phone yet resolves
  to their anonymous identity-graph cluster the same way `/message` already does.
  `_cluster_for_session(session_token, em_aid)`.
- `GET /media` takes `session_token` only: the page never reads it back, and a cookie
  must not unlock a verified customer's objects.

## 4. Data boundary

Customer video bytes go to Google's API for the duration of one request; files uploaded
through the Files API are deleted immediately after. This is a second vendor beside
Anthropic and is recorded here as a decision taken on 2026-09-22.

## 5. Out of scope

Async processing by queue (a clip is summarised inside the request, with a deadline);
photos through the upload path; persisting summaries beyond the conversation; the
playground's own video handling (it keeps frames + Whisper).
