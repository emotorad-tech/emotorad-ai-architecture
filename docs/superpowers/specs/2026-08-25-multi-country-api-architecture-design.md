# Multi-Country API and Data Architecture

**Status:** Approved design

**Date:** 2026-08-25

**Scope:** `EM-Website`, `EM-Web-Backend`, and the website-facing slice of `em-biz-backend`

**Primary objective:** Make country N+1 an operational configuration and data-onboarding exercise, not a new API or schema project.

## 1. Executive decision

Introduce a first-class **Market Context** and require every country-sensitive API operation to use it. A market controls commerce: catalogue availability, ERP price and inventory, currency, tax and shipping policy, providers, timezone, and feature availability. A locale controls language and presentation. A shipping or residence country remains domain data and is not interchangeable with either.

The core decisions are:

1. Customer identity is global across markets.
2. A customer has one active cart per `(user, market)`.
3. Products and variants retain global identities; their sellability, prices, tax metadata, and stock sources are market-specific.
4. ERPNext or the corresponding market ERP remains the source of price and inventory. The platform stores market-aware projections; it does not calculate foreign prices using exchange rates.
5. The website sends `X-Market` and `Accept-Language`. The Node backend resolves them once into a validated context and passes that context explicitly through cached and business-logic boundaries.
6. API payloads remain language-neutral. They return data, enums, and stable error codes. The frontend and CMS render website copy; the backend uses the selected communication locale only for email, SMS, and WhatsApp templates.
7. Node/Mongo and Django/Postgres stay shared across markets by default. A future regulatory split may route a market to isolated infrastructure without changing the public API contract.
8. Every market can support the full D2C path, but individual capabilities can be disabled per market until their data or provider integrations are ready.
9. CMS-owned descriptions, translations, and assets are deliberately outside this design.

## 2. Audit basis and current-state boundary

This design was produced from a read-only source audit of:

| Repository | Audited revision | Notes |
|---|---:|---|
| `EM-Website` | `main@550dd58` | Local `main` matched `origin/main`. |
| `EM-Web-Backend-Staging` / renamed `emotorad-tech/EM-Web-Backend` | `main@c652beb` | The user confirmed these names refer to the same backend project. |
| `em-biz-backend` | `origin/main@36e4eeef` | The working tree was on another branch with an unrelated `.DS_Store` change, so `origin/main` was inspected without switching or modifying it. |

The audit covered code-defined Mongoose models, Django models and migrations, controllers/views, route registries, provider integrations, cache usage, and the website's active backend call sites. It did **not** connect to production MongoDB, Postgres, Redis, or ERPNext. Phase 0 therefore requires a read-only production-data audit before any migration is authored. This distinction matters: tables such as `ProductMarket`, `VariantOffer`, and stock pools described below are target-state proposals, not current tables.

### 2.1 Verified current constraints

The source audit found the following load-bearing constraints:

- The website references 59 backend route members across two route registries. Not all are equally live: referral is feature-gated, the SaveIn result poll is not consumed by the current result page, and product-description content is moving to CMS ownership.
- Most calls use `src/lib/backend/client.ts`, but Google auth start, token refresh, warranty invoice upload/OCR, and parts of the Snapmint flow bypass that helper. Market propagation is therefore not a one-file change.
- Next.js cached catalogue getters cannot discover request headers inside a `"use cache"` function. Market and locale must be explicit function arguments so cache identity is correct.
- The website currently exposes only `in-en`, uses INR formatting, derives India as the default country, takes the last ten phone digits, and contains several six-digit postal-code validations.
- Node has no market middleware or canonical commerce-market registry. Its `Product`, `Variant`, `Inventorie`, `Cart`, `Order`, `Promocode`, `Store`, lead/form, test-ride, and warranty data are either global or India-shaped.
- `Cart.user` is globally unique, which enforces one cart per user rather than one per user and market.
- `Variant` has globally unique `erp_id` plus required India-specific `hsn_code` and `item_tax_template`; `Inventorie` has one quantity per globally unique ERP item.
- `Order` stores bare amounts without market or currency, and its address has no country. User addresses contain an Indian six-digit pincode validator.
- Price resolution is duplicated between catalogue, cart, and legacy order paths. Shipping thresholds and several rewards are hardcoded in rupees.
- Payment, SMS, WhatsApp, CRM, geocoding, shipping, and timezone code contains India-specific assumptions. Payment callbacks and background work do not consistently carry market context.
- Django already has useful beginnings: `Lead.country`, an open-lead uniqueness rule involving country, and `Franchise.country` and timezone. However, postal and geocode identities are not composite by country, phones are India-normalized, geographic assignment does not consistently filter by country, and global IST remains in active paths.
- Node is the website's public backend boundary. Django is an internal operational dependency behind Node for the relevant flows and should remain India-first except for its website-facing lead, postal, franchise, and warranty contract surface.

