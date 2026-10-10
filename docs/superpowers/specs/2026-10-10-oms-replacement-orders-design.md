# Replacement orders in OMS: design

10 October 2026 · Sagnik Mukherjee · approved in chat on 10 October 2026 · status: spec, awaiting review

## Why

`place_replacement_order` is a mock: it keeps orders in memory (`fulfilment.ReplacementOrders`) and nothing reaches OMS. Its guards also fall short of the rule the person set on 10 October 2026: **an order is placed automatically in one case only, when the evidence check has passed and the warranty is valid from the `em_purchase` purchase date.** Today:

- it checks only that a photo or video arrived (`evidence_seen`), never the evidence check's verdict;
- it judges every part by the battery's cover (`in_warranty` follows the battery), so a charger, covered for 6 months, is judged on the battery's 12;
- it cannot tell a date from `em_purchase` from an order's `invoice_at` (spec 2026-10-10, bikes from OMS orders);
- its duplicate check lives in memory for 48 hours and is lost on a restart.

OMS creates after-sales orders through the WebSocket action `afs_order_add` (em-biz-backend `order/afs_order.py:40`), which has no protection against a repeat: a second call makes a second order, a second customer row and a second ERP sales order.

## Decisions taken (10 October 2026)

| Question | Decision |
|---|---|
| How the chatbot reaches OMS | The `afs_order_add` WebSocket action, as the OMS admin's super-admin user. Its email, password and token live in Secrets Manager, set by a person. |
| Price | 0 for every warranty part |
| Sale type | `Warranty` |
| OMS product ids | Decided later, once the knowledge base is final. Until then nothing is sent. |
| Customer details | Asked for and read back in the chat |
| Repeats | Never two orders for the same bike and part |
| Evidence | The evidence check's verdict, for battery and motor |
| Cover | Each part by its own term |
| Dates that qualify | Only `em_purchase.purchase_date`. A date read by OCR, from OMS's invoice or the rider's, never gives an order: it gives a Zoho ticket. |
| Scope this round | The gates, the no-repeat ledger and the OMS client tested against a fake OMS, behind a switch that stays off |
| Motor agent | Gets the tool, but it is offered only once a part it may order exists (section 6) |

## 1. The gate (`place_replacement_order`, code)

An order is accepted only when every check holds, in this order; each refusal is a tool error the agent turns into a Zoho ticket (`create_support_ticket`), as refusals do today:

1. **Customer persona, a listed bike.** As today: not the rider's unlisted bike, and the frame is in the set the lookup returned (`_owned_bike`).
2. **Evidence verified** (`evidence_not_verified`).
   - With the evidence check on for the chat (`Runtime._evidence_gated`): `evidence_check.verdict_passed(state.evidence_verdict, state)`. This already requires the same run, the same chosen frame and the same fault component.
   - With it off (local runs, tests): `state.evidence_seen`.
   - The runtime injects the answer as the fact `evidence_verified`; the model never supplies it.
3. **Dated by a registration** (`warranty_not_from_registration`). The bike's `ownership_source` is `oms_purchase`. An order bike (`oms_order`), a bike from the warranty service or the fixtures without the field, and an undated bike are all refused. `ownership_source` is carried into each bike of the lookup result (`_api_coverage`) so the check reads the turn's own result.
4. **The part's own cover** (`part_not_in_warranty`). The part maps to its `warranty_terms` component (battery → battery, charger → charger, controller → controller, motor → motor, display → display); the component's `active` in the bike's `components` must be true. The bike-level `in_warranty` (which follows the battery) is no longer read by this tool. No component entry for the part: `coverage_undetermined`.
5. **The part may be ordered** (as today): in `knowledge/_replacement/parts.yaml` with `technician: false` and `ask: false`. Technician parts stay a dealer job, outside this spec.
6. **The rider's details** (section 2), each typed by the rider, and confirmed.
7. **No open order for this bike and part** (section 3). A repeat returns the existing reference with `already_placed: true`, never a new order.

Coverage from OCR never reaches step 4: an invoice reading writes `invoice_readings` and a `warranty_proof` ticket (`Runtime._with_invoice_result`), never `coverage_result`. A test pins that a bike whose only date is an OCR reading is refused.

## 2. The rider's details

The tool gains these model-supplied arguments, each checked by code:

| Argument | Check |
|---|---|
| `customer_name` | Every word appears in the rider's own messages in this chat (the address check's provenance rule, `customer_messages`) |
| `email` | One `@`, a dot in the domain, no spaces, and typed by the rider in this chat |
| `mobile` | Optional. Absent: the verified phone. Given: an Indian mobile (`oms_db.last_ten`) typed by the rider |
| `address` | As today: house or flat, building or street, area, landmark, pin code, city, with provenance and the pin-code check; or `use_record_address` |

The agent asks for each, reads them back in one message, and calls the tool only after the rider's "yes" (the prompt's existing read-back rule, extended to name, email and mobile). The battery and motor prompts get the same short rule. The values are kept on the order record (section 3), never logged.

