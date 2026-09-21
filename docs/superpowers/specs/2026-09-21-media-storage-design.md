# Media storage on S3 — design

**Date:** 2026-09-21
**Status:** approved design, awaiting implementation plan
**Scope:** every image and video the platform stores or serves lives in one private S3
bucket per environment. Customers and staff upload through the same presigned-URL flow.
Reads are short-lived presigned GETs. Guide media leaves Cloudinary. The key layout is
fixed by code, not by whoever uploads.

Depends on: `2026-09-21-app-config-store-design.md` (instance role, `boto3`, startup loader).
That spec lands first.

## 1. Why

- The only upload path today is the playground, storing base64 blobs on the local disk of
  whichever machine ran it (`playground.py`, `BLOB_DIR`). The public `/message` endpoint
  accepts text only. A customer on the web chat has no way to send a photo, which is the
  integration point the front-end prototype marks (`web/emotorad-support-chat-handoff.md`,
  item 2).
- Guide photos and clips are on Cloudinary. The decision on 2026-09-21 is to keep
  everything on AWS.
- Assets need a structure that survives more programmes than after-sales battery support:
  motor, pre-sales, dealer, and whatever comes next.

## 2. Bucket

One bucket per environment: `emotorad-ai-<env>-media`, `ap-south-1`. Block all public
access. Versioning on. SSE-S3. Two lifecycle rules:

| Prefix | Rule |
|---|---|
| `customers/` | expire current version after 180 days, non-current after 30 |
| `assets/` | none; versioning is the undo |

CORS allows `PUT` and `GET` from the web chat's origin(s) only, with `Content-Type` and
`Content-Length` as allowed headers, because the browser PUTs to S3 directly.

## 3. Key layout

Two trees with different owners, readers and lifecycles. Every key is derived by
`src/emotorad_ai/storage/keys.py`; the client never supplies a key.

### 3.1 `assets/` — authored by us, shown to customers on demand

```
assets/<programme>/<category>/<kind>/<slug>.<ext>
assets/<programme>/<category>/<kind>/<slug>.w900.webp      # derivative, images only
assets/<programme>/<category>/<kind>/<slug>.poster.jpg     # derivative, videos only

assets/afs/battery/photos/soc-button.jpg
assets/afs/battery/videos/key-turn.mp4
assets/afs/battery/tips/charging-in-monsoon.jpg
assets/afs/motor/photos/pas-sensor.jpg
assets/presales/emx-plus/photos/front-view.jpg
```

- `programme` ∈ `{afs, presales, dealer}`; `kind` ∈ `{photos, videos, tips, docs}`.
  Closed vocabularies in `keys.py`. `category` is free-form snake-case (`battery`,
  `motor`, `emx-plus`) because it grows with the catalogue. Adding a programme or kind is
  a code change with a test, the same discipline as the tool allowlists.
- `slug` is `[a-z0-9-]+`. Extension is derived from the MIME type, never from the file
  name.
- Knowledge records keep `id:` as `media.py` already requires. The id **is** the key
  without the `assets/` prefix, extension included: `id: afs/battery/photos/soc-button.jpg`.
  An id is an S3 asset only when its first segment is a programme (`afs`, `presales`,
  `dealer`) and it ends in a known extension; anything else is a Cloudinary public id.
  One line in a YAML record, resolved in one place.

### 3.2 `customers/` — uploaded by customers, private evidence

```
customers/<cluster_id>/<conversation_id>/<kind>/<upload_id>.<ext>

customers/clu_8f3a12/conv_01J9K3/images/upl_01J9K4ab.jpg
customers/clu_8f3a12/conv_01J9K3/videos/upl_01J9K5cd.mp4
```

- `cluster_id` is the identity-graph cluster, **never the phone number**. A phone in a key
  is PII in every access log. `kind` ∈ `{images, videos, docs}`.
- `upload_id` is a ULID minted by the API, so keys sort by time within a conversation.

## 4. Upload flow

The standard three-step presigned pattern. Same endpoint for customers and staff; the
tree decides the rules.

1. **Presign.** `POST /uploads` with
   `{conversation_id, tree: "customers" | "assets", kind, mime_type, size_bytes,
   path?: {programme, category, slug}}`. The API:
   - for `customers`: checks the conversation belongs to the caller's session (the same
     `session_token` `/message` uses) and takes `cluster_id` from the resolved identity;
   - for `assets`: requires the playground basic auth and a full `path`;
   - checks the MIME allowlist (`image/jpeg`, `image/png`, `image/webp`, `video/mp4`,
     `application/pdf`) and the size cap (images 10 MB, videos 100 MB, docs 10 MB);
   - derives the key, mints `upload_id`, and returns
     `{upload_id, key, url, headers: {"Content-Type": ...}, expires_in: 300}`.
   The presigned PUT pins `Content-Type` and `Content-Length` into the signature, so a
   client cannot swap in a different file after presign.
2. **PUT.** The client sends the bytes straight to S3. The API never proxies bytes.
3. **Attach.** The client sends `/message` with `attachments: [{upload_id}]`. The API
   looks the id up, `HeadObject`s the key, rejects a missing object or a mismatched type
   or size with a named error, and hands the runtime an `Attachment(kind, url=s3://...)`.

Presigned-but-never-completed uploads are ids nobody references; the lifecycle rule
handles the stragglers. There is no cleanup job to build.