## 3. Terminology and invariants

### 3.1 Market, country, and locale

These values must never be collapsed into one field:

| Concept | Example | Meaning | Authority |
|---|---|---|---|
| `market` | `IN` | Commercial context: catalogue, currency, ERP source, stock, providers, policies, and feature flags | URL-to-market mapping in the website; validated by Node's canonical registry |
| `country` | `IN` | Shipping, residence, store, lead, or warranty country on a business entity | Validated request data, constrained by the selected market |
| `locale` | `en-IN` | Communication and formatting preference | Website route/selection, validated against locales enabled for the market |

The existing website route locale `in-en` maps to market `IN` and BCP-47 communication locale `en-IN`. Route locale format is a website concern; APIs receive the normalized values.

A market may initially ship only to its home country, but the model supports `supportedShippingCountries` so that cross-border selling does not require redefining market identity. Currency is owned by the market and never accepted as an arbitrary client selection during checkout.

### 3.2 System-wide invariants

1. Every market-scoped database query contains a market or stock-pool predicate.
2. Every money value is associated with a currency and has an unambiguous unit.
3. The backend never trusts a request-body market or currency over resolved Market Context.
4. A callback, webhook, retry, or background job derives market from its stored entity or authenticated provider account, never from a missing browser header.
5. A product or variant without an active market row is not sellable in that market.
6. A cart cannot contain offers from more than one market or currency.
7. An order permanently snapshots market, currency, locale, prices, tax, fulfilment, and provider selections used at purchase time.
8. Cache identity and invalidation include market for every market-dependent result.
9. Provider availability is configuration, not a country `if/else` chain in controllers.
10. Missing legacy context may fall back to `IN` only during a measured migration window. The target contract is strict.

## 4. Target request architecture

### 4.1 Website-to-Node contract

For country-sensitive calls the website sends:

```http
X-Market: IN
Accept-Language: en-IN
```

The Next.js request boundary resolves the route locale once and creates an explicit value:

```ts
type BackendMarketContext = {
  market: string;              // canonical market code, e.g. IN
  communicationLocale: string; // BCP-47, e.g. en-IN
};
```

Server actions, route handlers, loaders, and cached data functions receive this context as an argument. `backendRequest` adds the headers for the normal transport path. The known direct transports—Google auth start, refresh, warranty upload/OCR, and payment-specific calls—must use the same header-building utility or explicitly declare themselves market-agnostic.

Passing context explicitly is mandatory for `"use cache"` functions. Reading `headers()` from a cached function risks both Next.js errors and cross-market cache leakage. A safe shape is `getAllBikes(context)` with `market` included in the cache key/tag, not a zero-argument getter that reads ambient request state.

### 4.2 Node Market Context

Node owns the canonical commerce registry. Middleware parses headers, validates the selected market and locale, and attaches an immutable context:

```ts
type MarketContext = {
  market: string;
  currency: string;
  timezone: string;
  communicationLocale: string;
  supportedShippingCountries: string[];
  pricingPolicy: string;
  taxPolicy: string;
  stockPolicy: string;
  providers: {
    payment: string[];
    messaging: string;
    crm: string | null;
    fulfilment: string;
    postal: string;
  };
  features: Record<string, boolean>;
};
```

Registry entries contain identifiers and policy references, not secrets. Credentials remain in the environment or secret manager under provider-account-specific names. The registry is schema-validated at application startup; an invalid enabled market prevents startup rather than failing during checkout.

