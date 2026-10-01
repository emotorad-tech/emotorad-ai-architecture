# Where conversations come from: design

Date: 1 October 2026. Branch: `feat/conversation-origin`, cut from
`feat/integrate-jev-mongo` (e693722).

## Purpose

Record roughly where each conversation comes from (country, state, city), for
**reporting only**: counts of conversations by place over a date range, India
against Spain. Not shown to the bot or to support staff, not used for fraud
checks. The person's decisions (1 October 2026):

- One location per conversation run, from its first message. Not per message.
- Built for the Amiigo app first, and modular for every channel.
- Two sources: the IP address and the verified phone number. No app GPS.
- IP to place through a database file on our own server (DB-IP Lite). No
  customer IP is sent to a third party.
- Kept permanently, and deleted whenever the customer asks.

## What each channel can give

| Channel | How it reaches us | Signals |
|---|---|---|
| Amiigo app | The app calls our server directly (`POST /message` today, `POST /amiigo/v1/message` when built) | IP; the phone from the rider's token |
| Website chat | The browser calls our server directly | IP; the phone once verified |
| WhatsApp | Meta's servers call our webhook; the customer's IP never reaches us | Phone only |

## Design

### 1. The real client IP (`src/emotorad_ai/client_ip.py`, new)

nginx sets `X-Real-IP $remote_addr` (checked on the staging server, 1 October
2026). The container sees every request from Docker's bridge gateway,
`172.17.0.1`, and uvicorn does not trust that, so today `request.client.host`
is the same for every customer. Both rate limits (`message_limiter`,
`upload_limiter`) therefore share one bucket across all customers.

`client_ip(request) -> Optional[str]`:

- If the direct peer is a trusted proxy and `X-Real-IP` parses as an IP
  address, return it.
- Otherwise return the direct peer.
- Trusted proxies: `EMOTORAD_TRUSTED_PROXIES`, comma-separated, default
  `127.0.0.1,::1,172.17.0.1`. A header from any other peer is ignored, so a
  client cannot fake its address.

Both rate limits use `client_ip`. Each customer gets their own limit.

### 2. Place lookup (`src/emotorad_ai/origin.py`, new)

```python
@dataclass(frozen=True)
class Place:
    country: str            # ISO 3166 code, "IN"; "unknown" when nothing resolved
    region: Optional[str]   # "Maharashtra"
    city: Optional[str]     # "Pune"
    source: str             # "ip" | "phone" | "none"
    db: Optional[str]       # "dbip-city-lite-2026-10" for an IP answer

UNKNOWN = Place("unknown", None, None, "none", None)

class IpLocator:            # wraps the .mmdb reader; a fake in tests
    def place(self, ip: Optional[str]) -> Optional[Place]: ...

def from_phone(phone: Optional[str]) -> Optional[Place]: ...
def choose(*places: Optional[Place]) -> Place:   # first that resolved, else UNKNOWN
```

- **IP.** `IpLocator` opens the file with `maxminddb` (memory-mapped) and reads
  `country.iso_code`, `subdivisions[0].names.en` and `city.names.en`. A
  private, loopback or otherwise non-global address, an address not in the
  file, or a read error gives `None`. A read error is logged as
  `origin_lookup_failed` with the exception class, never the address.
- **Phone.** The country from the calling code of an E.164 number: `+91` is
  `IN`, `+34` is `ES`, anything else `None`. Country only.
- **No file.** `ip_locator_from_env()` returns `None` when the file is missing,
  or unreadable (logged once at start-up as `origin_db_unavailable`); every IP
  lookup then gives `None` and the phone, or `unknown`, stands. The file's
  month (`dbip-city-lite-2026-10`) is read from its own build date.

Adding a channel means passing its signals to `choose`; nothing else changes.

### 3. When it is recorded (API and runtime)

- **API** (`post_message`): looks up the place for `client_ip(request)` and
  passes it as `entry_metadata["origin"]` (a dict of the `Place` fields). The
  raw IP never leaves the API function and is never logged. The lookup is an
  in-memory read of the local file, so it runs on every message; only the
  first one per run is kept.
- **Runtime** (`_node_prepare`, where `cluster_id` is already applied): if the
  run has no origin yet, it takes `choose(ip_place, from_phone(phone))`, where
  `phone` is the identity's verified phone if any, stores it on the working
  state (`state.origin`) and writes the run's origin record.
- **Unknown, then a phone.** If the run's origin has no country and a phone is
  proven later in the same run (website chat verifies after the first
  message), the record is updated once with the country from the phone,
  `source: "phone"`. A known origin is never changed.
- **The person, once known.** When a run's `user_key` is first set (the
  customer verifies part-way through), the run's record is updated with it, so
  a deletion by phone finds a run that began anonymous.
- **A new run** (a conversation resumed after its working state expired, or
  `restart_for` when a different person verifies) starts with no origin and
  records its own.
