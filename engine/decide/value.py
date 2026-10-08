"""Expected gross value of a propensity scoring run: `p × value × margin × horizon − cost` (Plan J M97).

A propensity model predicts who will convert, not who converts *because* of a contact, so the money it
implies is **not incremental**: a customer who would have bought anyway carries their full value here.
It is still useful - to size a list, to compare it with the uplift view - as long as it is never read
as what a campaign causes. So it is labelled "not incremental" everywhere it appears, and it exists
only when a use case opts in (DEC-1307 (g)):

* only on the **propensity** scoring path (`engine.pipeline._ScoreFlow`; an uplift run has its own,
  incremental, money in `policy_recommendation.json`);
* only when `uplift.policy.value_column` is set and the uploaded file carries that column.

With the default configuration nothing here runs: no file is written and no metric is added, so a
scoring run is byte-identical to one before M97.

**Where it goes.** `expected_gross_value.json` (:class:`ExpectedGrossValue`, aggregate only: counts and
rupee totals, overall and per band) beside the run's other artefacts, and two run-manifest metrics,
`expected_gross_value_not_incremental` and `expected_gross_value_rows_missing`. `scores.csv` is not
changed (its columns are Phase 1's contract); a per-customer figure is :func:`expected_gross_values` of
the scored rows and the uploaded values, for a later reader such as the treat list (M98) to call.

**The arithmetic.** Per row, `p × value × margin × horizon − (contact cost + offer cost × p)`: the same
margin, horizon and costs as the uplift policy's net value (`engine.uplift.policy.customer_net_values`),
the contact cost being `uplift.policy.cost_per_contact`, else the `value:` block of
`configs/pilot/value.yaml` (`engine.pilot.roi.lookup_value_costs`). A missing or non-numeric value is
never filled in: the row has no gross value, and it is counted.

**The seam.** :func:`install_gross_value` wraps `_ScoreFlow._bodies` once, at import, from the Plan J
block of `engine/pipeline.py` - the same seam the holdout service uses - and adds the step after the
export stage. With the option off the stage table is returned untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import Field

from engine.contracts import Artefact
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np
    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.pilot.roi import ValueCosts

__all__ = [
    "EXPECTED_GROSS_VALUE_FILENAME",
    "GROSS_VALUE_METRIC",
    "GROSS_VALUE_MISSING_METRIC",
    "NOT_INCREMENTAL",
    "BandGrossValue",
    "ExpectedGrossValue",
    "expected_gross_value_report",
    "expected_gross_values",
    "install_gross_value",
]

_LOGGER = get_logger(__name__)

EXPECTED_GROSS_VALUE_FILENAME: Final[str] = "expected_gross_value.json"
NOT_INCREMENTAL: Final[Literal["not incremental"]] = "not incremental"
GROSS_VALUE_METRIC: Final[str] = "expected_gross_value_not_incremental"
GROSS_VALUE_MISSING_METRIC: Final[str] = "expected_gross_value_rows_missing"
_INSTALLED: Final[str] = "_gross_value_installed"

GROSS_VALUE_NOTE: Final[str] = (
    "Expected gross value is the predicted chance of converting × the customer's value, less the cost "
    "of contacting them. It is not incremental: it counts customers who would have converted without "
    "any contact, so it is not what a campaign causes. Only an uplift model, measured against a "
    "control group, says that."
)


class BandGrossValue(Artefact):
    """Expected gross value of one band's contactable rows (not incremental)."""

    band: str = Field(description="The band, as `scores.csv` names it.")
    rows: int = Field(description="Contactable rows in the band (neither suppressed nor held out).")
    rows_valued: int = Field(description="Of those, rows whose value is known.")
    total: float = Field(description="Sum of their expected gross value, in rupees (not incremental).")


class ExpectedGrossValue(Artefact):
    """`expected_gross_value.json`: what a propensity list is worth before anyone asks what it causes."""

    run_id: str = Field(description="Scoring run the figures belong to.")
    label: Literal["not incremental"] = Field(
        default=NOT_INCREMENTAL, description="Always 'not incremental': read it as a size, not an effect."
    )
    value_column: str = Field(description="The uploaded column the value was read from.")
    score_field: str = Field(description="The predicted probability the value was weighted by.")
    contact_cost: float = Field(description="Rupees per contact used.")
    offer_cost: float = Field(description="Rupees per offer taken used (× the predicted probability).")
    margin_pct: float | None = Field(description="Margin applied, in percent; null is 100 %.")
    horizon_months: int | None = Field(description="Months of value counted; null is one.")
    rows: int = Field(description="Rows scored.")
    contactable_rows: int = Field(description="Rows neither suppressed nor held out as control.")
    rows_missing_value: int = Field(
        description="Rows whose value is missing or not a number: no gross value, never filled in."
    )
    total: float = Field(
        description="Sum of the contactable rows' expected gross value, in rupees (not incremental)."
    )
    by_band: tuple[BandGrossValue, ...] = Field(description="The same, band by band, in file order.")
    note: str = Field(default=GROSS_VALUE_NOTE, description="What the number is, and what it is not.")


def expected_gross_values(
    probability: np.ndarray,
    values: np.ndarray,
    config: UseCaseConfig,
    *,
    value_costs: ValueCosts | None = None,
) -> np.ndarray:
    """Per row `p × value × margin × horizon − (contact cost + offer cost × p)`; NaN where the value is
    missing or not a number (never filled in). Not incremental."""
    import numpy as np

    if value_costs is None:
        from engine.pilot.roi import lookup_value_costs

        value_costs = lookup_value_costs()
    policy = config.uplift.policy
    p = np.asarray(probability, dtype=np.float64)
    v = np.asarray(values, dtype=np.float64)
    margin = 1.0 if policy.margin_pct is None else policy.margin_pct / 100.0
    factor = margin * (1 if policy.horizon_months is None else policy.horizon_months)
    contact = policy.cost_per_contact if policy.cost_per_contact is not None else value_costs.contact_cost
    gross: np.ndarray = p * v * factor - (contact + value_costs.offer_cost * p)
    return np.where(np.isfinite(v), gross, np.nan)


