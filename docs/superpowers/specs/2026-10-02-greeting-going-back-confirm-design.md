# A greeting, going back from any step, confirming the bike, and photo safety

Date: 2 October 2026. Branch: `feat/going-back`, cut from
`feat/integrate-jev-mongo` at `c182bc3`.

## Purpose

From the person's staging tests on 1 October 2026:

- "hi" was answered with the AI line and an immediate demand for the mobile
  number. The bot should greet back and be more human, not too
  straightforward.
- "not this one", said after the bot had chosen a bike, was not understood:
  the bot asked "What is happening with the bike?" again. The customer must be
  able to go back from every step. Nothing is one-way.
- When the customer says their bike is not one of those listed, the bot must
  confirm the bike once before finalising it, whether it turns out to be a new
  bike or one already in the list.
- A photo of a smoking bike sent during late registration was not recognised:
  the photo's safety check timed out (`photo_check_skipped`, `timeout`, at
  14:35 and 15:00 UTC; it worked at 08:00 and found smoke), and the
  registration agent has no safety rule. That agent also said "photos cannot
  be uploaded in this chat", misreading the rule that the bot cannot send
  pictures.

The person's decisions (1 October 2026):

- A greeting-only first message is greeted back; the number is asked for only
  once the customer says what is wrong.
- The photo check waits longer (up to 30 seconds) and retries once; if it
  still has no answer, the agent is told; every agent gets the safety rule.
- Staging's use of the production OMS with codes shown on screen is left as it
  is (the person has spoken to Sachin). Placeholder numbers such as
  9090909090 show whatever the OMS holds; the two records for 9090909090 were
  confirmed through the OMS purchase API.

## 1. What the customer sees

### The greeting

- A first message that is only a greeting ("hi", "hello", "hey", "hii",
  "namaste", "namaskar", "good morning", "good afternoon", "good evening",
  with or without punctuation or an emoji) gets: "Hi there! I'm EMotorad's
  virtual assistant, an AI. How can I help with your bike today?" No number is
  asked for yet. The AI line the runtime adds to a first reply is not added a
  second time.
- The next message, or a first message that says what is wrong ("hi, my
  battery isn't charging"), gets: "Happy to help with that. First I need to
  confirm it's you: what's the mobile number your bike is registered on?"
- A signed-in app rider (no verification step) who only greets gets: "Hi
  there! How can I help with your bike today?"

### Going back, from every step

| Where the customer is | What they say | Where it goes |
| --- | --- | --- |
| The number step, or the code step | "wrong number", "change number", "use another number", "use a different number", "not my number", "galat number" | Back to the number step; a pending code is cancelled |
| The bike list | "change number" (and the phrases above) | Back to the number step: the conversation's verification is forgotten |
| Giving an unlisted bike's details | "go back", "back", "show the list", "show the list again", "the options", "wapas", or a number or frame number from the list | Back to the list |
| Confirming the bike | "no" | "Which is wrong, the frame number or the model?"; "show the list" goes back to the list |
| After the bike is chosen, also mid-troubleshooting | "not this one", "not this bike", "wrong bike", "change bike", "change the bike", "different bike", "other bike", "another bike", "doosri bike", "ye wali nahi" | Back to the bike list; the problem already described is kept and troubleshooting starts again for the new bike |
| Anywhere after verifying | "start over", "start again", "restart", "from the beginning", "shuru se" | Back to the bike list, still verified, with the problem cleared |

A signed-in app rider who asks to change the number is told: "You're signed in
to the app with your number, so I can't change it here."

### Confirming the bike once

Only after the customer said their bike is not in the list:

- Their details are complete: "Just to confirm: your bike is the Doodle Pro,
  frame TESTEMXP0000069. Is that right?" "yes" carries on as today; "no" gets
  "Which is wrong, the frame number or the model?"; the answer clears that part
  (or both) and asks for it again.
- Their details match a listed bike's frame number: "That frame number is the
  EMX Plus (Aqua) in your list. Is that the bike?" "yes" chooses it; "no" goes
  back to asking for the frame number and model.
- A part is still missing after the asks: the bot confirms what it has, for
  example "Just to confirm: your bike is the Doodle Pro, frame number not
  given. Is that right?"
- Anything other than yes or no is asked again once, then taken as no.

### Photo safety

- Every photo in a message is checked within 30 seconds in all, each with up to
  two tries.
- A photo that still has no answer: the agent's context for that turn says
  "A photo in this message could not be safety-checked. If it shows smoke,
  flames, swelling, leaking or sparks, tell the customer to stop using and
  charging the bike and hand over."
- Every customer agent's prompt gets one paragraph: a photo that shows smoke,
  flames, swelling, leaking fluid or sparks is a safety case; stop, tell the
  customer to stop using and charging the bike, and hand over, whatever else
  the conversation was doing.
- The rule for agents with no pictures to send becomes: "You cannot send
  pictures or videos to the customer in this chat. The customer can send you
  photos and videos."

## 2. Code

- `src/emotorad_ai/navigation.py` (new): `is_greeting_only(text)`,
  `wants_change_number(text)`, `wants_change_bike(text)`, `wants_list(text)`,
  `wants_start_over(text)`. Fixed phrases only; a bare "no" or "back" inside a
  longer sentence about the bike is not navigation.
- A new node, `navigation_gate`, after `safety_gate` and before `handoff_gate`:
  change number after verifying (forget the conversation's verification in the
  OTP store, clear the bike, the unlisted bike, the agent, the topic; ask for
  the number), change bike (clear the bike, the unlisted bike and the agent;
  keep the problem as the pending topic; show the list), start over (as change
  bike, the topic cleared), and the app rider's "change number" reply. It does
  nothing while an erasure confirmation is pending.
- `verify_first.py`: the greeting; the warmer first number question; "change
  number" at the code step cancels the code and asks for the number again.
- `triage.py`: the confirmation phase, `AWAITING_BIKE_CONFIRMATION`; "go back"
  while collecting; the app rider's greeting.
- `api.py`: `PHOTO_CHECK_DEADLINE_SECONDS = 30.0`; each photo gets up to two
  tries inside it; a message with an unchecked photo carries
  `photos_unchecked` in its entry metadata. The runtime adds the "could not be
  safety-checked" line to the agent's context for that turn; it is never put in
  an attachment's summary (the safety gate scans summaries and would fire on
  its words).
- `agents/base.py`: the safety paragraph for every customer agent; the new
  wording of the no-pictures rule.

## 3. Tests

- Every phrase list, with replies that must not count.
- Each row of the going-back table, through a whole chat with the test chat
  fixture (website), and the app rider's change-number reply.
- The confirmation questions: yes, no then "the model", no then "the frame
  number", a listed match confirmed and refused, the missing-part version,
  and an unclear answer.
- The greeting on the website and in the app, with the AI line exactly once.
- A photo check that times out then succeeds on the retry; one that fails
  twice, so the agent's context carries the line and the safety gate is not
  tripped by it.
- The registration agent's prompt carries the safety paragraph and the new
  pictures wording.
- The whole suite.
