# Knowledge base — authored content

One file per sub-issue. **Structured authoring is what makes chunking free**: each file is already
a retrieval unit with a natural boundary, so nothing has to be split by token count and no step ever
gets cut in half.

## Why files in a repo, not a CMS (R1 decision)

Git is the audit trail, PRs are the approval workflow, and rollback is `git revert`. This is a
single-author content set today — a CMS would be weeks of work to land somewhere worse. Revisit when
several non-technical SMEs are editing concurrently and waiting on each other.

## Rules

- **One sub-issue per file.** If a file needs the word "also", it is two files.
- **`applies_to` is a filter, not decoration.** A step that is wrong for a 36V bike must not be
  retrievable for one. Leave it empty only when the content is genuinely universal.
- **Steps must be safe for a customer alone.** Nothing that involves opening the pack, bypassing the
  BMS, or a charger that did not ship with the bike.
- **Media is a URL plus a caption.** The caption is what the model reads; the URL is what the
  customer sees. A photo with no caption is invisible to retrieval.
- **`superseded_by` retires a record.** Delete it from the index — a stale record that still
  retrieves is worse than a missing one, because it answers confidently.

## Format

```yaml
id: battery-wont-charge          # stable, referenced in evals — never renumber
title: Battery will not charge
topic: battery
symptoms: [wont_charge, not_charging, no_charge, charger light not coming on]
applies_to: {}                   # e.g. {battery_variant: "48V"} — empty means all
source: Battery care manual §4.1
media:
  - url: https://cdn.emotorad.test/kb/charger-seating.png
    caption: Charger barrel fully seated in the battery port
steps:
  - Confirm the wall socket works with another appliance.
  - ...
escalate_when: The LED still does not change after all steps.
```
