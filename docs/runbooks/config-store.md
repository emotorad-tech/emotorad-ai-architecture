# Runbook: the app config store

One Secrets Manager secret per environment, `/emotorad/<env>/ai/app`, read by the
container at startup (`docker/start.py` → `src/emotorad_ai/config_store.py`). The value is
a flat JSON object. Field names are the environment variables the code reads:

| Field | Read by |
|---|---|
| `OPENROUTER_API_KEY` | **required on staging since 2026-09-29** (`EMOTORAD_AI_MODE=openrouter`): Jev routing, the narrow and full agents, and `video_summary.py`, which describes a customer's uploaded video through OpenRouter once at ingest (`/health` reports `"video_summary":"openrouter"`) |
| `EMOTORAD_MONGO_URI` | **required on staging since 2026-09-29** (`EMOTORAD_STORE=mongodb`): `stores/mongo.py`, conversations in database `emotorad_ai`. The instance's egress address must be on the Atlas access list |
| `API_KEY_CLAUDE` | exported as `ANTHROPIC_API_KEY` too; the API in `anthropic` mode and the playground |
| `API_KEY_GEMINI` | exported as `GEMINI_API_KEY` too; `video_summary.py` uses Gemini directly only with `EMOTORAD_VIDEO_SUMMARY=gemini` or when there is no OpenRouter key. Optional: with neither key the video is sent to the model as sampled frames, and `/health` reports `"video_summary":"frames"` |
| `EMOTORAD_OMS_API_KEY` | `tools/oms.py` |
| `EMOTORAD_AI_PLAYGROUND_USER` | `api.py` basic auth on `/playground` |
| `EMOTORAD_AI_PLAYGROUND_PASSWORD` | `api.py` basic auth on `/playground` |
| `LANGFUSE_PUBLIC_KEY` | `tracing.py`; optional, tracing is off without both Langfuse keys |
| `LANGFUSE_SECRET_KEY` | `tracing.py`; see `docs/runbooks/tracing.md` |
| `EMOTORAD_ZOHO_REFRESH_TOKEN` | `zoho/settings.py`. **Secret.** The switch for Zoho Desk tickets: absent means the mock, as before. The OMS's own refresh token, shared by Sachin's decision of 5 October 2026. Never revoke it: removing this field is the rollback. See section 7 |
| `EMOTORAD_ZOHO_CLIENT_ID` | `zoho/settings.py`. **Secret.** The OMS's Zoho OAuth client id |
| `EMOTORAD_ZOHO_CLIENT_SECRET` | `zoho/settings.py`. **Secret.** The OMS's client secret. When it is rotated in the OMS, update it here the same day, or `/health` says `token refused: invalid_client_secret` |
| `EMOTORAD_ZOHO_ORG_ID` | `zoho/settings.py`. Not secret; needed with the token. The Desk organisation id |
| `EMOTORAD_ZOHO_TEST_DEPARTMENT_ID` | `zoho/settings.py`. Not secret; needed with the token. The configured test department (Inkodop technologies Pvt.Ltd), by id, where every test-mode ticket goes |
| `EMOTORAD_ZOHO_TEST_CONTACT_ID` | `zoho/settings.py`. Not secret; needed with the token. The "AI chatbot test" contact, on every test-mode ticket |
| `EMOTORAD_ZOHO_DEPARTMENT_ID` | `zoho/settings.py`. Not secret; live only. The real department |
| `EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID` | `zoho/settings.py`. Not secret; live only. The "Unverified AI chat" contact, on every ticket for a number nobody proved |
| `EMOTORAD_ZOHO_LIVE` | `zoho/settings.py`. Not secret; live only. Exactly `yes` sends to the real department; anything else is test mode. Set only after Sachin's sign-off. Refused while `EMOTORAD_AI_DEV_CODES` is on or the OTP sender is the mock |
| `EMOTORAD_ZOHO_LAYOUT_ID` | `zoho/settings.py`. Optional, not secret: the ticket layout every ticket is made in, from `scripts/zoho/probe.py`, when the department has more than one active layout. Absent means the department's default |
| `EMOTORAD_ZOHO_PRIORITY_HIGH` | `zoho/settings.py`. Optional, default `High`: Zoho's priority value for urgent tickets, from the probe |
| `EMOTORAD_ZOHO_PRIORITY_MEDIUM` | `zoho/settings.py`. Optional, default `Medium`: the priority for every other ticket |
| `EMOTORAD_ZOHO_CHANNEL` | `zoho/settings.py`. Optional, default `Chat`: a system channel from the probe, never an integration channel |
| `EMOTORAD_ZOHO_CREDITS_FLOOR` | `zoho/settings.py`. Optional, default `1000`: below this many API credits left today, only urgent tickets are sent |
| `EMOTORAD_ZOHO_ATTACHMENT_LIMIT_MB` | `zoho/settings.py`. Optional, default `20`: a photo or video over this is noted on the ticket, not attached |
| `EMOTORAD_ZOHO_WEBHOOK_SECRET` | `amiigo/webhooks.py`. **Secret.** Optional: the Zoho Desk webhook's secret, which tells the rider a ticket closed. 32 to 256 letters, digits, `-` or `_` (`secrets.token_urlsafe(32)` makes one), and the last segment of the address the webhook is set up with in Zoho Desk, `https://<host>/webhooks/zoho/tickets/<secret>`: Zoho Desk's webhooks send no header of ours. Absent or refused, the webhook answers 503 and `/health` says `"zoho_webhook":"not configured"` or why; tickets are sent either way. The API's access log writes the path without the secret; nginx's does not, unless that location's `access_log` is off. A person sets it, never a Claude session; what it is and how to rotate it are in section 8 |
| `EMOTORAD_AMIIGO_PUBLIC_KEY` | `amiigo/auth.py`. Not secret, but set by a person. The Amiigo app's PASETO v4 public key, 64 hexadecimal characters, which our server checks the app's access tokens with. It comes from the owner of the Amiigo backend (Sachin). It is the public key only: Amiigo's secret signing key is never copied anywhere. Absent or not 64 hex characters, no token is accepted: the app's calls answer 503, the chat socket closes 1011 before `ready`, and `/health` says `"amiigo_tokens":"not configured"`. See section 8 |
| `EMOTORAD_WARRANTY_API_KEY` | `tools/warranty_api.py`. **Secret.** Optional: the AI service key for EMotorad's warranty service (Sachin's), sent as `x-service-key` to `searchRegistrationsByPhone`. Set, the bot's bikes and coverage come from that service and no longer from the OMS (the OMS keeps the order-number look-up); `/health` shows `"warranty_source":"warranty_api: <host>"`. Absent, the OMS answers when `EMOTORAD_OMS_API_KEY` is set, the fixtures otherwise. Never logged or shown; removing it and redeploying is the rollback |
| `EMOTORAD_WARRANTY_API_URL` | `tools/warranty_api.py`. Optional, not secret: the warranty service's base URL. Default `https://d2c-storefront-staging.emotorad.com`, staging, the only deployment its spec names today; production sets it |
| `EMOTORAD_EVIDENCE_CHECK` | `evidence_check.py`, `api.py`. Not secret, and set on the `docker run` line in `deploy-staging.yml` (`on` there), not in the secret: the environment wins. Exactly `on` turns on the evidence check before a ticket: in a battery or motor chat, Gemini (through `OPENROUTER_API_KEY`) must see the fault in the customer's video or photo before a support or handover ticket is recorded. Anything else is off, as before. On without the OpenRouter key, nothing can pass and no fault ticket is recorded; `/health` then says `"evidence_check":"on, no checker: nothing can pass"` |
| `EMOTORAD_EVIDENCE_MODEL` | `evidence_check.py`. Optional, not secret: the OpenRouter model the evidence check asks. Default `google/gemini-3.8-flash`, the photo check's |
| `EMOTORAD_CUSTOMER_CARE_CONTACT` | `evidence_check.py`, `runtime.py`. Optional, not secret: EMotorad's customer care contact, given instead of a ticket when the evidence never passes. Unset, the bot says "Please contact EMotorad customer care." with no number |

