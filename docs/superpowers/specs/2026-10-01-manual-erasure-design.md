# Manual erasure: a person reviews and deletes each request

Date: 1 October 2026. Branch: `feat/manual-erasure`, cut from
`fix/staging-test-findings`. It changes the self-service erasure design
(`2026-10-01-self-service-erasure-design.md`): requests are still recorded the
same way, but nothing deletes them on its own any more.

## Purpose

The person's decisions (1 October 2026):

- Every deletion is started by hand, by a person, and deletes the customer's
  data from both MongoDB and S3.
- Before deleting, that person can read everything that will be deleted.
- The check is there for three reasons: nothing still needed is lost (an open
  safety case, a ticket in progress), the request is genuine, and someone
  accountable signs off each deletion.
- The person reads everything that will go: every chat in full, the
  summaries, the photos and videos, the location records and the request's
  own history.
- They do it with one admin command inside the app image, run on the server
  through an SSM session.
- After reading a request they can delete it or hold it. Nothing is declined.
- One person signs off: their name and reason go in the audit record.
- A daily check, counts only, reminds the team of open requests.

Customers only, as before. How a customer asks (the chat, the Amiigo app
button, the HTML chat menu), the `DEL-` reference, one pending request per
person and cancelling are unchanged.

## 1. The admin command

`src/emotorad_ai/erasure_admin.py`, in the app image:

```
aws ssm start-session --target i-02e7dc2874e0fdacb
sudo docker exec -it emotorad-ai python -m emotorad_ai.erasure_admin list
```

`docker exec` runs inside the live container, so the command has the
container's settings (`EMOTORAD_AI_SECRET_ID`, `EMOTORAD_AI_MEDIA_BUCKET`) and
the instance role. It loads the config store itself (`load_into_environ`), as
the nightly job does. No connection string or key reaches anyone's laptop.

It has five steps. Each one that changes something asks for the person's name
first. A name, a reason or a note that is empty after trimming is refused.

### `list`

Every open request (pending or held), oldest first, one line each: reference,
age in whole days, channel, `pending` or `held`, and counts (chats, transcript
turns, files). No phone number, no chat text, no note.

### `show DEL-XXXXXX`

Asks for the person's name, records the review (section 2), then prints, in
this order:

1. **Flags**, at the top, one line each, or "No flags":
   - a ticket raised in any of the person's chats (a summary with a
     `ticket_id`), with its reference;
   - a safety hand-over (a bot turn whose `handled_by` starts with
     `guardrail:` and contains `safety`), with the chat and the time;
   - a chat with a turn in the last 48 hours: the person may still be using
     it.
