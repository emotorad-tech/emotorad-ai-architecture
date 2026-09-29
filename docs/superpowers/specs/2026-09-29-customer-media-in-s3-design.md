# Customer photos and videos in S3, with a permanent record in MongoDB

Date: 2026-09-29. Owner: Sagnik. Status: for review.

## What the person asked for

- Image uploads reach the S3 bucket and work.
- The address of every image in the bucket is stored in our database.
- That address is permanent: never a presigned URL that expires.
- Everything is tested end to end.

Earlier decisions this builds on: media is kept permanently (2026-09-29), in the chatbot's own bucket `emotorad-ai-<env>-media` (infra/media.yaml, already deployed on staging; the person deploys template changes).

## Where things stand

- Videos: the page presigns through `POST /uploads`, PUTs to S3, and sends `{upload_id}`. The server claims it and the turn gets `s3://<key>`. Nothing records the object in MongoDB.
- Photos: the page sends them inline as a data URL. They reach the model and are never stored. The transcript says `data:(inline, not kept)`.
- The bucket deletes `customers/` objects after 180 days (lifecycle rule `customer-evidence-180d`), which contradicts "permanent".

## Design

### 1. The server stores inline photos itself

On `POST /message`, after `attachments.validate`, each inline image is written to the bucket by the server (`S3Store.put_bytes`, the instance role already has `s3:PutObject`) under the usual customer key, `customers/<cluster>/<conversation>/images/<id>.<ext>`. The turn then carries `s3://<key>`, as a claimed upload does.

Why the server and not the page: the photo already passes through the server (about 200 KB after the page's 1280 px downscale), the bucket's browser rules (CORS) allow only the staging origin, and every channel that sends inline photos is covered, not just the web page. The page does not change for photos.

With no bucket configured (a laptop), photos stay inline as today and nothing is recorded; `/health` already reports `media: not configured`.

### 2. A permanent media record

A new MongoDB collection `media`, no TTL index. One document per object, written when the object becomes part of a conversation (an inline photo stored, or an upload claimed):

| Field | Example |
|---|---|
| `_id` | the S3 key |
| `bucket` | `emotorad-ai-stage-media` |
| `key` | `customers/cl_ab12/c1/images/upl_9f.jpg` |
| `uri` | `s3://emotorad-ai-stage-media/customers/cl_ab12/c1/images/upl_9f.jpg` |
| `kind`, `mime_type`, `size_bytes` | `image`, `image/jpeg`, `183422` |
| `conversation_id`, `cluster_id` | `c1`, `cl_ab12` |
| `source` | `inline` or `upload` |
| `stored_at` | ISO time |

Never a URL with a query string: the record is built from bucket and key only, and a check refuses any value containing `X-Amz-` or `?`. The same record lives in an in-memory store for `EMOTORAD_STORE=memory`, behind the same methods (`record_media`, `media_of`) on the conversation store, which already owns the other permanent records and erasure.

### 3. Failures

- S3 write fails: the photo still reaches the model inline for this turn, so the customer is not stopped; `media_not_stored` is logged at error level with the reason (never the bytes or the key's customer part). The transcript says the photo was not kept.
- Record write fails after S3 succeeded: `media_record_failed` logged with the key. The object is in the bucket and the transcript has its key.
- No silent path: every branch logs a named event.

### 4. Retention and erasure

- `infra/media.yaml`: the `customers/` expiry rule is removed, so customer media is permanent. Superseded versions still expire after 30 days, and abandoned multipart uploads after 2. The person deploys it.
- `scripts/delete_person.py`: also removes the person's media records and, with `--yes`, the S3 objects with every version. The dry run lists what would go. Run by a person with credentials allowed to delete (the instance role cannot).

### 5. Testing

- Automated, end to end in process: `POST /message` with an inline photo → a fake S3 store receives the bytes under a customer key → the media record in mongomock holds bucket, key and `s3://` URI, and no document anywhere contains `X-Amz-` → the model was shown the photo (evidence) → the ticket is raised → the transcript names the key. The same for a claimed video upload. Failure branches each logged.
- By the person, on staging: steps to run the chat page against the real bucket and Atlas, then check the object with `aws s3api head-object` and the record with a read-only query.

## Out of scope

- WhatsApp and Amiigo media URLs (Meta's expire): not live yet; noted for when they are.
- Serving stored photos back to the page (it shows its local copy).

## Rollback

- Code: revert the commits; photos go back to inline, the `media` collection is left in place (harmless).
- Template: redeploy the previous `infra/media.yaml` to restore the 180-day expiry.
- Data to check: `media` document count against `customers/` object count.
- Tell: Sachin (bucket retention and a new collection).