### Note on EMOTORAD_OMS_API_KEY

The entrypoint exports every field to the Streamlit child too, so with this field set,
anyone holding the staging playground login can look up real customer warranties and
orders by phone number in the playground's Live customer mode. Leave the field out of the
secret until that exposure has been accepted; the API still starts without it — the OMS
client raises a named error only when a tool needs it.

Every command below uses `--profile emotorad-staging --region ap-south-1`. Set them once:

```bash
export AWS_PROFILE=emotorad-staging AWS_REGION=ap-south-1
```

## 1. Create the secret shell and the role permission (once per environment)

### Pre-flight: IMDS hop limit

The container fetches instance-role credentials through Docker's bridge network, which
needs `HttpPutResponseHopLimit` of at least 2 on the instance.

```bash
aws ec2 describe-instances --instance-ids i-02e7dc2874e0fdacb \
  --query 'Reservations[0].Instances[0].MetadataOptions.[HttpTokens,HttpPutResponseHopLimit]' --output text
```

The hop limit must be 2 or more, or the container cannot reach the instance role and will
restart-loop after the deploy has already removed the old container. Raise it with
`aws ec2 modify-instance-metadata-options --instance-id i-02e7dc2874e0fdacb --http-put-response-hop-limit 2 --http-tokens required`.

```bash
aws cloudformation deploy \
  --stack-name emotorad-ai-stage-config-store \
  --template-file infra/config-store.yaml \
  --parameter-overrides Environment=stage InstanceRoleName=emotorad-ai-stage-ec2-role \
  --capabilities CAPABILITY_NAMED_IAM
```

For prod: `Environment=prod` and the prod instance role name, stack
`emotorad-ai-prod-config-store`.

## 2. Set the value (a person, from a terminal)

Write the JSON to a file outside any repo, set it, then shred the file. Never paste a value
into a chat session, a commit, or a workflow.

```bash
cat > /tmp/app-config.json <<'EOF'
{"API_KEY_CLAUDE":"...","EMOTORAD_OMS_API_KEY":"...","EMOTORAD_AI_PLAYGROUND_USER":"...","EMOTORAD_AI_PLAYGROUND_PASSWORD":"...","LANGFUSE_PUBLIC_KEY":"pk-lf-...","LANGFUSE_SECRET_KEY":"sk-lf-..."}
EOF
aws secretsmanager put-secret-value --secret-id /emotorad/stage/ai/app --secret-string file:///tmp/app-config.json
rm -P /tmp/app-config.json
```

Check the shape without printing values:

```bash
aws secretsmanager get-secret-value --secret-id /emotorad/stage/ai/app --query SecretString --output text | python3 -c 'import json,sys; print(sorted(json.load(sys.stdin)))'
```

