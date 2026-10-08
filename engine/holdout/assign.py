"""Holdout membership, the explore slice and `holdout_assignment.parquet`.

**Membership (DEC-1302 (a)).** A customer is in a persistent holdout when

    int(sha256(f"{salt}:{scope_key}:{entity}")[:16], 16) < fraction * 2**64

The left side is the first 64 bits of the digest (:func:`draw`), so each customer gets one uniform
number in `[0, 2**64)` that depends on the salt, the scope and their own key only. That makes the
rule:

* **per customer, over the whole population**: a customer's membership does not depend on who else
  is in the file, on which rows are eligible this month, or on the order of the rows;
* **the same in every run**: no run id goes into the hash, so monthly re-scoring holds out the same
  customers, and a propensity run and an uplift run of the same customers agree;
* **nested**: the members at 5% are exactly the customers whose number is below `0.05 * 2**64`,
  all of whom are also below `0.10 * 2**64`.

The share is binomial around `fraction`, not exact: on 10,000 eligible customers at 10% it is
within about ±1.0 point 99.9% of the time (`3.29 × sqrt(0.1 × 0.9 / 10,000)`). That is the price of a membership that never moves.
The actions stage's control group under a persistent scope is `eligible ∧ member`: a suppressed
member is still a member (and is recorded as one), it just was never going to be contacted.

`entity` is the key as text (`engine.keys.key_text`, so account `1` is `"1"` whether the column
arrived as integers or as floats with a gap), and under a two-column key it is the entity column
alone: one membership per customer, at every snapshot.

**The context (why a context variable).** The actions stage's `_control_mask` and
`_entity_control_mask` are the only stage functions Plan J may edit (protocol §3 as amended), and
they are handed keys, eligibility, the run id and a fraction - not the configuration. The score
flow therefore resolves the holdout (salt, scope key, fraction) before the stage runs and makes it
*active* for the duration of the stage (:func:`holdout_context`); the two functions ask
:func:`active_holdout` and delegate here when one is active. Nothing is active by default, so every
caller that does not opt in - every existing test, every run under `scope: run` - draws exactly as
before. The flow then re-derives membership from the keys and checks the stage's control group
against it (`assignment_frame`), so a caller that forgot the context cannot pass silently.

**The explore slice (DEC-1302 (c)).** Rows that are eligible, not holdout members, not selected and
not a predicted sleeping dog are *candidates*; each candidate customer is explored when

    int(sha256(f"{salt}:explore:{seed_from(run_id)}:{entity}")[:16], 16) < explore_fraction * 2**64

A second label (`explore`) keeps the draw independent of membership, and the run seed re-draws it
every cycle so the same customers are not explored month after month. "Selected" is decided before
the holdout: on an uplift run the policy's `intended_treatment` rows (its `Treat` rows plus the
held-out rows it would have treated), and on a propensity run every band but the lowest (the lowest
band is the "rest"). The scored action of an explore row is not changed in `scores.*`: the hand-off
file reads the flag from here. Use-case ids `universal` and `explore` cannot take `scope: use_case`
(`RESERVED_SCOPE_KEYS`), so a membership text never shares a prefix with the universal holdout's or
with an explore draw.

**Two probabilities, for two questions.**

* `explore_probability` is `P(explored | candidate)`: `explore_fraction` for a candidate, 0 for
  everyone else. It describes the explore draw alone.
* `treatment_probability` is the **logging propensity** `P(treated | x)` that
  `engine.uplift.ope.evaluate_policy` needs, with `treated` (the column beside it) as the logged
  action: selected and not held out, or explored. With `h` the effective holdout fraction it is, ex
  ante (before membership is known, so held-out rows keep theirs):

  - `1 - h` for an eligible selected row (treated unless held out: the per-run draw under `run`, the
    salted hash - a Bernoulli(h) draw per customer - under a persistent scope);
  - `(1 - h) × explore_fraction` for an eligible, non-selected row that is not a predicted sleeping
    dog (an explore candidate unless held out, then explored at `explore_fraction`);
  - 0 for a suppressed row, a predicted sleeping dog, and a non-selected row while there is no
    explore slice: nothing could have treated them.

  Only rows with `0 < treatment_probability < 1` carry information about both actions;
  :func:`ope_rows` keeps exactly those, and `evaluate_policy(t=treated, propensity=
  treatment_probability, ...)` accepts them as they are. Under a two-column key "selected" and
  "candidate" are decided per row and per customer respectively, as the stage does.

`pandas` and `numpy` are imported inside the function bodies.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt
    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.contracts import PrimaryKey
    from engine.holdout.spec import HoldoutSpec
    from engine.storage import Storage

    BoolArray = npt.NDArray[np.bool_]

__all__ = [
    "ASSIGNMENT_COLUMNS",
    "EXPLORE_LABEL",
    "HOLDOUT_ASSIGNMENT_FILENAME",
    "TREATED_COLUMN",
    "TREATMENT_PROBABILITY_COLUMN",
    "ActiveHoldout",
    "Assignment",
    "active_holdout",
    "assignment_frame",
    "draw",
    "explore_flags",
    "holdout_context",
    "member_flags",
    "ope_rows",
    "persistent_control_mask",
    "run_holdout_spec",
    "selection_masks",
    "treated_flags",
]

HOLDOUT_ASSIGNMENT_FILENAME: Final[str] = "holdout_assignment.parquet"
"""`runs/<run_id>/holdout_assignment.parquet`: one row per scored row, suppressed rows included."""

HOLDOUT_MEMBER_COLUMN: Final[str] = "holdout_member"
EXPLORE_COLUMN: Final[str] = "explore"
EXPLORE_PROBABILITY_COLUMN: Final[str] = "explore_probability"
TREATED_COLUMN: Final[str] = "treated"
"""The logged action for off-policy evaluation: selected and not held out, or explored."""
TREATMENT_PROBABILITY_COLUMN: Final[str] = "treatment_probability"
"""`P(treated | x)`, the logging propensity `engine.uplift.ope.evaluate_policy` takes."""
ASSIGNMENT_COLUMNS: Final[tuple[str, ...]] = (
    HOLDOUT_MEMBER_COLUMN,
    EXPLORE_COLUMN,
    EXPLORE_PROBABILITY_COLUMN,
    TREATED_COLUMN,
    TREATMENT_PROBABILITY_COLUMN,
)
"""The columns after the key column(s), in order."""

EXPLORE_LABEL: Final[str] = "explore"
"""The second salt label: the explore draw never reuses the membership hash."""

_TWO_64: Final[float] = float(2**64)
_SEGMENT_COLUMN: Final[str] = "segment"
_SLEEPING_DOG: Final[str] = "sleeping_dog"
_TREAT_ACTION: Final[str] = "Treat"  # engine.uplift.contracts.SEGMENT_ACTIONS[PERSUADABLE]
_INTENDED_COLUMN: Final[str] = "intended_treatment"  # engine.uplift.actions.INTENDED_TREATMENT_COLUMN


@dataclass(frozen=True)
class ActiveHoldout:
    """A resolved persistent holdout, as the actions stage needs it. The salt never appears in a repr."""

    salt: str = field(repr=False)
    scope_key: str
    fraction: float


_ACTIVE: ContextVar[ActiveHoldout | None] = ContextVar("marketing_ai_active_holdout", default=None)


def active_holdout() -> ActiveHoldout | None:
    """The persistent holdout the current actions stage must use, or None for today's per-run draw."""
    return _ACTIVE.get()


