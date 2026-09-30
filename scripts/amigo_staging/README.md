# Test data for the chatbot in Amigo staging

Read-only access for the chatbot (`ro_chatbot`) and three invented test riders, in
the **old, unused** databases on `amigo-stage-db` (`userbike`, `garage`, `ride`).
The live staging platform uses the `revalt_…` databases; nothing here touches them.

A person runs these, in psql as `postgres`, never a Claude session. Each file is
one transaction and safe to run twice; each `cleanup_*.sql` removes exactly what
its seed added.

| Order | Connected to | File |
|---|---|---|
| 1 | any database | the role: `CREATE ROLE ro_chatbot LOGIN CONNECTION LIMIT 10;`, `\password ro_chatbot`, then the two `ALTER ROLE` lines at the top of `userbike.sql` |
| 2 | `userbike` | `userbike.sql` |
| 3 | `garage` | `garage.sql` |
| 4 | `ride` | `ride.sql` |

The riders: A `+919700000031` (two bikes), B `+919700000032` (one bike registered by
IMEI), C `+919700000033` (one bike with service records and three trips).

Tested on 30 September 2026 against a local Postgres built from the
`amigo-stage-deployment` migrations: grants, a second run, what `ro_chatbot` can and
cannot read (no MQTT password, no GPS, no writes), and both cleanup runs.
