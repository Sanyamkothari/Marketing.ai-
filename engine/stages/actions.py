"""Actions stage (M4): risk bands, suppression, the control group and the action per row.

Plan section 6.3, `actions`, in order: assign a band, suppress the rows that must not be contacted,
hold out a control group from what is left, and give every row exactly one action. Nothing here
touches storage or the network; it is a pure function of a scored frame, the configuration and the
run id, so the same inputs always produce the same output file.

**Bands.** `ActionsConfig.band_for` is the definition: the first band whose `min_score` the score
reaches, bands ordered from the highest `min_score` down, with a floor band at `0.0` guaranteed by
the config validators. A score exactly equal to a band's `min_score` belongs to *that* band, not to
the one below it.

* A score **above 1.0** lands in the top band and a score **below 0.0** lands in the floor band
  (DEC-A1). A calibrated probability that leaves `0..1` by a rounding error is a numeric artefact,
  not new information, so it is clamped into the nearest band rather than rejected.
* A **missing or non-numeric score** is refused: :func:`assign_bands` raises `ValueError`. There is
  no honest band for a row the model did not score, and the export must not invent one
  (plan section 13.3). The message carries counts only, never a value (plan section 13.7).

**Suppression precedence.** One row can break several rules at once, and the contract records one
reason per row (`ScoreRow.suppressed_reason`, `SuppressionCount.reason`), so the rules are applied
in a fixed order and the first match wins (DEC-A2):

1. `consent_false` - the `governance.consent_column` is not truthy. Consent is the *legal basis*
   for processing the row at all, so it outranks everything else.
2. `opted_out` - the `actions.suppression.opt_out_column` is not truthy. The customer's own
   standing marketing choice, and permanent until they change it.
3. `recently_contacted` - `actions.suppression.recently_contacted_column` is within
   `recently_contacted_days` of `now`. An operational frequency cap: the weakest rule, and the only
   one that stops applying by itself as time passes.

Consequently the `SuppressionCount` rows of `scoring_summary.json` count *winning* reasons and add
up to the number of suppressed rows.

A suppressed row **keeps its score and its band** and only has its action replaced by `Suppressed`,
so the Output page can still show what the model thought of a row it is not allowed to act on.

**Missing suppression columns.** A configured suppression column may simply be absent from a
scoring file; that is `SUPPRESSION_COLUMN_MISSING`, a *warning* (DEC-030), not an error. The rule is
then skipped, and the skip is recorded on the returned frame's `attrs` under
:data:`SKIPPED_RULES_ATTR` (rule code and column name) while :data:`APPLIED_RULES_ATTR` lists only
the rules that really ran. The export stage emits a `SuppressionCount` for an applied rule that
matched nothing (`rows: 0`) and **no entry at all** for a skipped one, so "the rule found nobody"
and "the rule never ran" stay distinguishable (DEC-A3).

**Truthiness.** Consent and opt-in columns arrive as booleans, as 0/1, or as text. A value counts as
truthy when it is a non-zero number or one of `true/t/yes/y/1` (case-insensitive); everything else,
**including a null**, is falsy and therefore suppresses. An unrecorded consent is not a consent. The
recency rule is the mirror image: an unparseable or missing contact date does not prove a recent
contact, so it does not suppress. Each rule fails towards the customer.

**Control group.** The holdout is drawn from *eligible* (non-suppressed) rows only, because a row
that may not be contacted is not a treatment that could have been withheld. The size is
`round_half_up(eligible * control_group_fraction)`, capped at the number of eligible rows; a
fraction that rounds down to zero yields no control group rather than a forced minimum of one
(DEC-A4). The draw itself is a **deterministic per-row hash**, not a shuffle: each eligible row gets
`sha256(f"{seed_from(run_id)}:{primary_key}")` and the rows with the smallest digests are held out,
ties broken by the key text. That is seeded by the run id exactly as the plan requires, and it is
order-independent by construction - no sort of the data, no dependence on how many rows precede a
row, so reordering the input file cannot move a single customer in or out of the holdout. It also
makes the holdout stable per (run, customer) rather than per (run, row position). Duplicate primary
keys would break the tie-break into input order; validation refuses them (`PK_NOT_UNIQUE`).

**Per entity, when a frame holds an entity more than once** (DEC-083). A periodic dataset has one row
per customer per snapshot date, and a customer is one person whichever snapshot a row describes. With
`entity_key` set, the decisions are taken per entity: a customer suppressed at any snapshot is
suppressed at all of them, with the highest-priority reason any of their rows earned (each rule
fails towards the customer), and the holdout draws *customers* - `round_half_up(eligible customers
* control_group_fraction)` of them, by the same salted digest of the entity key - so a customer is
either in the control group at every snapshot of a run or at none. Without `entity_key` nothing
changes.

`pandas` and `numpy` are imported inside the function bodies, never at module level, so
`import engine` stays fast.
"""