@contextmanager
def holdout_context(active: ActiveHoldout | None) -> Iterator[None]:
    """Make `active` the holdout of the actions stage run inside the block (None: today's draw)."""
    token = _ACTIVE.set(active)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def draw(text: str) -> int:
    """The first 64 bits of `sha256(text)`: `int(hexdigest[:16], 16)`, uniform in `[0, 2**64)`."""
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def member_flags(entities: Sequence[str], *, salt: str, scope_key: str, fraction: float) -> BoolArray:
    """One flag per entity text: `draw(f"{salt}:{scope_key}:{entity}") < fraction * 2**64`."""
    import numpy as np

    limit = fraction * _TWO_64
    seen: dict[str, bool] = {}
    flags = np.zeros(len(entities), dtype=bool)
    for position, entity in enumerate(entities):
        flag = seen.get(entity)
        if flag is None:
            flag = draw(f"{salt}:{scope_key}:{entity}") < limit
            seen[entity] = flag
        flags[position] = flag
    return flags


def persistent_control_mask(
    keys: pd.Series, eligible: pd.Series, active: ActiveHoldout, *, name: str
) -> pd.Series:
    """The control group under a persistent scope: eligible rows whose customer is a member."""
    import pandas as pd

    from engine.keys import key_text

    members = member_flags(
        key_text(keys).tolist(), salt=active.salt, scope_key=active.scope_key, fraction=active.fraction
    )
    return pd.Series(members & eligible.to_numpy(dtype=bool), index=keys.index, name=name)


