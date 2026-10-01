# Video first: how the bot asks to see a fault

Date: 1 October 2026. Branch: `feat/video-first-evidence`, cut from
`feat/integrate-jev-mongo` at `688536e`.

## Purpose

When a customer reports a fault ("my battery isn't charging", or any other),
the bot asks to see it: a video first, and a photo only if a video is not
possible. The person's decisions (1 October 2026):

- **When.** Only when evidence is needed, as now. The bot still troubleshoots
  first and asks at the moment the customer describes something visible, or
  before a conclusion. What changes is the medium: a video first.
- **Neither arrives.** After three asks with nothing back, a person takes over.
- **Enforced in code.** The runtime makes sure a photo-only ask also asks for a
  video, and counts the asks. The model still decides when to ask and what to
  show.

Unchanged: a safety report (smoke, swelling, heat, fire, leaking, sparks,
injury) is handed over at once and nobody is asked to film it; questions that
are not faults ("how do I switch it on?", "when is my service due?", an order)
never get an ask.

## 1. What the customer sees

- Every ask is for a short video of exactly what the bot needs to see, with a
  photo as the fallback, for example: "Could you send a short video of the
  charger plugged in, showing its light? If you can't take a video, a photo is
  fine."
- The fixed message sent when the bot concluded without evidence
  (`EVIDENCE_BLOCKED_MESSAGE`) becomes: "Before I can take this further I need
  to see it. Please send a short video of what you're describing. If you can't
  take a video, a photo will do."
- Once the customer says they cannot take a video, the bot asks for photos for
  the rest of that chat.
- After three asks with nothing back, a person takes over with the usual
  hand-over message (`HANDOVER_TEXT`, `agents/base.py`). Any video or photo that reaches the bot
  starts the count again. This replaces two rules that disagreed: the prompt
  closed the chat after three asks, and the code handed over on the second
  blocked conclusion.

## 2. Enforcement in code

### State

`ConversationState` gains:

- `evidence_asks: int = 0`: asks since a video or photo last reached the model.
  Set back to 0 wherever `evidence_seen` is set to true today
  (`Runtime._note_customer_turn`, when `shows_media(content)`).
- `video_declined: bool = False`: the customer said they cannot send a video.
  Set from the customer's message text with `declines_video` (below), and never
  cleared within a conversation.

`evidence_asked: bool` is removed; `evidence_asks` replaces it. A saved state
that still carries `evidence_asked` loads without it (`ConversationState.from_json`
ignores unknown keys).

### A new module, `src/emotorad_ai/evidence_asks.py`

- `asks_for_media(reply: str) -> Optional[str]`: `"video"` when a sentence of
  the reply asks the customer for a video (or a video and a photo), `"photo"`
  when the sentences that ask name only a photo, picture, image or snap, `None`
  when nothing is asked. A sentence asks when it has a media word and a request
  addressed to the customer: "could you / can you / would you (please) send,
  share, upload, record, take, film, show me", a sentence that starts with one
  of those verbs or with "please", or the Hinglish "bhej(o/iye/ein)",
  "dikha(o/iye)" and Devanagari "भेज", "दिखा". It is not an ask when the bot
  is the one sending ("I'll send you a picture", "let me send", "here's a
  photo"), or when it speaks of media already received ("thanks for the video",
  "I can see in your video", "your photo shows").
- `declines_video(text: str) -> bool`: the customer says a video is not
  possible: "can't / cannot / can not / unable to (take / send / record / shoot
  / make / upload) a video", "no video", "video (is) not possible", "video isn't
  possible", "camera doesn't / can't record", "only a photo", "photo instead",
  "video nahi", "वीडियो नहीं". "I've sent a video", "the video is uploading" and
  "here is the video" are not declines.

### Check 1: video first

In `Runtime._run`, after the existing post-checks and before the reply is
built, on the text the customer will be sent:

- the reply asks for a photo only (`asks_for_media == "photo"`) and
  `video_declined` is false: append `VIDEO_FIRST_LINE` = "If you can, a short
  video is even better: it shows me more than a photo." and log
  `video_first_added`;
- the reply asks for a video and `video_declined` is true: append
  `PHOTO_FALLBACK_LINE` = "A photo is fine." and log `photo_fallback_added`.

`_run` is the one place every agent's reply passes through, the narrow agent
included. The safety path, triage, verify-first, the erasure gate and the
standard responses do not reach it and are unchanged.

### Check 2: three asks, then a person

- A reply that asks for evidence (`asks_for_media` is not `None`) counts one
  ask, however many things it asks for. Before it is sent: if
  `evidence_asks >= 3`, the reply is replaced by `HANDOVER_TEXT` (plus what the
  turn already did, `_already_done`), the conversation is escalated with reason
  `evidence_not_forthcoming`, and a guardrail event of that name is logged with
  the suppressed text. Otherwise `evidence_asks` goes up by one.
- The evidence post-check (a conclusion with no evidence seen) counts as an ask
  the same way: at `evidence_asks >= 3` it hands over (as today's second block
  does); otherwise it sends the new `EVIDENCE_BLOCKED_MESSAGE` and counts one.

## 3. Wording

- `prompts/battery_support.md`:
  - §5a-observe: ask for a short video of what the customer describes; offer a
    photo only if they say they cannot take a video. "State what you saw" stays.
  - §5a-gate: "they get a request for a photo instead" becomes "a request for a
    video instead".
  - §5a Retry Rule, evidence: "close the chat gracefully" becomes "the platform
    hands the chat to a person after three asks with nothing back; do not close
    the chat yourself".
  - The opening line of the "cycle isn't turning on" flow ("may ask for a photo
    or video") becomes "may ask for a short video".
- `agents/motor_support.py` `_BASE_PROMPT` gains one paragraph: when you need
  to see something, ask for a short video of it; a photo only if the customer
  cannot take a video; never in a safety case.
- `agents/narrow_support.py` `_RULES`: the evidence rule adds "ask for a short
  video first, and a photo only if they cannot take one".
- Knowledge records under `knowledge/battery/` and `knowledge/motor/`: what each
  record asks to see stays; the medium becomes a video first, photos if a video
  is not possible. Where the detail is fine (a sticker, a serial number), the
  record adds "hold the camera still on it for a second".
- `docs/contracts/amiigo-support-chat.md`, "Photos and videos": one sentence,
  "The bot asks for a short video first, and a photo if the rider can't take
  one."

## 4. Tests

- `asks_for_media` and `declines_video`: tables of realistic replies and
  messages, including the ones that must not count: the bot's own guide picture,
  "thanks for the video", "I can see in your video", "here's a photo of the
  switch", and the customer's "I've sent a video".
- The runtime (scripted model replies):
  - a photo-only ask gets `VIDEO_FIRST_LINE`; a video ask is left alone;
  - after the customer declines video, a video ask gets `PHOTO_FALLBACK_LINE`;
  - three asks are sent, the fourth with nothing back becomes the hand-over
    (`evidence_not_forthcoming`); a photo or video arriving sets the count
    back to 0;
  - the evidence post-check counts as an ask and hands over at three;
  - the narrow agent's replies get both checks;
  - a safety report gets neither.
- Content: every knowledge record that asks for a photo also asks for a video;
  the battery prompt, the motor prompt and the narrow rules carry the
  video-first rule; `EVIDENCE_BLOCKED_MESSAGE` asks for a video first.
- The whole suite, with the tests that pinned the old behaviour (a hand-over on
  the second blocked conclusion, `evidence_asked`) updated to the new count.
