# Verify first: number, code, bike, then the issue

Date: 2026-09-30. Owner: Sagnik. Status: for review.

## What the person asked for

- If the chat is not coming from the Amiigo app or anywhere else the person is already verified, the bot first asks for their phone number, sends a one-time code to it, and checks the code. The OTP system already exists elsewhere; mock it for now.
- The phone number is asked for first.
- Once verified, the bot shows every bike on that number with its frame number, the person picks the one with the problem, and the conversation carries on about the issue.
- Guide pictures: the assets live in `emotorad-ai-stage-media/assets/afs/battery/photos/`. Only `battery-onoff-switch.png` and `soc-button-non-doodle.png` are useful; send those when needed.

Decisions taken in the brainstorm (2026-09-30):

- A fixed code step runs the verification, not the model.
- With one bike on the number, the bot still shows it and asks the person to confirm.
- A person who cannot recall their number can use their order or invoice number instead (the existing fallback).

## Where things stand

- `tools/verification.py` has `request_identity_verification(phone)` (sends a code, says nothing about the number), `verify_identity(code)` (the comparison is `==` in code, 5 attempts, 10 minute codes, 12 hour sessions) and `find_account_by_code(code)` (order or invoice number to the phone on it, never shown). The code is shown only on the dev route `/dev/verification/{cid}`, which is already a mock of the SMS.
- For an anonymous website visitor, verification happens only if the model decides to ask. Nothing makes it come first, and the model sees the number and the code.
- Triage (`triage.py`) asks "which bike" only for a verified person with two or more bikes, and shows the last four digits of each frame number. One bike is picked without asking.
- `find_account_by_code` is registered only when the live OMS key is set, so it cannot be tried locally.
- The picture catalogue (`knowledge/_media/catalogue.yaml`) has five items. The two wanted ones already have the right ids: `afs/battery/photos/soc-button-non-doodle.png` and `afs/battery/photos/battery-onoff-switch.png`, looked up under `assets/`, with the `.w900.webp` copies the bot sends.

## Design

### 1. Where the step runs

A new graph node, `verify_gate`, between `handoff_gate` and `persona_route`:

```
prepare -> safety_gate -> handoff_gate -> verify_gate -> persona_route -> jev_classify -> ...
```

So a safety report and "talk to a person" are answered before any verification, as today. The step runs only when all of these hold:

- the runtime was built with `verify_first=True` (the web chat API sets it; it defaults to False, so every other caller and every existing test is unchanged);
- the registry holds `request_identity_verification` and `verify_identity`;
- the person is a customer (`resolved.persona == "customer"`);
- their identity is not proven (`resolved.may_disclose` is False).

The Amiigo app (session token), WhatsApp and a signed-in website arrive verified and skip it. Dealers skip it. No model is called while the step is answering.

