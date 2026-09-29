# Runbook: media storage

One private, versioned S3 bucket per environment, `emotorad-ai-<env>-media`, holding two
trees: `assets/` (guide photos and clips we author) and `customers/` (evidence a customer
sends in chat). Every key is derived by `src/emotorad_ai/storage/keys.py` — no client ever
supplies one. Design: `docs/superpowers/specs/2026-09-21-media-storage-design.md`.

Every command below uses `--profile emotorad-staging --region ap-south-1`. Set them once:

```bash
export AWS_PROFILE=emotorad-staging AWS_REGION=ap-south-1
```

## 1. Create the bucket

```bash
aws cloudformation deploy --profile emotorad-staging --region ap-south-1 \
  --stack-name emotorad-ai-stage-media --template-file infra/media.yaml \
  --parameter-overrides Environment=stage InstanceRoleName=emotorad-ai-stage-ec2-role \
    "AllowedOrigins=https://ai-release-stage.emotorad.com,http://localhost:8000" \
  --capabilities CAPABILITY_NAMED_IAM
```

For prod: `Environment=prod`, the prod instance role name, and the prod origins in
`AllowedOrigins` — stack `emotorad-ai-prod-media`.

`AllowedOrigins` defaults to the deployed origin only. For local testing against the
real bucket, pass `http://localhost:8000` (or wherever the local server runs) explicitly
in `--parameter-overrides`, as the command above does — it is not part of the default,
so a deploy that omits it never opens the bucket to localhost.

## 2. Configure the service

The workflow's `docker run` line sets `EMOTORAD_AI_MEDIA_BUCKET=emotorad-ai-stage-media`.
Not a secret — it is the bucket name, same treatment as `S3_BUCKET`/`INSTANCE_ID`. Without
it, `store_from_env()` returns `None` and `/uploads` and `/media` answer 503; the rest of
the bot still works.

```bash
curl -s https://ai-release-stage.emotorad.com/health
```

Expected: `"media":"configured"`. `"media":"not configured"` means the bucket env var is
unset or empty on the running container.

With `API_KEY_GEMINI` set in the config store, a customer's uploaded video is described
once at ingest by Gemini (`gemini-3.8-flash`, 90 s deadline) and Claude reads that text
instead of sampled frames; `/health` shows `"video_summary":"gemini"`. The clip's bytes
cross to Google's API for that one request, and a clip over 14 MiB that goes through
Google's Files API is deleted there as soon as the description is back, so this is a second
vendor boundary beside Anthropic (decision recorded in
`docs/superpowers/specs/2026-09-22-video-evidence-gemini-design.md` §4).

Without the key, or when Gemini fails or times out, the frames path runs.
`EMOTORAD_AI_TRANSCRIBE_VIDEO=1` turns on speech-to-text for that path; it is off by
default because Whisper downloads a model on first use and has no deadline.

## 3. Upload a guide asset

```bash
.venv/bin/python3 scripts/upload_asset.py soc.png \
  --programme afs --category battery --kind photos --slug soc-button
```

Prints the id and its derivatives:

```
id: afs/battery/photos/soc-button.png
  w900 -> s3://emotorad-ai-stage-media/assets/afs/battery/photos/soc-button.w900.webp
```

Paste the `id:` value into the knowledge record's `media:` list and commit:

```yaml
media:
  - id: afs/battery/photos/soc-button.png
    caption: SOC button, held for three seconds
```

## 4. The customer upload flow

A curl walkthrough of what the web chat widget does: presign, PUT the bytes straight to
S3, send the message with the upload id, read the delivered media back. `/chat` sends
video through exactly this flow (with a progress bar on the PUT); photos stay inline as a
data URL on the message and never touch this path.

