# EMotorad Support Chat — Design Handoff

Front-end prototype for the customer-facing aftersales AI chat, built mobile-first (390×844 phone frame) with EMotorad's brand identity. This doc is for whoever picks this up for real implementation.

## Files

- `emotorad-support-chat-dev.html` — the design as a single, dependency-free HTML/CSS/JS file. No framework, no build step. Open it directly in a browser to see and click through the prototype.

## What's real vs. scripted

This file is a **visual and interaction prototype only** — it is not wired to the actual aftersales AI agent. The conversation plays out from a hardcoded `SCRIPT` array in the `<script>` block, advancing on a timer to simulate the agent "typing." It exists to validate the interface — bubble style, quick-reply chips, the evidence-upload moment, the ticket-confirmation card, mobile ergonomics — before the real integration work happens.

Search the file for the string `INTEGRATION POINT` — there are four, each marking a spot where scripted behavior needs to become a real call to the agent backend:

1. **First free-text message** (`sendFree`, awaitFree branch) — where the visitor's actual issue should be sent to the agent API instead of just being echoed back as a user bubble.
2. **Attach button** (`renderFooter`, attach mode) — where a real file/camera picker should open and upload the result, rather than faking a fixed filename/duration.
3. **Post-script free text** (`sendFree`, fallback branch) — the generic "Thanks, noted" reply should become a real agent turn once the script is exhausted.
4. Implicitly, every `ai` entry in `SCRIPT` (greeting, clarifying question, evidence request, ticket card, closing line) represents an agent turn that should ultimately be rendered from the agent's actual response rather than read from the array.

The rendering functions (`renderAiTurnHtml`, `renderUserTurnHtml`, `renderFooter`) and the state shape (`pointer` / `typing` / `filled` / `extra`) are reusable as-is — swapping in real responses just means calling them with agent-provided data instead of `SCRIPT[i]`.

## Interaction flow modeled

Mirrors the real transcript from the prompt-tuning playground (battery on/off-switch case):

1. AI opens with the required bot-disclosure line, then asks what's wrong.
2. Visitor answers in free text (a suggested chip is offered as a shortcut).
3. AI asks a clarifying question via two quick-reply chips.
4. AI requests video evidence with a bulleted checklist.
5. Visitor "attaches" a video (dedicated full-width button replaces the composer).
6. AI shows a ticket-confirmation card (id, next steps).
7. Conversation falls into a generic free-text loop.

## Brand tokens (pulled from emotorad.com)

| Token | Value | Use |
|---|---|---|
| Ink | `#171717` | Primary text, user bubbles, header title |
| Accent | `#FF6B2B` | CTAs, active states, avatar mark |
| Bubble (AI) | `#F1F1F1` | Assistant message background |
| Muted text | `#666666` | Captions, secondary text |
| Border | `#EFEFEF` / `#EAEAEA` | Hairlines, card borders |
| Typeface | Figtree (Google Fonts), weights 400–800 | All text |
| Corner radius | Fully rounded (`999px`) on buttons/chips, 16–18px on bubbles/cards | Matches site's pill-button language |

Buttons and chips are sized to a 44px minimum tap target throughout.

## Known gaps / open questions for engineering

- No real file upload — the attach flow is a single button tap, no picker, no validation, no upload progress.
- No error states (network failure, agent timeout, OTP/identification flow from the real system prompt is not represented at all — this prototype starts "already identified").
- No persistence — refreshing the page restarts the conversation from turn 0.
- Not tested against screen readers; interactive elements are plain `<div>`s with click handlers rather than semantic `<button>`s — worth revisiting for accessibility before shipping.
- The bot-disclosure line in turn 1 exists to satisfy the EU AI-disclosure requirement referenced in the system prompt — keep it in whatever replaces this greeting.