- The future `POST /amiigo/v1/message` and any WhatsApp webhook pass their
  signals the same way: `entry_metadata["origin"]` from the IP where there is
  one, and the identity's phone.

### 4. Storage (`conversation_origins`, new MongoDB collection)

One document per conversation run, written by the runtime through the
conversation store (`record_origin`), for every run including anonymous ones
(summaries are written only once a person is known, so they would miss
visitors who never verify):

```json
{"_id": "<conversation_id>#<started_at>", "conversation_id": "...",
 "started_at": "2026-10-01T09:12:03Z", "channel": "amiigo_app",
 "country": "IN", "region": "Maharashtra", "city": "Pune",
 "source": "ip", "db": "dbip-city-lite-2026-10",
 "user_key": "PHONE#+91..."}
```

- **Kept permanently** (the person's decision, 1 October 2026): no TTL index,
  like the transcript and the summaries.
- **Deleted on request**, through the existing erasure script,
  `scripts/delete_person.py`, which a person runs and which writes the
  `erasure_log` audit record:
  - `--phone` or `--dealer-id`: `conversations_of` and `delete_person` include
    `conversation_origins` (by `user_key`), like the other collections;
  - `--conversation-id`: `delete_conversation` deletes the run records by
    `conversation_id`, which covers a run that never verified;
  - the dry run's counts list the new collection.
- Indexes: `started_at`; `(country, region)`; `user_key`.
- The in-memory store gets the same method, for tests and local runs.
- `scripts/mongo_setup.py` creates the collection and its indexes; the person
  runs it against Atlas once.

A failed write is logged as `origin_record_failed` with the error class and
the chat carries on. It is never retried inside the turn and never raised.

### 5. The database file

- Built into the image: a `RUN` step in the `Dockerfile` downloads
  `https://download.db-ip.com/free/dbip-city-lite-YYYY-MM.mmdb.gz` for the
  current month, falling back to the previous month (the new file appears
  early in the month), and unpacks it to `/app/geo/dbip-city-lite.mmdb`. If
  both downloads fail the build still succeeds, without the file.
- Each deploy therefore takes the newest file. The file is about 121 MB
  compressed, so the image grows; the staging disk (8 GB) has room after the
  build-cache clean-up, and growing it to 20 GB is recommended separately.
- Licence: CC BY 4.0. Wherever results are shown, credit "IP Geolocation by
  DB-IP" with a link to https://db-ip.com. The report script prints it.
- `/health` gains `"ip_location": "dbip-city-lite-2026-10"`, or
  `"not configured"` without the file.
- New dependency: `maxminddb` (Apache 2.0) in `requirements.txt`.

### 6. Reporting (`scripts/origin_report.py`, new)

Read-only. Counts conversation runs by `country`, `region` or `city` (`--by`)
between two dates (`--from`, `--to`), optionally for one `--channel`, from
`conversation_origins`. Prints a table and the DB-IP credit. The person runs it
against Atlas with their own `EMOTORAD_MONGO_URI`.

## Privacy

- An IP address and a place derived from it are personal data under India's
  DPDP Act and, for Spanish customers, GDPR. This design keeps the minimum for
  the stated purpose: no raw IP stored or logged, city at most, one per run.
  It is kept permanently and erased on request with everything else held
  about the person.
- The privacy notice must say that an approximate location (city, state and
  country) is derived from the connection for reporting. The wording is for the
  person and Sachin to agree. It is not code and does not block the build.

## Errors

Nothing in this feature can stop or delay a reply: no file, a bad address, a
lookup error, or a failed write each give `unknown` or a logged
`origin_record_failed`, and the turn continues.

## Testing

- `client_ip`: a trusted peer with a header gives the header; an untrusted
  peer with a header gives the peer; a malformed header gives the peer; the
  rate limit counts two customers separately behind the proxy.
- `origin`: DB-IP-shaped records through a fake reader; private and loopback
  addresses give `None`; `+91` and `+34` numbers; another code gives `None`;
  `choose` order; no file gives `None` from every lookup.
- Runtime: recorded once per run on the first message, not changed by later
  messages; an unknown origin filled from a phone proven later; a new run
  records its own; an Amiigo-style message with a phone and no usable IP
  records the phone's country; a failing store logs `origin_record_failed`
  and the reply still arrives.
- Stores: `record_origin` on the in-memory store and on MongoDB (mongomock);
  the record gains `user_key` when the person verifies part-way through the
  run; `conversations_of`, `delete_person` and `delete_conversation` include
  the new collection; the erasure script's dry run counts it.
- API: `entry_metadata["origin"]` holds the place and never the IP; `/health`
  reports the file.
- Report: counts by region and by country over a date range (mongomock).
- Opt-in, like the local Postgres test: `EMOTORAD_TEST_GEO_DB=<path to a real
  .mmdb>` checks a known public address resolves to its country.

## Out of scope

App GPS; showing the place to the bot or to support staff; fraud checks; a
dashboard (the script is the report for now); building the Amiigo endpoint.
