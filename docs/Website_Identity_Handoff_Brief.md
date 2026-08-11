# Website identity — full context handoff

**Purpose of this file:** paste it into a fresh Claude (or any coding assistant) session as the
opening context, then start building. It carries the design, the reasoning behind each decision, the
code contracts, and — most usefully — the bugs a reference implementation already hit, so you do not
hit them again.

**Who is building what:** Vikas (frontend/BFF, Next.js `output: 'standalone'`) and the Nest backend
team. The AI/agent side is built separately and *consumes* what you build here; it does not need you
to know anything about it beyond two endpoints, described in §10.

**Status:** design is settled and has a working reference implementation in Python (the AI repo).
The rules in §6 and §7 are not suggestions — they are reproduced from tested code, and a wrong merge
is unpickable after the fact.

---

## 1. The problem, stated plainly

Today every visit to emotorad.com is a stranger. We cannot tell that the person reading the Doodle
page this morning is the same person who compared three e-cycles last week, or the same person who
messages us on WhatsApp tomorrow.

That matters more for EMotorad than for a typical D2C brand because **roughly 85% of our volume goes
through 350–600 dealers, not website checkout.** The website is largely a *discovery* surface that
hands the customer off — to WhatsApp, to a phone call, to a showroom. So the most valuable
information we ever collect (what someone researched, what they compared, what they were about to
spend) is generated online and then thrown away at exactly the moment it becomes useful.

A concrete journey, and what we retain from it today:

| | What happens | What we know |
|---|---|---|
| 10 Aug | Lands from an Instagram ad on a laptop. Compares three e-cycles. Leaves. | Nothing |
| 15 Aug | Returns on their phone. Looks at two more. Opens the EMI page. | Nothing |
| 20 Aug | Messages WhatsApp: "which is the nearest dealer?" | A phone number |

The dealer receives a phone number and no idea this person spent two sessions comparing models in
the ₹30–40k band and checked EMI terms.

**Timing is not negotiable.** This has to land *during* the revamp. A cookie you did not set on
launch day cannot be backfilled — every day without it is permanently unattributable traffic. There
is no migration that recovers it later.

---

## 2. Three ideas people constantly conflate

| | What it is | What it identifies |
|---|---|---|
| `em_aid` | A UUID in a first-party cookie, set by **your server** | **A browser.** Chrome on a laptop is one; Safari on their phone is another |
| `cluster_id` | A UUID with no cookie behind it — exists only in the database | **A person.** All their browsers, phone numbers, WhatsApp IDs |
| `identities` | One row per identifier, each pointing at a `cluster_id` | The mapping between the two |

The thing that trips everyone up: **`cluster_id` is not a user ID.** There is no `users` row behind
it. A cluster just means "these identifiers are the same human", and it exists from the first
anonymous page view — before we know anything at all about the person.

---

## 3. Where each piece lives

The website is a Next.js app (BFF + client). The identity and analytics backend is a **separate Nest
service**. One consequence is structural: **the cookie is the only piece that cannot move to Nest**,
because only the thing serving HTML on the domain can `Set-Cookie` on that response.

| | Next.js (BFF + client) | Nest (identity / analytics) |
|---|---|---|
| Mint + set the cookie | ✅ middleware | ❌ cannot |
| Read the cookie | ✅ only place that can | ❌ never |
| `identities`, `events`, `profile_cache` | ❌ | ✅ owns all three |
| `link_identity` + merge logic | ❌ | ✅ |
| `resolve` / `context` / `erase` endpoints | ❌ | ✅ |
| Knows what a `cluster_id` is | ❌ | ✅ |

### The rule that follows

> **Next.js never sends or receives a `cluster_id`.** Not the client, not the BFF. It sends
> `em_aid` to Nest; Nest resolves the cluster internally and never returns it.

The one place it could come up later is homepage personalisation — and there the BFF asks Nest "what
should I show this `em_aid`?" and Nest replies with **products**, not an ID.

> **Nest never reads a cookie.** It receives `em_aid` as an explicit parameter from a trusted caller.

That is a real security shift worth stating out loud. Today `em_aid` would be trustworthy *because*
it came from a cookie. Once Nest is a separate service it is just a value in a request body — so
**Nest's endpoints must not be publicly reachable.** Service-to-service auth (shared secret header,
mTLS, or network policy). Otherwise anyone can POST events and link identifiers against any UUID
they like.

