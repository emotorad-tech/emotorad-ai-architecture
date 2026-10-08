# Bikes and warranty from OMS production, with invoice OCR

Date: 8 October 2026. Status: design agreed in chat on 8 October 2026; this spec awaits the person's review.

## Why

The chatbot will serve every channel (Amiigo, WhatsApp, the website and more), so it must not depend on
Amiigo's services for anything but who the customer is. Today a customer's bikes and cover come from
the warranty service (`searchRegistrationsByPhone`, Amiigo's backend, `tools/warranty_api.py`) and the
Amigo database (`tools/amigo.py`). From this change they come from OMS production, the system of record
for registrations, and a missing purchase date is resolved from the invoice by OCR.

## The person's decisions (8 October 2026)

1. Identity is unchanged. Each channel yields a verified phone: the Amiigo access token, WhatsApp's
   number, or the website's one-time code. After that, nothing is read from Amiigo.
2. A customer's bikes are the frames registered to their phone in OMS production (`em_purchase`).
3. When the phone is also a dealer's, the registrations sold by **that dealer's own dealership** are left
   out; registrations from any other seller stay (a dealer can buy a bike elsewhere as a customer).
4. The chatbot reads the OMS production database directly, through its own read-only role.
5. Warranty starts on `purchase_date` and runs per part: display 6 months, charger 6, controller 12,
   battery 12, motor 12, frame 60.
6. No purchase date, invoice on file in OMS: the invoice is read by OCR (Gemini Flash through
   OpenRouter) and a ticket is raised with what it read.
7. No purchase date and no invoice in OMS: the customer is asked to upload one; it is read the same way
   and the same ticket is raised.
8. A confident OCR date is told to the customer, labelled as read from their invoice and to be confirmed
   by a support executive. **Sachin signs off before real customers hear it**, as on 6 October.

## What the data looks like (OMS production, read 8 October 2026, counts only)

- `em_purchase`: 61,979 live registrations; every one names a seller (`franchise_id`). 55% have no
  `purchase_date`. 4,184 rows repeat a frame already registered. Phones are stored as ten digits
  (55,158), `+91…` (753), `0…` (253), `91…` (26) and 5,795 other shapes; there is no index on `mobile`.
- Dealer numbers: `em_franchise.mobile` and `secondary_contact` (the dealership), and `em_users.mobile`
  where `user_type` is `franchise_manager` or `sale_franchise_person`, linked to the dealership by
  `related_id`. 769 registrations sit on a dealer's number; 218 of them on the selling dealer's own.
- The invoice is `em_purchase.invoice_image`, a file id. OMS serves it at
  `GET https://omsrest.emotorad.com/file/download/<file id>` with `X-API-KEY` (em-biz-backend
  `file/views.py` `download_file_without_login`); an unknown id answers 400.

## Design

### 1. Bikes for a phone: `tools/oms_db.py`

A read-only Postgres reader on the OMS production database, in the pattern of `tools/amigo.py`
(psycopg, a connect timeout, `application_name`, a short circuit breaker after a failure, errors that
carry the exception's class and never the connection string). Switched on by `EMOTORAD_OMS_PG_DSN`.

One query per lookup, by the verified phone only, matched on its last ten digits so every stored shape
is found:

```sql
WITH dealer_franchises AS (
  SELECT id FROM em_franchise
   WHERE deleted_at IS NULL
     AND (right(regexp_replace(coalesce(mobile, ''), '[^0-9]', '', 'g'), 10) = %(m10)s
       OR right(regexp_replace(coalesce(secondary_contact, ''), '[^0-9]', '', 'g'), 10) = %(m10)s)
  UNION
  SELECT related_id FROM em_users
   WHERE deleted_at IS NULL AND related_id IS NOT NULL
     AND user_type IN ('franchise_manager', 'sale_franchise_person')
     AND right(regexp_replace(coalesce(mobile, ''), '[^0-9]', '', 'g'), 10) = %(m10)s
)
SELECT DISTINCT ON (p.frame_number)
       p.id, p.frame_number, p.product_name, p.product_id, p.product_color, p.purchase_date,
       p.created_at, p.franchise_id, p.franchise_name, p.invoice_image, p.status, p.customer_name
  FROM em_purchase p
 WHERE p.deleted_at IS NULL
   AND right(regexp_replace(coalesce(p.mobile, ''), '[^0-9]', '', 'g'), 10) = %(m10)s
   AND coalesce(p.frame_number, '') <> ''
   AND (p.franchise_id IS NULL OR p.franchise_id NOT IN (SELECT id FROM dealer_franchises))
 ORDER BY p.frame_number, (p.purchase_date IS NULL), p.updated_at DESC
```

- `m10` is the verified phone's last ten digits; a phone that is not an Indian mobile never reaches the
  database (as `tools/oms.py` already does for the API).
- One row per frame: a frame registered twice keeps the row with a purchase date, then the newest.
- Only these columns are read. `customer_name` is read because a verified customer's own ticket already
  carries their name (spec 2026-10-05), and `full_address` because the replacement flow reads it back
  before an order; nothing else personal (email, date of birth, the referral's phone) is selected.
- Rows map onto the record shape `lookup_warranty_record` and the agents already use (frame number,
  bike model, `purchase_date`, seller, `invoice_on_file`, the computed coverage below), so the agents,
  the coverage post-check, enrichment and the Zoho payload keep working.
- Outcomes never collapse (the CLAUDE.md rule): rows → bikes; no rows → `no_warranty_record` (Late
  Warranty Registration); any database error or the breaker open → `oms_unavailable`.
- A short cache per phone (60 seconds) so hydration and the tool in the same turn do not query twice.

**Source order** (`api._build_registry`): the OMS database when `EMOTORAD_OMS_PG_DSN` is set, then the
warranty service, then the OMS API, then the fixtures. Turning the warranty service and the Amigo reads
off is removing `EMOTORAD_WARRANTY_API_KEY` and `EMOTORAD_AMIGO_PG_DSN` from the config store; `/health`
shows the source in use (`warranty_source: oms_db`).

### 2. Cover from the purchase date: `warranty_terms.py`

The one place the terms live (it replaces the provisional 24 months of `fixtures.warranty_term_months`):

| Part | Months |
|---|---|
| display | 6 |
| charger | 6 |
| controller | 12 |
| battery | 12 |
| motor | 12 |
| frame | 60 |

- Each part's cover: `valid_from` = purchase date, `valid_until` = the day before the same date `months`
  later, `active` = today is on or before `valid_until`. The same components shape the warranty-service
  path produced (`tools/mocks._api_coverage`), so `battery_support`'s coverage line and the coverage
  post-check read it unchanged.
- The bike's `in_warranty` follows the battery's part, as since 7 October.
- No `purchase_date`: coverage is undeterminable; the tool returns `purchase_date_missing` with
  `remedy: collect_purchase_proof` as today, plus `invoice_on_file: true | false`.
- `created_at` is never used as a purchase date (the CLAUDE.md rule stands).

### 3. Invoice OCR: `invoice_ocr.py`

**Reader.** Gemini Flash through OpenRouter, in the pattern of `photo_check.py` and `serial_read.py`
(JSON response, zero data retention, stable error codes). It is given the invoice (an image, or a PDF
when OMS or the customer has one) and the bike's frame number, and answers JSON:
`{invoice_date, seller, frame_numbers: [...], product, legible}`. It never sees anything the customer
typed.

**Confident** only when all hold, checked by code:
- `invoice_date` parses as one day-first date, is not in the future and is not before 1 January 2019;
- one of `frame_numbers` equals the bike's frame number (ignoring case, spaces and dashes);
- `legible` is true.

**Two ways in.**

- *Invoice on file in OMS.* When the warranty tool returns `purchase_date_missing` with
  `invoice_on_file`, the runtime starts the read in the background for that frame: it downloads the file
  with the OMS API key, keeps a copy in the media bucket under the customer's tree, and reads it. The
  reply to that turn says the bot is checking their invoice; the result is used on the next turn, or
  this one when it is back within the same wait as the serial reads (15 seconds).
- *No invoice in OMS.* The agent (late warranty, battery or motor) asks the customer for a photo or PDF
  of their invoice. A stored upload in a conversation whose chosen bike has no purchase date and no
  invoice on file is read the same way, beside the turn.

**What the customer hears**, written by code and appended to the agent's reply, never by the model:
- confident: "Going by your invoice dated 12 March 2025, your battery is covered until 11 March 2026,
  and the other parts as their terms run. A support executive will confirm this." (the part the chat is
  about leads; dates in the reply's language; Hindi drafts for a Hindi speaker to check);
- not confident: "Thanks, I have passed your invoice to our support team. A support executive will
  confirm your warranty."
- The coverage post-check accepts the invoice date and the derived end dates in the code-written line
  only; a model sentence stating an invoice date or an end date is still blocked (the guardrail built on
  `feat/warranty-status`, W-5 to W-29, comes across with this work).

**The ticket.** One `warranty_proof` ticket per frame, raised by code through the existing
`submit_warranty_proof` path with an idempotency key of the frame, carrying: the frame number, the bike,
where the invoice came from (OMS or the customer), the OCR findings (date, seller, frame numbers read,
product, confident or not), the covered-until dates if confident, the invoice copy's S3 key, and "Warranty
check: not confirmed yet". Nothing is written to OMS; a person confirms in OMS admin.

**Storage.** Each reading goes to a new `invoice_readings` collection (`_id` the invoice copy's S3 key):
the conversation, the person, the frame, the source (`oms` or `customer`), the findings, `confident`,
`read_at`, and the ticket reference. Permanent, erased with the person or the conversation, as
`serial_readings` is. Values are never logged; the log carries outcomes and codes only.

### 4. The OMS API key

Used for one thing: downloading invoice files. The order-number fallback in verification (the account
finder, `tools/oms.live_account_finder`) does **not** use it while `EMOTORAD_AI_DEV_CODES=1`, because the
dev-code page plus an order number would let anyone verify as the order's owner (the warning in
`docs/runbooks/config-store.md`). The key is shared with other systems and can write (memory:
key rotation pending); it is never logged and never sent to the model.

## Configuration and rollout (a person does these)

1. **Sachin creates the role** on OMS production, read-only, with column grants only:
   `em_purchase (id, mobile, frame_number, product_name, product_id, product_color, purchase_date,
   created_at, updated_at, deleted_at, franchise_id, franchise_name, invoice_image, status,
   customer_name, full_address)` (`full_address` added during planning: the replacement flow reads
   the address back before an order), `em_franchise (id, mobile, secondary_contact, deleted_at)`,
   `em_users (mobile, user_type, related_id, deleted_at)`.
2. **Sachin opens the network path** from the staging EC2 instance to the OMS database (its security
   group, port 5432).
3. **Config store**: add `EMOTORAD_OMS_PG_DSN` and `EMOTORAD_OMS_API_KEY`; remove
   `EMOTORAD_WARRANTY_API_KEY` and `EMOTORAD_AMIGO_PG_DSN`. A person enters the values; a Claude session
   never sees them.
4. Redeploy; `/health` shows `warranty_source: oms_db`, `invoice_ocr: openrouter`.
5. Optional, for speed: an index on `em_purchase (mobile)` through a reviewed em-biz-backend migration.

Rollback: remove `EMOTORAD_OMS_PG_DSN` and redeploy (the next source in the order takes over).

## Testing

- `oms_db`: the SQL against a local Postgres in the `local_check.sh` pattern (opt-in), and unit tests
  with a fake connection: the four stored phone shapes match; a dealer's own-shop registrations are left
  out and another seller's kept; a repeated frame keeps the dated row; no rows, an error and the breaker
  each give their own outcome; nothing but the listed columns is selected.
- `warranty_terms`: each part's end date, the day-before rule across month ends and leap years, the
  battery deciding `in_warranty`, no purchase date.
- `invoice_ocr`: parsing and the confidence rules (future date, before 2019, two-digit years, a frame
  mismatch, illegible); the OMS download's errors; the customer-upload path; the code-written reply in
  each case; one ticket per frame however often it runs; values never logged; erasure.
- Conversations through `runtime.handle()`: a dated bike, an undated bike with an invoice on file, an
  undated bike without one (the ask, the upload, the reply and the ticket), and a dealer's number.
- The whole suite, and the retrieval evals unchanged.

## Out of scope

- Writing a purchase date back to OMS (a person does it in OMS admin).
- Dealers' own warranty questions (the dealer persona).
- Per-model terms beyond the six parts above.

## Sign-offs needed

- Sachin: the read-only role and network path; the labelled OCR date before real customers; the use
  of the shared OMS API key for invoice downloads.
