# Self-service "delete my data": design

Date: 1 October 2026. Branch: `feat/self-service-erasure`, cut from
`feat/conversation-origin` (it erases that branch's `conversation_origins`).

## Purpose

A verified customer can ask the support chat to delete everything the chatbot
holds about them, without emailing anyone. The person's decisions (1 October
2026):

- A request, then processed: the chat records a request with a reference; it
  never deletes anything itself.
- A nightly job processes the requests.
- The job runs on the staging server as a one-off container, started each
  night by GitHub through SSM. The server's role gains plain delete (no
  version delete) on customer files only; S3 erases a deleted file for good
  within 30 days, so a mistake or misuse can be undone for 30 days.

- The Amiigo app also gets a "Delete my conversation data" button that does
  the same thing, through its own endpoints (section 4). We build and test the
  server side and add the endpoints to the Amiigo API contract; the app team
  builds the button in `emotorad-tech/amigo` with the Bolt design tokens.

Customers only (Amiigo app and website chat). Dealers, OMS data and ERP data
are out of scope.

## 1. In the conversation

### Recognising the request

A fixed phrase check (`src/emotorad_ai/erasure.py`), not the model:

- **Delete:** "delete my data", "delete my account", "delete my details",
  "erase my data", "remove my data", "forget me", "मेरा डेटा हटाओ",
  "मेरा डेटा डिलीट", "borrar mis datos", "eliminar mis datos", "eliminar mi
  cuenta". Case and extra spaces do not matter.
- **Cancel:** "cancel my deletion", "cancel the deletion", "don't delete my
  data", "do not delete my data".
- A cancel phrase is checked before a delete phrase ("don't delete my data"
  contains "delete my data").

### Where it runs

A new turn-graph node, `erasure_gate`, between `handoff_gate` and
`verify_gate`. Safety and "talk to a person" still come first. It applies to
customers only; for any other persona it does nothing.

The working state gains `erasure_step: Optional[str]`:
`"wanted"`, `"cancel_wanted"` or `"confirming"`.

1. **Delete phrase, verified customer.** A pending request already exists:
   reply `ERASURE_EXISTING` with its reference. Otherwise reply
   `ERASURE_CONFIRM` and set `erasure_step = "confirming"`.
2. **Delete phrase, not verified** (website). Set `erasure_step = "wanted"` and
   let the verify step run as it does today (number, then code). When the
   code is right, the verify step replies `CONFIRMED + " " + ERASURE_CONFIRM`
   instead of the bike list and sets `erasure_step = "confirming"`.
3. **The answer while confirming.** The whole message, trimmed and ignoring
   case, is `DELETE`: create the request and reply `ERASURE_REQUESTED`.
   Anything else: reply `ERASURE_KEPT`. Either way `erasure_step` goes back
   to `None`, and the next message is handled normally.
4. **Cancel phrase, verified.** A pending request: mark it `cancelled`, reply
   `ERASURE_CANCELLED`. None: reply `ERASURE_NOTHING_TO_CANCEL`.
5. **Cancel phrase, not verified.** Set `erasure_step = "cancel_wanted"`; once
   verified, the verify step cancels and replies `CONFIRMED + " " +` the
   cancel reply.

### The fixed replies

```
ERASURE_CONFIRM = (
    "This deletes everything this chat holds about you: your past chats with me, "
    "the photos and videos you sent, and the record of where you chatted from. "
    "It does not delete your warranty registration, orders, invoices or service "
    "tickets, which EMotorad keeps for your warranty and by law. It's done within "
    "30 days. Reply DELETE to confirm, or anything else to keep your data."
)
ERASURE_REQUESTED = (
    "Your deletion request is {reference}. Everything this chat holds about you "
    "will be deleted in tonight's run and removed for good within 30 days. If you "
    "change your mind before then, say 'cancel my deletion'."
)
ERASURE_KEPT = "OK, nothing has been deleted."
ERASURE_EXISTING = (
    "You've already asked for this. Your request is {reference}, and it will be "
    "done in tonight's run."
)
ERASURE_CANCELLED = "Your deletion request {reference} is cancelled. Nothing has been deleted."
ERASURE_NOTHING_TO_CANCEL = "There's no deletion request to cancel."
```

### Logging

Events `erasure_requested`, `erasure_cancelled` and `erasure_kept`, each with
the reference where there is one and nothing else about the person.

## 2. The request record (`erasure_requests`, new collection)

```json
{"_id": "DEL-7K3P9Q", "user_key": "PHONE#+91...", "status": "pending",
 "requested_at": "2026-10-01T10:12:03Z", "channel": "amiigo_app",
 "conversation_id": "...", "attempts": 0, "last_error": null}
```