```
browser  ──(cookie rides automatically)──▶  Next BFF
                                              │ reads cookie, forwards explicitly
                                              ▼
                                        Nest   /events   /identity/link
```

The AI bot calls Nest directly (`/identity/resolve` → `/identity/context`), never through Next.

---

## 4. The cookie

| Property | Value |
|---|---|
| Name | `em_aid` |
| Value | UUIDv4 |
| Set by | Server, `Set-Cookie`, on the first request where it is absent |
| `Domain` | `.emotorad.com` |
| `Max-Age` | `63072000` (2 years) |
| `SameSite` | `Lax` |
| `Secure` | yes |
| `HttpOnly` | **yes** |

### Why server-side, and why not Google Analytics

- **Safari ITP caps JS-set cookies at ~7 days.** iPhone visitors — our highest-value segment — would
  look new every week. A cookie set by our own server via a `Set-Cookie` header is not subject to
  that cap. *(Re-verify current ITP behaviour when you build; Apple changes it.)*
- **Ad blockers block GA heavily.** Those visitors would have no ID at all.
- **GA has no user-level real-time API.** User-level rows need the BigQuery export, which is a daily
  batch on the free tier — useless for a live conversation.
- **Google's terms prohibit sending PII to GA**, so the phone-to-cookie join has to happen our side
  regardless.

GA keeps doing marketing reporting. We capture its client ID as one more row so GA data can be
joined to the right person later, but nothing depends on it.

### `HttpOnly` — set it

It stops XSS stealing the ID. The frontend still needs the value for event calls, so the server
renders it into the page:

```html
<meta name="em-aid" content="{{ em_aid }}">
```

Client components read that, or receive it as a prop from a Server Component. **Never
`document.cookie`.** Do not drop `HttpOnly` for convenience.

### Next.js middleware

```ts
// middleware.ts
import { NextRequest, NextResponse } from 'next/server'

export function middleware(request: NextRequest) {
  let emAid = request.cookies.get('em_aid')?.value
  const isNew = !emAid

  if (!emAid) {
    emAid = crypto.randomUUID()
    // Set it on the REQUEST as well, so this very first render can see it.
    request.cookies.set('em_aid', emAid)
  }

  const response = NextResponse.next({ request: { headers: request.headers } })

  if (isNew) {
    response.cookies.set('em_aid', emAid, {
      domain: '.emotorad.com',
      maxAge: 63072000,
      httpOnly: true,
      secure: true,
      sameSite: 'lax',
      path: '/',
    })
  }
  return response
}

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'],
}
```

**The `request.cookies.set` line is the part everyone misses.** Set it only on the response and the
current render still sees no cookie — the ID appears from the *second* page view onward, so you
silently lose every visitor's landing page, which is the highest-signal page they ever view. Verify
this behaviour on your exact Next version; the API around passing modified request headers has moved.

Two more Next-specific notes:

- `cookies()` is **async in Next 15+**: `const emAid = (await cookies()).get('em_aid')?.value`.
- Calling `cookies()` opts a route into **dynamic rendering**. Read it only where needed, or you
  accidentally make the whole marketing site dynamic and lose caching.
- `output: 'standalone'` is fine. `output: 'export'` would break this entirely — no middleware, so
  nothing mints the cookie.

Skip known bot user-agents so the table does not fill with crawler rows.

**Creating the cookie does *not* create a database row.** Rows are created on the first
`link_identity` call or first event. A visitor who loads one page and leaves costs us nothing.

---

## 5. Schema

