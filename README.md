# Emotorad AI platform — skeleton

The architecture skeleton from `docs/Emotorad_Platform_Build_Plan.md`, built end to end
on the first use case: **customer chatbot for after-sales battery support**, on the
website chat widget, with every tool mocked.

Nothing here touches a real OMS, ERP or ticketing system. The point is to validate the
conversational flow and the guardrails against fake data first, then swap tools for real
integrations one at a time (build plan §5, steps 6–7).

## Run it

No install and no AWS needed for the offline path — a fixed planner stands in for the
model, so the contract, identity resolution, guardrails, routing, tools and logging are
all the real code:

```bash
python3 -m unittest discover -s tests -t .              # 35 tests, no dependencies

PYTHONPATH=src python3 -m emotorad_ai.cli --offline "my battery won't charge"
PYTHONPATH=src python3 -m emotorad_ai.cli --offline "the battery is bulging"      # safety hard stop
PYTHONPATH=src python3 -m emotorad_ai.cli --offline                               # interactive
```

Against Claude on Bedrock (needs `pip install anthropic` and AWS credentials for the
account and region in `config.py`):

```bash
PYTHONPATH=src python3 -m emotorad_ai.cli --session sess-ananya
```

Every turn appends to `logs/conversations.jsonl` — that file is what the golden
regression set gets built from.

## Layout

| Module | Build plan | What it does |
|---|---|---|
| `contract.py` | §3.1 | The internal message contract every adapter emits |
| `identity.py` | §3.2 | Deterministic persona resolution, plus profile/warranty hydration |
| `adapters/website_chat.py` | §3.3 | The one adapter use case #1 needs |
| `tools/registry.py` | §3.4 | Envelope, identity injection, idempotency, error containment |
| `tools/mocks.py` | §5.2 | The seven mocked tools, with real shapes |
| `router.py` | §3.5 | Stub router — records why, so classification slots in later |
| `agents/battery_support.py` | §4 | System prompt and tool set for the first sub-agent |
| `agents/base.py` | — | The persona-agnostic tool-calling loop |
| `guardrails.py` | §4 | Safety branch and human handoff, matched in code |
| `knowledge.py` | §5.4 | Battery retrieval — the one genuinely-RAG piece |
| `observability.py` | §3.6 | JSONL event log with PII redaction |
| `runtime.py` | — | The wiring, in the order the design requires |
| `llm.py` | §6 | Claude on Bedrock, plus scripted stand-ins for tests |

## Guardrails that are code, not prompt

- **Battery safety is a hard stop.** Swelling, smoke, heat, fire, leaks, cracks and sparks
  are matched before the turn reaches the model, which is then never called. A critical
  `battery_safety` ticket is raised deterministically. Tested by asserting the model
  received zero requests.
- **A request for a human exits immediately**, at any point, before any model call.
- **Warranty is always a tool call.** It is also pre-fetched into the system prompt as
  fact, so the agent has nothing to infer.
- **The model cannot name a customer.** `customer_id` is injected by the registry from the
  resolved session and is absent from the schema the model sees.
- **Writes are idempotent.** A retried ticket or booking returns the first result.
- **Tools never raise into the loop.** Failures come back as error envelopes.

## Open items this code encodes

- `get_battery_diagnostics` is registered only when `diagnostics_available=True`. Until
  telematics are confirmed, the tool does not exist rather than existing and returning
  nothing — an absent tool is a fact the model can reason about.
- `MockTicketSystem` stands in for Zoho Desk. Confirm customer-side tickets land there
  before swapping it.
- `SessionDirectory` and the website event shape are assumptions. Confirm against the real
  widget, and confirm whether Amiigo should be the first surface instead.
- Warranty terms in `tools/fixtures.py` are placeholders. Confirm per model against the
  real policy before this drives a coverage answer to a customer.

## What is deliberately not here

WhatsApp, voice, dealer and internal adapters; real classification in the router; AIRO.
Each plugs into the same contract and registry when its turn comes — see build plan §7.
