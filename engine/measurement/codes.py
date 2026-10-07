"""The check codes Plan J M93 writes on a `ValidationCheck` (the readiness report's two warnings).

Plan J keeps its codes in one set, `engine.decide.codes.PLAN_J_CODES` (DEC-1300 (d)), which
`engine.pilot.help.known_codes()` returns, so every code there needs a plain-language entry in
`configs/pilot/help.yaml`. These two are the Plan J codes that travel as a `ValidationCheck` row, so
`engine.contracts.ValidationCheck` also accepts them (its PLAN-J hook reads this set). They are not in
`engine.contracts.CHECK_CODE_TABLES`: that registry stays the Phase 1 to 3b and Plan G tables, and
`PLAN_J_CODES` takes this set as its subset at integration (one definition, imported by both).

- `LABEL_RATE_UNSTABLE` (warning): the share of customers with the outcome jumps from one month to the
  next by more than `configs/pilot/readiness.yaml`'s tolerance and more than chance would explain
  (`engine.pilot.readiness.label_rate_checks`).
- `TREATMENT_HISTORY_NOT_RANDOM` (warning): who past campaigns contacted can be told from the customers'
  own data (`engine.uplift.checks.treatment_history`).

Neither blocks a build or a run; both inform a plan.
"""

from __future__ import annotations

from typing import Final

__all__ = ["MEASUREMENT_CHECK_CODES"]

MEASUREMENT_CHECK_CODES: Final[frozenset[str]] = frozenset(
    {
        "LABEL_RATE_UNSTABLE",
        "TREATMENT_HISTORY_NOT_RANDOM",
    }
)