```sql
-- One row per identifier. Many rows sharing a cluster_id = one person.
create table identities (
  id             bigserial primary key,
  cluster_id     uuid not null,          -- the person. NOT a users.id
  identity_type  text not null,          -- anon_id | phone | email
                                         -- | ga_client_id | user_id
                                         -- NOTE: no wa_id. See §7.
  identity_value text not null,
  verified       boolean not null default false,
  first_seen_at  timestamptz not null default now(),
  last_seen_at   timestamptz not null default now(),
  unique (identity_type, identity_value)   -- the entire dedup mechanism
);
create index on identities (cluster_id);

-- Audit trail of merges. Never delete from this.
create table cluster_merges (
  id           bigserial primary key,
  from_cluster uuid not null,
  to_cluster   uuid not null,
  reason       text not null,
  merged_at    timestamptz not null default now()
);

-- Buying-intent events only. Not a general analytics firehose.
create table events (
  id          bigserial primary key,
  em_aid      uuid not null,             -- keyed on the COOKIE, not cluster_id
  event_name  text not null,
  properties  jsonb not null default '{}',
  occurred_at timestamptz not null default now()
) partition by range (occurred_at);
create index on events (em_aid, occurred_at desc);

create table events_2026_09 partition of events
  for values from ('2026-09-01') to ('2026-10-01');

-- SAFETY NET: without this, an insert with no matching partition FAILS.
-- Rows landing here mean the partition job didn't run — alert on it being non-empty.
create table events_default partition of events default;

-- Short code -> cookie, for the WhatsApp click-through (§9).
create table whatsapp_refs (
  code       text primary key,           -- 6 chars, e.g. '7KQ2M9'
  em_aid     uuid not null,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null default now() + interval '24 hours'
);

-- Lazily-populated cache of assembled context. NOT precomputed for everyone.
create table profile_cache (
  cluster_id  uuid primary key,
  payload     jsonb not null,
  computed_at timestamptz not null default now()
);
```

### Two schema decisions worth understanding

**Events are keyed on `em_aid`, not `cluster_id`.** Cluster IDs get retired when two clusters merge;
cookie values never change. Keying on the cookie and resolving to a cluster at *read* time means
merges apply retroactively to all historical events with zero data migration. Keying on `cluster_id`
would mean rewriting history on every merge.

**Frame numbers are deliberately absent.** Bike ownership lives in the OMS warranty table, keyed on
phone, and stays there as the single source of truth. This table maps `cluster_id ↔ phone`;
ownership is one further hop.

**`events` partition maintenance is not optional.** A missing partition makes inserts **fail
outright**, so event capture breaks at midnight on the 1st of a month. Use `pg_partman` or a monthly
cron that creates next month's partition ahead of time and drops partitions older than 90 days, and
alert if `events_default` ever holds rows.

---

## 6. `link_identity` — the one function that matters

Called whenever a new identifier becomes known. **Not on every page load** — a visitor who reads
thirty pages triggers it once.

```python
def link_identity(em_aid, identity_type, identity_value, verified) -> uuid:
    """Returns the cluster_id this identifier now belongs to.
    em_aid may be None — a WhatsApp message with no ref code has no browser.
    MUST run inside a single transaction."""

    identity_type  = canonical_type(identity_type)              # §7 — always first
    identity_value = normalise(identity_type, identity_value)   # §7 — always first

    with transaction():
        existing = select_one(
            "select * from identities where identity_type=%s and identity_value=%s",
            identity_type, identity_value)

        if em_aid is None:
            if existing:
                return existing.cluster_id
            new_cluster = uuid4()
            insert_identity(new_cluster, identity_type, identity_value, verified)
            return new_cluster

        my_cluster = cluster_for(em_aid)

        # 1. Never seen this identifier — attach it to this browser's person.
        if existing is None:
            insert_identity(my_cluster, identity_type, identity_value, verified)
            return my_cluster

        # 2. Known, same person — common case, keep it cheap.
        if existing.cluster_id == my_cluster:
            update("update identities set last_seen_at=now(), "
                   "verified = verified or %s where id=%s", verified, existing.id)
            return my_cluster

        # 3. Known, DIFFERENT cluster — two clusters are one human.
        if verified:
            survivor, loser = older_of(existing.cluster_id, my_cluster)
            update("update identities set cluster_id=%s where cluster_id=%s", survivor, loser)
            insert("insert into cluster_merges (from_cluster, to_cluster, reason) "
                   "values (%s,%s,%s)", loser, survivor, f"{identity_type} verified")
            delete("delete from profile_cache where cluster_id in (%s,%s)", survivor, loser)
            return survivor

        # Unverified identifier claiming an existing person: DO NOT MERGE.
        log.warning("unverified collision", extra={"em_aid": em_aid, "type": identity_type})
        return my_cluster
```

### The three rules that must not be relaxed

**Merge only on `verified`.** An OTP-confirmed phone, yes. A typed-in email, never. If you merged on
an unverified email, one person mistyping someone else's address fuses two customers — their
browsing, their conversations, potentially their purchase history visible to each other. That is a
privacy incident, not a bug, and it is **unpickable afterwards** because you no longer know which
rows came from whom. Unverified identifiers are still *stored*; they just never trigger branch 3.