Expected: the field names above (the two Langfuse keys only if tracing is wanted).

## 3. Deploy

Run the "Deploy to staging (EC2)" workflow. It passes `EMOTORAD_AI_SECRET_ID`,
`EMOTORAD_AI_MODE=anthropic`, `EMOTORAD_AI_APPROVAL_MODE=reasonable` and
`EMOTORAD_AI_DEV_CODES=1`; nothing sensitive. Runtime settings like these are `-e` flags
on the workflow's `docker run` line, read once at container start, so changing one is a
commit plus a redeploy.

`EMOTORAD_AI_DEV_CODES=1` is staging only. It opens `/dev/verification/<conversation_id>`
so a tester can read the one-time code (there is no SMS provider yet). On a public URL
that is a phone-verification bypass for anyone holding a conversation id, so keep
`EMOTORAD_OMS_API_KEY` out of the staging secret while it is on (see the note on it at the top of this runbook), and
never set it on prod. Then:

```bash
curl -s https://ai-release-stage.emotorad.com/health
```

Expected: `{"status":"ok","mode":"anthropic","secrets":"loaded",...,"tracing":"on"}`. A container that could
not read the secret does not start; its reason is one line in the CloudWatch log group
`emotorad-ai-stage` beginning `startup config:`.

A release that adds a MongoDB collection needs a person to rerun `python scripts/mongo_setup.py`
against the environment's database. The first deploy after 2026-10-06 adds `verification_sessions`
(the number each web chat proved, kept 12 hours so a restart does not ask for it again): check that
it is listed as `expires` with `expires_at_ttl (TTL 0s)`. Until that index exists the service saves
nothing there: it keeps proofs in memory as before, logs `verification_sessions_ttl_missing` at
error level, and `/health` shows `"verification_sessions":"memory: TTL index missing, run
scripts/mongo_setup.py"`. Run the script, then restart the container (`sudo docker restart
emotorad-ai`) and check that `/health` shows `"verification_sessions":"mongodb"`. The Amiigo support
chat adds more collections and indexes the same way: see section 8, step 3.

## 4. After the first successful deploy

Delete the two GitHub Actions secrets the workflow no longer reads:

```bash
gh secret delete PLAYGROUND_BASIC_AUTH_USER --env staging --repo emotorad-tech/emotorad-ai-architecture
gh secret delete PLAYGROUND_BASIC_AUTH_PASSWORD --env staging --repo emotorad-tech/emotorad-ai-architecture
```

## 5. Rotate a value

Repeat step 2 with the new value, then redeploy (step 3). The loader reads the secret only
at container start.

## 6. Switch the model path

`EMOTORAD_AI_MODE` in the workflow's `docker run` line: `anthropic` (default), `bedrock`
(needs `bedrock:InvokeModel` on the inference profile, see the deployment plan §1.1 and
§2.1), or `offline`.

`EMOTORAD_AI_MODE=bedrock` uses `anthropic.claude-opus-5` by default — the Bedrock-shaped
model id, distinct from the Anthropic path's default of `claude-opus-5`. `EMOTORAD_AI_MODEL`,
if set, overrides the default for whichever mode is active, so it must match that path's id
format: the unprefixed id (`claude-opus-5`, `claude-sonnet-5`, …) for `anthropic`, the
`anthropic.`-prefixed id for `bedrock`.

### The Jev kill switch

`EMOTORAD_JEV` in the config store (8 October 2026), read when the container starts. It
matters only in `openrouter` mode, which staging runs.

- Unset, or `on`: Jev routes each turn to a standard reply, the narrow model or the full
  agent, as before.
- `off`: no Jev call is made, and every turn goes to the full agent (Haiku on OpenRouter).
  The turn's route is logged with the reason `jev_disabled`.
- Anything else is treated as `off`, and `/health` says `"jev":"off: EMOTORAD_JEV must be on
  or off"`.

It is a config-store key, not a `docker run -e` flag on purpose: the environment wins over
the secret (section 2), so a flag in `deploy-staging.yml` would make the secret's value
ignored. To flip it, change the key in the secret (any of the ways in section 2 that keeps
the other keys), then restart the container so it reads the secret again: `sudo docker
restart emotorad-ai` through an SSM session, or a redeploy. Check `/health`: `"jev":"on"` or
`"jev":"off"`. Rollback is setting it back and restarting again.

## 7. Zoho Desk tickets