```bash
# 1. presign
curl -s -X POST http://127.0.0.1:8000/uploads -H 'Content-Type: application/json' \
  -d '{"session_token":"sess-ananya","conversation_id":"c1","tree":"customers","mime_type":"image/png","size_bytes":'"$(stat -f%z photo.png)"'}'
# On Linux use `stat -c%s photo.png`.
# A visitor with no session yet sends the website cookie instead; it resolves
# to their anonymous cluster, the same as on /message and /media:
#   -d '{"em_aid":"<cookie>","conversation_id":"c1","tree":"customers","mime_type":"video/mp4","size_bytes":...}'
# 2. PUT the bytes to the returned url with the returned headers
curl -s -X PUT "<url>" -H 'Content-Type: image/png' --data-binary @photo.png
# 3. send the message with the upload id
curl -s -X POST http://127.0.0.1:8000/message -H 'Content-Type: application/json' \
  -d '{"conversation_id":"c1","session_token":"sess-ananya","text":"what is this light","attachments":[{"upload_id":"<upload_id>"}]}'
# 4. read it back
curl -s -o /dev/null -w '%{redirect_url}\n' "http://127.0.0.1:8000/media/<key>?session_token=sess-ananya"
```

Step 1's response carries `upload_id`, `key` and the presign fields for step 2. Step 4
302s to a fresh 15-minute presigned GET each time, so a transcript rendered later still
loads. `session_token` on `/media` is the access check — it must resolve to the same
cluster the upload was made under, or the read 403s. `em_aid` is accepted on `/uploads`
and `/message` only; a cookie does not unlock a read.

## 5. Migrate off Cloudinary

```bash
EMOTORAD_CLOUDINARY_CLOUD=... .venv/bin/python3 scripts/migrate_cloudinary.py          # plan only
```

Prints the Cloudinary id to asset id mapping for every media item under `knowledge/`
whose `id` has no `/` (i.e. not already an asset id). Then:

```bash
EMOTORAD_CLOUDINARY_CLOUD=... .venv/bin/python3 scripts/migrate_cloudinary.py --apply  # upload + rewrite ids
```

Review the YAML diff (only `id:` lines under `media:` change) and commit. Once every
record is migrated and confirmed serving from S3, remove `EMOTORAD_CLOUDINARY_CLOUD` from
`.github/workflows/deploy-staging.yml`.

## 6. Delete a customer's evidence on request

Customer media is kept permanently (decision 2026-09-29): the bucket's lifecycle rule
that used to expire `customers/` objects after 180 days has been removed (it is now
`customer-evidence-old-versions-30d`, which only expires superseded versions, at 30
days). There is no routine expiry any more, so every erasure is an explicit request, run
through `scripts/delete_person.py`, which removes a person's conversation records
(working state, transcript turns, summaries, idempotency receipts) and their media:
the `media` records in MongoDB and the matching S3 objects, every version.

```bash
.venv/bin/python3 scripts/delete_person.py --phone 9876543210                          # dry run: counts only
.venv/bin/python3 scripts/delete_person.py --phone 9876543210 --yes --reason "email from customer, 2026-09-29"
```

The dry run (no `--yes`) lists what would go, including the S3 key of every media
object, and deletes nothing. `--dealer-id` and `--conversation-id` work the same way;
see the script's own docstring.

Run this with a person's own admin credentials, not the instance role — the role's
policy only grants `PutObject`/`GetObject`/`ListBucket` (see `MediaAccessPolicy` in
`infra/media.yaml`), on purpose, so a compromised instance cannot delete evidence.
Deleting media needs `EMOTORAD_AI_MEDIA_BUCKET` set and credentials also allowed
`s3:ListBucketVersions` and `s3:DeleteObjectVersion` on the bucket. If the person had
no media, the script does not need the bucket at all.

If the person sent media but `EMOTORAD_AI_MEDIA_BUCKET` is not set (or the credentials
cannot see the bucket), `--yes` refuses outright: it prints that the objects cannot be
deleted without the bucket and credentials, deletes nothing at all, not even the
database records, and exits 2. Set the bucket, then rerun.

If the S3 deletion fails partway through (a permissions problem, a network blip), the
script stops immediately: no database record is touched, the keys deleted so far and
the keys still remaining are printed, and the audit record is written with
`"incomplete": true` and the counts so far. Deleting a version twice is harmless, so
simply rerun the same command once the problem is fixed; it picks up the same keys.

Redeploying `infra/media.yaml` with the command in §1 applies the lifecycle change to
the existing bucket: CloudFormation updates the bucket's lifecycle configuration in
place, nothing is recreated and nothing already stored is touched.

## 7. Housekeeping

The old `playground-uploads/` object in the deploy bucket (`S3_BUCKET`, pre-dating this
media bucket, referenced by no code on `master`) can be deleted.

