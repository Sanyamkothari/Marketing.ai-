"""The onboarding specs: the pydantic models Phase 2 §8 names.

**This module is deliberately empty, and that is a blocker, not a style.** The contracts-first
task is meant to add the models `MARKETING_AI_PHASE2_PLAN.md` §8 lists, with the fields it lists,
so that the three branches agree on the shape before any of them starts. That plan is not in the
repository, so the field list it defines is not available, and the models are not written here.

Inventing them would be worse than leaving them out. A branch that starts against a guessed
`SourceSpec` has to rename its fields the moment the real plan appears, and renaming a field of a
shared contract is the one thing `PARALLEL_WORK_PROTOCOL.md` §2 forbids outright.

What exists instead: this module, its import path, and an open entry in
`docs/CROSS_BRANCH_REQUESTS.md` asking for the plan. When it arrives, the models land here and
nothing else in the contracts-first work changes - `RunRequest.dataset_id`,
`RunRequest.client_id`, `RunManifest.dataset_id`, `RunManifest.client_id` and the composite
`primary_key` are already in place, and they are the surface the other two branches depend on.
"""

from __future__ import annotations

__all__: list[str] = []