def expected_gross_value_report(
    banded: pd.DataFrame,
    uploaded: pd.DataFrame,
    config: UseCaseConfig,
    *,
    run_id: str,
    row_key: str,
    value_costs: ValueCosts | None = None,
) -> ExpectedGrossValue | None:
    """The report for a propensity run's banded rows, or `None` when the option is off or unusable.

    `banded` is the actions stage's output (score, band, suppression and control columns); `uploaded`
    is the rows as uploaded, joined by `row_key` so the value is the client's own, not a replayed
    feature. `None` when `uplift.policy.value_column` is unset, the upload lacks it, or the rows cannot
    be joined by key (then a warning says so; the run itself is never failed for it).
    """
    import numpy as np
    import pandas as pd

    from engine.stages.actions import BAND_COLUMN, CONTROL_GROUP_COLUMN, SUPPRESSED_REASON_COLUMN

    column = config.uplift.policy.value_column
    if column is None or column not in uploaded.columns:
        return None
    if row_key not in uploaded.columns or row_key not in banded.columns:
        _LOGGER.warning("expected_gross_value: no %r column to join the uploaded values by", row_key)
        return None
    keys = uploaded[row_key].astype(str)
    if keys.duplicated().any():
        _LOGGER.warning("expected_gross_value: the uploaded rows repeat a key; no gross value is written")
        return None
    if value_costs is None:
        from engine.pilot.roi import lookup_value_costs

        value_costs = lookup_value_costs()
    lookup = pd.Series(
        pd.to_numeric(uploaded[column], errors="coerce").to_numpy(dtype=np.float64), index=keys
    )
    values = lookup.reindex(banded[row_key].astype(str)).to_numpy(dtype=np.float64)
    score_field = config.actions.score_field
    probability = pd.to_numeric(banded[score_field], errors="coerce").to_numpy(dtype=np.float64)
    gross = expected_gross_values(probability, values, config, value_costs=value_costs)
    contactable = banded[SUPPRESSED_REASON_COLUMN].isna().to_numpy(dtype=bool) & ~banded[
        CONTROL_GROUP_COLUMN
    ].astype(bool).to_numpy(dtype=bool)
    valued = np.isfinite(gross)
    bands = banded[BAND_COLUMN].astype(str).to_numpy(dtype=object)
    by_band = tuple(
        BandGrossValue(
            band=str(band),
            rows=int((contactable & (bands == band)).sum()),
            rows_valued=int((contactable & valued & (bands == band)).sum()),
            total=float(gross[contactable & valued & (bands == band)].sum()),
        )
        for band in dict.fromkeys(bands.tolist())
    )
    policy = config.uplift.policy
    return ExpectedGrossValue(
        run_id=run_id,
        value_column=column,
        score_field=score_field,
        contact_cost=(
            policy.cost_per_contact if policy.cost_per_contact is not None else value_costs.contact_cost
        ),
        offer_cost=value_costs.offer_cost,
        margin_pct=policy.margin_pct,
        horizon_months=policy.horizon_months,
        rows=len(banded.index),
        contactable_rows=int(contactable.sum()),
        rows_missing_value=int((~valued).sum()),
        total=float(gross[contactable & valued].sum()),
        by_band=by_band,
    )


def install_gross_value(score_flow: type[Any]) -> None:
    """Wrap `score_flow._bodies` so a propensity run that opts in writes its expected gross value.

    Idempotent. The wrapper returns the stage table untouched for an uplift scoring run and whenever
    `uplift.policy.value_column` is unset, so the default run is unchanged.
    """
    if getattr(score_flow, _INSTALLED, False):
        return
    original: Callable[[Any], tuple[tuple[Any, Callable[[], Any]], ...]] = score_flow._bodies

    def _bodies(self: Any) -> tuple[tuple[Any, Callable[[], Any]], ...]:
        bodies = original(self)
        config = getattr(getattr(self, "_ctx", None), "config", None)
        if config is None or getattr(getattr(config, "uplift", None), "policy", None) is None:
            return bodies
        if config.uplift.policy.value_column is None or _is_uplift_flow(self):
            return bodies
        return tuple((key, _after_export(self, key, body)) for key, body in bodies)

    score_flow._bodies = _bodies
    setattr(score_flow, _INSTALLED, True)


def _is_uplift_flow(flow: Any) -> bool:
    from engine.uplift.flow import UpliftScoreFlow

    return isinstance(flow, UpliftScoreFlow)


def _after_export(flow: Any, key: Any, body: Callable[[], Any]) -> Callable[[], Any]:
    from functools import wraps

    from engine.contracts import StageKey

    if key is not StageKey.EXPORT:
        return body

    @wraps(body)
    def exported() -> Any:
        outcome = body()
        _write_report(flow)
        return outcome

    return exported


def _write_report(flow: Any) -> None:
    ctx = flow._ctx
    if flow._scored is None or flow._frame is None:
        return
    report = expected_gross_value_report(
        flow._scored, flow._frame, ctx.config, run_id=ctx.run_id, row_key=ctx.row_key
    )
    if report is None:
        return
    flow._write(EXPECTED_GROSS_VALUE_FILENAME, report)
    flow._manifest.add_metrics(
        {GROSS_VALUE_METRIC: report.total, GROSS_VALUE_MISSING_METRIC: float(report.rows_missing_value)}
    )