**Survivor = the older cluster** (earliest `first_seen_at`). Not arbitrary: it makes merges
deterministic. Without it, the result depends on which request happened to arrive first, and
replaying the same events gives different answers in staging and production.

**`cluster_for` needs `ON CONFLICT`.** Two concurrent first requests from the same new browser will
both find no row and both try to insert:

```python
def cluster_for(em_aid) -> uuid:
    row = select_one("select cluster_id from identities "
                     "where identity_type='anon_id' and identity_value=%s", em_aid)
    if row:
        return row.cluster_id

    new_cluster = uuid4()
    insert("""insert into identities (cluster_id, identity_type, identity_value, verified)
              values (%s, 'anon_id', %s, false)
              on conflict (identity_type, identity_value) do nothing""", new_cluster, em_aid)
    # Re-select: if a concurrent request won the race, take its cluster.
    return select_one("select cluster_id from identities "
                      "where identity_type='anon_id' and identity_value=%s", em_aid).cluster_id
```

Also: **invalidate `profile_cache` on merge**, or the bot serves pre-merge context.

---

## 7. Normalisation — and three bugs already found for you

All of this happens **inside `link_identity`, never at call sites.** Doing it at call sites is how
one of them gets forgotten, and there is no error when that happens — just a duplicate person nobody
notices for months.

| Type | Rule |
|---|---|
| `phone` | E.164: `+919876543210`. See the three bugs below |
| `wa_id` | **Stored as `phone`.** Not its own type — see bug 1 |
| `email` | Trim, lowercase. Never a merge key |
| `anon_id`, `ga_client_id`, `user_id` | Verbatim |

### Bug 1 — `wa_id` and `phone` as separate types create two clusters

The original spec listed `wa_id` as its own `identity_type`. Because the unique key is
`(identity_type, identity_value)`, `wa_id:+919876543210` **never collides** with
`phone:+919876543210`. The same human, the same digits, two clusters, and no error anywhere.

Worse, it silently defeats the highest-value feature in the design (§9): someone browses, clicks
through to WhatsApp, and the conversation opens blind while appearing to work.

**A WhatsApp ID *is* a phone number** — Meta hands you the subscriber's number in international
form, not a separate handle. Collapse the type at storage time:

```python
def canonical_type(identity_type):
    return "phone" if identity_type == "wa_id" else identity_type
```

Callers may still pass `wa_id` because that is what the channel gives them.

### Bug 2 — trunk prefix and international access code produce corrupt E.164

Both shapes are ordinary in Indian data entry, and a naive implementation produces garbage that can
never match that person's other rows:

```
'09876543210'      ->  '+09876543210'      ← corrupt, duplicate person created silently
'00919876543210'   ->  '+00919876543210'   ← same
```

Correct handling:

```python
digits = re.sub(r"\D", "", value)
if value.startswith("+"):            return "+" + digits
if digits.startswith("00"):          return "+" + digits[2:]        # international access code
if digits.startswith("0") and len(digits) == 11:
                                     return "+91" + digits[1:]      # Indian trunk prefix
if len(digits) == 10:                return "+91" + digits
return "+" + digits
```

The trunk strip fires **only at 11 digits**, so a ten-digit number starting with 0 keeps all ten
rather than being silently shortened to nine.

**Known limitation, accepted:** a *foreign national-format* number (a German `0151…`) genuinely
cannot be normalised without knowing the country, and would get `+91` here. Safe today because every
channel supplies E.164 — WhatsApp guarantees it — leaving hand-entered data as the only exposure.

### Bug 3 — `\b` word boundaries do not work on Devanagari