def _every_row(entities: Sequence[str], flags: BoolArray) -> tuple[BoolArray, dict[str, bool]]:
    """Per row, whether every row of its entity has the flag; and the same per entity."""
    import numpy as np

    by_entity: dict[str, bool] = {}
    for entity, flag in zip(entities, flags.tolist(), strict=True):
        by_entity[entity] = by_entity.get(entity, True) and bool(flag)
    return np.array([by_entity[entity] for entity in entities], dtype=bool), by_entity


def explore_flags(
    entities: Sequence[str], candidate: BoolArray, *, salt: str, run_seed: int, fraction: float
) -> tuple[BoolArray, BoolArray]:
    """`(explore, candidate per entity)`: an entity is a candidate only when every one of its rows is."""
    import numpy as np

    eligible, by_entity = _every_row(entities, candidate)
    if fraction <= 0.0:
        return np.zeros(len(entities), dtype=bool), eligible
    limit = fraction * _TWO_64
    drawn: dict[str, bool] = {
        entity: draw(f"{salt}:{EXPLORE_LABEL}:{run_seed}:{entity}") < limit
        for entity, ok in by_entity.items()
        if ok
    }
    flags = np.array([drawn.get(entity, False) for entity in entities], dtype=bool)
    return flags, eligible


def selection_masks(banded: pd.DataFrame, config: UseCaseConfig) -> tuple[BoolArray, BoolArray]:
    """`(selected, sleeping)` per row of the actions stage's output, decided before the holdout.

    On an uplift run (a `segment` column) `selected` is the policy's `intended_treatment` (its `Treat`
    rows plus the held-out rows it would have treated), else its `action` being `Treat`, and `sleeping`
    marks a predicted sleeping dog. On a propensity run `selected` is every band but the lowest (the
    "rest") and nothing is a sleeping dog. This is the one definition of "selected": the assignment
    uses it, and so does the treat list (M98) for a row the assignment does not cover.
    """
    import numpy as np

    from engine.stages.actions import ACTION_COLUMN, BAND_COLUMN

    if _SEGMENT_COLUMN in banded.columns:
        segments = banded[_SEGMENT_COLUMN].astype("object").to_numpy()
        sleeping = segments == _SLEEPING_DOG
        if _INTENDED_COLUMN in banded.columns:  # selected before the holdout: Treat, or held out from it
            selected = banded[_INTENDED_COLUMN].to_numpy(dtype=bool)
        else:
            selected = banded[ACTION_COLUMN].astype("object").to_numpy() == _TREAT_ACTION
    else:
        sleeping = np.zeros(len(banded.index), dtype=bool)
        floor = config.actions.bands[-1].name
        selected = banded[BAND_COLUMN].astype("object").to_numpy() != floor
    return selected, sleeping


def treated_flags(
    eligible: BoolArray, selected: BoolArray, control: BoolArray, explore: BoolArray
) -> BoolArray:
    """The logged action: eligible, selected and not in the control group, or explored."""
    treated: BoolArray = (eligible & selected & ~control) | explore
    return treated


@dataclass(frozen=True)
class Assignment:
    """`holdout_assignment.parquet`'s table and the counts the run's summary reports."""

    table: pd.DataFrame
    members: int
    explored: int
    candidates: int


