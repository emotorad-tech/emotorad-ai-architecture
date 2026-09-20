# Replacement fulfilment: design

Written 2026-09-20. Decided with the business owner in one session; every rule
below was stated by them or explicitly agreed. Where I made a call, it says so.

This closes the open item the 2026-09-20 handoff called "fulfilment": what
happens once the bot has concluded a part needs replacing. Today the flow ends
at "get it to a service centre", which is wrong for a straight replacement and
is what the customer complained about.

## The rule in one paragraph

When the bot is sure a part needs replacing, it initiates the replacement
itself. If the part needs no technician, it confirms the delivery address,
takes payment when out of warranty, places the order in the OMS on the
customer's behalf, and gives them the order id. If the part needs a technician,
it routes them to a dealer and gets the part to wherever the customer chooses.
A support ticket is raised alongside the order in every case. A configurable
approval mode decides whether the bot's decision stands on its own or waits
for a human.

## Scope

In: India, the customer persona, website chat and WhatsApp, the parts in the
decision table, in-warranty and chargeable, the technician and no-technician
branches, approval modes, the payment boundary.

Out, and named so nobody assumes otherwise: Europe and UK payment (Razorpay is
India only), real dealer stock (assumed absent), the bot asking the dealer on
the dealer line (needs the dealer persona, R4), waste battery collection (the
customer disposes of the old part; there is no process), real ERP and OMS
writes in the first build (every write is mocked until the flow has been
reviewed in the playground, per `CLAUDE.md`).

## Where the logic lives

In code, in this service, as a deterministic `fulfilment` module the model
reaches through tools. The model may choose from sets the tools offer and may
confirm things with the customer. Code decides technician or not, sure or not,
approve or not, what the price is, what the item code is, and whether an order
already exists. The OMS owns the order once placed.

Rejected: in the prompt (the model skipped a step and improvised the impact
hedge on 2026-09-20; a prompt cannot be trusted with a decision tree that ends
in a shipment), and in the OMS (no such flow exists there, 8848 do not own it,
and it can move there later without the conversation changing).

## Definitions

**Sure.** All four hold: a knowledge record's flow reached its concluding step;
evidence arrived in the conversation (`ConversationState.evidence_seen`); the
part named is one that record allows for that conclusion; the coverage
post-check passes. Every one of these is a fact the runtime already holds.
Anything else is **not sure**: the model concluded without a photo, the record
says "service-centre diagnosis", the part is not in the record's list, the
part has no resolvable item code. The model's own confidence is never
consulted. This is the one definition the approval modes depend on, and it is
deliberately something code can check. It is evaluated once, at the moment the
order would be placed, with every fact above already collected; nothing is
approved or refused earlier than that.

**Technician.** Whether fitting the part needs a technician's hands. Anything
involving fitment, alignment or complicated steps does. The table below is
the source of truth and is business data, not code.

**In flight.** An open replacement order, or an unpaid payment link, for the
same frame and part, less than 48 hours old.

## The decision table

A YAML file under `knowledge/`, one row per part, because the business will
change it and Git is the audit trail:

| part | technician | ask the customer |
|---|---|---|
| battery | no | no |
| charger | no | no |
| display | no | yes |
| seat | no | yes |
| motor | yes | |
| controller | yes | |
| brakes | yes | |
| suspension | yes | |

`ask the customer` means the bot offers self-fit or dealer and the answer picks
the branch. A part not in the table is not sure, by definition.