2. **Is it genuine:** the phone number (from the request's `user_key`), the
   channel, the proof (`app sign-in`, or `OTP, verified at <time>`; "not
   recorded" for requests made before this change), when it was asked and from
   which conversation, and the person's earlier requests (reference, status,
   asked, closed), found by `user_key` or by `key_sha256`.
3. **Every chat**, oldest first: its id, first and last turn times, channel,
   where it came from (each `conversation_origins` record: source, country,
   region, city), its summary line when one exists, then every transcript turn
   in order: time, `customer` or `bot`, the text, `handled_by`, and the
   attachments.
4. **Every photo and video:** kind, type, size, when it was stored, the S3
   key, and a link that opens it for 15 minutes (`S3Store.presign_get`, the
   bucket's existing GET expiry). The instance role already has
   `s3:GetObject` on customer files.
5. **Totals:** exactly what `delete` would remove: `delete_person(user_key,
   dry_run=True)` per collection, plus the number of files.

The output goes to the person's terminal only. Nothing is written to a file.

### `hold DEL-XXXXXX`

Asks for the person's name and a note (for example "safety case EM-00001
open"). The request stays `pending` and is marked held (section 2). Holding
again replaces the note. A held request can still be shown and deleted, and the
customer can still cancel it.

### `delete DEL-XXXXXX`

Asks for the person's name and a reason. It refuses, and deletes nothing,
unless all of these hold:

- the request exists and is `pending` (a customer who cancelled after the
  review, or a request already done, stops it here);
- the same person (the name, compared trimmed and without regard to case) ran
  `show` on it in the last 24 hours;
- the totals now are exactly the totals that review saw. If the customer
  chatted again or sent another file since, it says so and asks for `show`
  again, so nothing is deleted that was not read.

It then prints the totals again and asks the person to type the reference.
Anything other than the reference (compared trimmed and in capitals) stops it.
Then, in this order, the steps the nightly job runs today:

1. hide every file (`S3Store.hide`, a plain delete);
2. `delete_person(user_key)`;
3. an `erasure_log` record (`erasure.audit_record`): `run_by` is the person's
   name, the reason is "self-service request DEL-XXXXXX: <their reason>", what
   went, the file count, the person only as a hash;
4. close the request as `done`, recording who closed it.

If any file cannot be hidden, nothing in the database is deleted: the request
stays pending, counts an attempt and keeps the error, which is printed. The
person runs `delete` again once the cause is fixed.

### `check`

For the daily workflow (section 4). One line per open request: reference, age
in whole days, `pending` or `held`. Then "open erasure requests: N". It exits 1
when any request, held ones included, is 25 days old or more (the 30-day
promise stands either way), or when the store cannot be read. It prints no
phone number, no counts and no note.

### Exit codes and failures

`0` when the step did what it said. `1` when it refused, the store could not be
reached (a clear message, no stack trace with data in it), or a file could not
be hidden. An unknown reference says "No erasure request DEL-XXXXXX." Ctrl-C at
any prompt stops with nothing changed.

## 2. What a request records

`erasure_requests` gains three fields. Both stores (`InMemoryConversationStore`
and `MongoConversationStore`) carry them, under the store contract tests.

- `proof`, set when the request is made:
  - `{"method": "app_sign_in"}` for the Amiigo app's signed-in rider (the
    `/erasure-requests` endpoints by `session_token`, and the chat when the
    identity came from the app session);
  - `{"method": "otp", "verified_at": "<ISO time>"}` for a website visitor
    whose chat verified a number (`VerificationStore` already keeps
    `verified_at`; it gains a read of it per conversation).
  - Missing on requests made before this change; `show` prints "not recorded".
- `reviews`: a list, one entry per `show`: `{"by", "at", "totals"}`. `delete`
  checks the latest entry by the same name.
- `held`: `{"by", "at", "note"}`, or missing.

`request_erasure` takes the proof as a new argument. New store methods:
`record_erasure_review(reference, by, at, totals)`, `hold_erasure(reference,
by, at, note)` and `erasure_history(user_key)` (every request of that person,
open or closed). `close_erasure` gains `by`. A closed request keeps its
`reviews`, `held` and `by`: names and times only, nothing about the customer,
and its `user_key` is still replaced by its hash.

The review record in `erasure_requests` is the log of who read what. Output
from `docker exec` reaches the person's terminal, not CloudWatch; the SSM
session itself is recorded in CloudTrail under the person's AWS identity.

## 3. What the customer is told

Two texts in `erasure.py` promise "tonight's run" and change:

- `ERASURE_REQUESTED`: "Your deletion request is {reference}. Our team will
  check it and delete everything this chat holds about you within 30 days. If
  you change your mind before then, say 'cancel my deletion'."
- `ERASURE_EXISTING`: "You've already asked for this. Your request is
  {reference}, and our team will complete it within 30 days."

`ERASURE_CONFIRM`, `ERASURE_DIALOG` and the two dialogs already say "It's done
within 30 days" and stay. A held request is `pending` wherever the customer
looks (`/erasure-requests/status`, the chat).

The Amiigo contract (`docs/contracts/amiigo-support-chat.md`): "The request is
carried out by a nightly job and the data is gone for good within 30 days"
becomes "Our team checks each request and deletes the data within 30 days."
The endpoints and their answers do not change. The PDF on the Desktop is
regenerated.

A deleted file stays in the bucket's version history for 30 days after the
delete, then the lifecycle removes it for good (`infra/media.yaml`, unchanged).
The server is not given permanent-delete rights.

## 4. Nothing deletes on its own

- `erasure_job.process`, which deleted every pending request, goes. Its steps
  for one request move into `erasure_admin` (section 1, `delete`). The module
  `erasure_job.py` is removed, so no schedule or script can delete without a
  person.
- `.github/workflows/erasure-nightly.yml` becomes
  `.github/workflows/erasure-check.yml`, "Erasure check (staging)": daily at
  03:30 UTC (09:00 India time) and by hand, it runs `python -m
  emotorad_ai.erasure_admin check` in a one-off container through SSM, exactly
  as the nightly run does now. A red run means a request is 25 days old or
  more, or the store could not be read; GitHub emails the workflow's owner.
  GitHub runs a workflow, by schedule or by hand, only once it is on `main`;
  until then, run `erasure_admin check` in the container by hand.
- `scripts/delete_person.py` stays, for deletions asked for outside the chat
  (by email, for example).

## 5. Documentation

- `CLAUDE.md`: the self-service erasure bullet describes the admin command and
  the daily check instead of the nightly job, and says a Claude session never
  runs `erasure_admin show`, because it prints customer data.
- Code comments that say the nightly job deletes (`erasure.py`, `runtime.py`,
  `api.py`, `conversation.py`, `storage/s3.py`, the HTML chat) say the admin
  command does.

## 6. Tests

- `erasure_admin`, against the in-memory store and a fake bucket, with the
  prompts and the output passed in:
  - `list` prints no phone number and no chat text;
  - `show` prints the flags (ticket, safety hand-over, recent chat), the
    proof, the earlier requests, every turn, every file with its link, and the
    totals, and records the review;
  - `hold` keeps the request pending, records the note, and `list` shows it
    held;
  - `delete` refuses with no review, a review by someone else, a review older
    than 24 hours, totals that changed, the wrong reference typed, an empty
    name or reason, and a request that is cancelled, done or unknown, and in
    every case deletes nothing;
  - `delete` succeeds: the files are hidden, the records are gone, the audit
    record has the name and reason, the request is closed with `by`;
  - a file that cannot be hidden leaves every record and the request pending
    with the error;
  - `check` exits 1 at 25 days (held included) and 0 below, and prints no
    phone number or note.
- The store contract: `proof`, `reviews`, `held`, `erasure_history`, and
  `close_erasure` with `by`, for both stores.
- A request made in the chat and one made by the app button and the HTML
  chat's button each record their proof.
- No customer text and no contract line says "tonight"; the scheduled workflow
  runs only `erasure_admin check`, and nothing in `src/` or `.github/` runs a
  deletion on a schedule.