def assignment_frame(
    banded: pd.DataFrame,
    config: UseCaseConfig,
    *,
    primary_key: PrimaryKey,
    row_key: str,
    entity_key: str | None,
    run_id: str,
    active: ActiveHoldout | None,
    explore_fraction: float,
    key_source: pd.DataFrame | None = None,
) -> Assignment:
    """One row per row of `banded` (the actions stage's output): key, then `ASSIGNMENT_COLUMNS`.

    `active` is the persistent holdout the stage ran under, or None under `scope: run`, where a
    member is a row of today's control group. Under a persistent scope membership is re-derived from
    the keys and the stage's control group must equal `eligible ∧ member` row for row, or this raises
    `RuntimeError`: the file would otherwise record a holdout the scores do not have. It also raises
    when an explore row is a predicted sleeping dog, which the candidate rule rules out, or when a
    treated row would be recorded with no chance of treatment.
    """
    import numpy as np
    import pandas as pd

    from engine.holdout.spec import effective_holdout_fraction
    from engine.keys import key_text
    from engine.stages.actions import CONTROL_GROUP_COLUMN, SUPPRESSED_REASON_COLUMN
    from engine.stages.export import _key_output
    from engine.utils.ids import seed_from

    entities = key_text(banded[entity_key if entity_key is not None else row_key]).tolist()
    eligible = banded[SUPPRESSED_REASON_COLUMN].isna().to_numpy(dtype=bool)
    control = banded[CONTROL_GROUP_COLUMN].to_numpy(dtype=bool)
    if active is None:
        member = control.copy()
    else:
        member = member_flags(
            entities, salt=active.salt, scope_key=active.scope_key, fraction=active.fraction
        )
        if not np.array_equal(control, member & eligible):
            raise RuntimeError(
                "The actions stage's control group is not the persistent holdout's eligible members; "
                "refusing to record a holdout the scores do not carry."
            )

    selected, sleeping = selection_masks(banded, config)
    explorable_rows = eligible & ~selected & ~sleeping
    candidate = explorable_rows & ~member

    seed = seed_from(run_id)
    explore_salt = active.salt if active is not None else str(seed)
    explore, candidates = explore_flags(
        entities, candidate, salt=explore_salt, run_seed=seed, fraction=explore_fraction
    )
    if bool((explore & sleeping).any()):
        raise RuntimeError("A sleeping dog was put in the explore slice; refusing to record it.")
    probability = np.where(candidates, float(explore_fraction), 0.0).astype(np.float64)

    held_out = active.fraction if active is not None else effective_holdout_fraction(config.actions)
    explorable, _ = _every_row(entities, explorable_rows)
    treated = treated_flags(eligible, selected, control, explore)
    treatment_probability = np.where(
        eligible & selected & ~sleeping,
        1.0 - held_out,
        np.where(explorable, (1.0 - held_out) * float(explore_fraction), 0.0),
    ).astype(np.float64)
    if bool((treated & sleeping).any()) or bool((treated & (treatment_probability <= 0.0)).any()):
        raise RuntimeError(
            "A treated row has no chance of treatment (or is a predicted sleeping dog); refusing to record it."
        )

    data: dict[str, pd.Series] = {
        **_key_output(banded, primary_key, key_source=key_source),
        HOLDOUT_MEMBER_COLUMN: pd.Series(member, index=banded.index, dtype=bool),
        EXPLORE_COLUMN: pd.Series(explore, index=banded.index, dtype=bool),
        EXPLORE_PROBABILITY_COLUMN: pd.Series(probability, index=banded.index, dtype="float64"),
        TREATED_COLUMN: pd.Series(treated, index=banded.index, dtype=bool),
        TREATMENT_PROBABILITY_COLUMN: pd.Series(treatment_probability, index=banded.index, dtype="float64"),
    }
    table = pd.DataFrame(data).reset_index(drop=True)
    return Assignment(
        table=table,
        members=int(member.sum()),
        explored=int(explore.sum()),
        candidates=int(candidates.sum()),
    )


def ope_rows(table: pd.DataFrame) -> pd.DataFrame:
    """The rows of a `holdout_assignment.parquet` table off-policy evaluation can use.

    Those whose `treatment_probability` is strictly inside `(0, 1)`: a row that could only ever get
    one action says nothing about the other, and `evaluate_policy` refuses it. Pass the result's
    `treated` as `t` and its `treatment_probability` as `propensity`.
    """
    probability = table[TREATMENT_PROBABILITY_COLUMN]
    return table.loc[(probability > 0.0) & (probability < 1.0)]


def run_holdout_spec(storage: Storage, run_id: str) -> HoldoutSpec | None:
    """The holdout a scoring run used (`holdout_assignment.json`'s spec), or None when it wrote none.

    Only an engaged run writes the file (DEC-1302 (e)); a default run drew its control group per run,
    which a reader treats as scope `run`. Read by the campaign record (M94) to fix the run's epoch.
    """
    from engine.holdout.spec import HOLDOUT_REPORT_FILENAME, HoldoutAssignmentReport
    from engine.storage import StorageError, run_key

    try:
        report = storage.read_model(run_key(run_id, HOLDOUT_REPORT_FILENAME), HoldoutAssignmentReport)
    except StorageError:
        return None
    return report.spec