from __future__ import annotations

import hashlib
import math
import time
from typing import TYPE_CHECKING, Final

from engine.utils.ids import seed_from
from engine.utils.logging import get_logger, log_stage

if TYPE_CHECKING:
    from datetime import datetime

    import pandas as pd

    from engine.config import UseCaseConfig

__all__ = [
    "ACTION_COLUMN",
    "APPLIED_RULES_ATTR",
    "BAND_COLUMN",
    "CONTROL_ACTION",
    "CONTROL_GROUP_COLUMN",
    "OUTPUT_COLUMNS",
    "SKIPPED_RULES_ATTR",
    "SUPPRESSED_ACTION",
    "SUPPRESSED_REASON_COLUMN",
    "SUPPRESSION_REASONS",
    "apply_actions",
    "assign_bands",
    "suppression_rules",
]

_LOGGER = get_logger(__name__)

BAND_COLUMN: Final[str] = "band"
ACTION_COLUMN: Final[str] = "action"
SUPPRESSED_REASON_COLUMN: Final[str] = "suppressed_reason"
CONTROL_GROUP_COLUMN: Final[str] = "control_group"

OUTPUT_COLUMNS: Final[tuple[str, ...]] = (
    BAND_COLUMN,
    ACTION_COLUMN,
    SUPPRESSED_REASON_COLUMN,
    CONTROL_GROUP_COLUMN,
)
"""The four columns :func:`apply_actions` adds to the scored frame, in order."""

SUPPRESSED_ACTION: Final[str] = "Suppressed"
CONTROL_ACTION: Final[str] = "Control (hold out)"

SUPPRESSION_REASONS: Final[tuple[str, ...]] = ("consent_false", "opted_out", "recently_contacted")
"""Suppression rule codes in precedence order; the first rule a row breaks is the one recorded."""

APPLIED_RULES_ATTR: Final[str] = "suppression_rules_applied"
"""`frame.attrs` key: the rule codes that really ran, in precedence order."""

SKIPPED_RULES_ATTR: Final[str] = "suppression_rules_skipped"
"""`frame.attrs` key: `(rule code, column name)` per rule skipped because its column was absent."""

_TRUTHY_TEXT: Final[frozenset[str]] = frozenset({"true", "t", "yes", "y", "1"})


def suppression_rules(config: UseCaseConfig) -> tuple[tuple[str, str], ...]:
    """The `(reason code, column name)` of every suppression rule this configuration turns on.

    In precedence order (:data:`SUPPRESSION_REASONS`). A rule whose column is `null`, or whose
    `suppress_*` switch is off, is not a rule at all and never appears - neither here nor in the
    scoring summary.
    """
    suppression = config.actions.suppression
    consent = config.governance.consent_column
    rules: list[tuple[str, str]] = []
    if consent is not None:
        rules.append(("consent_false", consent))
    if suppression.suppress_opted_out and suppression.opt_out_column is not None:
        rules.append(("opted_out", suppression.opt_out_column))
    if suppression.suppress_recently_contacted and suppression.recently_contacted_column is not None:
        rules.append(("recently_contacted", suppression.recently_contacted_column))
    return tuple(rules)


def assign_bands(scores: pd.Series, config: UseCaseConfig) -> pd.Series:
    """The band name of each score: the first band whose `min_score` the score reaches.

    Returns an object-dtype series named `band`, aligned to `scores.index`. Boundaries are exact
    (`score >= min_score`), scores outside `0..1` are clamped into the nearest band, and a missing
    or non-numeric score raises `ValueError` - see the module docstring.
    """
    import pandas as pd

    numeric = pd.to_numeric(scores, errors="coerce")
    unusable = int(numeric.isna().sum())
    if unusable:
        raise ValueError(
            f"{unusable} of {len(numeric)} rows have no usable numeric score, so they cannot be "
            f"banded. Every scored row needs a value in {config.actions.score_field!r}."
        )
    bands = config.actions.bands
    names = pd.Series(bands[-1].name, index=scores.index, dtype="object", name=BAND_COLUMN)
    for band in reversed(bands):  # ascending min_score, so the highest matching band wins
        names[numeric >= band.min_score] = band.name
    return names