## 8. Staging test: a photo from the chat page to S3 and MongoDB

A person's own end to end check that a real photo sent from the chat page lands in the
staging bucket and gets a permanent record in MongoDB, never a presigned URL. Run this
after any change to how photos are stored. Do not run any of the commands below against
anything real from inside a Claude session; this section is for a person, in their own
shell.

1. Deploy the template change, if it has not already gone out. This is the same command
   as section 1 above, nothing new to invent:

   ```powershell
   aws cloudformation deploy --profile emotorad-staging --region ap-south-1 `
     --stack-name emotorad-ai-stage-media --template-file infra/media.yaml `
     --parameter-overrides Environment=stage InstanceRoleName=emotorad-ai-stage-ec2-role `
       "AllowedOrigins=https://ai-release-stage.emotorad.com,http://localhost:8000" `
     --capabilities CAPABILITY_NAMED_IAM
   ```

   You should see CloudFormation report `UPDATE_COMPLETE`, or say there is nothing to
   update if it is already current.

2. Make sure the `media` collection and its index exist. This is safe to rerun even if
   they already do.

   ```powershell
   python scripts/mongo_setup.py
   ```

   You should see `media` listed among the collections and indexes it prints.

3. Set this shell's environment. PowerShell does not use the cmd.exe `set VAR=...`
   form; use `$env:VAR = "..."` instead. Use your own named AWS profile, with
   permission to put and get objects in the staging media bucket, and never paste a
   real key or connection string into a chat message.

   ```powershell
   $env:EMOTORAD_AI_MEDIA_BUCKET = "emotorad-ai-stage-media"
   $env:AWS_REGION = "ap-south-1"
   $env:AWS_PROFILE = "<your named profile>"
   $env:OPENROUTER_API_KEY = "<your key, same as for the combined test>"
   $env:EMOTORAD_MONGO_URI = "<the staging connection string, same as for the combined test>"
   ```

   Nothing prints back; these are only set in this shell, for this session.

4. Start the chat page.

   ```powershell
   python scripts/chat_local.py
   ```

   You should see the status lines include
   `EMOTORAD_AI_MEDIA_BUCKET: set (photo and video storage in S3)`, then a sign-in
   link and a chat link. Open the sign-in link and sign in, then open the chat link,
   attach a photo and send it with a short message.

5. Find the object in the bucket. First get its key with a read-only query in
   `mongosh`, against the newest `media` document:

   ```
   db.media.find().sort({stored_at: -1}).limit(1)
   ```

   Copy the `key` field from the one document it returns, then check the object is
   really in the bucket:

   ```powershell
   aws s3api head-object --bucket emotorad-ai-stage-media --key "<key from the query above>"
   ```

   You should see a JSON response with a `ContentLength` and `ContentType` matching
   the photo you sent.

6. Look at the same document's `uri` field. It should read
   `s3://emotorad-ai-stage-media/<key>`, with no `X-Amz-` anywhere in it and no `?`.
   Either one showing up means a presigned URL leaked into the permanent record,
   which the design forbids.

7. If step 5's query returns nothing, the photo was not stored. Read the failure
   event, from the server's own console output or `logs/conversations.jsonl`:
   - `media_not_stored` with `reason: no_cluster`: nothing identified who sent it, no
     session and no cookie, so there was nowhere to key the object under. Sign in
     first, then send the photo again.
   - `media_not_stored` with `reason: store_failed`: the write to S3 itself failed.
     Check the bucket name and the AWS credentials in this shell.
   - `media_record_failed`: the object reached S3 but the MongoDB write failed.
     Check `EMOTORAD_MONGO_URI` and that Atlas is reachable.

8. Clean up the test conversation and its media. First a dry run, which deletes
   nothing:

   ```powershell
   python scripts/delete_person.py --phone 9876543210
   ```

   You should see a count of what would be deleted, including the S3 key from step 5.
   Then delete it for real, which needs credentials allowed to delete object
   versions, not just put and get:

   ```powershell
   python scripts/delete_person.py --phone 9876543210 --yes --reason "staging media test"
   ```

   You should see the same counts printed again, this time as done, and the object
   gone if you rerun step 5's `head-object` command.