Middleware is mounted in two layers:

- A global optional parser recognizes and validates any supplied context.
- `requireMarket` protects routes classified as market-scoped and guarantees `req.market` exists.

During migration only, an absent market on an old India client resolves to `IN` and increments `market_context_legacy_fallback_total` with route and client labels. Once observed legacy traffic reaches the agreed zero window, fallback is removed and scoped routes return `MARKET_REQUIRED`. Unknown and disabled markets fail before controller execution.

`X-Market` is business context, not an authorization mechanism. The public backend still validates all domain rules. A body containing `market`, `country`, or `currency` must either match the resolved context and permitted shipping countries or be rejected.

### 4.3 Language behavior

API responses do not localize product descriptions, validation sentences, or UI copy. They return stable codes and structured data. A representative error is:

```json
{
  "error": {
    "code": "PRODUCT_NOT_AVAILABLE_IN_MARKET",
    "details": { "market": "AE", "variantId": "..." }
  }
}
```

Any human-readable `message` retained for backward compatibility is non-contractual. The website maps `code` to CMS/dictionary copy.

The backend persists `communicationLocale` on orders, leads, test rides, and warranties because later notifications may run without a web request. Email, SMS, and WhatsApp select a provider template using the entity's stored market and locale. Locale fallback is market default, not a global India default.

### 4.4 Response and caching rules

Every market-dependent response includes `market` and, when money appears, `currency`. During compatibility migration, current major-unit numeric fields remain unchanged and gain additive market/currency metadata. Internal calculations and new storage use integer minor units. A later versioned API can replace ambiguous numeric fields with a canonical `Money` object; no existing field may silently change units.

HTTP responses use `Vary: X-Market` where intermediary caching is possible. Redis keys, Next cache keys, and invalidation tags include market, for example `catalog:all-bikes:IN` and `variant:{id}:IN`. Locale is included only when the returned API data genuinely varies by locale; CMS-owned content is cached in the CMS layer.

## 5. Data architecture

### 5.1 Global identity

`User` remains a global identity, keyed by normalized E.164 phone and/or the current verified identifiers. Login and profile identity are not duplicated per country. Addresses gain an explicit ISO country and market-aware postal/phone validation.

Account order history is global by default so the same customer can see purchases from multiple markets. Each row exposes its own market and currency, and the endpoint may accept a market filter for the current storefront. Authorization remains user-based.

### 5.2 Product identity and market availability

`Product` and `Variant` retain global identity and non-commercial attributes. Two new target collections separate market decisions from identity:

```text
ProductMarket
  productId
  market
  active
  sortOrder
  badges/flags that are data rather than translated copy
  availability windows
  optional structured, language-neutral overrides

unique: (productId, market)
index:  (market, active, sortOrder)
```

```text
VariantOffer
  variantId
  market
  erpSource
  erpItemId
  currency
  mrpMinor
  salePriceMinor
  optional approved price overrides
  taxClass
  marketTaxMetadata
  stockPoolId
  pricingVersion
  active
  sourceUpdatedAt

unique: (variantId, market)
unique: (market, erpSource, erpItemId)
index:  (market, active)
```

The relationship rules are:

- Price, stock source, tax, or availability differs: same Product and Variant, different `VariantOffer`.
- Hardware or regulatory specification differs enough to be a different sellable SKU: same Product, new Variant, with offers only in applicable markets.
- The model itself is materially different: create a new Product normally.

India-specific `hsn_code` and `item_tax_template` become optional legacy fields on Variant and move into the India offer's market tax metadata or referenced tax class. They must not be required for a non-India variant.

One `resolveOffer(variantId, market)` service becomes the only source for sellability and current commercial data. Catalogue, product detail, comparison, cart, promotion, and checkout all use it. The duplicated catalogue/cart/legacy-order price rules are retired after shadow comparison.

### 5.3 Inventory and stock pools

Inventory is keyed by physical or logical stock pool, not directly by country:

```text
StockPool
  id
  code
  erpSource
  fulfilmentProvider
  timezone
  active

InventoryBalance
  stockPoolId
  variantId
  availableQuantity
  reservedQuantity
  sourceUpdatedAt

unique: (stockPoolId, variantId)
```

