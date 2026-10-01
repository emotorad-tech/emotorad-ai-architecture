# Photo safety: design

Date: 1 October 2026. Branch: `feat/photo-safety`, cut from
`feat/integrate-jev-mongo` (015dd60).

## Why

Staging, 1 October 2026: a rider sent a photo of a swelling, smoking battery.
The narrow agent replied with the right stop instructions and "I'm raising an
urgent support ticket for you right now", but raised none in that turn; the
event log shows the ticket only on the rider's next message, by chance. The
safety gate scans typed text and video descriptions, and nothing described a
photo before it ran, so only the model saw the hazard.

The person chose both fixes (1 October 2026):

- **B. Every photo is checked** before the safety gate, as videos are.
- **A. A backstop** after every agent reply: a safety reply with no ticket
  raises the safety ticket itself; any other unbacked ticket claim is replaced
  by a hand-over to a person.

## Part 1: the photo check

### `src/emotorad_ai/photo_check.py` (new)

- `OpenRouterPhotoChecker(transport, model=OPENROUTER_PHOTO_MODEL, zdr=True)`
  with `check(data: bytes, mime: str) -> str` and `provider = "openrouter"`.
  Same route as the video summariser: Gemini Flash through OpenRouter
  (`google/gemini-3.8-flash`), zero data retention and data collection
  denied, the image sent inline as a data URL, a 15-second timeout.
- The prompt asks for 2 to 4 plain sentences on what is visible of the bike,
  battery, charger or wiring, and requires a statement for each of: smoke,
  flames, scorch or burn marks, swelling or bulging, melting, leaking fluid,
  sparks. Each one not visible is written as "no <thing> visible", so the
  safety scan's negation handling reads it as absent.
- Errors raise `PhotoCheckError` with a stable code only (never the response,
  which can echo the request): `too_large` above the inline limit, the
  OpenRouter error code, or `empty`.
- `photo_checker_from_env(environ=None)` returns a checker when
  `OPENROUTER_API_KEY` is set, otherwise `None` (offline and local runs keep
  today's behaviour).

### Where it runs (`src/emotorad_ai/api.py`, `post_message`)

For every image attachment, both ways it arrives:

- **Inline** (a data URL in the message): the bytes are already in hand when
  the photo is stored to S3.
- **Uploaded** (`upload_id`, kind `images`): the bytes are read from the media
  store, as `_summarise_video` does for a clip.

`PHOTO_CHECKER.check(data, mime)` runs, and the text becomes the attachment's
`summary`. At most 3 attachments per message, checked one after another.

- The runtime's safety gate already scans every attachment's `summary` with
  the negation-aware `check_safety_in_description`; a hazard now gets the
  standard safety reply and the priority safety ticket at once, whatever the
  model would have said.
- The safety ticket's description, which today says "Seen in the customer's
  video", says "Seen in the customer's photo or video".
- What the model sees is unchanged: `content_blocks` sends an image as an
  image whether or not it has a summary (only a video is replaced by its
  text).
- A failure or a timeout leaves the photo without a summary, as today, and is
  logged as `photo_check_skipped` with the error class. The photo still goes
  to the model, and the backstop (Part 2) still applies.
- Each check is logged as `photo_check` with the conversation id, the
  description's length and the description (as `video_summary` is logged).
- `/health` gains `"photo_check": "openrouter"`, or `"off"` without a checker.

## Part 2: the backstop

In `Runtime._run`, the one place every agent's reply gets its post-checks,
after the existing coverage, order and evidence checks, using this turn's
`turn.tool_calls`. "A ticket was raised this turn" means a
`create_support_ticket` call whose result is not an error.

### Safety replies (`guardrails.gives_safety_stop`)

A reply is a safety reply when both hold:

1. It names a hazard: `check_safety(reply)` matches (smoke, swelling, fire,
   burning smell, overheating, leak, physical damage, sparks, and the motor
   terms).
2. It gives an unconditional stop instruction: a sentence containing "stop
   using", "stop charging", "stop riding", "do not charge", "move it
   outside/outdoors", or "away from anything flammable / from people", where
   the sentence does not begin with, and does not have before the instruction,
   a conditional ("if", "in case", "should", "whenever", "when", "unless").
   "If you ever see smoke, stop charging it" is advice, not a safety reply.

When a safety reply comes with no ticket raised this turn, and the customer's
phone is known:

- The runtime raises the safety ticket exactly as the safety gate does
  (`battery_safety`, `critical`, the same idempotency key, so at most one per
  conversation run; the chosen bike as the conversation's choice). The code
  shared with the safety gate moves into one method,
  `_raise_safety_ticket(message, state, resolved, description) -> Optional[str]`.
- The description says the model gave safety instructions without raising a
  ticket, and includes the customer's message and the reply.
- The reply gets "I have raised this as a priority safety case, reference
  EM-…" appended, unless it already names that reference; it is marked
  escalated, with the ticket id.
- Logged as `safety_backstop` with the matched hazard terms.

### Other unbacked ticket claims (`guardrails.claims_ticket`)

A reply claims a ticket when it says one was or is being raised: "I've / I
have raised|opened|created|logged … ticket", "I'm / I am raising|opening|
creating|logging … ticket", "ticket … has been raised|created|opened", or "I'll
raise … ticket now / right away / immediately". An offer ("I can raise a
ticket", "shall I raise a ticket?", "would you like me to raise a ticket")
is not a claim.

When a reply claims a ticket, is not a safety reply, and no ticket was raised
this turn:

- The reply is replaced by the hand-over to a person (`HANDOVER_TEXT`, plus
  anything the turn already did), as the coverage and order checks do when
  they block a reply. The customer is never told of a ticket that does not
  exist.
- Logged as `ticket_promise_unbacked` with the suppressed text, and an
  escalation with that reason.

## Errors

Nothing here may stop a reply: a photo check that fails leaves the photo as
today; a backstop ticket that fails to be raised leaves the reply as the
model wrote it, logged as `safety_backstop_failed` with the error class.

## Testing

- Photo checker: the request (model, inline data URL, ZDR, the prompt);
  `too_large`; an OpenRouter error gives its code only; an empty answer; no key
  gives no checker.
- API: an inline photo and an uploaded photo each get the checker's text as
  `summary`; a hazard description ("The battery casing is swollen and white
  smoke rises from it") gives `guardrail:battery_safety` and a ticket on that
  very turn; "no smoke visible, no swelling visible" does not; a checker that
  raises leaves the photo without a summary, logs `photo_check_skipped`, and
  the reply still arrives; `/health` shows `photo_check`.
- `gives_safety_stop`: the staging reply is one; "If you ever see smoke, stop
  charging it." is not; "Stop using the throttle for a minute and try again."
  is not (no hazard); "When it cools, stop charging at 80%." is not.
- `claims_ticket`: "I'm raising an urgent support ticket for you right now",
  "Ticket EM-00001 has been raised", "I've opened a ticket" are; "I can raise a
  ticket if you'd like", "Shall I raise a ticket?" are not.
- Runtime: a scripted model that gives the staging reply with no tool call gets
  a ticket on that turn and the reference appended; with the ticket tool called
  in the turn, no second ticket; a non-safety claim with no call is replaced by
  the hand-over; an offer is left alone; a failing ticket call leaves the reply
  and logs `safety_backstop_failed`.

## Out of scope

Describing photos to the model; checking photos already in a conversation's
history; a separate photo model or prompt per agent; non-image documents.
