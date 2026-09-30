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

## The chatbot's own SQL, locally

`local_check.sh` builds the same local Postgres, loads these files, and runs the
chatbot's reader (`src/emotorad_ai/tools/amigo.py`) against it. It needs the Postgres
binaries (`PG_BIN`, default `C:/Program Files/PostgreSQL/17/bin`), `psycopg`, and the
backend repo (first argument). `EMOTORAD_TEST_LOCAL_PG=1` runs it as a test
(`tests/test_amigo_local_pg.py`).

## Chat with the test riders from a laptop

The database is not reachable from the internet; a laptop reaches it through the
chatbot's staging server.

1. Install AWS's Session Manager plugin for Windows, then reopen PowerShell.
2. Keep this running in one PowerShell window:

   ```powershell
   aws ssm start-session --profile emotorad-staging --target i-02e7dc2874e0fdacb --document-name AWS-StartPortForwardingSessionToRemoteHost --parameters "host=amigo-stage-db.c94k446ourh7.ap-south-1.rds.amazonaws.com,portNumber=5432,localPortNumber=5433"
   ```

3. In a second window, from the repo, set the connection string to `localhost:5433`
   with the `ro_chatbot` password (from the password manager, never pasted anywhere
   else) and start the chat:

   ```powershell
   $env:EMOTORAD_AMIGO_PG_DSN = "postgresql://ro_chatbot:<password>@localhost:5433/userbike?sslmode=require"
   python scripts/chat_local.py --store memory
   ```

4. `http://localhost:8000/health` shows `"amigo": "configured"`. Chat as rider A, B or C:
   verify with `9700000031`, `9700000032` or `9700000033` and the code on the page.