Not strictly identity, but it will bite you the moment you match Hindi text anywhere. Python's `\w`
(and JS's, by default) excludes Devanagari **combining marks**, so a word boundary after `तीसरी`
(ending in the vowel sign `ी`) never matches. Hindi input silently fails while English works.

If you do any keyword matching on user text, use a substring test for non-ASCII tokens rather than a
word-boundary regex.

---

## 8. Events to capture

**Eight events, not everything.** Page views and scrolls go to GA; this table is for buying intent.

| `event_name` | `properties` |
|---|---|
| `product_viewed` | `{model, price, category}` |
| `category_viewed` | `{category}` |
| `price_filter_applied` | `{min, max}` |
| `emi_page_viewed` | `{}` |
| `test_ride_page_viewed` | `{}` |
| `dealer_locator_used` | `{pincode}` |
| `add_to_cart` | `{model, price}` |
| `checkout_started` | `{cart_value}` |

The client posts `{event_name, properties}` to a Next route handler. **The BFF reads `em_aid` from
the cookie and forwards it to Nest.** Never accept `em_aid` from the request body — it is an
unauthenticated write endpoint, and a body-supplied ID lets anyone write events against another
person's identity.

```ts
// app/api/events/route.ts
export async function POST(request: NextRequest) {
  const emAid = request.cookies.get('em_aid')?.value        // ← cookie, always
  if (!emAid) return NextResponse.json({ ok: false }, { status: 400 })

  const { event_name, properties } = await request.json()   // ← never em_aid from body
  await forwardToNest({ em_aid: emAid, event_name, properties })
  return NextResponse.json({ ok: true })
}
```

Rate-limit it.

---

## 9. The WhatsApp reference code — highest-value item here

When a visitor taps "chat on WhatsApp", append a short code to the prefilled message:

```
https://wa.me/<number>?text=Hi%2C%20I%27d%20like%20to%20know%20more.%20%5Bref%3A7KQ2M9%5D
```

`GET /api/whatsapp/ref` generates a 6-character code (**not** the raw UUID — a UUID in a
customer-visible message looks broken and gets deleted before sending), stores `code → em_aid` with
a 24-hour TTL, and returns it. The frontend builds the link.

The bot backend parses `ref:` from the first inbound message and calls
`link_identity(em_aid_from_code, 'phone', <sender>, verified=True)`.

**This single mechanism converts an anonymous browser into a verified phone number with the browsing
history already attached** — the most valuable link available, and a frontend change rather than an
architectural one.

A missed or expired code must degrade to phone-only resolution, never fail the conversation.

Verify current behaviour against Meta's docs before building: plain `wa.me` links carry prefilled
text only, while Click-to-WhatsApp ads deliver a structured `referral` object.

---

## 10. Endpoints (Nest owns all of these)

| Endpoint | Called by | Does |
|---|---|---|
| `POST /events` | Next BFF | Insert an event. Rate-limited |
| `POST /identity/ga` | Next BFF | `link_identity(em_aid, 'ga_client_id', <_ga>, false)` |
| `GET /whatsapp/ref` | Next BFF | Short code mapped to this `em_aid` |
| **`POST /identity/resolve`** | **the AI bot** | `{identity_type, identity_value}` → `{cluster_id}` |
| `POST /identity/link` | Next BFF, and the bot | After OTP/login, and for the WhatsApp stitch |
| `GET /identity/context?cluster_id=` | **the AI bot** | Assembled context |
| `DELETE /identity/erase` | support/legal tooling | Erases a whole cluster |

**`/identity/resolve` is what joins the two halves of the system.** The bot never has a `cluster_id`
— it has a phone number (WhatsApp, IVR) or a cookie (website chat). It calls `resolve` first, then
`context`. Without it there is no path from an inbound message to a person and the whole thing is
inert. `resolve` creates a cluster if the identifier is new, so it never returns empty.

### Call sites to add in existing handlers

```ts
// OTP verification (registration, test-ride booking, checkout) — the most valuable one
if (await otpService.check(phone, otp)) {
  await linkIdentity(emAid, 'phone', phone, true)
}

// Login
await linkIdentity(emAid, 'user_id', user.id, true)

// Any form capturing an email
await linkIdentity(emAid, 'email', email, false)   // never merges
```

**OTP matters more than login.** It is what gives you a verified *phone*, which is what makes the
WhatsApp stitch work later. A `user_id` is useful but connects no channel.

### The context endpoint — compute lazily, cache

Do **not** build a scheduled job that precomputes profiles for every visitor. Most visitors never
chat, so it is ~97% wasted work, and a nightly rollup would miss the browsing someone did five
minutes before opening the chat — which is the most relevant browsing there is. Cache in
`profile_cache` with a ~30-minute TTL and invalidate on merge.

`verified_phone` in the response is load-bearing: it tells the bot whether it may say a customer's
name out loud. A cookie identifies a *browser*, and shared family laptops are common — "Hi Ananya,
about your EMX Plus?" to her husband is a data leak.

---

## 11. Consent and erasure

India's DPDP Act applies, and **we have EU customers on the same site**, so treat GDPR as the
baseline and DPDP as the subset — it is close to the same work done once.

- Get legal's read on whether `em_aid` needs consent. A first-party functional cookie is a different
  question from analytics tracking, and the answer affects *where in the page lifecycle* it can be
  set. For EU visitors, assume consent-gated: they stay anonymous until they opt in, and the
  anonymous path must work standalone.
- **Erasure must propagate across the whole cluster.** `DELETE /identity/erase` takes a
  `cluster_id`, deletes all its `identities` rows, all `events` for its `em_aid`s, and its
  `profile_cache` row. Straightforward now, awkward to retrofit.

---

## 12. Acceptance tests

### Automated — these belong in CI

1. First request to any page sets `em_aid`; a second request reuses the same value.
2. **The very first render can read the cookie it just minted** (the middleware trap in §4).
3. The value is identical across `www.emotorad.com` and any subdomain.
4. `link_identity` with a new phone creates one row sharing the browser's `cluster_id`.
5. `link_identity` with a phone already on **another** cluster, `verified=true`, merges the two,
   writes a `cluster_merges` row, and the **older** cluster survives.
6. Same, with `verified=false`, does **not** merge.
7. Called twice with identical arguments produces one row, not two.
8. `9876543210`, `+91 98765 43210`, `09876543210` and `00919876543210` all resolve to one row.
9. A WhatsApp sender `919876543210` resolves to the **same row** as web-form `9876543210`.
10. `link_identity` with `em_aid=None` creates a standalone cluster and does not raise.
11. Two concurrent `cluster_for` calls for the same new `em_aid` produce **one** cluster.
12. After a merge, `profile_cache` for both clusters is gone.
13. `POST /api/events` with an `em_aid` in the body ignores it and uses the cookie.
14. `resolve` on an unseen identifier returns a fresh `cluster_id` rather than an error.
15. An insert dated next month lands in a real partition, not `events_default`.
16. Erasing a cluster removes its `identities`, `events` and `profile_cache` rows.

### Manual / QA — before launch

17. On a real iOS Safari device, the ID survives more than 7 days.
18. With uBlock Origin enabled, `em_aid` is still set (proves independence from GA).
19. Clicking the WhatsApp button produces a link containing `ref:`, and that code resolves to the
    right `em_aid` on the bot side.

---

## 13. Decisions still open — yours to make

- **Do browser events go through the BFF, or straight to Nest?** Recommendation: **through the
  BFF** — one public surface instead of two, no CORS, the service credential stays server-side, and
  rate limiting lives in one place. Direct-to-Nest saves a hop but means making Nest publicly
  reachable, which is what §3's rule is trying to avoid.
- **Shared browsers.** Person B logs in on the same laptop person A used. Same `em_aid`, so
  `link_identity` sees a verified `user_id` and attaches it to A's cluster — two humans, one person,
  each holding the other's history. **Fix: mint a fresh `em_aid` on logout, and on any login where
  the `user_id` differs from one already on that cluster.** A few lines now; unpickable later.
- **Service-to-service auth** between Next and Nest: shared secret, mTLS, or network policy.
- **Bot user-agent skip list** in middleware.

---

## 14. Before the data migration — run this query

Separate from the build. We plan to backfill the warranty table so one table can answer "is this
person known to us". Before making phone the universal key:

```sql
select mobile, count(distinct frame_number) as bikes
from purchase
group by mobile
having count(distinct frame_number) > 3
order by bikes desc;
```

**Dealers perform most warranty registrations and frequently enter their own number.** If a handful
of numbers each own dozens of bikes, those are dealers — merging on them would fuse hundreds of
unrelated customers into one profile. This query gates the backfill; it does not follow it.

---

## 15. Explicitly out of scope

Device fingerprinting. Probabilistic matching. Call tracking. Buying a CDP.

If someone proposes a CDP or analytics product to solve this, the question to ask is: **does its
open-source/self-hosted tier give you identity resolution with a verified-only merge rule, or just
an event pipeline?** For RudderStack specifically, identity resolution (Profiles/Unify) is a paid
feature — the open-source tier is event collection and routing, and its `anonymousId` is set
client-side, which reintroduces the exact ITP problem §4 exists to solve.