## 3. The order ledger (no repeats)

A MongoDB collection, `replacement_orders` (an in-memory store of the same shape for tests and offline), replaces `fulfilment.ReplacementOrders`' dictionary.

- **Reference:** `RO-` and seven digits, from the `counters` collection by one atomic `$inc` (the pattern behind `EM-` tickets). The existing order-claim post-check (`guardrails.check_order_claim`) is widened from five to seven digits.
- **One open order per bike and part:** a unique index on `open_key` = `<FRAME>|<part>` (frame upper-cased, spaces removed), set while the order is open and unset when it is cancelled. Inserting a second open order fails on the index and the tool returns the first one's reference with `already_placed: true`. This holds across chats, runs, restarts and servers.
- **Fields:** `_id` (the reference), `open_key`, `frame_number`, `part`, `product_id` (or null), `customer` (name, email, mobile, address, `pin_code_id` once looked up), `conversation_id`, `ticket_reference` (the EM- ticket raised with it), `status`, `oms` (`intent_at`, `order_code`, `order_id`, `attempts`, `last_error`), `created_at`, `updated_at`.
- **Statuses:** `recorded` (saved, not to be sent: switch off or no product id), `queued` (to be sent), `sent` (OMS order found or created; `oms.order_code` set), `failed` (gave up; alarm raised), `cancelled` (by a person; `open_key` unset).
- **Permanent,** like `tickets`. Erasure: `scripts/delete_person.py` and `erasure_admin` remove the rider's orders by phone; this is added to both.
- `mongo_setup.py` creates the collection and its unique partial index on `open_key`. Until it has, the API keeps orders in memory and `/health` says so (`replacement_orders: memory: index missing`), as the receipts do.

## 4. Sending to OMS (the order worker)

A daemon thread in the API process, started by the lifespan only when the switch is on, modelled on the Zoho worker (`zoho/worker.py`). It takes one `queued` order at a time under a five-minute lease.

For each order:

