# Plans

The plans the phases were built from. Plan A M39 asks for the Phase 2, 3a, 4a and Evolve plans to be
kept here. Four of them have never been committed to this repository, so they are listed below as
missing rather than rewritten from memory: a plan reconstructed after the event would read as the
source of decisions it was actually written to justify.

## In the repository

| Plan | File | Phase / branch |
|---|---|---|
| Phase 1 | [`plan.md`](../../plan.md) (left at the repository root, the path the README and `docs/DECISIONS.md` cite) | trunk |
| Plan A: Phase 2 completion and integration hardening | [`MARKETING_AI_PLAN_A_PHASE2B.md`](MARKETING_AI_PLAN_A_PHASE2B.md) | `phase-2b-completion`, M34 … M39, DEC-083 … 099 |
| Plan C: Phase 4b first deployment, access control, privacy and scheduling | [`docs/PHASE4B_PLAN.md`](../PHASE4B_PLAN.md) (left where it was committed; `PARALLEL_WORK_PROTOCOL.md` §4 cites that path) | `phase-4b-production`, M46 … M52 |
| The parallel-work protocol (the contract between the phase branches, not a phase plan) | [`PARALLEL_WORK_PROTOCOL.md`](../../PARALLEL_WORK_PROTOCOL.md) | all |

## Missing

None of these files appears in any commit on any branch of this repository (checked 2026-09-23 with
`git log --all`). `docs/CROSS_BRANCH_REQUESTS.md` records on 2026-09-22 that the Phase 2 and 3a
plans reached their branches without being committed and that the Phase 4a plan never arrived. Each
is needed from the plan owner as the original text, to be added here unchanged.

| Plan | Expected file | What cites it |
|---|---|---|
| Phase 2 — data onboarding | `MARKETING_AI_PHASE2_PLAN.md` | `PARALLEL_WORK_PROTOCOL.md` §1, §3; Plan A; `engine/onboarding/specs.py` (§8) |
| Phase 3a — generative and hybrid | `MARKETING_AI_PHASE3A_PLAN.md` | `PARALLEL_WORK_PROTOCOL.md` §1; `engine/generative/contracts.py` (§7) |
| Phase 4a — AWS and production | `MARKETING_AI_PHASE4A_PLAN.md` | `PARALLEL_WORK_PROTOCOL.md` §1; Plan C |
| Evolve (Phase 5) | name unknown | Plan A M39; `docs/API.md` ("the Evolve layer of Phase 5") |
| Plan E — pilot readiness | `MARKETING_AI_PLAN_E_PILOT.md` | `PARALLEL_WORK_PROTOCOL.md` §1; `docs/DECISIONS.md` DEC-900 |

Plan B (Phase 3b), which Plan A says starts after M34 merges, is not here either; M39 does not ask for
it, and it is listed only so that its absence is not mistaken for an oversight.
