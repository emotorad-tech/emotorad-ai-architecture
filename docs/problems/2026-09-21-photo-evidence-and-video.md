# Problem: the bot judges photos it cannot see properly, and cannot use video at all

Logged 2026-09-21. Owner: Krishna. Status: open, deliberately not solved yet.
Intended for the AI engineer intern joining the platform team. Read `CLAUDE.md`
and `docs/handoff-2026-09-21-fulfilment.md` first; this document assumes both.

## Why this matters

The battery support flow (`knowledge/battery/melted-terminal-or-connector.yaml`)
asks the customer for two photos: the battery terminal, then the controller's
battery connector. What the model says it sees in those photos decides whether
a heat-damaged pack is replaced or the customer is sent away with it. "Intact"
is the quiet failure: nothing escalates, no ticket is raised, and the customer
keeps riding and charging a battery that has had a heat event.

Today the verdict is entirely the model's free reading of one picture. No code
checks whether the picture was good enough to read.

## What we saw (evidence, all from `logs/conversations.jsonl`)

Two photos of the same melted terminal on the owner's X1 C were sent to `/chat`
across six conversations on 20 and 21 September 2026. The right-hand pin's
housing is melted and blackened. Photos are stored nowhere, so which photo went
into which run is inferred from the model's own description, and the match is
unambiguous.

| Conversation | Photo | Model's verdict |
|---|---|---|
| `74fb0a2a`, `b186a5dd` | close-up, terminal fills the frame | melted: "the fourth one on the right has melted, plastic deformed and fused, blackening at that corner". Correct. |
| `3dc7627d`, `9f8ef756`, `0e9e8d36` | wide shot, whole pack on the floor, terminal about 100 x 60 px of a 900 x 1600 frame | intact: "four separate pins, no fused metal or blackened plastic, just some ordinary dust and scuffing". Wrong. |
| `e7073ca9` (21st, replayed by script) | the same wide shot | "the shot is too far back for me to tell"; asked for a close-up. Right, but only by luck. |

So: the model reads the close-up correctly every time, and on the wide shot it
either guesses "intact" or admits it cannot tell, with nothing deciding which.
Whether it admits uncertainty is left to the model, and the record's rule
"judge only what is in the photo, do not guess at damage you cannot see"
(`melted-terminal-or-connector.yaml`, the "Judge only" step) pushes a
low-resolution read towards "intact", not towards "send me a closer one".

Two more things the photo path does not handle:

- **The over-close photo.** Once told "closer", a customer may photograph four
  pins with no housing around them. That could be the battery terminal, the
  controller connector or a charger plug, and the model will name whichever
  part it was just asked about. The record already warns against describing a
  photo you were not sent; the framing makes it easy to do anyway.
- **Video.** `/chat` accepts images only (`attachments.py`,
  `ALLOWED_MEDIA_TYPES`). The playground samples a clip into eight stills and
  transcribes the audio with Whisper (`playground.py`, "video" section), but
  the playground bypasses `runtime.handle()` and needs `ffmpeg`, which the dev
  machine lacks. Nothing in the production loop can take a video. For motor
  noises and intermittent display faults the customer's narration on a clip is
  usually the whole diagnosis.

## What the pipeline does with a photo today

1. The page downscales to 1280 px on the long edge, JPEG quality 0.8
   (`web/emotorad-support-chat-dev.html`, `MAX_EDGE`), and sends it inline as
   base64. Max 3 attachments, 4 MB each (`attachments.py`).
2. `agents/base.py` puts the image block first and the customer's text after
   it in a single `user` turn. Photos are never logged or stored.
3. The model replies. Its description of the photo is the verdict. The only
   post-checks that exist are on coverage claims and on `RO-` order ids, not
   on photo evidence.
4. The conversation goes on. "Intact" leads to the next step of the record;
   "melted" leads to the replacement flow.

## The problem statement

Design and build the piece that decides whether a photo (and later, a video)
is good enough evidence to conclude anything from, and make the flow ask for
a retake when it is not, in a way that works on every channel we serve.

Three sub-problems, in the order we would tackle them:

1. **Adequacy before verdict.** Before the agent may conclude intact or melted
   about an end, something must establish: what part is in the photo (battery
   terminal, controller connector, display, other, unsure), whether the
   framing is usable (too far, usable, too close), and whether the image
   quality permits a read (blurred, dark, ok). The conclusion must be gated on
   that in code, not asked for in the prompt. The model may fill in the
   classification from a fixed set; code decides what it unlocks. A prompt
   instruction alone has already been shown not to hold (see the handoff:
   "the model ignored a wait-a-turn instruction within the hour").
2. **Guidance that produces a usable photo first time**, on WhatsApp as well
   as the website. Ideas already discussed, none decided: ask for a mid-range
   shot showing the part in its housing plus a close-up (inspection apps do
   both because the overview proves what the detail is a detail of); send an
   example of the correct framing alongside the request the way the
   melted-versus-normal comparison is already sent; a camera-led stencil on
   the website and Amiigo app, as in the Cars24 inspection app or video KYC,
   which was judged premature because it does not reach WhatsApp or IVR, needs
   per-model outlines to maintain, and we have no count yet of how often bad
   framing happens.
3. **Video in the production loop.** Sampling frames plus transcribing the
   narration, done so that no clip has to be stored on the server (sampling in
   the browser was the suggested direction), inside the same adequacy gate as
   photos. The playground's implementation is a reference, not a base to copy:
   it drops the model's narration beside tool calls and does not go through
   `runtime.handle()`.

## Constraints the solution must respect

- Guardrails in code, not prompts. The model chooses from a set; it never
  invents a member of it.
- Every channel: WhatsApp (native camera, we get a file), website chat, Amiigo,
  and IVR (no camera at all; the flow must degrade to a handover, not a crash).
- Photos and videos are personal data. Today they are stored nowhere and never
  logged. Keep it that way unless the owner decides otherwise, and if a clip
  must be kept for the human who picks up the ticket, that is a design
  decision to raise, not to make.
- Capture, don't solve. Log the adequacy classification for every photo from
  day one so we can count `too_far`, `too_close`, `blurred`. Those counts are
  what justify (or bury) the stencil work.
- Retrieval and conversation are evaluated separately in this repo
  (`docs/Emotorad_Testing_Strategy.md`). Do the same here: a golden set of
  photos with known subject, framing and quality, scored on their own, before
  any conversational test. Start with the two photos in this incident; they
  are the first two rows.
- Test on a phone. Four days of reading the chat page code missed three
  defects that two minutes on a handset found.
- Do not touch `EMOTORAD_AI_APPROVAL_MODE=bot` with real customers, do not
  send the fixture phone `+919876543210` through the live server, and never
  push without the owner's yes in the session.

## What done looks like

- The wide shot of the owner's pack, replayed through `runtime.handle()`,
  produces a retake request every time, never "intact".
- The close-up produces "melted" every time.
- An over-close photo of bare pins produces "I can't tell which part this is"
  and a request for a framed shot.
- A short clip with narration reaches the agent as frames plus transcript
  through the production loop, with nothing written to disk.
- Every classification is in `logs/conversations.jsonl` with the same
  redaction rules as everything else.
- The suite runs offline and the photo golden set reports per class.

## How to reproduce today's behaviour

The replay script used on the 21st lives outside the repo; it is fifteen lines
of `urllib` against `POST /message` with `agent: "battery_support"`, the
owner's number, the code from `GET /dev/verification/{id}`, then one inline
JPEG. The run command for the server is in the handoff. Ask the owner for the
two photos; they are not in the repo and should not be committed.