- **Reference.** `DEL-` and six characters from `23456789ABCDEFGHJKMNPQRSTVWXYZ`
  (no 0/O, 1/I/L, U), from `secrets`.
- **One pending request per person.** Asking again returns the pending one.
- **Statuses.** `pending`, then `done`, `cancelled` or `failed`.
- **Once done.** `user_key` is removed and `key_sha256` (SHA-256 of it) is
  kept, with `processed_at` and the deletion counts, so the record shows a
  request was honoured without naming the person. The same applies to
  `cancelled` and `failed` records once closed.
- **Not deleted by `delete_person`.** It is the record that the person asked;
  it holds nothing about them once closed.
- Store methods, on the in-memory and MongoDB conversation stores:
  `pending_erasure_of(user_key) -> Optional[Dict]`,
  `request_erasure(user_key, channel, conversation_id, now) -> str`,
  `cancel_erasure(user_key, now) -> Optional[str]`,
  `pending_erasures() -> List[Dict]`,
  `record_erasure_failure(reference, error) -> int` (the attempts so far),
  `close_erasure(reference, status, counts, error, now) -> None`,
  `log_erasure(entry) -> None` (the `erasure_log` audit record).
- Indexes: `(user_key, status)`; `status`. `scripts/mongo_setup.py` creates
  them through `ensure_indexes`.

## 3. The nightly job

### What runs

`src/emotorad_ai/erasure_job.py`, run as `python -m emotorad_ai.erasure_job`
inside the image (PYTHONPATH is already `/app/src`). It:

1. Loads the secret into the environment (`config_store.load_into_environ`),
   as `docker/start.py` does.
2. Connects to MongoDB and to the media bucket.
3. For each pending request, oldest first:
   1. Finds the person's conversations (`conversations_of`) and their media
      records (`media_of`).
   2. Deletes each media object with a plain delete (`S3Store.hide(key)`, a
      `DeleteObject` without a version id), so it disappears at once and S3's
      lifecycle erases it for good within 30 days. A failure here stops this
      request before any database record is touched, as the erasure script
      does.
   3. Deletes the database records (`delete_person`).
   4. Writes the `erasure_log` audit record in the script's shape:
      `key_sha256`, `kind`, `reason` ("self-service request DEL-…"), `run_by`
      ("nightly erasure job"), `at`, the counts and `s3_objects`.
   5. Closes the request as `done`.
4. A request that fails gets `attempts + 1` and `last_error` (the exception
   class only) and stays `pending`. It is retried every night until the cause
   is fixed, never given up on (the person's decision after the final review,
   1 October 2026); each red run alerts the workflow's owner.
5. Prints one line per request (reference, outcome, counts) and exits 1 if any
   request failed this run, so the GitHub run turns red.

The erasure logic is shared with `scripts/delete_person.py` where it can be:
the log-record shape moves to one function both use.

### How it is started

`.github/workflows/erasure-nightly.yml`:

- `schedule: cron "30 20 * * *"` (02:00 India time) and `workflow_dispatch`.
- Assumes the existing deploy role (`AWS_DEPLOY_ROLE_ARN`, OIDC) and sends an
  SSM command to the instance:
  `docker run --rm --log-driver=awslogs ... -e AWS_REGION=... -e
  EMOTORAD_AI_SECRET_ID=/emotorad/stage/ai/app -e
  EMOTORAD_AI_MEDIA_BUCKET=emotorad-ai-stage-media emotorad-ai:stage python -m
  emotorad_ai.erasure_job`, with `set -e`. No secret is in the command.
- Polls the command like the deploy does and fails the run unless it
  succeeded, printing the job's output.
- **GitHub runs scheduled workflows only from the default branch** (`main`).
  Until this reaches `main`, the job runs only when started by hand.

### Infrastructure (the person applies)

In `infra/media.yaml`:

- The instance role's policy gains `s3:DeleteObject` on
  `arn:aws:s3:::emotorad-ai-${Environment}-media/customers/*` only. No
  `s3:DeleteObjectVersion`.
- The `customers/` lifecycle rule gains `ExpiredObjectDeleteMarker: true`, so
  the delete markers left after the 30 days are removed too.

`S3Store.hide` is called only by the job; the chat code never deletes.

## 4. The Amiigo app button

The app shows its own confirmation dialog, then calls our endpoints. A request
made by the button and one made in the chat are the same request: one pending
request per person, whichever way it was asked.

### Endpoints

