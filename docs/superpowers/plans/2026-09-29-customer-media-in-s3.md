# Customer Media in S3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every customer photo and video that becomes part of a conversation is in the media bucket, and MongoDB holds its permanent address (bucket, key, `s3://` URI), never a presigned URL.

**Architecture:** The server writes inline photos to S3 itself on `POST /message` (`S3Store.put_bytes`) and hands the turn an `s3://` key, as claimed uploads already do. One builder (`media_records.media_record`) makes the record, the conversation store keeps it (`record_media`, `media_of`) in a new `media` collection with no TTL, and erasure covers it. The bucket template stops expiring customer media.

**Tech Stack:** Python 3.12, FastAPI, pymongo (mongomock in tests), boto3 (a fake store in tests), unittest.

**Spec:** `docs/superpowers/specs/2026-09-29-customer-media-in-s3-design.md`

## Global Constraints

- A stored media address is bucket + key only: no `?`, no `X-Amz-`, never a presigned URL.
- The `media` collection has no TTL index; transcripts and summaries stay permanent as before.
- A Claude session never writes to the Atlas cluster or the real bucket; tests use mongomock and a fake store.
- Never swallow an exception: every failure branch logs a named event (`media_not_stored`, `media_record_failed`).
- Logs carry counts, kinds and upload ids, never image bytes.
- British English, no em dashes, in code comments and docs.

## Review Focus

1. An anonymous visitor with no cookie sends a photo: no cluster, so no key; the photo must still reach the model inline and `media_not_stored` must say `no_cluster`.
2. S3 is down: the customer still gets an answer; nothing half-written claims the photo is stored.
3. The same message retried: two puts of one photo must not leave two records claiming one conversation twice under one key (keys carry a fresh upload id, so each put is its own object and record; acceptable, and tested).
4. Erasure of a person with media: records and every S3 version go; the dry run lists keys and deletes nothing.
5. A presigned URL sneaking into a record through a future caller: the builder refuses it.

---

### Task 1: The media record and where it is kept

**Files:**
- Create: `src/emotorad_ai/media_records.py`
- Modify: `src/emotorad_ai/conversation.py` (InMemoryConversationStore: `record_media`, `media_of`, erasure)
- Modify: `src/emotorad_ai/stores/mongo.py` (MEDIA collection and index, `record_media`, `media_of`, erasure)
- Test: `tests/test_media_records.py`

**Interfaces:**
- Produces: `media_record(bucket, key, kind, mime_type, size_bytes, conversation_id, cluster_id, source, stored_at) -> dict`; raises `ValueError` on a key or bucket carrying `?` or `X-Amz-`.
- Produces: `store.record_media(record: dict) -> None` (upsert by `_id` = key), `store.media_of(conversation_id) -> List[dict]`, and `delete_conversation` counts `media`.

- [ ] Step 1: failing tests: the builder's fields and URI; refusal of a signed key; both stores record, list by conversation, and erase with the conversation (`media` count); Mongo `INDEXES` has `media` with no TTL.
- [ ] Step 2: run, see them fail (ImportError, then missing methods).
- [ ] Step 3: implement the builder and both stores' methods.
- [ ] Step 4: run, see them pass; run the store contract and mongo tests.
- [ ] Step 5: commit.

### Task 2: The server stores inline photos and records claimed uploads

**Files:**
- Modify: `src/emotorad_ai/api.py` (`_inbound_attachments`, a `_persist_media` helper)
- Test: `tests/test_api_media_persistence.py`

**Interfaces:**
- Consumes: Task 1's `media_record`, `stores.conversations.record_media`.
- Produces: inline photos become `{"kind": "image", "url": "s3://<key>", "mime_type": ...}` when the bucket is configured and the caller has a cluster; claimed uploads are recorded with `source="upload"`.

- [ ] Step 1: failing tests through `TestClient` with a fake store that has `put_bytes`:
  - an inline photo is put under `customers/<cluster>/<conversation>/images/`, recorded with `source=inline`, and the turn's user content is an image the model was shown;
  - a claimed video upload is recorded with `source=upload`;
  - no media store: the photo stays inline, nothing recorded;
  - `put_bytes` raising `StorageError`: the reply still arrives, `media_not_stored` logged with `reason=store_failed`, no record;
  - `record_media` raising `StoreUnavailable`: `media_record_failed` logged, the reply still arrives;
  - no cluster (no session, no cookie): inline, `media_not_stored` with `reason=no_cluster`.
- [ ] Step 2: run, see them fail.
- [ ] Step 3: implement `_persist_media` and call it from `_inbound_attachments` for inline items (after validation) and for each claim.
- [ ] Step 4: run, see them pass; run `test_api_uploads`, `test_chat_surface`.
- [ ] Step 5: commit.

### Task 3: Permanent retention in the bucket template

**Files:**
- Modify: `infra/media.yaml` (remove `customer-evidence-180d`'s `ExpirationInDays`; keep noncurrent 30 days and abandoned multipart 2 days)
- Modify: `docs/runbooks/media.md` (retention and the deploy command for the person)
- Test: `tests/test_media_template.py`

- [ ] Step 1: failing test: the template parses (CloudFormation tags handled), no rule expires current objects under `customers/`, the multipart rule is kept.
- [ ] Step 2: run, fail. Step 3: edit the template. Step 4: pass. Step 5: commit.

### Task 4: Erasure covers media, in MongoDB and in S3

**Files:**
- Modify: `scripts/delete_person.py`
- Modify: `src/emotorad_ai/storage/s3.py` (`delete_every_version(key)`)
- Test: `tests/test_mongo_scripts.py` (new cases)

**Interfaces:**
- Produces: `S3Store.delete_every_version(key) -> int` (versions removed, via `list_object_versions` + `delete_objects`).

- [ ] Step 1: failing tests: the dry run lists the person's media keys and deletes nothing; `--yes` removes the records and calls the S3 deletion for each key; with no `EMOTORAD_AI_MEDIA_BUCKET` the script says the objects were not deleted, loudly, and the audit record says so.
- [ ] Step 2: fail. Step 3: implement. Step 4: pass. Step 5: commit.

### Task 5: End to end, in process

**Files:**
- Test: `tests/test_media_end_to_end.py`

- [ ] Step 1: one conversation through `TestClient` with `EMOTORAD_STORE=mongodb` on mongomock and the fake store: an inline photo, then a complaint; assert the object in the fake bucket, the `media` record with bucket, key and `s3://` URI, the transcript turn naming the key, the ticket raised (evidence), and no document in any collection containing `X-Amz-` or a presigned host.
- [ ] Step 2: the same with a presigned video upload.
- [ ] Step 3: run the full suite; commit.

### Task 6: Docs and the person's staging test

**Files:**
- Modify: `CLAUDE.md` (one line), `docs/runbooks/media.md` (the staging test steps)

- [ ] Step 1: write the steps: deploy the template, set `EMOTORAD_AI_MEDIA_BUCKET`, run `scripts/chat_local.py`, send a photo, check `aws s3api head-object` and a read-only `media` query.
- [ ] Step 2: final full suite, a reviewer on the branch, commit.
