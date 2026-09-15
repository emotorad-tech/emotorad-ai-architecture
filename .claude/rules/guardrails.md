---
paths:
  - "src/emotorad_ai/guardrails.py"
  - "src/emotorad_ai/runtime.py"
  - "src/emotorad_ai/tools/**"
  - "src/emotorad_ai/agents/**"
  - "src/emotorad_ai/triage.py"
  - "src/emotorad_ai/disclosure.py"
---

# Guardrail rules (loaded when you touch the safety, routing or tool layer)

- The model requests; code enforces. Anything financial, safety-critical or about coverage is decided by a deterministic function with a test, never by prompt wording.
- Order in `runtime.handle()` is the design. Do not move safety after triage, do not move the coverage post-check after disclosure, do not add a branch that bypasses `_outbound()`.
- A safety term list change ships with a test per language that the phrase is caught, and one that a near miss ("accident" for "dent") is not.
- The coverage post-check must block any reply that asserts coverage the turn's tool result does not support. Extending it to Hindi, Marathi and Tamil claim shapes is the next piece of work here; do not weaken it to make a test pass.
- Tool arguments the model supplies are validated against the spec before the call: types, enums, `additionalProperties: False`. Reject, do not coerce.
- Identity is injected by the registry and absent from the model's schema. `frame_number` is model-supplied and must be validated against the set the warranty tool returned.
- Every write tool takes an idempotency key. `place_order` re-prices and re-checks credit inside the tool.
- The bot always says it is a bot; the golden set asserts it. A prompt edit cannot remove it because `_outbound()` adds it.
