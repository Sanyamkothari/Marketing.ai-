"""The holdout service (Plan J M92): a persistent, per-customer control group and an explore slice.

Today's control group (`engine.stages.actions`) is drawn per run: salted by the run id and taken as
the smallest digests among the eligible rows. That is the default here too (`actions.holdout.scope:
run`), byte for byte. A deployment can switch a use case to a **persistent** holdout:

* `use_case` - one holdout per use case, the same customers every month;
* `universal` - one holdout across every use case, so a held-out customer is never contacted by
  any use case that has switched to it.

Membership is a threshold rule decided per customer over the whole population, independent of the
run, of eligibility and of row order (`assign.member`):
`int(sha256(f"{salt}:{scope_key}:{entity}")[:16], 16) < fraction * 2**64`. Smaller fractions nest
inside larger ones. The salt is a secret setting (`MARKETING_AI_HOLDOUT_SALT`), fingerprinted in the
platform database so a changed salt is refused (`salt`). An **explore slice** (`actions.explore_fraction`,
0 to 10%) marks a random share of eligible customers outside the target, recorded with its per-row
probability for off-policy evaluation.

Modules:

* `spec` - the configuration types, `HoldoutSpec` (what a run used) and `effective_holdout_fraction`.
  Imports nothing from `engine`, because `engine.config` declares fields of its types.
* `assign` - membership, the explore draw, the context the actions stage reads, and the
  `holdout_assignment.parquet` table.
* `salt` - the salt, its fingerprint, and the epoch ledger in `platform_setting`.
* `flow` - the seam into the score flow (installed by `engine.pipeline`).

Nothing here imports pandas or numpy at module level, so `import engine` stays fast.
"""

from __future__ import annotations
