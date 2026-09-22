"""Generative and hybrid features: RAG, root-cause summaries and generated copy (Phase 3a).

Owned by the `phase-3a-generative` branch (PARALLEL_WORK_PROTOCOL.md §3). The contracts-first task
created this package and `contracts.py`; the `LLMClient` protocol and `FakeLLMClient` these
features are written against live in `engine/llm.py`, where Phase 3a adds `BedrockLLMClient` and
`GroundedFakeLLMClient` beside them.

This package **consumes** what the predictive engine produces and never joins it. A root-cause
summary reads a finished run's `feature_importance.json`, `row_explanations.parquet` and
`scores.csv`; campaign copy reads a finished scoring run's bands. Neither is a stage, neither is
listed in `StageKey`, and nothing here is imported by `engine.pipeline` or by any module under
`engine.stages` - the dependency runs one way, and a test proves it (DEC-210).

Three rules hold everywhere in here, and each has a module that enforces it rather than a comment
that asks for it:

**Grounded or nothing.** The assistant answers from retrieved chunks or refuses; a summary's every
claim carries a reference to an item in the evidence pack it was given; copy may use only the
fields a use case whitelisted. A model is never handed a number it could restate as a fact unless
that number was in its input.

**Everything generated passes guardrails before it is stored.** `guardrails.check` runs the free,
deterministic rules first and the judges only on what survives them, and a failure is recorded
rather than quietly regenerated more than `retries` times.

**Cost is an artefact.** Every call goes through `budget.Meter`, which records the model, the
tokens, the latency and - when the price table knows the model - the cost, and refuses the call
that would take the run past its budget.
"""

from __future__ import annotations
