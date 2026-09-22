"""The generative artefact contracts: the pydantic models Phase 3a §7 names.

**This module is deliberately empty, and that is a blocker, not a style.** It should hold the
models `MARKETING_AI_PHASE3A_PLAN.md` §7 lists; that plan is not in the repository, so the field
list is not available and nothing is written here. `engine/onboarding/specs.py` says why guessing
is the worse option, and `docs/CROSS_BRANCH_REQUESTS.md` carries the open request.

The part of the Phase 3a surface that *is* settled is in place: `engine.llm` defines `LLMClient`,
`FakeLLMClient` and `usage_from`, `engine.contracts.LLMUsage` is the per-run total, and
`RunManifest.llm_usage` carries it. A generative artefact that this module will describe is
measured by those regardless of what its own fields turn out to be.
"""

from __future__ import annotations

__all__: list[str] = []
