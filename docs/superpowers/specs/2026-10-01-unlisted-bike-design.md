# A bike that is not in the list

Date: 1 October 2026. Branch: `fix/unlisted-bike`, cut from
`feat/integrate-jev-mongo` at `449225e`.

## Purpose

On staging, after the code was confirmed the bot listed two bikes and the
customer replied "its not one of these 2". The reply was read as a choice of
bike 2: `match_bike` matched the ordinal "2" and nothing checked for the "not".
The battery agent then ran on the wrong bike. With two or more bikes listed
there was also no path for "my bike is not here" at all; with one bike listed,
"no" went straight to late warranty registration.

The person's decisions (1 October 2026):

- When the customer's bike is not in the list, the bot asks for its frame number
  and model first, then carries on with the conversation.
- After that, it helps with the issue only: no registration step.
- The same flow whether one bike or several were listed.

## 1. What the customer sees

1. The bike list is unchanged. The customer can say their bike is not in it by
   saying "not one of these", "none of these", "neither", "it's a different
   bike", "not mine", "no" when one bike is listed, the Hinglish and Hindi
   equivalents ("koi nahi", "dono nahi", "इनमें से कोई नहीं"), or by sending a
   frame number that is not in the list.
2. The bot asks: "No problem. Please send your bike's frame number and model.
   The frame number is printed on the sticker on the frame." When the customer
   already gave a frame number it asks only for the model ("Thanks. Which model
   is it?"), and the other way round ("Thanks. What's its frame number? It's
   printed on the sticker on the frame.").
3. A reply may carry both, one, or neither. Each missing part is asked for at
   most twice; after that the bot carries on with what it has, so nobody is
   stuck.
4. Model names are matched against EMotorad's known models, ignoring case,
   spaces and hyphens ("trex air" is T-Rex Air). Anything else the customer
   types as the model is kept as typed.
5. Once it has the bike, the bot carries on. If the customer described the
   problem earlier, the issue's agent takes it from there; otherwise it asks
   "Thanks: T-Rex Air, frame EMXP2026009999. What is happening with the bike? A
   short description is enough."
6. During the issue the bot troubleshoots for that model, never says the bike is
   or is not under warranty (there is no record of it on this number), and puts
   the frame number on any ticket, marked as given by the customer. There is no
   registration step.

## 2. Code

### State (`conversation.py`)

- A new phase constant, `AWAITING_UNLISTED_BIKE`.
- `ConversationState.unlisted_bike: Optional[Dict[str, Optional[str]]] = None`:
  `{"frame_number": ..., "model": ...}`, either part `None` if the customer never
  gave it.
- `ConversationState.unlisted_asks: int = 0`: asks made in the collecting step.

### Triage (`triage.py`)

- `not_listed(text) -> bool`: the phrases in section 1, checked in
  `_resolve_selection` **before** `match_bike`. With one bike listed, `says_no`
  counts as well (as today). A frame number that matches no listed bike starts
  the same step, with that frame number already given.
- The collecting step, for `AWAITING_UNLISTED_BIKE`: each reply is read for a
  frame number (the existing `_FRAME` pattern, upper-cased) and a model
  (`known_model(text) -> Optional[str]` against the model names in
  `tools/amigo.MODEL_NAMES` and the fixtures' product names; a reply with no
  frame number and no known model, when only the model is missing, is taken as
  the model as typed). The bot asks only for what is missing, at most twice per
  part.
- When it has the bike (or has asked twice): `state.unlisted_bike` is set,
  `state.select_bike(<frame number, or "unlisted">, "<model> (frame <frame>)")`
  records it, the phase moves to `AWAITING_ISSUE`, and triage continues with the
  pending topic exactly as after an ordinary bike choice.
- The route from a one-bike "no" to `late_warranty_registration`
  (`reason="bike_not_listed"`) is removed. Late registration is still reached
  when no bike at all is registered on the number.

### Runtime (`runtime.py`)

- `_selected_bike` returns, when `state.unlisted_bike` is set, a bike made from
  it (`{"product_name": model, "model": model, "frame_number": frame,
  "on_record": False}`), not a listed bike. Without this, with one bike listed it
  falls back to the bike the customer rejected.
- The agent's context gets, while the bike is unlisted: "The customer's bike for
  this conversation is not registered on their number: <model>, frame <frame>,
  as they read it. There is no warranty record for it: never say it is or is not
  covered. Use this frame number on any ticket."
- The coverage post-check blocks any claim that the bike is or is not covered
  while `state.unlisted_bike` is set (reason `unlisted_bike`), whatever the
  customer's listed bikes' records say.
- The late facts passed to the agent's tools gain `unlisted_bike`.

### Ticket tool (`tools/mocks.py`)

- `create_support_ticket` takes `unlisted_bike` as an optional injected fact.
  When it is set and the call names no frame number, or names the unlisted
  bike's frame number, the ticket's bike is the unlisted bike, with
  `frame_number_source = "given by the customer; not registered on this
  number"`. Frame numbers of listed bikes are checked as today.

## 3. Tests

- Triage: every "not in the list" phrase; "its not one of these 2" never picks a
  bike; the frame number and model in one reply, in two, with a part missing;
  carrying on after two asks; the one-bike "no"; a frame number not in the list.
- A replay of the staging chat with the two-bike test rider: "its not one of
  these 2", then the frame number and model, then "battery isnt charging" reaches
  the battery agent with the unlisted bike as the conversation's bike, and no
  "which bike" question is asked.
- Wiring: the agent's context carries the bike; a coverage claim about it is
  blocked; a ticket raised for it carries its frame number and source.
- The existing tests that expected a one-bike "no" (and a frame number not in a
  one-bike list) to reach late registration change to the new flow.
