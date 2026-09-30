# Amigo data in the chatbot, read-only

Date: 2026-09-30. Owner: Sagnik. Status: for review.

## What the person asked for

- The chatbot reads data from the Amigo staging database and uses it in real conversations. Conversations still go to MongoDB and photos to S3, as today.
- Read-only access through a database role, with the connection string in Secrets Manager. No change to the Amigo backend's code. Nothing in the `revalt_…` databases is touched.
- Test data, not real riders: three invented riders in the old, unused databases on `amigo-stage-db` (`userbike`, `garage`, `ride`), added by the person (`scripts/amigo_staging/`).

Decisions from the brainstorm (2026-09-30):

- After verification the bot lists **both** systems' bikes, merged: the OMS (warranty records) and Amigo (app registrations).
- Amigo's service records and trips are used **inside troubleshooting**, by the battery and motor agents. No new topics or routes.
- A bike registered in the app by IMEI, whose frame number field holds the IMEI, is shown as **"frame number not on record"**.

## Where things stand

- Access is in place (checked 2026-09-30): role `ro_chatbot` on `amigo-stage-db`, read-only by default, a 5-second statement limit, SELECT on only these columns:
  - `userbike`: `emuser` (emuserid, username, phone), `bike` (vin, model, color, framenumber, imei, nickname, createdat), `userbikemap` (emuserid, bikevin, primarymapping, createdat);
  - `garage`: `servicedetail` (emuserid, vin, bikemodel, purchasedate, odometer, services), `servicehistory` (emuserid, vin, bikemodel, servicetype), `servicetype` (all);
  - `ride`: `trips` (emuserid, tripid, vin, startsat, endsat, distance, duration, faultdatastorage).
- The chatbot's staging server (`emotorad-ai-stage`, 192.168.56.210) is in the same VPC as the database and inside its security group's `192.168.0.0/16` rule. The database is not reachable from the internet; a laptop reaches it through a Session Manager port forward via that server.
- The connection string is the field `EMOTORAD_AMIGO_PG_DSN` in `/emotorad/stage/ai/app`, which the config store already exports as an environment variable at start-up. Its database is `userbike`.
- Every part of the bot that needs a rider's bikes asks one source by phone: `build_registry(warranty_source=...)`, used by `lookup_warranty_record` (and so by identity hydration, the verify-first step, triage and the context) and by `bikes_on` (tickets and replacement orders). Without the OMS key the source is the fixtures.
- Test riders: A `+919700000031` (EMX Plus and Doodle Pro), B `+919700000032` (a T-Rex Smart registered by IMEI), C `+919700000033` (a T-Rex Air, with a service record: 1180 km, the 250 km service done, the 1000 km one pending; and three trips, 27 to 29 September).

## Design

### 1. The Amigo reader

A new module, `src/emotorad_ai/tools/amigo.py`:

- `AmigoReader(dsn)` connects with `psycopg` (new dependency), a 3-second connect timeout, `application_name=emotorad-ai-chatbot`. The DSN names the `userbike` database; the reader derives the other two by replacing that name (`userbike` to `garage` and `ride`; `revalt_userbike` to `revalt_garage` and `revalt_ride`, for later). A short-lived connection per query; volume is low and the instance is a `db.t3.micro`.
- Three reads, each by the verified phone (E.164, `+91…`) and nothing else:
  - `bikes(phone)`: the rider's own bikes (`primarymapping = true`), oldest first: vin, model, color, framenumber, imei, nickname; and the rider's username.
  - `service_status(phone)`: the rider's `servicedetail` row and completed `servicehistory` types, with the `servicetype` names and thresholds.
  - `recent_trips(phone, limit=5)`: the latest trips by `startsat`: tripid, vin, start, end, distance, duration.
- A 5-minute cache per phone for `bikes`, because hydration asks every turn. Service status and trips are read when a tool is called, not cached.
- Only SELECT statements. The role cannot write either.
- `from_env()` returns a reader when `EMOTORAD_AMIGO_PG_DSN` is set, otherwise None. Without it everything behaves as today.

### 2. The merged bike source

`merged_source(oms_source, reader)` returns OMS-shaped records, so `_coverage`, the tool and `bikes_on` keep working:

- OMS records come first, as they are. An Amigo bike whose frame number equals an OMS record's (ignoring case and spaces) adds nothing new to that record except `in_app: true` and its `vin` (for the two tools).
- An Amigo bike with no OMS match becomes a record with:
  - `product_name` from a small model table: `EMXPLUS` "EMX Plus", `EMX` "EMX", `DOODLEPRO` "Doodle Pro", `TREXAIR` "T-Rex Air", `TREXPLUS` "T-Rex Plus", `TREXPLUSV2` "T-Rex Plus V2", `TREXPLUSV3` "T-Rex Plus V3", `TREXSMART` "T-Rex Smart", `X1`/`X2`/`X3`, `S2`, `DYNEM`/`dy` "Dynem"; an unknown code as it is. The knowledge base's Doodle-only records then apply by name, as they do for OMS bikes.
  - `product_color` from the colour, capitalised.
  - `warranty_on_record: false`. `_coverage` gives it `coverage_status: "not_registered"`, `in_warranty: None`, `remedy: "late_warranty_registration"` and a note: the bike is in the Amigo app but not registered for warranty with EMotorad; state no coverage; offer to register it. Amigo's `purchasedate` is typed by the rider and is never used for warranty.