With `verify_first` on, `prepare` also applies a phone this conversation has proved (the runtime's `phone_resolver`) to the inbound identity, as `api.apply_verified_identity` does, so the runtime does not depend on the API having done it.

### 2. The conversation

```
Rider: my battery isn't charging
Bot:   Before I look into this, I need to confirm it's you. What's the mobile number your bike is registered on?
Rider: 97000 00010
Bot:   I've sent a 6-digit code by SMS to •••••••010. Please type it here.
Rider: 482913
Bot:   Thanks, that's confirmed. I found 2 bikes on this number:
       1. EMX Plus (Grey), frame EMXP2026001234
       2. Doodle V3 (Matte Black), frame DDL32023045678
       Which one needs help? Reply with its number in the list or its frame number.
Rider: 2
       (triage selects DDL32023045678 and routes to the battery agent, from "charging" in the first message)
```

### 3. Steps and rules

The step's progress is two new fields on `ConversationState`, saved with it: `verify_step` (`None`, `"number"` or `"code"`) and `verify_masked` (the masked number the code went to, for the replies). `from_json` already ignores unknown fields, so old and new versions can share a store.

**First contact** (anonymous, `verify_step` is None):
- The topic is taken from a tapped pill or the words (`triage.topic_from_pill`, `triage.classify_issue`) and kept in `pending_topic`, so it is not asked for again later.
- A phone number in the message is used straight away (go to "number given").
- Otherwise the bot asks for the number. If the message carries a photo or video, the ask adds: "If you can see smoke, heat or swelling, stop using the bike and tell me now." (A hazard seen only in a photo is not caught by the keyword gate; this asks the person to say it, which the gate does catch.)

**Waiting for the number** (`verify_step == "number"`):
- A phone number: `request_identity_verification(phone)`. On success: "I've sent a 6-digit code by SMS to {masked}. Please type it here." A number that is not a valid Indian mobile: "That doesn't look like a 10-digit mobile number. Please send it again."
- An order or invoice number, when `find_account_by_code` is registered: look it up. Found: send the code with no phone argument (to the number on the order, which the bot never sees): "I found that order. I've sent a 6-digit code to the number on it, {masked}. Please type it here." Not found: "I couldn't find an order with that number. Please check it, or send your mobile number instead."
- Anything else: "I need the 10-digit mobile number your bike is registered on. If you can't recall it, send your order or invoice number instead." Without the order tool, the second sentence is "If you can't recall it, say 'talk to a person'." A topic found in this message is kept if none was kept yet.

**Waiting for the code** (`verify_step == "code"`):
- A phone number: a new code goes to that number (the person gave the wrong one).
- "resend", "send again", "new code", "didn't get it" and similar: a new code to the same number. `request_identity_verification` with no phone argument now falls back to the number already pending for this conversation, after the order-code candidate.
- A 6-digit code (spaces allowed): `verify_identity(code)`.
  - Wrong: "That code isn't right. You have {n} tries left. Please check the SMS and type it again."
  - Expired (older than 10 minutes, which costs no try): "That code has expired. Say 'resend' and I'll send you a new one."
  - Locked (5 wrong): "That's too many wrong codes, so I can't confirm it's you here. I'm passing you to our support team, who can verify you another way." The reply is escalated.
  - Right: see "verified".
- Anything else: "Please type the 6-digit code I sent to {masked}. Say 'resend' for a new code, or send a different number."

**Verified**, in the same turn:
- The identity is rebuilt with the proved phone and hydrated again, so the turn's `resolved` has the bikes. `verify_step` is cleared, `context_block` is cleared (the next turn builds it with the bikes and past conversations), and `state.agent` is cleared so the selection runs through triage.
- Bikes found: the list, every bike with its full frame number, and `phase` moves to `awaiting_bike_selection`.
  - Several: "Which one needs help? Reply with its number in the list or its frame number."
  - One: "Is this the bike that needs help? Reply yes, or send the frame number of the bike you mean."
- No bike on the number: "Thanks, that's confirmed. I couldn't find a bike registered on this number. Would you like to register it now?" The next turn resolves `no_warranty_record`, and the persona step already sends that to the warranty registration agent.
- The OMS did not answer: "Thanks, that's confirmed. I can't load your bikes just now. What's happening with the bike?" The conversation carries on as for a verified person whose lookup failed, as today.

**Choosing the bike** (the next turn, now verified, handled by triage):
- Selection works as today: a number from the list, "first", "the second one", the frame number or its tail, the model name, or the colour to break a tie.
- With one bike, "yes", "haan", "correct" and similar select it. "no", "nahi", "a different one" and similar hand the chat to the warranty registration agent, because the bike they mean is not registered on this number.
- Unmatched: "Sorry, I did not catch which bike you meant." and the list again (today's wording, kept).
- No bikes to choose from (the lookup failed on the choosing turn): straight on to the issue, never a list of none.
- After the choice: the kept topic routes to its agent, or the bot asks "What is happening with the bike?".

**Also:**
- The "which bike" list shows full frame numbers everywhere, including for a verified person on the Amiigo app with several bikes. One renderer serves both.
- A pinned agent (the test page's `?agent=`) is not applied while the bike is being chosen; it applies once the bike is chosen. The persona step runs the selection whenever the phase is `awaiting_bike_selection`.
- The model's history and the transcript get the step's turns with the number, code and order number replaced (`[phone]`, `[code]`, `[order number]`), so no model, no Jev call and no transcript reader sees them. `observability.redact_pii` is also widened to hide a number typed with a leading 0 or with a space or dash in the middle ("97000 00010"), which it missed, so the event log hides those forms too.
- A verified session that has expired (12 hours) makes the person anonymous again, and the step starts from the top.
- Every step logs one `verify_first` event with the step and its outcome, never the number or the code.

### 4. The mocks

- **Code sending.** The API passes a sender to `register_verification_tools` that logs `otp_sent` with the masked number, never the code. The code is still shown only on `/dev/verification/{cid}` (playground login plus `EMOTORAD_AI_DEV_CODES=1`), where the chat page already shows it. The real SMS or OTP service replaces this sender and nothing else changes.
- **Order lookup.** Without the OMS key, the API registers `find_account_by_code` against invented test order and invoice numbers in `tools/fixtures.py` (for example `EMO-100234` and `INV-2026-0042` for the Amiigo test rider). With the key, the live OMS lookup is used, as today.

### 5. Guide pictures

- `knowledge/_media/catalogue.yaml` keeps `soc_button` and `battery_onoff_switch`. `battery_revival`, `melted_battery_terminal` and `melted_controller_connector` are removed.
- The knowledge records drop the same three from their `media` lists (`wont-charge.yaml`, `wont-power-on.yaml`, `melted-terminal-or-connector.yaml`).
- The em dash in the switch caption becomes a comma.
- No path changes: the ids already match the bucket. Staging signs links with its instance role; this laptop's AWS login is invalid, so locally `/health` still reports `guide_media: 0 of 2 sendable` and the bot offers no pictures.

### 6. What else changes

- `web/e2e-console.html`: the anonymous scenarios now go through the step (number, the dev code, then the bike). The safety scenarios keep their checks; a smoke photo with no words now expects the safety line in the number request.
- `CLAUDE.md` and `docs/runbooks/media.md` section 9: a line each on the new first step and the test order numbers.

## Failures

- A tool refusing (invalid number, order not found, wrong or locked code) is answered by the fixed replies above. Nothing is swallowed: each one is a named `verify_first` outcome in the log.
- The warranty lookup failing after verification is the "OMS did not answer" reply, not an error.
- An unexpected exception in the step is not caught: it fails the turn, as a failure in any other node does, so it is seen.

## Testing

`tests/test_verify_first.py`, with a runtime whose model has no scripted replies, so any model call during the step fails the test:

- The whole flow for the two-bike rider: issue, number, code (read from the verification store), list with both frame numbers, "2", then the battery agent runs with `selected_frame == "DDL32023045678"`. No digit of the phone and not the code appear anywhere in the history the agent was given.
- One bike: "yes" selects it. "no" goes to the warranty registration agent.
- No bike on the number: the registration question, then the registration agent.
- Wrong code five times: the tries-left replies, then the escalated handover.
- "resend" and a different number while waiting for the code.
- The order number fallback: an unhelpful reply, the fallback ask, an order number, the code, the list.
- Safety before verification: "smoke from the battery" gets the safety reply. "talk to a person" gets the handoff.
- A photo on the first message adds the safety line.
- Skipped: a verified session (Amiigo test rider), a dealer, and a runtime without `verify_first`.
- A pinned agent still gets the bike choice.
- Parsing, on its own: phone numbers in the forms people type (`9700000010`, `97000 00010`, `+91 97000-00010`, `09700000010`), codes (`482913`, `482 913`, `code is 482913`), order numbers, resend words, yes and no in English and Hindi.

An API test through `TestClient`: the `/message` flow with the dev code from `/dev/verification`, `otp_sent` logged without the code, the fixture order number accepted.

The pictures: the catalogue has exactly the two keys, no knowledge record names another id, and no caption has an em dash. The existing picture tests that named a removed key move to a kept one.

The whole suite runs with the command in `CLAUDE.md`.

## Not in this change

- The real SMS or OTP service and the real order lookup outside the OMS.
- Hindi or Hinglish wording for the step's replies. They are fixed English text, like triage's.
- Keeping verification across a server restart (the store is in memory).
- The other end-to-end findings from 2026-09-29.
- Removing the identity tools from the agents' tool lists. They stay, unused on the web chat once this step runs, and still used where `verify_first` is off.