Each `VariantOffer` references a stock pool. This supports both operating models without a schema change:

- Dedicated inventory: IN and AE offers reference different pools.
- Shared inventory: multiple market offers reference the same pool.

Reservations and decrements operate on the pool, so shared inventory cannot be accidentally duplicated by maintaining one quantity per country. Availability responses remain market-scoped through the offer-to-pool relationship.

### 5.4 ERP ingestion

ERP remains authoritative for market price and stock. Every ERP credential maps server-side to an `erpSource`, allowed market(s), currency, and stock pool. Payload market/currency values, if present, must match that credential mapping; they never grant access to another market.

Ingestion upserts `VariantOffer` and `InventoryBalance` idempotently. An ERP item links to the platform's stable Variant identity. Unknown identity mappings are quarantined and alerted rather than creating accidental duplicate products. No runtime foreign-exchange conversion is performed.

The existing India feed is mapped to `IN`, `INR`, and its initial India stock pool. Its current fields remain readable during migration. Counters identify each read that still falls back to legacy Variant/Inventorie data.

### 5.5 Cart

The current unique constraint on `Cart.user` is replaced by a compound unique constraint on `(user, market)`. Cart also stores currency and a pricing version or last-priced timestamp. Every line references global product/variant identity but is resolved through the cart market's active offer.

Cart invariants are checked on add, merge, view/reprice, and checkout:

- offer is active in the cart market;
- offer currency equals cart currency;
- stock pool is valid;
- promotion applies to the market and currency;
- feature or fulfilment prerequisites are enabled.

Switching storefront market opens or creates that market's cart; it never mutates an existing cart into another market. Cross-market cart merge is rejected with a stable code. This is the selected **one cart per `(user, market)`** behavior.

### 5.6 Order and payments

Order becomes the immutable audit boundary and stores:

- `market`, shipping `country`, `currency`, and `communicationLocale`;
- pricing/tax/shipping policy versions;
- line snapshots with offer, ERP item, unit/discount/tax totals in minor units;
- order totals in minor units;
- selected fulfilment provider/account and stock pool;
- selected payment provider/account and provider currency;
- normalized address including country;
- idempotency and source metadata.

The server constructs these values from Market Context and resolved offers. A browser-supplied total, currency, tax, provider account, or ERP identifier is not authoritative.

Payment initiation is routed by market configuration and feature flags. Confirmation and webhooks load the order first and verify that provider account, currency, and amount match its snapshot. Replayed callbacks are idempotent. A provider callback without a trustworthy order/account-to-market mapping is quarantined rather than defaulted to India.

### 5.7 Promotions, referrals, stores, and reviews

- Promotion code identity becomes `(market, normalizedCode)` unless a deliberate global campaign abstraction is later required. Fixed-value promotions store currency; percentage promotions still declare applicable markets.
- Referral policy stores market, reward minor units, and currency. The current hardcoded 500 reward is removed from domain logic.
- Stores gain market and country, normalized GeoJSON coordinates, and market-aware indexes. Nearest-store lookup filters market before distance calculation.
- Reviews remain global to the global Product/Variant initially. APIs may expose market metadata on review origin, but display filtering is a separate business choice and is not required for launch.

### 5.8 Leads, forms, test rides, and warranty

Every persisted operational record created by a website flow stores market, relevant country, and communication locale. Phone numbers are normalized to E.164. Postal validation uses the selected country's resolver rather than an India regex.

This applies to contact and campaign forms, EMI/exchange/factory-visit flows, dealer-cycle leads, exit intent, pre-buy and insurance flows, test rides, warranties, invoice/OCR handoffs, and any queue/outbox record created from them. Existing loose country strings are normalized to ISO codes during backfill.

Feature flags decide whether a route is available in a market. Disabled capabilities return `FEATURE_DISABLED_FOR_MARKET`; they do not silently submit an India-shaped record.

### 5.9 Django/Postgres website-facing slice

Django remains the India OMS by default. Its multi-country work is limited to data and APIs that Node legitimately calls for website flows:

