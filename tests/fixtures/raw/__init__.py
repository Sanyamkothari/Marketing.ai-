"""Synthetic **raw client tables** for Phase 2 onboarding (Phase 2 plan section 11).

Where `tests/fixtures/make_data.py` writes the one-row-per-entity file Phase 1 trains on, this
package writes the six messy tables a telecom client would actually hand over, so the onboarding
flow - profiling, role detection, mapping, snapshots, features, label derivation - has something to
run on. Nothing here is real data and no table carries a churn column: the outcome is recovered
from the activity log, which is the whole point of Phase 2.
"""

from __future__ import annotations
