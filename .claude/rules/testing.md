# Testing rules

- stdlib `unittest`, files under `tests/` named `test_<module>.py`, run with `python3 -m unittest discover -s tests -t .`. No network, no AWS, no model: the offline planner stands in for Claude.
- Two properties are load-bearing and must keep their own tests: the safety branch asserts the model was never called, and retrieval is evaluated separately from conversations (`tests/test_retrieval_evals.py`, with accuracy floors, per language).
- A new sub-agent or bot ships with a conversation test through `runtime.handle()` that covers identity, one blocked path and the disclosure line.
- Fixtures mirror the real API shapes in `docs/api-shapes/`; never invent a field the real response does not carry.
- Test data is fake: phone `+919999999999`, frame numbers from the fixtures, no real customer records from `logs/conversations.jsonl`.
- Paste the test count and the last lines in the PR. A retrieval change pastes the per-language eval table.
