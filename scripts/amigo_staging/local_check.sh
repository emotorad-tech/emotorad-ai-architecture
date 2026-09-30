#!/usr/bin/env bash
# The reader's real SQL against a throwaway local Postgres holding the Amigo
# tables as the amigo-stage-deployment migrations create them, seeded with
# the three test riders. Nothing here reaches AWS. Needs Postgres binaries
# (PG_BIN), psycopg, and the backend repo ($1).
set -u
BACKEND="${1:-C:/Users/user/emotorad/backend}"
PG_BIN="${PG_BIN:-/c/Program Files/PostgreSQL/17/bin}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
WORK="$(mktemp -d)"
BRANCH=origin/amigo-stage-deployment
export PGHOST=localhost PGPORT=55432

cleanup() { "$PG_BIN/pg_ctl" -D "$WORK/data" -m fast stop >/dev/null 2>&1; rm -rf "$WORK"; }
trap cleanup EXIT

"$PG_BIN/initdb" -D "$WORK/data" -U postgres --auth=trust -E UTF8 >/dev/null || exit 1
"$PG_BIN/pg_ctl" -D "$WORK/data" -o "-p 55432" -l "$WORK/pg.log" -w start >/dev/null || exit 1
q() { "$PG_BIN/psql" -X -q -v ON_ERROR_STOP=1 -U "$1" -d "$2" "${@:3}"; }

for db in userbike garage ride; do q postgres postgres -c "CREATE DATABASE $db" || exit 1; done
git -C "$BACKEND" show "$BRANCH:server/userbike-server/db/migration/000001_schema.up.sql" > "$WORK/userbike.sql"
git -C "$BACKEND" show "$BRANCH:server/garage-server/db/migration/000001_schema.up.sql" > "$WORK/garage.sql"
git -C "$BACKEND" show "$BRANCH:server/trip-server/db/migration/000001_schema.up.sql" > "$WORK/ride1.sql"
git -C "$BACKEND" show "$BRANCH:server/trip-server/db/migration/000002_add_merge_columns.up.sql" > "$WORK/ride2.sql"
q postgres userbike -f "$WORK/userbike.sql" >/dev/null || exit 1
q postgres garage -f "$WORK/garage.sql" >/dev/null || exit 1
q postgres ride -f "$WORK/ride1.sql" >/dev/null 2>&1 && q postgres ride -f "$WORK/ride2.sql" >/dev/null 2>&1 || exit 1

q postgres postgres -c "CREATE ROLE ro_chatbot LOGIN CONNECTION LIMIT 10" \
  -c "ALTER ROLE ro_chatbot SET default_transaction_read_only = on" -c "ALTER ROLE ro_chatbot SET statement_timeout = '5s'"
for db in userbike garage ride; do q postgres "$db" -f "$HERE/$db.sql" >/dev/null || exit 1; done

PYTHONPATH="$ROOT/src" python - <<'PY'
from emotorad_ai.tools.amigo import AmigoReader, describe_service, describe_trips
r = AmigoReader("postgresql://ro_chatbot@localhost:55432/userbike")
a = r.bikes("+919700000031"); b = r.bikes("9700000032"); c = "+919700000033"
print("A bikes:", [x["model"] for x in a["bikes"]])
print("B frame:", b["bikes"][0]["framenumber"], "imei:", b["bikes"][0]["imei"])
print("C service:", describe_service(r.service_status(c))["stages"])
print("C trips:", [t["distance_km"] for t in describe_trips(r.recent_trips(c), r.bikes(c))["trips"]])
PY