Identity comes from `session_token` in the body, resolved exactly as
`POST /message` does (`resolver.resolve_website`). Only a verified customer
(a session that maps to a phone) may use them; anything else gets 403
`{"detail": "Sign in to the app to delete your data."}`. The token is never in
a URL. All three share the message rate limit (per customer IP).

| Endpoint | Body | Answer |
|---|---|---|
| `POST /erasure-requests` | `{"session_token": "...", "confirm": true, "conversation_id": "..." (optional)}` | 201 `{"reference": "DEL-7K3P9Q", "status": "pending", "text": ERASURE_REQUESTED}`; 200 with the same shape and `ERASURE_EXISTING` when one is already pending; 400 without `"confirm": true` |
| `POST /erasure-requests/status` | `{"session_token": "..."}` | 200 `{"reference": "DEL-7K3P9Q", "status": "pending", "requested_at": "..."}`, or `{"reference": null, "status": "none"}` |
| `POST /erasure-requests/cancel` | `{"session_token": "..."}` | 200 `{"reference": "DEL-7K3P9Q", "status": "cancelled", "text": ERASURE_CANCELLED}`; 404 `{"detail": ERASURE_NOTHING_TO_CANCEL}` |

The channel recorded is `amiigo_app`. A store failure gives 503
`{"detail": "I couldn't record your request just now. Please try again in a few minutes."}`
and is logged as `erasure_request_failed`.

### The dialog (for the app team)

- Title: "Delete my conversation data?"
- Body: `ERASURE_DIALOG`, the chat's confirmation without its last sentence:
  "This deletes everything this chat holds about you: your past chats with me,
  the photos and videos you sent, and the record of where you chatted from. It
  does not delete your warranty registration, orders, invoices or service
  tickets, which EMotorad keeps for your warranty and by law. It's done within
  30 days."
- Buttons: "Delete" (calls `POST /erasure-requests` with `"confirm": true`)
  and "Keep my data" (closes the dialog).
- After a request, the screen shows the reference and a "Cancel deletion"
  action until the request is done (`/status`, `/cancel`).

### The contract

The Amiigo API contract moves into the repo at
`docs/contracts/amiigo-support-chat.md` (from the copy on the person's
Desktop), with a new "Deleting conversation data" section: the three
endpoints, their answers, the dialog wording, and the note that a future
`/amiigo/v1/...` path will take the rider's Amiigo token in the header instead
of `session_token`. The Desktop copy is refreshed from it.

## Errors

- The chat side never stops a reply: a store failure while creating or
  cancelling a request is logged as `erasure_request_failed` (the exception
  class) and the customer is told "I couldn't record your request just now.
  Please try again in a few minutes." with `erasure_step` reset.
- The job never deletes database records for a request whose files could not
  all be deleted.

## Testing

- Phrases: each delete and cancel phrase; "don't delete my data" is a cancel;
  ordinary sentences with "delete" in them ("how do I delete a ride?") are not
  matched.
- Conversation, verified (Amiigo-style): delete, confirm with `DELETE`,
  reference given, request stored; `delete` in lower case confirms; any other
  answer keeps the data; asking again returns the same reference; cancel marks
  it cancelled; cancel with none pending.
- Conversation, website: delete phrase, then number and code; the verified
  reply is the confirmation, not the bike list; then `DELETE` creates the
  request; the same for cancel.
- Safety and handoff still win over a deletion phrase in the same message.
- A dealer saying "delete my data" is not handled by the gate.
- Store contract (both stores): one pending request per person; cancel; close
  replaces `user_key` with `key_sha256`; `pending_erasures` oldest first;
  `delete_person` leaves `erasure_requests` alone.
- Job (in-memory store and a fake S3): a pending request erases the person's
  records and files and writes the audit record; the files are hidden before
  the records go; an S3 failure leaves the records and counts an attempt; the
  third failure closes it `failed`; a cancelled request is not processed;
  another person's data is untouched; exit code 1 when any request failed.
- `S3Store.hide` calls `delete_object` without a version id (botocore Stubber).
- Endpoints: a verified session creates a request (201) and a second call
  returns the same reference (200); no `confirm` is 400; an unknown or
  anonymous session is 403; status before and after; cancel, then cancel again
  is 404; a request made in the chat is the one the status endpoint shows; a
  store failure is 503; the token never appears in the logged events.
- Workflow: YAML loads; the command carries no secret value.

## Out of scope

SMS confirmation (no SMS service yet); dealers; data in the OMS, the ERP or
the Amigo backend; a waiting period before the run; telling the customer when
the run has finished.