1. **Look first.** Search OMS for our reference: `afs_order_list` with `search=<reference>` (it matches `ticket_id`). Found: record `order_code` and `order_id`, status `sent`, and stop. Nothing is ever sent twice.
2. **Record the intent.** Save `oms.intent_at` before sending. If that save fails, send nothing.
3. **Send** `afs_order_add` (section 5).
   - Status 200 (OMS's `OK_CODE`) with an order: record it, status `sent`.
   - Any other answer, a timeout or a dropped socket: **look again** (step 1) before deciding. OMS can fail after it has written the order (its `except` answers an error for anything raised after the insert), so an error never means "no order".
   - Not found after the look: count the attempt and retry later.
4. **Retries:** after 1, 5, 15 and 60 minutes, then hourly. After 8 attempts, or 24 hours, status `failed`: log `replacement_order_failed` at error level (reference and error class only) and leave it for a person. A `failed` order keeps its `open_key`, so the bot never orders the part again by itself.

An order that the look finds was split by OMS (a child `<code>/n` when stock is short) is `sent`; the parent's `order_code` is kept.

## 5. The OMS client (`oms_afs.py`)

**Settings** (Secrets Manager, set by a person; never read, printed or written to a file by a Claude session; never logged):

| Name | Holds |
|---|---|
| `EMOTORAD_OMS_WS_URL` | The WebSocket base, for example `wss://omsapi.emotorad.com/ws/` (staging: the staging host's) |
| `EMOTORAD_OMS_LOGIN_URL` | The REST login, for example `https://omsrest.emotorad.com/user/login` |
| `EMOTORAD_OMS_ADMIN_EMAIL`, `EMOTORAD_OMS_ADMIN_PASSWORD` | The admin user |
| `EMOTORAD_OMS_ADMIN_TOKEN` | The admin's token (optional) |

**The token.** The stored token is used first. If OMS refuses it (the connection answers `url: unauthorized`, or the response's status is 403, OMS's `AUTH_CODE`), the client logs in with the email and password (`POST user/login`, `device_type: "web"`, `device_id: "emotorad-ai-chatbot"`), keeps the new token in memory, and tries once more. OMS's own code ends a non-dealer token at the next midnight India time (`user/auth.py`), so this re-login is expected. A socket that closes before answering is a call error, never a refusal: OMS can still process a frame on a closing socket, so the client neither logs in again nor resends. A login does not end other sessions of the same user. A failed login is logged as `oms_login_failed` with the HTTP status only.

**One call, one socket.** Each call opens `<ws_url><token>/`, sends one frame and waits up to 20 seconds for the answer, then closes:

```json
{"transmit": "single", "url": "afs_order_add", "client_ref": "<reference>:<attempt>", "request": {...}}
```

OMS echoes the whole frame back with a `response` key added (`{"status", "msg", "data"}`); the answer is the frame whose `url` and `client_ref` match ours. Frames that do not match are ignored.

**The order sent** (`afs_order_add` `request`):

| Field | Value |
|---|---|
| `order_type` | `CO` |
| `bill_customer_name`, `ship_customer_name` | the rider's name |
| `bill_mobile`, `ship_mobile` | the rider's mobile, ten digits |
| `bill_email`, `ship_email` | the rider's email |
| `bill_pin_code_id`, `ship_pin_code_id` | OMS's id for the pin code, read through the read-only database role (`em_pin_code`, already granted) |
| `bill_address`, `ship_address` | house or flat, building or street |
| `bill_address2`, `ship_address2` | area, landmark, city |
| `items` | `[{"product_id": <id>, "product_qty": 1, "is_demo": false, "rate": 0, "amount": 0, "idx": 1}]` |
| `sale_type`, `sale_type_id` | `Warranty`, and its id from `sale_type_list` (looked up once and kept) |
| `frame_number` | the bike's frame |
| `ticket_number` | our reference (`RO-…`): this is what the look in section 4 finds |
| `remark` | `Warranty replacement placed by the EMotorad support chatbot. Ticket <EM-reference>.` |

`zoho_ticket_id` is not sent: our Zoho ticket number exists only after the Zoho worker has sent it.

**Search.** `afs_order_list` with `{"search": "<reference>", "limit": 5, "page_no": 1}`; a match is a row whose `ticket_id` equals the reference exactly.

## 6. Product ids

- `fulfilment.ItemCodes` is replaced by `ProductIds`: (bike model, part) → OMS `product_id`. It is empty until the product ids are agreed.
- An order with no product id is saved as `recorded` with its ticket, and never sent.
- **The motor agent** is given `place_replacement_order` with the same gate, but the tool is offered to an agent only when at least one part it may order (section 1, step 5, and a `ProductIds` entry for the rider's bike) exists. Today none does, so the motor agent is not offered it; motor faults keep going to the Approval team as tickets.

## 7. Switch, health and rollback

- `EMOTORAD_OMS_AFS_ORDERS`, exactly `on`, with the five settings above present. Off (the default, and staging until Sachin agrees): orders are saved as `recorded` and ticketed, nothing is sent, no worker starts.
- `/health` gains `oms_afs_orders`: `on`, `off`, or `misconfigured: <setting names missing>`; and `replacement_orders`: `mongodb` or `memory: <reason>`.
- **Rollback:** set the switch off (or remove it) and restart. Orders already sent stay in OMS; a person cancels them in the OMS admin (`afs_order_cancelled_reject_return`), which does not cancel the ERP sales order.

## 8. What the rider is told

As today: the reply names the `RO-` reference and says the replacement is being arranged and the support team will confirm. Code never says the part has shipped. With the switch on, nothing about OMS's answer is told in the same turn.

## 9. Tests

- **Gate**, through the registry:
  - evidence check on and no passing verdict → `evidence_not_verified`; a verdict for another frame or component → refused; evidence check off and `evidence_seen` → allowed;
  - an order bike (`oms_order`), a bike with no `ownership_source`, and an undated bike → `warranty_not_from_registration`;
  - a bike whose only date is an OCR reading → refused;
  - a charger past 6 months with the battery inside 12 → `part_not_in_warranty`; the battery on the same bike → allowed;
  - a name or email the rider never typed → refused; a malformed email → refused.
- **Ledger:** two calls for the same bike and part, in the same chat and in two chats → one order, the second `already_placed`; after a cancellation a new order is allowed; the reference is `RO-` and seven digits; the order-claim post-check accepts it and blocks an invented one.
- **Worker and client**, against a fake OMS WebSocket server:
  - a found order is never sent;
  - a refused token → one login → one send;
  - an error answer with the order created anyway → `sent`, one order;
  - a timeout, then a crash between sending and recording → the next pass finds it, one order;
  - the kill drill: OMS down, an order queued, OMS back → exactly one order;
  - the frame sent carries rate 0, sale type `Warranty`, our reference in `ticket_number`, and no setting value appears in any log or error.
- **Conversation** through `runtime.handle()`: a rider with a registered, covered bike and a passing verdict gives their details, confirms, and gets an `RO-` reference; the same chat with an order bike gets a ticket and no order.
- **`/health`:** both new keys.

## 10. Risks noted, not changed here

- The OMS WebSocket logs every incoming frame in full at info level (`core/consumers.py`), so the rider's details appear in OMS's logs. NOT OURS.
- An inactive token's frame is still processed (`websocket_receive` sends a close but does not return). NOT OURS.
- `afs_order_add` posts to the ERP synchronously, with no timeout, inside a transaction on the split path. NOT OURS; it is why the worker never trusts an error.
- A super-admin token in our secret store can do far more than place orders. The person's decision, with Sachin.

## Out of scope

- The OMS product ids and the model-to-product table's contents.
- Technician-fitted parts and dealer fitment.
- Telling the rider later when the order ships.
- Any change to OMS.
- Changing the knowledge records (frozen).