1. Add country to State, District, Region, and postal-code identities; replace globally unique postal codes with `(country, postalCode)` uniqueness.
2. Give geocode cache a surrogate primary key and unique `(country, postalCode)` lookup.
3. Preserve and validate existing `Lead.country`; normalize phone to E.164 and retain the correct open-lead uniqueness semantics.
4. Filter franchise and geographic assignment candidates by country before distance.
5. Use franchise/market timezone for relevant website-facing dates instead of the global IST constant.
6. Accept a validated internal market contract from Node and reject body/header mismatches.
7. Route non-IN fulfilment and warranty operations away from the India OMS adapter unless that market is explicitly configured to use it.

Node owns the canonical public Market Context. Django stores only the operational configuration it needs and does not duplicate the entire commerce registry.

## 6. API and route disposition

Every route must declare one of four scopes:

- **Market read:** response depends on market data.
- **Market write:** market is validated and stamped on the entity.
- **Global:** identity or aggregate data spans markets, with market shown on contained entities.
- **Entity-derived:** callbacks/jobs derive context from an existing entity or authenticated provider account.

No new route can be left unclassified.

| Website/API surface | Scope | Target behavior |
|---|---|---|
| `/bikes/all-bikes`, `/bikes/all-accessories`, `/bikes/bike/:slug`, `/bikes/sub-category/:category` | Market read | Filter `ProductMarket`; resolve each VariantOffer and stock pool; return market/currency. |
| `/products/all-accessories`, `/products/product/:name`, `/products/spare/parts`, `/products/spare/compatible` | Market read | Same offer/availability rule; compatibility identity remains global. |
| Comparison, slug, and sitemap data paths | Market read | Pass explicit context into cached getters; generate URLs only for markets where the product is active. |
| `/store`, `/store/nearest` | Market read | Filter by market before geographic calculation; use country-aware postal resolver. |
| `GET/POST /cart`, `POST /cart/merge` | Market write/read | Load unique `(user, market)` cart; reject cross-market merge; reprice through offers. |
| `/promocode/visible`, `/promocode/verify-cart` | Market read | Apply market and currency eligibility. |
| `/order/create`, `/order/:id/status` | Market write/read | Stamp order snapshot; authorize user; status reads expose stored market/currency. |
| `/user/orders` and order detail | Global aggregate | Return all authorized orders with market/currency, with optional current-market filter. |
| Razorpay, SaveIn, Snapmint, PayU and future payment initiation | Market write | Offer only configured providers; bind provider account and currency to order. |
| Payment callbacks/webhooks and result polling | Entity-derived | Resolve order/provider account, verify signed metadata and stored market; never trust browser market. |
| OTP, session, profile | Global identity | Identity stays global; OTP provider and policy are selected by current market; addresses are country-aware. |
| Address create/update | Market write on global user | Validate shipping country against market; persist explicit country. |
| Contact, EMI, exchange, factory visit, pre-buy, insurance, dealer-cycle and campaign forms | Market write | Stamp market/country/locale; route downstream CRM/messaging per market. |
| Test ride and dealer lookup | Market write/read | Feature gate, market dealers/stores, country-aware phone/postal validation. |
| Warranty registration, pincode, franchise, frame and invoice/OCR handoff | Market write/read | Stamp context, route through market fulfilment/warranty adapter; forward validated context to Django where applicable. |
| Reviews | Global initially | Keep product reviews shared; retain stable language-neutral metadata. |
| Product-description API | CMS transition | Do not build a second localization model. Retain only structured live commerce data until consumers move to CMS. |
| Generic upload/OCR transport | Agnostic transport, market-stamped result | Binary upload can be agnostic, but the created warranty/lead/job must inherit context. |
| Referral and inactive SaveIn poll paths | Dormant/feature-gated | Classify and make safe, but do not let dormant routes drive launch sequencing. |

### 6.1 Stable error codes

The initial cross-market error catalogue includes:

