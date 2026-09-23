"""Onboarding (Phase 2): a client's raw tables become the dataset Phase 1 already trains on.

Phase 1 requires a file that is already one row per entity with our column names. Real clients have
a customer master, a billing table with one row per invoice, a complaints table with one row per
ticket - and usually no target column at all, because "churn" is something they define rather than
something they store. This package closes that gap:

    sources -> profile -> detect roles -> suggest mapping -> (user confirms) -> build
                                                                                 |
      build = apply mappings -> snapshot dates -> features -> labels -> assemble -> validate -> write

Everything it does is driven by four declarative, hashed specs (mapping, features, label, snapshot);
the engine executes specs and never contains client-specific logic. Two rules are absolute:

*Point-in-time correctness.* A feature for snapshot date T may read only events at or before T; a
label for T may read only events after T. The join clause that enforces it is generated in exactly
one place and asserted everywhere (`FUTURE_EVENTS_LEAKED` is a bug, never an acknowledgeable warning).

*Suggest, never decide silently.* Roles, mappings and features are proposed with a confidence and
confirmed by the user; an auto-accepted item is still shown and still reversible.

`specs.py` is the vocabulary every other module here speaks. Import from it, not from each other's
internals.
"""

from __future__ import annotations