- **Frame number not on record.** When Amigo's frame number equals its IMEI, or is 15 digits, the record has `frame_number: None` and `frame_on_record: false`. Every bike also gets `bike_ref`: its frame number when on record, otherwise `vin:<VIN>`. Selection (triage, `state.selected_frame`, `Runtime._selected_bike`) and ownership checks (`_owned_bike`) use `bike_ref`. Nothing shown to the rider or written into the context shows a `vin:` reference, a VIN or an IMEI: the bike list says "frame number not on record", and when a ticket needs one the bot asks the rider to read it off the sticker and puts it in the ticket's description.
- **Name.** `customer_name` from the OMS. Amigo's username only when the OMS has none and it is not the default "User".
- **Failures.**
  - Amigo unreachable or slow: OMS records only; `amigo_unavailable` logged with the error class, never the DSN.
  - OMS down (`oms_unavailable`) and Amigo has bikes: the Amigo bikes, each with `coverage_status: "warranty_unavailable"` and a note that warranty cannot be checked right now. With no Amigo bikes, the error is raised as today.
  - Neither has a bike: `no_warranty_record`, as today (registration).

### 3. Two tools for troubleshooting

Registered only when a reader exists, added to `battery_support` and `motor_support` (full agents; the narrow path is unchanged in this step):

- `get_service_status` (injects the phone): for the rider's bike in the app, each service stage with its threshold and status, for example "250 km / 1 month: done", "1000 km / 6 months: due", "2000 km / 12 months: upcoming", and the odometer. "No service record in the app" when there is none. The description: use it when a motor, brake or noise problem might be down to a missed service; never book or promise a service from it.
- `get_recent_trips` (injects the phone): the last five rides, newest first, each with date and time (IST), bike model, distance in km, duration in minutes and average speed in km/h. "No rides in the app" when there are none. The description: use it to check a range or power complaint against real rides. No locations: the role cannot read them.

One sentence in each of the two agents' prompts says when to use the tool. Frame numbers and phone numbers never appear in either tool's output.

### 4. Wiring

- `api._build_registry`: `warranty_source=merged_source(<live OMS or fixtures>, reader)` when a reader exists; the two tools registered through `build_registry(amigo=reader)`.
- `/health` gains `"amigo": "configured" | "not configured"`.
- `scripts/chat_local.py` prints whether `EMOTORAD_AMIGO_PG_DSN` is set, never its value.
- Staging needs nothing more than a deploy: the secret field is already there, and the container reaches the database through the host.

## Testing

- `tests/test_amigo_source.py`, with a fake reader holding riders A, B and C:
  - merging: OMS only, Amigo only, both with one bike in common (listed once, `in_app`), frame numbers compared ignoring case;
  - rider B: `frame_number` None, "frame number not on record" in the bike list, `bike_ref` used for selection, no IMEI or VIN in any reply or in the context;
  - the model table and the Doodle records still applying to "Doodle Pro";
  - failures: Amigo down (OMS only, logged), OMS down with Amigo bikes (listed, warranty unavailable), neither (`no_warranty_record`);
  - the cache: a second hydration within 5 minutes does not query again.
- `tests/test_amigo_tools.py`: both tools' output for rider C, the empty cases, a failure, and that neither shows a phone or frame number.
- A flow test through the runtime: rider A verifies (verify first), sees both bikes, picks the Doodle Pro, and the battery agent's `get_recent_trips` call reaches the fake reader.
- `scripts/amigo_staging/local_check.sh`: builds a local Postgres from the backend's `amigo-stage-deployment` migrations, loads the seed, and runs the reader's real SQL as `ro_chatbot`. An automated test runs it only when `EMOTORAD_TEST_LOCAL_PG=1`.
- Then the person: from the laptop through the port forward (`EMOTORAD_AMIGO_PG_DSN` pointing at `localhost:5433`), chatting as riders A, B and C; later on staging after a deploy.

## Not in this change

- The `revalt_…` databases (a later switch: the secret's database name and the grants).
- Battery data (Redis, S3 Parquet).
- Bikes shared with a rider (`primarymapping = false`).
- The narrow agent's tools.
- New questions such as "when is my next service?" as their own topic.
- Anything written to Amigo.