- `MARKET_REQUIRED`
- `MARKET_UNSUPPORTED`
- `MARKET_DISABLED`
- `LOCALE_UNSUPPORTED`
- `MARKET_CONTEXT_MISMATCH`
- `SHIPPING_COUNTRY_UNSUPPORTED`
- `PRODUCT_NOT_AVAILABLE_IN_MARKET`
- `PRICE_NOT_AVAILABLE_IN_MARKET`
- `STOCK_POOL_NOT_CONFIGURED`
- `CURRENCY_MISMATCH`
- `PROMO_NOT_VALID_IN_MARKET`
- `PAYMENT_METHOD_UNAVAILABLE`
- `FEATURE_DISABLED_FOR_MARKET`

Controllers return these codes consistently. Website dictionaries own user-facing text.

## 7. Provider and background-work architecture

Controllers call capability interfaces rather than country-named implementations:

- `PaymentRouter`
- `MessagingRouter` for OTP, SMS, email, and WhatsApp
- `CrmRouter`
- `FulfilmentRouter`
- `PostalResolver`
- `TaxPolicyResolver`

The Market Registry selects adapter and provider-account identifiers. Implementations can be shared by multiple markets. Adding a market that uses an existing provider requires configuration and credentials; a genuinely new provider requires one adapter conforming to the existing interface, not changes throughout controllers.

Every outbox event, delayed job, retry record, and webhook-processing record stores or deterministically derives:

- market and communication locale;
- entity type and ID;
- provider and provider-account ID;
- idempotency key;
- attempt count and terminal/retryable error code.

Provider callbacks resolve context in this order: signed order/entity metadata, stored provider transaction mapping, authenticated provider account. Request headers are ignored for authority. Scheduled jobs load the entity snapshot before choosing a provider.

The India-only operational integrations remain valid India adapters. Non-IN routes cannot fall through to them accidentally; unsupported capabilities queue/alert or return an explicit disabled/unsupported result according to the operation's contract.

## 8. Repository change boundaries

### 8.1 `EM-Website`

Primary changes belong around:

- `src/lib/i18n/config.ts`: add route-locale-to-market and BCP-47 mapping, with supported locale metadata.
- `src/lib/backend/client.ts`: accept `BackendMarketContext` and add normalized headers.
- `src/lib/backend/routes.ts` and `src/lib/auth/config.ts`: consolidate or at minimum classify both registries so coverage can be checked.
- cached catalogue, comparison, slug, store, and product helpers: accept explicit market; partition keys and tags.
- auth refresh, Google auth, warranty upload/OCR, and payment transports: use the common context/header utility or declare agnostic behavior.
- money formatting: derive currency from response/market, not an INR default.
- phone, address, and postal form helpers: use country metadata; remove ten-digit and six-digit assumptions.
- sitemap generation: enumerate active product slugs per market rather than assuming one global catalogue.

The frontend's current instruction to avoid adding new tests remains in force. Verification uses its existing lint/type/build gates plus the staging journeys in this design unless that repository policy is changed explicitly.

### 8.2 `EM-Web-Backend`

Primary additions are:

- validated Market Registry and context middleware;
- route-scope declarations and missing-context guardrails;
- `ProductMarket`, `VariantOffer`, `StockPool`, and `InventoryBalance` models;
- additive market/country/locale/currency fields and compound indexes on existing records;
- a single offer/pricing service used by catalogue, cart, promotion, and checkout;
- ERP source-to-market/stock-pool credential mapping;
- market-aware provider routers and entity-derived callback processing;
- market-partitioned cache keys and revalidation events;
- additive migration/backfill, shadow-read, and dual-write tooling.

### 8.3 `em-biz-backend`

Changes are constrained to the website-facing Postgres models, lead assignment/ingest, postal/geocode lookups, franchise selection, timezone usage, and the Node-to-Django internal contract. The full internal OMS does not become a universal multi-country platform as part of this work.

## 9. Migration and rollout

Each phase is independently deployable and reversible. India behavior must remain unchanged until a phase's comparison and staging gates pass.

### Phase 0 — production-data reconnaissance

Perform read-only inspection of production MongoDB and Postgres plus representative ERP payloads:

- row/document counts and null/cardinality distributions for all affected models;
- actual indexes, uniqueness, and schema drift from source definitions;
- duplicate users, phones, ERP IDs, variants, carts, postal codes, and open leads;
- real price types/precision, currency assumptions, overrides, negative stock, and stale timestamps;
- address and country-string shapes;
- live provider transaction metadata and webhook-to-order linkage;
- cache key inventory and background queue payload shapes.

