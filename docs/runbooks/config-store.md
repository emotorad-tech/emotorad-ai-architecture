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
   The stack emails on eight events, each named in the alarm:

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
