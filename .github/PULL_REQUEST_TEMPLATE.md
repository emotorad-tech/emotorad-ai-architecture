## What and why

<!-- Two or three sentences. Link the build plan section or the spec. -->

## What changed

<!-- One bullet per behaviour change. -->

## Tier

- [ ] Tier-2: no change to guardrails, runtime order, tool registry, identity or the model clients
- [ ] Tier-1: touches one of those; rollback section filled in; human review before merge

## Test evidence

```
python3 -m unittest discover -s tests -t .
<last lines, including the test count>
```

- Safety or guardrail change: test that asserts the model was never called on the blocked path: <!-- file::test -->
- Retrieval change: per-language eval numbers: <!-- table -->
- Languages exercised: <!-- English, Hindi, Marathi, Tamil, Hinglish -->

## Rollback (required for Tier-1)

- How to revert:
- Conversations or logs to check after revert:
- Whom to tell:

## Not done on purpose

<!-- Anything deliberately left out, with why. -->