Produce India compatibility fixtures from anonymized real shapes and assign business owners for pricing, tax, fulfilment, provider, and launch feature decisions. Migration scripts are not authored until this gate is complete.

### Phase 1 — context foundation with no commercial behavior change

- Add and validate the Node Market Registry with IN only.
- Add optional parser, route classification, `requireMarket`, stable error catalogue, and response metadata.
- Propagate explicit context through all website transports and cached functions.
- Add context to logs, traces, cache keys, outbox/jobs, and internal Node-to-Django requests.
- Retain measured missing-header fallback to IN.

Rollback is configuration-based: disable strict route enforcement while retaining non-breaking headers and telemetry.

### Phase 2 — additive schemas and India backfill

- Create ProductMarket, VariantOffer, StockPool, and InventoryBalance.
- Add nullable market/country/currency/locale fields to current documents and SQL rows.
- Backfill existing data as IN/INR and create compound indexes after duplicate audit/remediation.
- Change cart uniqueness from user to `(user, market)` using a staged index migration.
- Dual-write new records to legacy and target fields where rollback needs it.
- Add market and locale to background payloads before any asynchronous cutover.

Rollback keeps reads on legacy fields while target writes remain harmless and observable.

### Phase 3 — catalogue, ERP, inventory, and cache cutover

- Map India ERP credentials to IN/INR/India stock pool.
- Shadow-read offers and stock next to legacy Variant/Inventorie values.
- Compare price, active status, and quantity on every sampled request; alert without changing responses.
- Backfill and verify all active India catalogue identities.
- Flip catalogue families to the shared offer resolver behind a market flag.
- Make revalidation and sitemap generation market-aware.

Rollback flips the India resolver flag to legacy reads. New-market traffic remains disabled.

### Phase 4 — cart, promotions, checkout, and providers

- Enable market carts and reprice all lines through VariantOffer.
- Cut promotions, shipping, tax, referrals, and order snapshots to market-aware policies.
- Route payment initiation through configured provider accounts.
- Verify all callbacks from stored entity context.
- Introduce notification, fulfilment, CRM, and postal adapters for the staging market with unsupported features disabled.

Rollback disables checkout for the new market and returns India to legacy-compatible provider selection without deleting target data.

### Phase 5 — operational forms and Django slice

- Stamp market/country/locale on leads, forms, test rides, and warranty paths.
- Apply country-aware phone/postal rules.
- Migrate Django geography and geocode uniqueness, lead normalization, assignment filtering, and timezones.
- Route non-IN operations away from India OMS unless explicitly configured.

Rollback disables affected market features; India continues through the existing adapter with the additive IN context.

### Phase 6 — prove with a disabled staging market, then launch

Create a non-customer-visible staging market with:

- a different currency and deliberately different prices;
- at least one product absent and one product active;
- a dedicated stock pool plus one controlled shared-pool test if shared stock is planned;
- market-specific stores/postal data;
- provider accounts or explicit disabled feature flags;
- a second communication locale when launch scope requires it.

Run complete browse, product detail, auth, cart, promotion, checkout, callback, order history, lead, test ride, warranty, notification, cache, and background-job journeys. Only enable public routing after the isolation gates pass.

## 10. Safety, observability, and verification

### 10.1 Required metrics and alerts

- missing/legacy Market Context by route and client;
- unsupported or disabled market requests;
- legacy price/inventory reads after target cutover;
- missing ProductMarket/VariantOffer/stock-pool mappings;
- request/order/provider currency mismatches;
- unsupported provider and feature-disabled attempts;
- cache hit/miss and invalidation by market;
- ERP lag and rejected identity mappings by source/market;
- OMS, CRM, fulfilment, postal, and messaging failures by provider account/market;
- callbacks that cannot derive an entity market;
- cross-market query guard violations in development/staging.

All dashboards and alerts include market as a bounded label. High-cardinality identifiers remain in structured logs, not metric labels.

### 10.2 Guardrails

