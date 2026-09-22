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
cross to Google's API for that one request, and a clip over 20 MB that goes through
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
loads. `session_token` (or `em_aid`) on `/media` is the access check — it must resolve to
the same cluster the upload was made under, or the read 403s.

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

```bash
aws s3 rm --recursive s3://emotorad-ai-stage-media/customers/<cluster_id>/
```

Run this with a person's own admin credentials, not the instance role — the role's
policy only grants `PutObject`/`GetObject`/`ListBucket` (see `MediaAccessPolicy` in
`infra/media.yaml`), on purpose, so a compromised instance cannot delete evidence.

The 180-day lifecycle rule (`customer-evidence-180d`) handles the routine case; this is
for an explicit deletion request ahead of that.

## 7. Housekeeping

The old `playground-uploads/` object in the deploy bucket (`S3_BUCKET`, pre-dating this
media bucket, referenced by no code on `master`) can be deleted.
