# Bikes from OMS orders: design

10 October 2026 · Sagnik Mukherjee · approved in chat on 10 October 2026 · status: spec, awaiting review

## Why

A rider's bikes come from `em_purchase`, OMS's warranty registration table (spec 2026-10-08). A row exists there only when someone registers the warranty, so most sold bikes are missing: on 10 October 2026, 91,436 sold frames could be tied to an order through `em_stock_transactions`, and 52,537 of them had no `em_purchase` row. A rider with such a bike is told it is not registered (warranty step, case 4).

`em_orders` carries a phone on every order. For now we assume that phone is the customer's, and give a verified rider the bikes sold on orders with their number.

## Decisions taken (10 October 2026)

| Question | Decision |
|---|---|
| Which orders count | Every order except `Stock Transfer`, and none at all when the phone is a dealer's (the check registrations already use) |
| Purchase date | The order's `invoice_at`, as a date in India time |
| A frame registered in `em_purchase` under any number | Registration wins: the frame is never given to another phone through an order |
| Where the phone is | `em_orders.mobile` (the person's answer; the grant in section 6 fails if the column is wrong) |
| Approach | A second query in `tools/oms_db.py`, same role, connection, cache and breaker |

## 1. Switch

- `EMOTORAD_OMS_ORDERS`, exactly `on`, and only with `EMOTORAD_OMS_PG_DSN` set. Set by `deploy-staging.yml`.
- `/health` gains `"oms_orders"`: `"on"`, or `"off"` (switch not `on`, or no database setting).
- **Rollback:** remove `-e EMOTORAD_OMS_ORDERS=on` from `deploy-staging.yml` and redeploy. Riders go back to registrations only. Nothing is stored, so nothing needs undoing.

## 2. The query (`tools/oms_db.ORDERS_SQL`)

One query per phone, by the phone's last ten digits (the `_LAST10` expression the registrations query uses, so every stored shape is found). It returns one row per frame:

1. **The phone is a dealer's:** the same `dealer_franchises` check as `REGISTRATIONS_SQL` (`em_franchise` mobile or secondary contact, or an `em_users` dealer login). If any row matches, the query returns nothing.
2. **The rider's orders:** `em_orders` rows with `mobile` matching, `deleted_at` null, `cancel_at` null, `is_return` not true, and `order_source` not `Stock Transfer`.
3. **The frames on them:** for every frame with a SOLD row in `em_stock_transactions` on one of those orders, the frame's **latest** SOLD row that carries an order code (by `created_at`). The frame is kept only when that latest row is one of the rider's orders, so a bike resold later belongs to its newest buyer.
4. **Registration wins:** a frame with any `em_purchase` row (`deleted_at` null), under any number, is dropped. Frame numbers are compared trimmed and upper-cased.
5. **Columns returned:** `frame_number` (as stored on the transaction, trimmed), `product_name` (from the transaction), `purchase_date` (`(invoice_at AT TIME ZONE 'Asia/Kolkata')::date`), `order_source`. No order code, name, address or amount.

The session settings are the existing ones: read-only, `TimeZone=UTC`, a 5-second statement timeout.

## 3. The record (`tools/oms_db.order_to_record`)

Each row becomes the record the lookup tool reads, in the shape `to_record` gives a registration:

| Field | Value |
|---|---|
| `frame_number`, `product_name` | from the row |
| `product_color`, `product_id`, `franchise_name`, `full_address`, `customer_name` | `None` |
| `purchase_date` | the row's date, ISO, or `None` |
| `registration_status` | `active` |
| `invoice_on_file`, `invoice_with_support` | `False` |
| `term_source` | `oms_terms` |
| `warranty_api` | `warranty_terms.coverage(purchase_date, today)` |
| `ownership_source` | `oms_order` (registrations get `oms_purchase`) |

So a dated order bike is warranty case 1 and its cover is worked out from the invoice date, part by part, as for a registration. An order with no `invoice_at` gives an undated bike with no invoice on file: case 3, the rider is asked for their invoice.

## 4. Reading and merging (`OMSDatabase`, `db_warranty_source`)

- `OMSDatabase(..., orders=False)`; `orders=True` from `reader_from_env` when the switch is on.
- `OMSDatabase.orders(phone)` runs `ORDERS_SQL` with the same last-ten rule (`last_ten` refuses a foreign number), cache (60 seconds per phone) and connection settings as `registrations`. It has **its own breaker**, so a failing orders query never stops registrations, and registrations' breaker never stops orders.
- `db_warranty_source` returns registrations first, then order bikes:
  - the orders query fails: logged as `oms_orders_unavailable` with the exception's class only, and the registrations are returned alone;
  - the registrations query fails: `oms_unavailable`, as today, whatever the orders query did;
  - both empty: `None`, so `no_warranty_record` and warranty case 4.
- `OMSDatabase.row(phone, frame)` and `invoice_file` keep reading registrations only. An order bike has no OMS invoice file.

Nothing else changes: the verify-first bike list, the merged sources, the warranty step, the date post-check, the coverage post-check and tickets all read the merged list.

## 5. Logging and privacy

- The phone is never logged. `oms_orders_unavailable` carries `error=<class>` only.
- The model sees an order bike as it sees a registered one: model and frame, then cover after the warranty step. It never sees that the bike came from an order.
- This reads customers' order phones. **Sachin signs off before real riders.** For now the phone on an order is assumed to be the rider's; section 8 lists where it is not.

## 6. Set-up (a person; runbook section 9)

The read-only role needs column grants on two more tables:

```sql
GRANT SELECT (order_code, mobile, order_source, invoice_at, cancel_at, is_return, deleted_at) ON em_orders TO <role>;
GRANT SELECT (frame_number, frame_status, order_code, product_name, created_at) ON em_stock_transactions TO <role>;
```

`em_purchase`'s `frame_number` and `deleted_at` are already granted. Without the new grants the orders query fails on permission, which is logged and leaves registrations working.

## 7. Tests

- **Unit, fake connection:**
  - the query runs with the phone's last ten digits, and only when `orders=True`;
  - the merge order: registrations, then order bikes;
  - an orders failure returns registrations and logs `oms_orders_unavailable`; a registrations failure is `oms_unavailable`;
  - the two breakers are separate;
  - `order_to_record`: a dated and an undated row, cover from the date, `ownership_source`.
- **Runtime, through `runtime.handle()`:** a rider whose only bike is on an order is verified, the bike is in their list, and with the warranty step on the step is case 1, not case 4.
- **SQL, opt-in** (`EMOTORAD_TEST_OMS_PG_DSN`, inline rows only, like `tests/test_oms_db_sql.py`): every phone shape found; a dealer's phone gets nothing; a stock transfer, a cancelled, a returned and a deleted order are left out; a frame resold later goes to the newer order only; a frame registered under another number is left out; the date is the India-time date (an invoice at 20:00 UTC is the next day).
- **`/health`:** `oms_orders`.

## 8. Edge cases (Edge Case Register, CAPTURE)

- The phone on the order is the buyer's, not the rider's: a gift, a company purchase, a family member.
- A dealer's number missing from `em_franchise` and `em_users`: that dealer verifying on it sees the bikes on their orders.
- Marketplace orders whose phone is masked or a relay number.
- A frame swapped out in a replacement (`em_order_rr`) still shows on the original order.
- For a dealer order, `invoice_at` is when the dealer bought the bike, so the cover shown may end before the real one.

## Out of scope

- Writing anything to OMS, including registering the bike from the order.
- Orders without a stock transaction, and sales never recorded in OMS (about 57,000 of 1.5 lakh).
- The dealer persona.
- Changing the knowledge records (frozen).