1. Route definitions declare their market scope; CI fails when a new route is unclassified.
2. Development/staging query guards reject market-scoped model reads without the required predicate.
3. CI scans new domain/controller code for hardcoded INR, rupee symbols, `+91` concatenation, India timezone, country names, and six-digit postal regex outside approved registry/provider fixtures.
4. API contract checks call representative scoped endpoints with IN and the staging market and assert isolation.
5. Database migrations assert expected row counts and indexes before and after each backfill.
6. ERP ingestion tests prove one credential cannot modify another market or stock pool.
7. Payment tests prove amount, currency, account, order, and callback market all agree.
8. Job/outbox tests prove retries preserve entity market and communication locale.

### 10.3 Launch gates

The staging market cannot be enabled until:

- no catalogue response contains another market's product, price, stock, promo, or store;
- cache warm-up in one market cannot change another market's response;
- missing offers fail closed rather than using India values;
- cart uniqueness and cross-market merge behavior are verified;
- payment/provider mismatch tests fail closed;
- background notifications use the stored locale and correct provider account;
- Django postal/franchise queries cannot cross country;
- disabled features return stable codes and create no partial records;
- India golden journeys and API shapes remain compatible.

## 11. Country N+1 operating playbook

After the architecture is complete, adding a market consists of:

1. Add the route locale and display metadata in the website.
2. Add and schema-validate one Node Market Registry entry with every feature initially disabled.
3. Configure secret references and provider accounts.
4. Register ERP source credentials, currency, tax policy, and stock pool mappings.
5. Load ProductMarket/VariantOffer, postal/store, promotion, and operational configuration data.
6. Add notification templates for enabled communication locales.
7. Configure the Django operational slice only when that market uses those capabilities.
8. Run the standard market contract and staging journey suite.
9. Enable browse first, then lead/operational features, then checkout using independent flags.
10. Monitor market-specific error, fallback, provider, and ERP-lag dashboards.

If an existing adapter serves the market, this process requires no new endpoint, database schema, or country branch. Only a genuinely new external provider adds an adapter behind an existing capability interface.

## 12. Alternatives considered

### Route-by-route country patching

Adding country fields and `if country === ...` logic to each controller appears faster initially but preserves duplicated pricing, inconsistent cache identity, and provider leakage. Every future endpoint could omit the country predicate. This approach was rejected.

### Separate application and database stack per country

Country-specific stacks offer strong isolation but immediately multiply deployments, migrations, secrets, operations, and cross-market customer/account reporting. Shared infrastructure with a hard Market Context is the default. The adapter and routing boundaries in this design preserve the option to isolate a regulated market later without changing clients. Full separation is therefore deferred until a concrete legal or scale requirement exists.

### Runtime foreign-exchange conversion

Market prices are commercial price lists with local tax and promotion rules, not currency conversions. ERP-sourced offers were selected instead.

### Language columns on API tables

This would duplicate the CMS localization programme and mix copy with commerce data. APIs remain language-neutral except for the locale stored to select outbound templates.

## 13. Explicitly out of scope

- CMS descriptions, translations, banners, images, and other content/assets;
- runtime FX-based pricing;
- redesigning the full internal Django OMS for every country;
- immediate per-region infrastructure or database separation;
- market-specific review visibility unless requested by product policy;
- selecting actual tax, payment, CRM, fulfilment, or messaging vendors for an unnamed launch market;
- changing the global customer-identity decision;
- implementing the design in this documentation phase.

## 14. Business inputs required before enabling a real market

The technical foundation and India backfill can proceed without these, but public enablement needs:

- market code, supported shipping countries, currency, timezone, and allowed locales;
- legal entity, tax mode, invoicing requirements, rounding rules, and refund policy;
- ERP source, SKU identity mapping, warehouses, and dedicated/shared stock-pool decision;
- price list and promotion ownership;
- payment methods and provider accounts;
- shipping/fulfilment, returns, warranty, CRM, OTP, WhatsApp/SMS/email providers;
- enabled feature set for browse, checkout, test rides, dealers, EMI, exchange, factory visits, warranty, and referrals;
- data residency or regulatory constraints that require infrastructure routing;
- owners and go/no-go criteria for each launch gate.

These are configuration and operational dependencies, not reasons to introduce country-specific endpoint shapes.
