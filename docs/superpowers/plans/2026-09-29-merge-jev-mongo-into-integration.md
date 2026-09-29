# Merge Jev routing, the MongoDB store and the live evaluation into feat/integration

> **For agentic workers:** executed with superpowers:executing-plans (Native). Each task ends with the test gate it names.

**Goal:** one branch, `feat/integrate-jev-mongo`, cut from `origin/feat/integration` (951afd1), that holds both lines of work with both test suites green and staging behaviour unchanged.

**What is merged:** `feat/live-eval` (a7cc568): 46 commits built on `main` 847a710 (Jev routing on OpenRouter, the narrow path and the LangGraph turn graph, standard replies, the MongoDB conversation store with transcripts, summaries, memory and write receipts, the live evaluation), plus the 3 setup commits on `main`.

**Decided by the person (2026-09-29):** merge onto a new branch (not a rebase, not a rebuild); customer media is kept permanently; video descriptions go through OpenRouter. The last two are Phase 3 below, not this merge.

**Analysis this plan argues from:** three read-only comparisons of 211508d..origin/feat/integration against 211508d..feat/live-eval (turn core; edges and operations; media and tests), summarised in the session of 2026-09-29.

## Global constraints

- Staging keeps running exactly as today: `EMOTORAD_AI_MODE=anthropic`, conversations in memory, OpenRouter off, the deploy health check (`"secrets":"loaded"`) unchanged.
- Nothing is pushed and no PR is opened without the person's yes. No shared environment is written.
- Baselines: THEIRS 1,022 tests with 3 known failures (two `test_video` from missing ffmpeg, `test_start` path separator on Windows); OURS 731 with the two `test_video` errors. After the merge nothing else may fail.
- Tests: `PYTHONPATH="src;." python -m unittest discover -s tests -t .` with `OPENROUTER_API_KEY` and `EMOTORAD_MONGO_URI` unset.

## Resolution rules

| Area | Base | Ported in |
|---|---|---|
| `.gitignore`, `README.md`, `requirements.txt`, `tools/fixtures.py`, `tools/registry.py` | union | both sides' additions |
| `config.py` | union | one `MODES = ("offline", "anthropic", "bedrock", "openrouter")`, imported by `llm.py`; our fields and checks; their `approval_mode` |
| `llm.py`, `wiring.py` | THEIRS `llm.py` + OUR OpenRouter classes | `build_models` stays the single entry point and calls `select_llm` for offline, anthropic and bedrock; `from_openai_response` sets `model` |
| `conversation.py` | OURS (store classes, JSON, transcripts) | THEIRS' fields (`cluster_id`, `coverage_result`, `placed_order_ids`, `consumed_codes`), `trim_history`, `customer_texts`, `address_tokens`, `peek` on both stores |
| `tools/mocks.py` | union | our `idempotency` and `attach_transcript`; their replacement, location and address tools and kwargs |
| `agents/base.py` | THEIRS | our `prefetched`; `model` on `llm_turn` |
| `runtime.py` | OURS (graph, persistence loop) | THEIRS' constructor arguments, identity tools, video-summary safety scan, unknown-agent clear, `facts` and `on_tool_result`, remembered coverage and orders, order post-check, `user_content`, model-outage apology; our fallback keyed on `escalation_reason`; `_merge_onto_fresh` carries their fields |
| `observability.py`, `tracing.py` | THEIRS | our `jev_decision`; our `llm_error` carries `error=` so redaction keeps it |
| `api.py` | THEIRS | `build_stores` and `build_models`; `/health` keeps their fields and adds `store`; the pinned agent and `cluster_id` are applied inside the turn so the versioned save keeps them |
| `cli.py` | OURS | their `guide_media`, `sent_media` |
| `.github/workflows` | both | install `requirements-dev.txt` so the store tests run |
| `CLAUDE.md` | OURS' structure | their notes moved to the build log; stale lines corrected (working branch, start script, staging mode) |

## Tasks

1. Merge `feat/live-eval` with `--no-commit`; resolve the mechanical files. Gate: none (tree does not build yet).
2. `config.py`, `llm.py`, `wiring.py`. Gate: `test_llm_select`, `test_wiring`, `test_openrouter_*`, `test_decisions_config`.
3. `conversation.py`, stores, `peek`. Gate: store contract, `test_conversation_*`, `test_history_window`, `test_coverage_memory`.
4. Tools (`mocks.py`, `registry.py`, `fixtures.py`). Gate: `test_tools`, `test_idempotency_claims`, `test_replacement_order_tool`.
5. `agents/base.py`. Gate: `test_agent_attachments`, `test_history_window`, `test_narrow_support`.
6. `runtime.py`. Gate: the whole suite.
7. `observability.py`, `tracing.py`, `api.py`, `cli.py`. Gate: the whole suite.
8. Workflows, `CLAUDE.md`, docs. Gate: the whole suite; commit the merge.
9. A fresh reviewer on the most capable model reviews the merge commit before anything else is built on it.

## Out of scope (later phases, each its own branch or PR)

- **Phase 2:** the live evaluation on the merged runtime (their registry wiring, a fake media store, raw tool results through `on_tool_result`), the final-review fixes recorded on 2026-09-29, and a live re-run after the person's yes.
- **Phase 3:** media: permanent retention (lifecycle change in `infra/media.yaml`, versioned erasure in `scripts/delete_person.py`), video described through OpenRouter in the runtime for every channel, OpenRouter image and PDF conversion, honest `evidence_seen`, history compaction, transcripts that record S3 keys and never `data:` URLs, media events with cost.
- Flagged, not fixed: per-process verification and replacement-order state are not safe across several servers.

## Rollback

The merge lives on an unpushed branch. `git branch -D feat/integrate-jev-mongo` removes it; `feat/integration` and our branches are untouched.