Spec: `docs/superpowers/specs/2026-10-05-zoho-desk-tickets-design.md`. Zoho is off while
`EMOTORAD_ZOHO_REFRESH_TOKEN` is absent: tickets go to the mock and `/health` says
`"zoho":"not configured"`. `EMOTORAD_AI_ENV` (already on the workflow's `docker run` line) is
needed too, because it starts every chat reference, for example `stage:EM-1000001`. The
playground, the CLI and the local chat page never use these settings, even when they are present.
The entrypoint exports every field to the Streamlit child too (see the note on
`EMOTORAD_OMS_API_KEY`), so the playground inherits the Zoho settings, but it keeps the mock: only
the API's lifespan starts Zoho.

Run every command here in a terminal window outside the Claude app, and clear the scrollback
afterwards. Never paste a value into a chat session.

**Shared with the OMS.** The chatbot uses the OMS's own Zoho client id, client secret and refresh
token (Sachin's decision, 5 October 2026). No grant is made for it, so `consent_url.py` and
`exchange_code.py` are not part of the setup (see "For a future client of our own" below).

- **Zoho's limits** (`zoho.com/accounts/protocol/oauth/token-limits.html`, checked 5 October): at most
  10 active access tokens per refresh token, the oldest invalidated when an eleventh is made; at most
  10 access-token requests in 10 minutes; at most 20 refresh tokens per client per user. The OMS
  (`em-biz-backend`, `zoho/zoho_api_client.py`, `get_token`) keeps its access token in Postgres and
  refreshes it when it is more than an hour old. The chatbot's worker keeps its own for the hour too.
  Together that is about two refreshes an hour on the one refresh token. `probe.py` and
  `test_ticket.py` ask for tokens on it as well, so run each once, not in a loop.
- **A rotation of the OMS's client secret** must be copied to `/emotorad/stage/ai/app`
  (`EMOTORAD_ZOHO_CLIENT_SECRET`, by step 4 below) the same day. Until then `/health` says
  `token refused: <error>` and `zoho_token_refused` raises its alarm.
- **Scopes.** The OMS's config asks for `Desk.tickets.ALL Desk.tickets.READ Desk.tickets.WRITE
  Desk.tickets.UPDATE Desk.tickets.CREATE Desk.contacts.READ Desk.contacts.WRITE Desk.contacts.UPDATE
  Desk.contacts.CREATE Desk.search.READ Desk.basic.CREATE`. It does not ask for `Desk.basic.READ` or
  `Desk.settings.READ`, which only `probe.py` uses (organisations, departments and layouts). The
  service never needs them. Whether the token really lacks them, the probe shows (step 1), and it
  carries on either way.
- **Never revoke the token,** with `revoke.py` or on Zoho's Connected Apps page: it stops the OMS's
  ticketing and AFS dispatch. Rollback is removing `EMOTORAD_ZOHO_REFRESH_TOKEN` (below).

**What Zoho rules filter on.** The Desk has no room for custom fields, so none is set. Each
ticket's subject contains `[AI chat]` and ends with its chat reference in square brackets, for
example `[AI chat] Battery: charging - EMX Plus [stage:EM-1000001]`. A ticket for a number nobody
proved puts `[Unverified]` first: `[Unverified] [AI chat] Unverified customer - bike not given [stage:EM-1000002]`.
So a Zoho rule or webhook criterion filters on the subject containing `[AI chat]`. One written as
"starts with" misses every unverified ticket. The reference at the end is how the worker finds
its own ticket again after a timeout, so nobody edits a subject in Desk. The description's
second line says `Source: AI chatbot`.

**The scripts** (`scripts/zoho/`). Each asks for its secrets by hidden input and prints names, ids
and counts only. With the token shared, the person's steps start at the probe.

| Script | What it does |
| --- | --- |
| `probe.py` | Read only. Lists the departments (or, with the OMS's token, confirms each one through a ticket in it), every active ticket layout (its id, whether it is the default, each field and the required custom ones), the channels and the contacts, and writes masked shapes to `docs/api-shapes/` |
| `test_ticket.py` | Writes one ticket in the test department and reads it back. It asks for the department's name every time. If Zoho enforces the layout's required fields, it names them and stops |
| `tickets_report.py` | Waiting, stuck and held tickets, for the support lead |
| `revoke.py` | Refuses, and says why: the token is the OMS's. Only with `--own-client` does it revoke, and only a token of a client of our own |
| `consent_url.py`, `exchange_code.py` | Not part of the setup while the token is shared. Kept for a future client of our own (below) |

1. **The probe.** Run `python scripts/zoho/probe.py --org-id <org id> --test-department-id <id>
   --test-contact-id <id>`. It asks for the OMS's client id, client secret and refresh token by
   hidden input. Claude reviews the masked shapes it writes and fills in the settings. It prints the
   scopes Zoho says it granted (see the scope note above).

   With the OMS's token, this is what you see, and all of it is expected:

   - `scopes MISSING: Desk.basic.READ, Desk.settings.READ`.
   - `organisations: refused (error=SCOPE_MISMATCH)`, then that the token cannot list
     organisations and the probe carries on. Near the end, `organisation <id>: confirmed, the test
     contact read sent with it succeeded.` If it says `NOT confirmed`, check the organisation id and
     the test contact id, and run the probe again.
   - `departments: refused (error=SCOPE_MISMATCH)`, then each department given is confirmed through
     one ticket in it (`GET /api/v1/tickets` with the department's id, which needs only
     `Desk.tickets.READ`): `test department <id>: <name> (confirmed through a ticket in it)`. Only
     the department's id and name are kept, never the ticket. They go to
     `docs/api-shapes/zoho-departments.json`, which `test_ticket.py` checks the typed name against.
     The test department already holds the manual ticket #120125. A department with no ticket says
     `NOT CONFIRMED`, and `test_ticket.py` refuses it: create one ticket in that department by hand
     in Desk, then run the probe again.
   - `... layouts: refused (error=SCOPE_MISMATCH)`, once with the note that the layout id is
     optional. Leave `EMOTORAD_ZOHO_LAYOUT_ID` out: the first ticket is sent without one.
   - `channels: refused (error=SCOPE_MISMATCH)` and `channels: none listed`.

   Any other refusal of the organisations read (`FORBIDDEN`, `OAUTH_ORG_MISMATCH` and the rest)
   still stops the probe.
2. **The test ticket.** Run `python scripts/zoho/test_ticket.py` with the ids the probe confirmed,
   then close the ticket in Desk.
3. **The collection.** Run `python scripts/mongo_setup.py` against the environment's
   database, and check that `tickets` lists the `source_key` index. Without it the service
   refuses Zoho and `/health` says `misconfigured: tickets index missing`.
4. **The settings.** Add the six fields needed with the token: the three secrets, all the OMS's
   (`EMOTORAD_ZOHO_REFRESH_TOKEN`, `EMOTORAD_ZOHO_CLIENT_ID`, `EMOTORAD_ZOHO_CLIENT_SECRET`),
   `EMOTORAD_ZOHO_ORG_ID`, `EMOTORAD_ZOHO_TEST_DEPARTMENT_ID` and `EMOTORAD_ZOHO_TEST_CONTACT_ID`.
   Add `EMOTORAD_ZOHO_LAYOUT_ID` too when the probe shows more than one active layout and the
   support lead names the one to use. The secret is replaced whole, so start from its current
   value. Write it to a file, never to the screen:

   ```bash
   umask 077
   aws secretsmanager get-secret-value --secret-id /emotorad/stage/ai/app \
     --query SecretString --output text > ~/app-config.json
   # Add the Zoho fields to ~/app-config.json in an editor. Keep every existing field.
   aws secretsmanager put-secret-value --secret-id /emotorad/stage/ai/app \
     --secret-string file://$HOME/app-config.json
   rm -P ~/app-config.json        # macOS
   # shred -u ~/app-config.json   # Linux, instead of the line above
   ```

   Then check the names only, with the command in section 2.
5. **Deploy** (section 3). `curl -s https://ai-release-stage.emotorad.com/health` should show
   `"zoho":"test department"`. Anything else says what is wrong (see the table below).
6. **The alarms,** once per environment:

   ```bash
   aws cloudformation deploy --profile emotorad-staging --region ap-south-1 \
     --stack-name emotorad-ai-stage-zoho-alarms --template-file infra/zoho-alarms.yaml \
     --parameter-overrides LogGroupName=emotorad-ai-stage AlarmEmail=<the person who receives them>
   ```

   AWS emails that address to confirm the subscription. No alarm reaches it until they confirm.
   The stack emails on nine events, each named in the alarm:

   | Event | What it means |
   | --- | --- |
   | `zoho_misconfigured` | Zoho is switched on but a start-up check failed: the mock is used. `/health` says why |
   | `zoho_token_refused` | Zoho refused the refresh token: nothing reaches Desk until it is fixed |
   | `zoho_worker_error` | The worker hit an unexpected error in a pass. It carries on; the line names the error class |
   | `zoho_worker_store_unavailable` | The worker could not reach MongoDB Atlas in each of three consecutive five-minute periods: no ticket reaches Desk until it can. A single blip is logged and not emailed |
   | `zoho_ticket_stuck` | A ticket has waited 24 hours: run `tickets_report.py` and tell the support lead |
   | `safety_ticket_late` | A safety ticket has waited 10 minutes: ask the support lead to call the customer now |
   | `safety_ticket_not_recorded` | A safety report has no ticket: read the conversation and reach the customer |
   | `unverified_ticket_capped` | The daily cap on unverified tickets refused one: check for abuse, or whether the cap is too low |
   | `zoho_webhook_store_unavailable` | One closure is lost. Support closed a ticket in Zoho Desk and MongoDB could not record it, and Zoho documents no retry, so it will not come again: the rider still sees the ticket as open. Check Atlas, then find the ticket and close the chat's record by hand (section 8, "A lost closure"). The line's `ticket_hash` is the first 12 hex characters of the SHA-256 of Zoho's ticket id: for an id you already have, `python3 -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:12])" <Zoho ticket id>` prints the hash to match against |
7. **Live** (person step 11 only, after Sachin's sign-off, in an environment with real phone
   verification): add `EMOTORAD_ZOHO_DEPARTMENT_ID`, `EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID` and
   `EMOTORAD_ZOHO_LIVE=yes` by step 4, with the OMS's refresh token as on staging. That makes a
   third refresher on it, about three refreshes an hour, still inside Zoho's limits above.

**For a future client of our own.** Not part of the setup while the chatbot shares the OMS's token.
If the chatbot is ever given a Zoho client of its own, its refresh token comes from these two
scripts, and then goes into the config store by step 4:

1. **Consent.** Run `python scripts/zoho/consent_url.py --redirect-uri <the address registered on
   the client>` and open the address it prints while signed in to Zoho as the granting user.
   Approve, and copy the code from the address bar (the page itself may show an error).
2. **Exchange.** Within two minutes, run `python scripts/zoho/exchange_code.py --redirect-uri <the
   same address>`. It refuses unless Zoho's answer names the India data centre, and shows the
   refresh token once.

Only a token made this way may ever be revoked, with `python scripts/zoho/revoke.py --own-client`.

**Erasing someone's ticket records, by hand until spec section 11 ships.** `erasure_admin` and
`scripts/delete_person.py` neither list nor remove `tickets` records (spec 2026-10-05, the
5 October decision to defer section 11). So a person erasing someone also removes their ticket
records by hand, in `mongosh` against the environment's database, from a terminal outside the
Claude app. A Claude session never runs these commands: they read and delete customer records.
Find the records by each of the person's conversation ids (`erasure_admin show` lists them) and
by their phone, matched on its last ten digits, whatever form it was stored in:

```javascript
use emotorad_ai
db.tickets.find({conversation_id: "<conversation id>"}, {_id: 1, state: 1, lease_until: 1, "zoho.ticket_number": 1})
db.tickets.find({phone: {$regex: "<last ten digits>$"}}, {_id: 1, conversation_id: 1, state: 1, lease_until: 1, "zoho.ticket_number": 1})
```

Then delete each one by its `_id`, once its `lease_until` is empty or past (the worker holds a
record five minutes at a time):

```javascript
db.tickets.deleteOne({_id: "<reference, for example EM-1000001>"})
```

The worker drops a record deleted under it (`zoho_record_dropped`) and sends nothing more. A
ticket already in Desk (`zoho.ticket_number`) keeps its copy of the chat and the photos: what
happens to it is section 11's open decision, so tell Sachin which ones.

**To stop sending.** First run `python scripts/zoho/tickets_report.py` and give the support lead
the list: those customers were told someone would be in touch. Then remove
`EMOTORAD_ZOHO_REFRESH_TOKEN` by step 4 and redeploy. That is the whole rollback: the OMS keeps its
token. Never revoke it. `python scripts/zoho/revoke.py` refuses without `--own-client`, and Zoho's
Connected Apps page is never used for it either: revoking the shared token stops the OMS's
ticketing and AFS dispatch.

| `/health` `zoho` | Meaning |
| --- | --- |
| `not configured` | No refresh token: the mock, as before |
| `test department` | Sending to the test department |
| `live` | Sending to the real department |
| `misconfigured: <reason>` | A start-up check failed: the mock is used and nothing is recorded |
| `not allowed in this region` | The region begins with `eu-`: the mock is used. The guard reads `AWS_REGION`, then `AWS_DEFAULT_REGION` when the first is unset |
| `token refused: <error>` | Zoho refused the refresh token, for example after the OMS's client secret was rotated and not copied here the same day |
| `sending failing: <code>` | Zoho refused the calls themselves, for example a missing scope |

## 8. The Amiigo support chat: tokens, the socket and ticket closure

Contract: `docs/contracts/amiigo-support-chat.md`. Two fields of the config store switch it on. A person sets both, in a terminal outside the Claude app, and clears the scrollback afterwards. A Claude session never sets either, and neither value goes into a repo, a commit, a chat or a log. This section never states a value.

**`EMOTORAD_AMIIGO_PUBLIC_KEY`.**

- What it is: Amiigo's PASETO v4 public key, 64 hexadecimal characters. Our server uses it to check the access token the app sends and to read the rider's phone number from it. It cannot make a token.
- Who sets it: the owner of the Amiigo backend (Sachin) gives it to the person who sets the config store.
- It is the public key only. Amiigo's secret signing key is never copied anywhere: not into the config store, a file, a chat, a ticket or a test.
- With it, `/health` shows `"amiigo_tokens":"on"`. Without it, or when it is not 64 hex characters, `/health` shows `"amiigo_tokens":"not configured"`, `amiigo_tokens_not_configured` is logged once, and the app's calls answer 503 until it is set.
- The first time, try it with a real staging token: Amiigo's token format was read from its code and checked with tokens made for our tests.

**`EMOTORAD_ZOHO_WEBHOOK_SECRET`.**

- What it is: the secret in the address Zoho Desk calls when support closes a ticket, `https://ai-release-stage.emotorad.com/webhooks/zoho/tickets/<secret>`. Zoho Desk webhooks cannot send a header of ours, so the secret is the last part of the path. It is 32 to 256 of `A-Z a-z 0-9 - _`.
- Who sets it: a person with write access to the config store, who makes it with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` and enters it in two places that must match: the config store, and the webhook's address in Zoho Desk.
- With a usable value, `/health` shows `"zoho_webhook":"on"`. Without one, the webhook answers 503 `not_configured` and `/health` shows `"zoho_webhook":"not configured"`. With a value of the wrong shape, `/health` shows `misconfigured: secret must be 32 to 256 letters, digits, - or _`, and the log says `zoho_webhook_misconfigured`. Tickets are sent to Zoho Desk either way.
- To rotate it: make a new one, put it in the config store (section 5) and redeploy, and change the webhook's address in Zoho Desk to the new secret, all in the same hour. While the two differ, every closure is a 401 (`zoho_webhook outcome=secret_invalid`), and Zoho documents no retry, so a closure in that gap is lost. Afterwards, ask the support lead which tickets were closed in the gap and treat each as in "A lost closure" below.
- A mismatch at any other time does the same. A run of `secret_invalid` lines right after a rotation is the old secret still in Zoho Desk.

**Setting up the webhook, in this order.** Zoho Desk validates the address when the webhook is created, so the order matters.

1. **nginx first.** Set `access_log off;` for the `/webhooks/zoho/` location on the host, before the webhook exists in Zoho Desk. The secret is in the path, and nginx writes the path to its access log. The nginx config lives on the host, not in this repo, so a person with access to the host changes it and reloads nginx. Our API's own access log already writes the path as `/webhooks/zoho/tickets/[secret]`.
2. **The secret.** Set `EMOTORAD_ZOHO_WEBHOOK_SECRET` as above, and redeploy (section 3).
3. **The collections and indexes.** Rerun `python scripts/mongo_setup.py` against the environment's database, then restart the container (`sudo docker restart emotorad-ai`): the receipts check for their indexes only when the container starts. Check that the output lists:
   - `conversation_notices` as `permanent`, with `conversation_at` and `one_notice_per_ticket` (and not `one_notice_per_text`, which an earlier build made and the script now removes);
   - `amiigo_receipts` as `expires`, with `expires_at_ttl (TTL 0s)`, `one_processing_per_conversation`, `user_key` and `conversation_at`;
   - `tickets` with `zoho_ticket`.

   Without `zoho_ticket`, each closure scans `tickets`, which is small. Without `one_notice_per_ticket`, two servers racing could write two notices; staging runs one. Without the receipts indexes, see "If the indexes are missing" below.
4. **Check `/health`.** `curl -s https://ai-release-stage.emotorad.com/health` shows `"amiigo_tokens":"on"`, `"zoho_webhook":"on"` and `"amiigo_receipts":"mongodb"`.
5. **Create the webhook in Zoho Desk,** with the secret already in its address. Zoho's documentation says it checks the address with a GET, and with a POST when the GET is not answered 200. Ours answers both 200 behind the secret (the GET is logged as `zoho_webhook outcome=validated`; the POST, whatever its body). Before the secret is set and deployed, both are refused (503), and Zoho Desk refuses to create the webhook: set the secret and redeploy first (step 2).
6. **Subscribe to `Ticket_Update`** with `departmentIds` (the test department on staging) and `fields: ["status"]`, plus the criterion that the subject contains `[AI chat]` (never "starts with": see "What Zoho rules filter on" in section 7). Without these filters every ticket update in the organisation reaches us. Each is answered quickly and logged, as one line.
7. **The live check.** Close a ticket in the test department that the chatbot raised from a test chat in the app, with a socket open for that rider. The log shows `zoho_webhook outcome=closed`, and the socket gets a `ticket_update`. (A ticket made by hand in Desk, or by `scripts/zoho/test_ticket.py`, has no record of ours, so its outcome is `not_ours`.)
8. **Replace the fixture.** `docs/api-shapes/zoho-webhook-ticket-update.json` comes from Zoho's documentation, not from a live call. After step 7 a person captures one real delivery, masks it, and replaces the file as its `_source` asks. This never comes from a Claude session: a capture holds a real ticket.

Two more things to check before the app team connects. First, the alarm stack (section 7, step 6) must be redeployed with this release's `infra/zoho-alarms.yaml`: the `zoho_webhook_store_unavailable` alarm exists only once it is. Second, nginx must pass WebSocket upgrades to `/amiigo/v1/chat` (the playground's Streamlit needs the same). The nginx config is not in this repo, so connect once with a staging token and see `ready`.

**A lost closure.** The `zoho_webhook_store_unavailable` alarm means one closure was lost: Zoho documents no retry, so it will not come again, and the rider still sees the ticket as open. There is no tool for this yet, so this is the step:

1. Check that Atlas is reachable again.
2. Find the ticket in Zoho Desk. The log line carries `ticket_hash`, the first 12 hex characters of the SHA-256 of Zoho's ticket id. For each ticket closed around the alarm's time, print its hash with the command in the alarm table in section 7, and match it. Zoho's ticket id is the long number (the sample in `docs/api-shapes/zoho-webhook-ticket-update.json` has `31138000011967402`), not the short ticket number.
3. Close the chat's record by hand, in `mongosh` against the environment's database, from a terminal outside the Claude app. A Claude session never runs these commands: they change customer records.

   ```javascript
   use emotorad_ai
   db.tickets.find({"zoho.ticket_id": "<Zoho ticket id>"}, {_id: 1, conversation_id: 1, support_status: 1, closed_at: 1})
   db.tickets.updateOne({"zoho.ticket_id": "<Zoho ticket id>", support_status: {$ne: "closed"}}, {$set: {support_status: "closed", closed_at: "<the closing time, ISO 8601 in UTC, for example 2026-10-07T11:02:10+00:00>"}})
   ```

   That sets the same two fields a closure sets. The rider's history then shows the ticket as closed, with no notice message and no `ticket_update`. If the notice matters, ask the support lead to reopen the ticket in Desk and close it again: the event arrives again, and the closure finishes whatever is missing (the record, the notice and the push).

**Things to know.**

- **Noise.** The webhook's address is public, so a caller without the secret can fill the log with `zoho_webhook outcome=secret_invalid` lines. This is not alarmed, by design.
- **Erasure.** `delete_person` and `delete_conversation` remove a person's `conversation_notices` and `amiigo_receipts`, and `erasure_admin delete` refuses when a notice arrived after `show`. But a closure that lands while a delete is running can leave a notice behind. After a delete, check `conversation_notices` for that person's chats, in `mongosh` from a terminal outside the Claude app, and remove what is left:

  ```javascript
  use emotorad_ai
  db.conversation_notices.find({conversation_id: "<conversation id>"})
  db.conversation_notices.deleteMany({conversation_id: "<conversation id>"})
  ```
- **What the receipts keep.** `amiigo_receipts` holds one document per message a rider sent on the chat socket, for 24 hours, so a message sent again is answered once. Its `_id` names the rider's user key, which contains their phone number, and it keeps the bot's reply as sent, unmasked, for those 24 hours. It is erased with the person.
- **If the indexes are missing.** Until `mongo_setup.py` has made the two receipts indexes and the container has restarted, receipts stay in the API process's memory. A restart then forgets which messages were answered, `amiigo_receipts_ttl_missing` is logged at error level (`reason` is `indexes_missing`, or `index_unreadable` when the indexes could not be read), and `/health` shows `"amiigo_receipts":"memory: indexes missing, run scripts/mongo_setup.py"` (or `"memory: indexes could not be checked"`) instead of `"mongodb"`. Nothing is alarmed on it, so check `/health` after a deploy. With MongoDB conversations but receipts in memory, `erasure_admin` cannot reach the API process's receipts, because it runs in its own process. They expire within 24 hours.
- **A deploy mid-turn.** A turn still running when the container stops (a deploy or a restart) never finishes, and its receipt stays `processing` until its 10-minute lease runs out. So that one chat answers `conversation_busy` for up to 10 minutes after the deploy; the rider's other chats are not held. The app sees `1012` when the server closes its sockets on the way down, or `1006` when the container is killed without closing them, and reconnects either way. Deploy when the chat is quiet if you can.
- **One container.** A `ticket_update` is pushed only to sockets on the container that took Zoho's call. Staging runs one. More than one needs a shared channel between them first.

## 9. Bikes and warranty from OMS production, and invoice OCR

Spec: `docs/superpowers/specs/2026-10-08-oms-warranty-source-design.md`. Off until
`EMOTORAD_OMS_PG_DSN` is set; invoice reading is off until both that and
`EMOTORAD_INVOICE_OCR=on` (set by `deploy-staging.yml`) with the OpenRouter key.

1. **The read-only role (Sachin).** On OMS production (`emotorad`), a role that can log in,
   with column grants only:
   ```sql
   GRANT SELECT (id, mobile, frame_number, product_name, product_id, product_color, purchase_date,
                 created_at, updated_at, deleted_at, franchise_id, franchise_name, invoice_image, status,
                 customer_name, full_address) ON em_purchase TO <role>;
   GRANT SELECT (id, mobile, secondary_contact, deleted_at) ON em_franchise TO <role>;
   GRANT SELECT (mobile, user_type, related_id, deleted_at) ON em_users TO <role>;
   ```
   `full_address` is there because the replacement flow reads the address back before an order.
   The nearest-dealers tool (spec 2026-10-09) reads the dealer stores with the same connection setting,
   so the role also needs:
   ```sql
   GRANT SELECT (id, customer_name, address, address2, pin_code_id, district_id, state_id, franchise_type_id,
                 poc_name, mobile, is_active, deleted_at, is_distributor) ON em_franchise TO <role>;
   GRANT SELECT (id, franchise_type_name) ON em_franchise_type TO <role>;
   GRANT SELECT (id, pin_code) ON em_pin_code TO <role>;
   GRANT SELECT (id, district_name) ON em_district TO <role>;
   GRANT SELECT (id, state_name) ON em_state TO <role>;
   GRANT SELECT (full_name, mobile, user_type, related_id, is_active, deleted_at, updated_at) ON em_users TO <role>;
   ```
   `/health` shows `"dealer_stores":"oms_db"` once the setting is in; without it `"not configured"`, and the
   tool is not offered (offline mode alone uses three made-up stores, `"fixtures"`).
   Before real customers: Sachin's yes to giving riders the dealer managers' mobile numbers.
2. **The network path (Sachin).** From the staging EC2 instance to the OMS database on port
   5432 (its security group).
3. **The config store (a person, in AWS CloudShell, region `ap-south-1`).** Add
   `EMOTORAD_OMS_PG_DSN` (the role's connection string) and `EMOTORAD_OMS_API_KEY`; remove
   `EMOTORAD_WARRANTY_API_KEY` and `EMOTORAD_AMIGO_PG_DSN`. This keeps every other key, and
   asks for each value without echoing it, so nothing is pasted into a command or a chat:
   ```bash
   python3 - <<'EOF'
   import getpass, json, boto3
   sm = boto3.client("secretsmanager", region_name="ap-south-1")
   sid = "/emotorad/stage/ai/app"
   cfg = json.loads(sm.get_secret_value(SecretId=sid)["SecretString"])
   for name in ("EMOTORAD_OMS_PG_DSN", "EMOTORAD_OMS_API_KEY"):
       value = getpass.getpass("Paste %s (Enter to keep the current one): " % name).strip()
       if value:
           cfg[name] = value
   for name in ("EMOTORAD_WARRANTY_API_KEY", "EMOTORAD_AMIGO_PG_DSN"):
       cfg.pop(name, None)
   sm.put_secret_value(SecretId=sid, SecretString=json.dumps(cfg))
   print("Keys now:", sorted(cfg))
   EOF
   ```
4. **Deploy** `feat/zoho-desk-tickets` (section 3). `/health` shows
   `"warranty_source":"oms_db"` and `"invoice_ocr":"openrouter"`.
5. **Check.** In a test chat, verify with a test number that has an OMS registration; its
   bikes should be listed. A bike with no purchase date and an invoice on file should get the
   invoice line within a turn or two, and a `warranty_proof` ticket in the test department.

Rollback: remove `EMOTORAD_OMS_PG_DSN` from the secret (the same script, with the name
added to the removal list) and redeploy; the warranty service takes over if its key is back,
otherwise the OMS API or the fixtures.

A slow lookup: `em_purchase` has no index on `mobile`, so each lookup scans the table
(about 62,000 rows, planned at cost ~6,000). An index through a reviewed em-biz-backend
migration fixes it.