Staff bulk loading: `scripts/upload_asset.py <file> --programme afs --category battery
--kind photos --slug soc-button` calls the same endpoint, makes the derivatives (§6) and
prints the record `id:` line to paste into YAML.

## 5. Reading back

- **To the model.** Claude takes base64 or a URL. The runtime fetches customer evidence
  through the instance role and sends base64 blocks, exactly as the playground does today,
  so no URL of any kind reaches the model. Video keeps the frame-sampling and
  transcription path, reading the object into a temp file instead of a local blob.
- **To the customer.** `media.resolve()` returns a presigned GET valid 15 minutes for
  `assets/` items, and for a customer's own evidence when the transcript echoes it. The
  web chat gets `GET /media/{key}` which authorises (assets: anyone with a conversation;
  customers: only the owning session) and 302s to a fresh presigned URL, so a transcript
  rendered an hour later still loads.
- **Cloudinary compatibility.** `media.resolve()` keeps its absolute-URL branch, so
  records already authored with Cloudinary URLs keep working until migrated. The
  `EMOTORAD_CLOUDINARY_CLOUD` path is removed once the migration script has run.

## 6. Derivatives, without a CDN

Cloudinary resized and format-negotiated on every request. S3 does not. Derivatives are
made **once, at upload time**, in `scripts/upload_asset.py` and in the `assets` branch of
the upload endpoint:

- Images: the original, plus `<slug>.w900.webp` (Pillow, `c_limit` semantics: shrink to
  900px wide, never enlarge). `media.resolve()` returns the WebP for chat clients and the
  original as `fallback`.
- Videos: untransformed original (the Cloudinary lesson: a transformed video is a dead
  player), plus `<slug>.poster.jpg` from the first frame using the `imageio-ffmpeg` binary
  already in `requirements.txt`.

CloudFront with origin access control is the upgrade when volume or latency justifies it;
one row in the risk register, no code shaped around it now.

## 7. Modules

| Module | Responsibility |
|---|---|
| `storage/keys.py` | vocabularies, key derivation, validation. Pure, no AWS |
| `storage/s3.py` | thin client: `presign_put`, `presign_get`, `head`, `get_bytes`. `boto3` only here |
| `storage/uploads.py` | the presign → attach state (`upload_id → key, expected type, size`), in-memory now, same shape as `ConversationStore` |
| `api.py` | `POST /uploads`, `GET /media/{key}`, `attachments` on `MessageIn` |
| `media.py` | `resolve()` learns `assets/` ids and presigned GETs; keeps absolute URLs |
| `agents/base.py` | inbound `s3://` attachments become base64 blocks via `storage/s3.get_bytes` |
| `playground.py` | uploads go to S3 under `customers/playground/<chat_id>/...` on staging; local disk stays the default when no bucket is configured |
| `scripts/upload_asset.py`, `scripts/migrate_cloudinary.py` | staff loading and the one-time move |

Configuration: `EMOTORAD_AI_MEDIA_BUCKET` (unset → uploads disabled, `/uploads` returns
503 with a reason; the playground keeps local blobs). Presign expiries are constants in
`storage/s3.py`.

## 8. Infrastructure

`infra/media.yaml` (CloudFormation): the bucket with public-access block, versioning,
SSE-S3, the two lifecycle rules, CORS with the origin as a parameter; a managed policy
with `s3:PutObject`, `s3:GetObject`, `s3:HeadObject` on `arn:aws:s3:::<bucket>/*` and
`s3:ListBucket` on the bucket, attached to the instance role `emotorad-ai-stage-ec2-role`
(parameter). Applied with `aws cloudformation deploy` from the CLI (`emotorad-staging`
profile) for staging; prod is a re-run with the environment parameter. Runbook in
`docs/runbooks/media.md`.

Decided 2026-09-21: a **new** bucket, not the existing `emotorad-ai-stage-851725486214`,
which holds deploy tarballs and has no versioning, lifecycle or CORS. Customer evidence is
personal data and gets its own bucket, rules and future prod split. The one orphan object
under `playground-uploads/` in the old bucket (2026-08-25, referenced by no code on `main`)
is deleted in the runbook.

## 9. Testing

All with `botocore.stub.Stubber` and a fixed clock; no test touches AWS.

- `keys.py`: every vocabulary, slug and extension rule, phone-in-key rejection.
- `s3.py`: presign parameters (method, content type, length, expiry), `head` mapping.
- `uploads.py`: presign → attach round trip; attach with wrong size or type is rejected;
  an unknown `upload_id` is rejected; expiry.
- `api.py`: `/uploads` per tree, auth on both, size and MIME rejection, `/media` redirect
  and ownership check, `/message` with an attachment reaching the runtime.
- `media.resolve()`: `assets/` id → presigned URL and WebP derivative; absolute URL
  untouched; bucket unset → `unresolved` with a reason (existing behaviour kept).
- `agents/base.py`: an `s3://` attachment becomes a base64 image block.

## 10. Out of scope

- WhatsApp media webhooks. The `customers/` layout accommodates them; the adapter is a
  follow-up.
- CloudFront, image transforms on request, and virus scanning of uploads.
- Deleting a customer's evidence on request (a runbook line: delete the
  `customers/<cluster_id>/` prefix), not a feature.
- The custom-bot-builder PR.