def apply_actions(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
    primary_key: str,
    entity_key: str | None = None,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Add band, action and suppression reason to every scored row, seeded from the run id.

    Returns a copy of `frame` with :data:`OUTPUT_COLUMNS` added (any same-named input column is
    replaced) and every other column kept, so the KPI formula can still sum a source column. `now`
    is the reference time of the recency rule and defaults to the current UTC time; pass the run's
    start time to make a run reproducible end to end.
    """
    from engine.utils.time import utc_now

    started = time.perf_counter()
    score_field = config.actions.score_field
    missing = [
        name
        for name in (primary_key, score_field, *([entity_key] if entity_key else []))
        if name not in frame.columns
    ]
    if missing:
        raise ValueError(
            f"The scored frame is missing {', '.join(repr(name) for name in missing)}; "
            f"actions needs the primary key and the score column."
        )

    result = frame.copy()
    bands = assign_bands(result[score_field], config)
    band_action = {band.name: band.action for band in config.actions.bands}
    actions = bands.map(band_action).astype("object")

    reasons, applied, skipped = _suppression(result, config, now=utc_now() if now is None else now)
    if entity_key is not None:
        reasons = _per_entity_reasons(reasons, result[entity_key], config)
    suppressed = reasons.notna()
    actions[suppressed] = SUPPRESSED_ACTION

    if entity_key is None:
        control = _control_mask(
            result[primary_key],
            ~suppressed,
            run_id=run_id,
            fraction=config.actions.control_group_fraction,
        )
    else:
        control = _entity_control_mask(
            result[entity_key],
            ~suppressed,
            run_id=run_id,
            fraction=config.actions.control_group_fraction,
        )
    actions[control] = CONTROL_ACTION

    result[BAND_COLUMN] = bands
    result[ACTION_COLUMN] = actions
    result[SUPPRESSED_REASON_COLUMN] = reasons
    result[CONTROL_GROUP_COLUMN] = control
    result.attrs = {**frame.attrs, APPLIED_RULES_ATTR: applied, SKIPPED_RULES_ATTR: skipped}

    _LOGGER.info(
        "actions suppression_rules_applied=%s suppression_rules_skipped=%s suppressed_rows=%d control_rows=%d",
        ",".join(applied) or "-",
        ",".join(code for code, _ in skipped) or "-",
        int(suppressed.sum()),
        int(control.sum()),
    )
    log_stage(_LOGGER, "actions", rows=len(result), seconds=time.perf_counter() - started)
    return result


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------
def _suppression(
    frame: pd.DataFrame,
    config: UseCaseConfig,
    *,
    now: datetime,
) -> tuple[pd.Series, tuple[str, ...], tuple[tuple[str, str], ...]]:
    """The winning reason per row, the rules that ran and the rules skipped for a missing column."""
    import pandas as pd

    # A list of `None`, not the scalar `None`: a scalar would give a column of NaN, and the contract
    # wants a real null the JSON and the Parquet file can both carry.
    reasons = pd.Series([None] * len(frame), index=frame.index, dtype="object", name=SUPPRESSED_REASON_COLUMN)
    applied: list[str] = []
    skipped: list[tuple[str, str]] = []
    for code, column in suppression_rules(config):
        if column not in frame.columns:
            skipped.append((code, column))  # SUPPRESSION_COLUMN_MISSING (warning, DEC-030)
            continue
        applied.append(code)
        if code == "recently_contacted":
            broken = _recently_contacted(
                frame[column], days=config.actions.suppression.recently_contacted_days, now=now
            )
        else:
            broken = ~_truthy(frame[column])
        reasons[broken & reasons.isna()] = code
    return reasons, tuple(applied), tuple(skipped)


def _truthy(values: pd.Series) -> pd.Series:
    """A boolean series: which values count as true for a consent or opt-in column (nulls do not)."""
    import pandas as pd

    if pd.api.types.is_bool_dtype(values.dtype):
        return values.fillna(value=False).astype(bool)
    numeric = pd.to_numeric(values, errors="coerce")
    from_number = numeric.notna() & (numeric != 0)
    text = values.astype("string").str.strip().str.lower()
    from_text = text.isin(_TRUTHY_TEXT).fillna(value=False)
    return (from_number | from_text).astype(bool)


def _recently_contacted(values: pd.Series, *, days: int, now: datetime) -> pd.Series:
    """A boolean series: which contact timestamps are strictly less than `days` old at `now`.

    Unparseable and missing timestamps are not recent contacts, so they never suppress. A timestamp
    in the future is treated as a contact that has just happened. Naive timestamps are read as UTC.
    """
    import pandas as pd

    stamps = pd.to_datetime(values, errors="coerce", utc=True)
    reference = pd.Timestamp(now)
    reference = reference.tz_localize("UTC") if reference.tzinfo is None else reference.tz_convert("UTC")
    age = reference - stamps
    return (stamps.notna() & (age < pd.Timedelta(days=days))).astype(bool)


# ---------------------------------------------------------------------------
# Control group
# ---------------------------------------------------------------------------
def _control_mask(
    keys: pd.Series,
    eligible: pd.Series,
    *,
    run_id: str,
    fraction: float,
) -> pd.Series:
    """A boolean series marking the holdout: the `fraction` of eligible rows with the lowest digest."""
    import numpy as np
    import pandas as pd

    chosen = np.zeros(len(keys), dtype=bool)
    positions = np.flatnonzero(eligible.to_numpy(dtype=bool))
    take = _holdout_size(len(positions), fraction)
    if take:
        salt = f"{seed_from(run_id)}:"
        values = keys.to_numpy()
        texts = [(int(p), "" if pd.isna(values[p]) else str(values[p])) for p in positions.tolist()]
        ranked = sorted((_digest(salt + text), text, position) for position, text in texts)
        for _, _, position in ranked[:take]:
            chosen[position] = True
    return pd.Series(chosen, index=keys.index, name=CONTROL_GROUP_COLUMN)


def _per_entity_reasons(reasons: pd.Series, entities: pd.Series, config: UseCaseConfig) -> pd.Series:
    """Every row of an entity takes the highest-priority reason any of that entity's rows earned."""
    import pandas as pd

    from engine.keys import key_text

    codes = [code for code, _ in suppression_rules(config)]
    if not codes or not bool(reasons.notna().any()):
        return reasons
    rank = reasons.map({code: position for position, code in enumerate(codes)}).astype("Float64")
    best = rank.groupby(key_text(entities).to_numpy(), sort=False).transform("min")
    values = [None if pd.isna(value) else codes[int(value)] for value in best.tolist()]
    return pd.Series(values, index=reasons.index, dtype="object", name=SUPPRESSED_REASON_COLUMN)


def _entity_control_mask(
    entities: pd.Series,
    eligible: pd.Series,
    *,
    run_id: str,
    fraction: float,
) -> pd.Series:
    """The holdout drawn over entities: every row of a chosen entity, and nothing else.

    Eligibility is already per entity (`_per_entity_reasons`), so an entity's rows are either all
    eligible or none are; the draw ranks the distinct eligible entities by the salted digest.
    """
    import numpy as np
    import pandas as pd

    from engine.keys import key_text

    texts = key_text(entities)
    mask = eligible.to_numpy(dtype=bool)
    pool = sorted(set(texts[mask].tolist()))
    take = _holdout_size(len(pool), fraction)
    chosen = np.zeros(len(texts), dtype=bool)
    if take:
        salt = f"{seed_from(run_id)}:"
        ranked = sorted((_digest(salt + text), text) for text in pool)
        picked = {text for _, text in ranked[:take]}
        chosen = texts.isin(picked).to_numpy(dtype=bool) & mask
    return pd.Series(chosen, index=entities.index, name=CONTROL_GROUP_COLUMN)


def _holdout_size(eligible: int, fraction: float) -> int:
    """How many eligible rows to hold out: the fraction rounded half up, capped at what exists."""
    if eligible <= 0 or fraction <= 0.0:
        return 0
    return min(eligible, math.floor(eligible * fraction + 0.5))


def _digest(text: str) -> int:
    """A stable 64-bit draw for one salted primary key; the smallest draws form the holdout."""
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
