"""What build of the playground this is, and what changed in it.

Separate from prompt versions, which are content and live in ``.playground``.
This is the harness: the tool loop, the guardrails, the media pipeline, what the
uploader accepts. Both numbers matter when reading a saved transcript — a reply
is the product of a prompt version *and* the build that ran it, and "the bot did
not send the photo" means something different on 0.4 than on 0.7.

Hand-maintained rather than derived from git, because the deployed container
carries no ``.git``. A test asserts the number matches the newest changelog
entry, so the two cannot drift apart silently.

Bump the minor number for a change a tester would notice; bump the patch for a
fix that only closes a defect in existing behaviour.
"""

from __future__ import annotations

from typing import List, Tuple

PLAYGROUND_VERSION = "0.14.0"

# (version, date, what a tester would notice). Newest first.
CHANGELOG: List[Tuple[str, str, str]] = [
    (
        "0.14.0",
        "2026-09-10",
        "Battery diagnosis finished moving into knowledge records — the standard flow, the "
        "SOC-indicator fault and the on/off switch fault join the Doodle flow. The prompt is "
        "down from 29,416 to 16,491 characters and each flow now reaches the model whole, "
        "scoped to the bikes it applies to.",
    ),
    (
        "0.13.0",
        "2026-09-10",
        "First knowledge migration. The Doodle battery flow moved out of the prompt into a "
        "record and battery can search again; records can now exclude a model as well as "
        "require one, so a Doodle owner gets the Doodle flow and nobody else can see it.",
    ),
    (
        "0.12.1",
        "2026-09-10",
        "\"Melted\" now reaches the agent so the two-photo damage assessment can actually "
        "run; fire, sparks, smoke, heat, leaks, cracks and swelling still stop the "
        "conversation immediately.",
    ),
    (
        "0.12.0",
        "2026-09-10",
        "Melted-terminal and melted-controller comparison photos are available to send, and "
        "\"my battery is swollen\" now reaches the safety gate — the pattern matched "
        "\"swelling\" but not the irregular past participle people actually use.",
    ),
    (
        "0.11.2",
        "2026-09-09",
        "An error code asked about on the same turn the customer verified now resolves, and a "
        "model retrying a call that failed is no longer killed as stuck — that combination was "
        "ending turns with a handover message instead of the answer.",
    ),
    (
        "0.11.1",
        "2026-09-09",
        "Fixes a crash on sending a message: moving attachments into the chat input removed "
        "the line defining the old uploader counter but left two uses of it.",
    ),
    (
        "0.11.0",
        "2026-09-09",
        "Trimmed the frame. Streamlit's dashboard defaults put roughly 400px of heading, "
        "boilerplate and padding above the first message; the title, build number and view "
        "switch now share one row, the redundant 'Test conversation' heading is gone, API key "
        "and token cap fold away once set, and an empty chat suggests where to start.",
    ),
    (
        "0.10.0",
        "2026-09-09",
        "Layout pass. Attachments moved into the chat input itself, so the standing dropzone "
        "is gone; a Chat / Side by side / Prompt switch stops the editor holding half the "
        "screen while you chat; the four stacked status captions became one line; and past "
        "chats and the verification panel moved to the sidebar, out of the transcript.",
    ),
    (
        "0.9.0",
        "2026-09-09",
        "Display error codes are looked up against the customer's own bike, with the "
        "technician's diagnostic chain behind a Technical breakdown control.",
    ),
    (
        "0.8.0",
        "2026-09-09",
        "Evidence is enforced in code: a reply concluding a fault with no photo or video "
        "anywhere in the conversation is blocked, beside the coverage post-check. `/proof` "
        "stands in for an upload so the flow can be walked without making files. Turns are "
        "numbered.",
    ),
    (
        "0.7.0",
        "2026-09-08",
        "The bot sends guide photos and clips from Cloudinary — through a retrieved knowledge "
        "record, or by naming a key from a fixed catalogue. The same picture is never sent "
        "twice in one conversation. Prompt versions can be published from outside the browser "
        "and the page says when one arrives.",
    ),
    (
        "0.6.0",
        "2026-09-07",
        "Live customer mode: anonymous until a one-time code proves the number. An order or "
        "invoice number recovers an account without revealing it, and a customer who never "
        "verifies still gets an intake ticket.",
    ),
    (
        "0.5.0",
        "2026-08-30",
        "Real OMS reads behind the existing tool shapes, with coverage measured from the "
        "registration date when no purchase date exists — labelled as provisional wherever it "
        "is used.",
    ),
    (
        "0.4.0",
        "2026-08-30",
        "Video and PDF uploads. A clip is sampled into stills because the model cannot watch "
        "video, and its audio track is transcribed so the customer's narration is not lost.",
    ),
    (
        "0.3.0",
        "2026-08-29",
        "The agent's tools actually run. Every call and result is shown inline, sliced by the "
        "selected agent's own TOOL_NAMES.",
    ),
    (
        "0.2.0",
        "2026-08-29",
        "Chats are archived per id rather than overwritten, attachments stored once, and a "
        "turn that produced no text says why instead of rendering blank.",
    ),
    (
        "0.1.0",
        "2026-08-29",
        "Rider, chat and prompt versions survive a reload.",
    ),
]


def current() -> Tuple[str, str, str]:
    return CHANGELOG[0]
