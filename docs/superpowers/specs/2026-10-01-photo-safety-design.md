# Photo safety: design

Date: 1 October 2026, revised the same day after the final review. Branch:
`feat/photo-safety`, cut from `feat/integrate-jev-mongo` (015dd60).

## Why

Staging, 1 October 2026: a rider sent a photo of a swelling, smoking battery.
The narrow agent replied with the right stop instructions and "I'm raising an
urgent support ticket for you right now", but raised none in that turn; the
event log shows the ticket only on the rider's next message, by chance. The
safety gate scans typed text and video descriptions, and nothing described a
photo before it ran, so only the model saw the hazard.

The person chose both fixes (1 October 2026), and after the final review chose
the redesign below:

- **B. Every photo is checked** before the safety gate, as videos are.
- **A. A backstop** for a reply that claims a ticket that does not exist.

### What the review changed, and why

The first version asked the photo model to name every hazard and say it was
absent, and its backstop acted on any reply that gave stop instructions about
a hazard. The review showed both would raise false critical tickets:

- A prose description that lists absent hazards ("There are no signs of smoke,
  flames, swelling...") trips the gate. This repo made the same mistake with
  video descriptions and fixed it on 22 September (the video prompt "never
  enumerates hazards to say they are absent").
- Ordinary advice ("Stop charging it if you ever see smoke", "Stop charging at
  80%") read as a safety reply.

So: the photo check answers yes or no per live hazard in a fixed format and the
description is written by code, only when a hazard is present; the backstop
acts only on ticket claims, never on advice.

## Part 1: the photo check

### `src/emotorad_ai/photo_check.py`

- `OpenRouterPhotoChecker(transport, model=OPENROUTER_PHOTO_MODEL, zdr=True)`,
  `provider = "openrouter"`, `check(data: bytes, mime: str) -> List[str]`: the
  live hazards in the photo, from `LIVE_HAZARDS`, in that order; `[]` when
  none.
- `LIVE_HAZARDS = ("smoke", "flames", "swelling", "leaking", "sparks")`. Not
  dents, cracks, punctures, scorch marks or melting: those are damage that has
  already happened, which the agents assess (the gate already leaves "melted"
  out for that reason, and the damage flows ask for exactly these photos).
- The model is asked for JSON only (`response_format: json_object`): one
  boolean per live hazard, true only when it is happening in the photo now.
  Same route as the video summariser: `google/gemini-3.8-flash` through
  OpenRouter, zero data retention and data collection denied, the image inline,
  a 15-second request timeout.
- Errors raise `PhotoCheckError` with a stable code only: `too_large`, the
  OpenRouter error code, `bad_json`.
- `photo_checker_from_env(environ=None)`: a checker when `OPENROUTER_API_KEY` is
  set, else `None`.
- `describe(hazards) -> Optional[str]`: the text the safety gate scans,
  written by code: `"The customer's photo shows smoke and swelling."` for
  `["smoke", "swelling"]` ("leaking" is written "leaking fluid"); `None` for
  `[]`. A photo with no live hazard therefore gets no description and cannot
  trip anything.

### Where it runs (`src/emotorad_ai/api.py`)

Every image attachment, inline or uploaded, is checked. All the photos in one
message are checked at the same time, under one overall deadline of 15
seconds (`PHOTO_CHECK_DEADLINE_SECONDS`): the reply is never held longer than
that, however many photos there are. A check still running at the deadline is
logged as `photo_check_skipped` with error `timeout`; one that fails is logged
with its code (`PhotoCheckError`) or its class.

- `describe(hazards)` becomes the attachment's `summary`, which the runtime's
  safety gate already scans; a hazard gets the standard safety reply and the
  priority safety ticket on that turn.
- What the model sees is unchanged: an image is sent as an image.
- The safety ticket's description says "Seen in the customer's photo or video".
- With `DEV_CODES` on, each check is logged as `photo_check` with the hazards
  found (customer content stays off production logs, as for video summaries).
- `/health` gains `"photo_check": "openrouter"`, or `"off"`.

### The safety turn keeps the photo

When the gate fires on a photo, the photo stays in the conversation history as
an image (the safety branch fetches stored photos, as agent turns do), so the
next turn still has it and the evidence rule sees it.

## Part 2: the backstop for ticket claims

In `Runtime._run`, after the existing post-checks.

### What counts

- **A ticket raised this turn**: any tool result this turn that carries a
  `ticket_id` (`create_support_ticket`, `raise_intake_ticket`).
- **A claim** (`guardrails.claims_ticket`): a sentence saying a ticket was or
  is being raised ("I've raised / I'm raising / your ticket has been raised /
  I'll raise one right away / your ticket number is"). Not a question, and not
  a sentence with a negation or a condition in it ("No ticket has been raised
  yet", "Once a ticket has been raised, the team calls", "If the light stays
  off, I'll raise a ticket").
- **A known ticket**: the conversation already holds one (`state.ticket_id`),
  or the reply names a reference that appears in the conversation's context
  (earlier conversations' tickets are listed there).

### What happens

A claim with no ticket raised this turn and no known ticket:

- If the reply names a live hazard (the negation-aware
  `check_safety_in_description`, so "no swelling" does not count) and the
  customer's phone is known: the runtime raises the safety ticket itself,
  through the same method as the safety gate, once per conversation run, and
  the reply gets "I have raised this as a priority safety case, reference
  ..." added if it does not already name it. Logged as `safety_backstop`.
- Otherwise, or if that ticket cannot be raised: the reply is replaced by the
  hand-over to a person (`HANDOVER_TEXT`), as the coverage and order checks do.
  The customer is never sent a claim with no ticket behind it. Logged as
  `ticket_promise_unbacked`.

Advice without a claim is never acted on.

## Errors

Nothing here stops a reply: a failed or slow photo check leaves the photo as
today, within the deadline; a backstop that cannot raise its ticket hands over.

## Testing

- Checker: the request (model, inline image, ZDR, JSON mode); hazards read from
  JSON in `LIVE_HAZARDS` order; a false or missing key is no hazard; bad JSON
  and other errors give stable codes; `describe` for none, one and several.
- API: a hazard photo is a safety turn with a ticket; a photo with no hazard is
  an ordinary turn and gets no summary; one hazard among three photos is
  enough; three slow checks finish within the deadline together; a check past
  the deadline is skipped and logged as `timeout`; an uploaded photo is
  checked; an unverified visitor gets the safety reply without a ticket; the
  photo stays in history after the safety turn.
- `claims_ticket`: claims, including after another sentence; not questions,
  negations, conditions or offers.
- Runtime: an unbacked claim about a live hazard raises the safety ticket on
  that turn; a claim after an intake ticket in the same turn is left alone; a
  claim when the conversation holds a ticket is left alone; a reference from
  the context is left alone; advice without a claim never raises a ticket; a
  hazard claim with no phone, or whose ticket cannot be raised, is handed over;
  a later turn never raises a second safety ticket.

## Out of scope

Describing photos to the model; checking photos already in history; non-image
documents; Hindi claim phrases.
