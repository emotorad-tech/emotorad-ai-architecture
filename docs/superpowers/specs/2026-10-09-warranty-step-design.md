# The warranty step, after the issue is verified: design

9 October 2026 · Sagnik Mukherjee · approved in chat on 9 October 2026 · status: spec, awaiting review

## Why

Once a rider's issue is verified, the next thing is the warranty. There are four cases:

1. **The phone and frame are in OMS, with a purchase date.**
2. **The phone and frame are in OMS, no purchase date, an invoice on file.** The invoice is read with OCR and a ticket is raised.
3. **The phone and frame are in OMS, no purchase date, no invoice.** The rider uploads a photo of the invoice, it is read, and a ticket is raised.
4. **The phone is known but no frame is in OMS.** The app will let the rider register the warranty. The backend team is building that flow, so for now this is a placeholder.

Most of the machinery exists (spec 2026-10-08: the OMS database lookup, `warranty_terms.py`, `invoice_ocr.py`, the `warranty_proof` ticket). What is missing is the order. Today:

- the lookup runs at verification;
- each bike's cover is in the agent's instructions from the first turn;
- the case 2 read starts only after the agent happens to call `lookup_warranty_record` on an earlier turn;
- the case 3 ask is left to the agent;
- a case 4 rider is sent straight to the registration chat before any troubleshooting, whatever they asked. That chat cannot troubleshoot, so a rider with a battery fault gets no help.

## Decisions taken (9 October 2026)

| Question | Decision |
|---|---|
| What starts the step | The evidence check passing for the chosen bike (`evidence_check.verdict_passed`) |
| Cover before the step | Hidden from the agent: model and frame only. The cover line appears after the step. |
| Case 4 until the app flow exists | A message and a `register_warranty` button. No ticket. |
| Where the logic lives | In code (the runtime), never left to the prompt |

## 1. The trigger

- **When:** the step runs at the end of an agent turn for a customer once both hold:
  - the chat has a chosen bike, or a case 4 rider with a bike described;
  - `verdict_passed(state.evidence_verdict, state)` is true.
- **How often:** once per bike per chat. `ConversationState.warranty_step_frames` records each bike done; case 4 records `"-"`.
- **With the evidence check switched off** (`EMOTORAD_EVIDENCE_CHECK` not `on`: local runs, tests), the trigger is `state.evidence_seen`, the first fault photo or video reaching the agent.
- **Never on:**
  - a reply that carries a hazard (`guardrails.carries_caution`);
  - a safety hand-over;
  - the dealer persona;
  - a reply a post-check blocked. The step waits for the next turn that is not blocked.

## 2. Cover hidden until the step

- **Before the step, for the chosen bike,** the agent's instructions show only the bike's model and frame number. No cover line comes from `agents/battery_support._coverage_line`, and none from the bikes block in `enrichment.ContextEnricher._bikes_block`.
- **After the step,** both show the cover as today.
- **Unchanged:**
  - the lookup itself still runs at verification (hydration), so the bike list and the case are known;
  - the bike list the rider sees already shows model and frame only;
  - the coverage post-check;
  - the invoice rule.
- **The model can still call `lookup_warranty_record` before the step.** To keep that from undoing the hiding, the tool answers `warranty_after_issue` (a plain result, not an error) until the step has run for that bike. The note tells the agent that the warranty is checked once the fault is confirmed. After the step, the tool behaves as today.

## 3. The four cases, run by code (`warranty_step.py`)

`warranty_step.case_of(resolved, state) -> "dated" | "invoice_on_file" | "needs_invoice" | "no_frame"` reads the hydrated bike for the chosen frame:

| Case | Condition |
|---|---|
| `dated` | a purchase date, so cover was computed |
| `invoice_on_file` | `purchase_date_missing` and `invoice_on_file` |
| `needs_invoice` | `purchase_date_missing`, not `invoice_on_file`. This includes `invoice_with_support`, which is told rather than asked again. |
| `no_frame` | the identity is `no_warranty_record`, or the bike is the rider's unlisted one with no frame on record |

What code does at the step:

| Case | Action |
|---|---|
| `dated` | Runs `lookup_warranty_record` through the registry, so `state.coverage_result` holds it (`Runtime._remember_coverage`). The agent may then state the cover, and the replacement and ticket flows go on as today. Nothing is appended to the reply. |
| `invoice_on_file` | Runs the lookup as above. Starts `InvoiceService.read_from_oms` in the background for the frame. Appends `CHECKING_INVOICE_LINE`: "I'm checking the invoice we have on file for your bike." When the read finishes, the existing `Runtime._with_invoice_result` tells the rider what it found and raises the one `warranty_proof` ticket, in this reply if the read is done in time, otherwise in the next. |
| `needs_invoice` | Runs the lookup as above. Appends `NEEDS_INVOICE_LINE`: "To check your warranty, please send a clear photo or PDF of your purchase invoice showing the date." If the invoice is already with support, it appends the existing with-support wording instead and asks for nothing. The upload is then read and the ticket raised by the existing code (`api._start_invoice_reads`, `_with_invoice_result`). |
| `no_frame` | Appends `REGISTER_LATER_LINE`: "Your bike isn't registered with us yet. You'll be able to register its warranty in the app soon." Adds the action `{"kind": "register_warranty", "label": "Register warranty"}`. Raises no ticket. |

- **Hindi:** each line has a Hindi draft used for a Devanagari chat, marked DRAFT for a Hindi speaker to check, as the melt and serial asks are.
- **Appending:** code adds the lines after the agent's text, through the same path as the serial ask (`_with_serial_ask`). They never cut a caution and never reach a blocked reply.

## 4. Case 4 routing

- **The rule removed:** `Runtime._node_persona`'s rule "a customer with `no_warranty_record` goes straight to `LATE_WARRANTY`" (runtime.py, "3. A customer with no bike on record...") is removed. Such a rider goes through triage to the battery, motor or narrow agent like anyone else, with no bike chosen.
- **The agent's instructions** say the rider has no bike registered with EMotorad, and to help with the issue first.
- **The evidence check** treats the rider's described bike as theirs: no frame, `frame` absent from the verdict, as for a verdict made before a bike was chosen.
- **Registration-only riders:** a rider with no frame who asks only to register their warranty gets `REGISTER_LATER_LINE` and the action straight away, from code, with no model. Triage's registration intent routes to the step's case 4 when the identity is `no_warranty_record`.
- **`late_warranty`** stays for the path "the listed bike is not mine" (an unlisted bike), unchanged.
- **The verify-first message** for a number with no bike changes. It was "Would you like to register it now?". It becomes "I couldn't find a bike registered on this number. Tell me what is happening with your bike and I'll help." Registration comes at the warranty step.

## 5. The action and the app contract

- `register_warranty` is a new `actions` kind, an addition within Amiigo v1. The contract already tells the app to ignore kinds it does not know (`docs/contracts/amiigo-support-chat.md`, "Versioning").
- `Runtime._finish` gains an optional `actions` argument, so a code-written reply can carry it.
- The contract's two "today only `request_location`" lines are updated.
- `/Users/macbookpro/amiigo-dealer-store-cards.md`, the Flutter team's note, gains a short section: draw a "Register warranty" button that opens the app's registration screen once it exists; until then, ignoring the kind is correct.
- The website chat page ignores the kind, as it does any unknown one.

## 6. State

`ConversationState.warranty_step_frames: List[str]` lists the bikes the step has run for in this run.

- It is cleared by `restart_for`, like the run's other facts.
- It is added to `TURN_FACT_FIELDS`, so a merge after a conflict keeps it.

## 7. Tests

**Through `runtime.handle()`, scripted model, fake OMS records:**

- **Before the evidence check passes:**
  - no cover line is in the system prompt for the chosen bike;
  - `lookup_warranty_record` answers `warranty_after_issue`;
  - nothing is appended.
- **On the passing turn:**
  - `dated`: `coverage_result` is set and nothing is appended;
  - `invoice_on_file`: the read is started and `CHECKING_INVOICE_LINE` is appended;
  - `needs_invoice`: `NEEDS_INVOICE_LINE` is appended;
  - `no_frame`: `REGISTER_LATER_LINE` and the `register_warranty` action are on the reply, and no ticket is raised.
- **Once per bike:** the next turn appends nothing.
- **Never on a hazard:** a hazard reply, a safety hand-over or a blocked reply never runs the step.
- **Evidence check off:** with it off, `evidence_seen` triggers the step.
- **Case 4:**
  - a rider with no frame and a battery fault reaches the battery agent, not `late_warranty`;
  - a registration-only request gets the placeholder with no model call.
- **Persona:** the dealer persona never gets the step.
- **Hindi:** Hindi lines for a Devanagari chat.
- **The contract and `_finish` with actions:** a code reply carries `register_warranty` on the Amiigo frame.
- **The whole suite** stays green. Tests that pinned the old case 4 route or the verify-first wording are updated, each named in the PR.

## Rollback

Revert the commits. The lookup, the invoice reading and the tickets are unchanged underneath, so nothing in the stores needs undoing.

## Out of scope

- The app's registration flow itself.
- Changes to invoice reading or to the `warranty_proof` ticket's contents.
- The dealer persona.
- Changing the knowledge records (frozen).