Region does not gate the table. It sets the default the bot leads with: India
leads with the table, Europe and UK lead with self-fit for everything, and
both are offered both. (Owner's call: "not a hard gate, a preference.") That is
design intent so the table is built region-aware from the start; this round
builds India only, as the scope says.

## Approval modes

One setting, `approval_mode`, three values. Business-facing names are for the
panel that will sit over this later; the setting exists in config from day one
so the panel is a UI over it and not a rewrite.

| value | panel name | sure | not sure |
|---|---|---|---|
| `bot` | Bot in love with customer | bot approves | bot approves |
| `reasonable` | Reasonable bot | bot approves | human |
| `human` | No brain, human approval only | human | human |

An order the bot may not approve is still placed, in `pending_approval`, with
the ticket carrying the photographs. The customer is told it is raised and who
confirms it. Nothing waits silently and nothing is dropped.

Launch on `reasonable`. (My recommendation; owner agreed to the three modes,
the launch value is mine.)

## The no-technician branch

In order, each step a tool or a code check:

1. **Conclude.** The knowledge record flow ends with a named part. Existing.
2. **Coverage.** `lookup_warranty_record`, remembered for the conversation,
   and the coverage post-check. Existing, fixed 2026-09-20.
3. **Already in flight?** Code checks for an open order or unpaid link for
   this frame and part inside 48 hours. If one exists, the bot reports it and
   stops: "that is already on its way, order EM-xxxx". This is idempotency,
   the same rule every write tool here follows, not a judgement about the
   customer. It exists because the chat page loses its conversation on reload
   (open bug), because requests retry, and because a customer asking "did that
   go through?" tomorrow must not get a second battery.
4. **Item code.** `product_id` from the warranty record, to the ERP Item, to
   its BOM, to the component's item code. Read only. The ERP is 8848's; the
   read is prepared here and exposed by them. A part with no resolvable item
   code is not sure. Mocked in the first build.
5. **Address.** `full_address`, `pin_code_id` and `state_id` from the
   warranty record, read back to the customer: "send it here?". Confirmed, or
   replaced with what they type, which goes on the order as a customer-stated
   address.
6. **Price**, chargeable only. From the ERP by item code. The bot never states
   a figure it did not get from this call; a post-check enforces it the way
   the coverage check does.
7. **Payment**, chargeable only. See the payment boundary below.
8. **Place the order.** `place_replacement_order`: frame, item code, address,
   coverage outcome, approval state, idempotency key. Into the OMS, where
   stores and logistics pick it up. Mocked in the first build.
9. **Ticket.** `create_support_ticket` as today, carrying the photographs and
   the order id. Both exist; the ticket is the evidence trail, the order is
   the action.
10. **Tell the customer.** Order id, ticket id, that logistics will contact
    them with a date, and any safety instruction the record carries.

What Krishna hears, for the melted terminal:

> That's a heat fault at the terminal, which is a defect: it's covered, and
> your warranty runs to April 2027 on our record. I'll send a replacement
> battery. Is this still the right address: [full_address]? ... Done. Order
> EM-xxxx, and I've put the photos on ticket EM-xxxx with it. Keep the old
> battery off the bike and don't charge it. You'll hear from logistics with a
> delivery date.

No service centre, no impact, no figures.

## The payment boundary

Order after payment. That crosses a boundary the conversation does not
control: the customer goes away, pays or does not, and Razorpay tells us by
webhook, minutes or days later, possibly after the conversation has ended.

- `create_payment_link` records a pending replacement against the frame and
  part, with the amount and the link, and gives the customer the link. The
  order does not exist yet.
- A webhook endpoint receives Razorpay's confirmation and places the order
  through the outbox. Never inline in the request. This is the org rule that
  side effects never happen inside a transaction, and it is the root cause of
  the 2026 OMS-to-ERP incidents, so it is not negotiable.
- Unpaid links expire after 48 hours. An expired link is not in flight.
- A customer returning in a new conversation finds the pending replacement
  and its state, and the bot says where it stands rather than starting again.
- The setting is per region. Europe's payment is not built in this round.

## The technician branch

1. Say a technician is needed and why (fitment, alignment).
2. Offer two dealers: the selling dealer from the warranty record, and the
   nearest by pincode. The customer picks. (Owner's call.)
3. Stock is assumed absent. The bot never says a dealer has the part. It
   offers the part to the customer's address or to the dealer, and then a
   visit. Existing `find_service_slots` and `book_service_slot` for the visit.
4. In warranty: the part is free and goes wherever chosen. Out of warranty:
   the customer pays at the dealer. The bot generates no link and says so
   plainly: the dealer confirms the cost before any work.
5. Deferred, and the bot says so in as many words: whether the dealer has it
   ("call them; if it's not there, come back and I'll raise it"), and the bot
   raising it with the dealer itself, which needs the dealer persona.

Both ends melted: the controller needs a technician and the battery does not.
The bot says so, offers both parts to home or both to the dealer, and the
customer decides. If both go home, the ticket records the controller as
unfitted so nobody assumes the job is done.

## Guardrails, all in code

- Item code and price come only from tool results. A reply stating a price or
  a part that no tool returned is blocked, by a post-check shaped like the
  coverage one.
- The address is read back before any order is placed.
- The approval mode is consulted by code. The model does not know which mode
  is set and cannot argue with it.
- Idempotency on every write: order, payment link, ticket.
- Region-aware payment. An India link is never generated for a Europe
  customer; there is no Europe link yet.
- Every write is mocked until the conversational flow has been reviewed in the
  playground. Real OMS and ERP wiring is its own change with its own review.

## Data this needs and where it comes from

| fact | source | exists today |
|---|---|---|
| customer address, pincode, state | warranty record `full_address`, `pin_code_id`, `state_id` | yes |
| selling dealer | warranty record `franchise_id` | yes |
| nearest dealer by pincode | OMS dealer table | to confirm |
| bike model | warranty record `product_id` | yes |
| replacement item code | ERP: Item, BOM, component | to build, 8848 |
| price | ERP by item code | to build, 8848 |
| dealer stock | none | assumed absent |
| open replacement orders per frame | OMS | to build |
| payment status | Razorpay webhook | to build |

## Build order

1. No-technician, in-warranty, India. Every tool mocked. Approval mode
   `reasonable`. The in-flight check. The decision table. This is Krishna's
   case and it is the whole first build.
2. Chargeable: price, link, webhook, outbox, expiry.
3. Technician branch: dealer choice, part routing, visit booking.
4. Real ERP reads and OMS writes behind the mocks, with 8848 for the ERP.

Each step is reviewed in the playground before the next starts, and the
conversation tests for each are written before the tools.

## Open questions, for the record

- "48 hours" was given once, under duplicate orders. This spec applies it to
  both the in-flight window and the payment link expiry. Correct it if only
  one was meant.
- Whether `pending_approval` orders are visible to the customer as an order id
  before approval, or only as a ticket. This spec gives the order id, on the
  grounds that the customer should have a reference either way.
- Turnaround wording for a home delivery. The ticket says "within 24 hours on
  working days"; a battery to a home is a different promise and nobody has
  set it.
