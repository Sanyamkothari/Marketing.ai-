"""Who was actually contacted: the contact file, contamination and the complier-adjusted effect (Plan J M103, DEC-1313).

A campaign is measured on who it was **meant** for: the customers assigned to be contacted against the
ones held back (intent to treat, `engine.uplift.incrementality`). That is the primary result and it is
never replaced. What a send log adds is the other half of the story: of the customers meant to be
contacted, how many were, and how many of the held-back customers were contacted anyway. This module
turns an uploaded **contact file** (one row per customer: the id and whether they were contacted) into
that readout. It is the manual version of the send-log reconciliation the integration phase will
automate; nothing here writes into a client's systems.

**Contact rate and contamination (exact counts).** Inside the population the campaign is measured on
(`intended`, in the treated or held-back group), a customer the file lists as contacted or not is
*listed*; one it does not list is *not listed*, and their status is **unknown**, never guessed. The
**contact rate** is contacted / listed among the customers meant to be contacted; the **contamination**
is contacted / listed among the held-back ones. Both are plain fractions of counted customers, reported
with the counts, so an injected rate can be recovered exactly. With `unlisted_customers="not_contacted"`
(a send log that lists only the customers it sent to) a customer the file does not list counts as not
contacted, and the readout says it was read that way. A rate over nobody is null with a plain reason.

**The complier-adjusted effect (secondary).** The effect on customers who really were contacted is the
intent-to-treat difference divided by the difference in contact rates (the Wald ratio, an instrumental
variable estimate with the random assignment as the instrument):

    effect on the contacted  =  (treated mean - held-back mean) / (treated contact rate - held-back contact rate)

It is **labelled secondary** everywhere: it answers a different question from the main result (what the
campaign did to the customers it reached), and it rests on two assumptions the main result does not -
that being assigned to the campaign changed the outcome only through being contacted, and that nobody
was contacted *because* they were held back. The interval is Fieller's (1954), not the delta method's:
when few customers were contacted the ratio's sampling distribution is badly skewed and the delta-method
interval covers far less than 95%. Fieller's interval solves

    (dy - t dd)^2 <= z^2 Var(dy - t dd)       for the effect t,

with `dy` the outcome difference and `dd` the contact-rate difference between the two groups, and the
variance taken with their covariance. When the contact-rate difference is itself not distinguishable
from zero the set of effects that fit is unbounded: **no interval and no estimate is given**, with the
reason, because any number would be invented. The nightly suite
(`tests/statistical/test_complier_coverage.py`) checks the interval's 95% coverage with 40% to 90% of
the meant customers contacted and some held-back ones contacted too.

**The same customers as the main result.** The estimate is computed from the rows `measure_incrementality`
measured - the same population, the same maturity rule, the same join - rebuilt here by `measured_rows`
and **checked against the report's counts**; if they ever differ the complier part is withheld with that
reason rather than computed on a different set. A customer the contact file does not list is left out of
this estimate (and counted), since their contact status is unknown. The estimate is unadjusted: the
registered adjustment (CUPED) belongs to the main result.

`pandas` is imported inside the function bodies, never at module level, so `import engine` stays fast.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal

from pydantic import AwareDatetime, Field

from engine.config import PrimaryKey, StrictBase, key_columns
from engine.contracts import Artefact
from engine.measurement.campaign import CONTACT_FILENAME, CONTACT_READOUT_FILENAME
from engine.uplift.contracts import ConfidenceValue
from engine.uplift.incrementality import CONFIDENCE_LEVEL, Z_95, newcombe_interval

if TYPE_CHECKING:
    import numpy as np
    import pandas as pd
    from numpy.typing import NDArray

__all__ = [
    "CONTACT_FILENAME",
    "CONTACT_READOUT_FILENAME",
    "CONTACT_RECONCILE_CODES",
    "CONTACT_UNREADABLE",
    "ComplierEffect",
    "ContactFileError",
    "ContactReadout",
    "UnlistedRule",
    "complier_effect",
    "contact_counts",
    "measured_rows",
    "normalise_contacts",
    "reconcile_contacts",
]

CONTACT_UNREADABLE: Final[str] = "CONTACT_FILE_UNREADABLE"
"""422 of the routes that take a contact file: a repeated customer, a missing column, or a value that is neither yes nor no."""

CONTACT_RECONCILE_CODES: Final[frozenset[str]] = frozenset({CONTACT_UNREADABLE})
"""M103's contact-file code, joined into `engine.decide.codes.PLAN_J_CODES` at integration (one definition)."""

CONTACTED_COLUMN: Final[str] = "contacted"
_TRUE_TEXT: Final[frozenset[str]] = frozenset({"1", "true", "yes", "y", "sent", "contacted"})
_FALSE_TEXT: Final[frozenset[str]] = frozenset({"0", "false", "no", "n", "not sent", "not contacted"})

UnlistedRule = Literal["unknown", "not_contacted"]


class ContactFileError(ValueError):
    """A contact file that cannot be read, in a sentence that carries counts and column names, never values."""


# ---------------------------------------------------------------------------
# The records
# ---------------------------------------------------------------------------
class ComplierEffect(StrictBase):
    """The effect on the customers who were actually contacted: secondary, with Fieller's interval."""

    secondary: bool = Field(default=True, description="Always true: this is never the headline result.")
    unit: Literal["rate", "amount"] = Field(
        description="`rate`: extra responders per contacted customer (a yes/no outcome); `amount`: extra amount per contacted customer."
    )
    effect: ConfidenceValue = Field(
        description="Per contacted customer, with Fieller's 95% interval (the ratio of the two group differences)."
    )
    first_stage: ConfidenceValue = Field(
        description="Contact rate of the customers meant to be contacted minus that of the held-back ones, with its 95% interval."
    )
    treated_rows: int = Field(description="Customers meant to be contacted that the estimate rests on.")
    control_rows: int = Field(description="Held-back customers that it rests on.")
    offer: str | None = Field(
        default=None,
        description="The offer the estimate is for, when the campaign had several (the first offer's customers and the held-back ones); null for a single offer.",
    )
    label: str = Field(description="What this number is and is not, in one plain paragraph.")


class ContactReadout(Artefact):
    """`campaigns/<id>/contact_readout.json`: who was actually contacted, and what that does to the reading."""

    campaign_id: str = Field(description="The campaign the contact file belongs to.")
    contact_rows: int = Field(description="Customers the file lists.")
    unlisted_customers: UnlistedRule = Field(
        description="How a customer the file does not list was read: `unknown` (left out) or `not_contacted`."
    )
    treated_customers: int = Field(description="Customers meant to be contacted, in the population measured.")
    treated_listed: int = Field(description="Of them, the ones whose contact status is known.")
    treated_contacted: int = Field(description="Of them, contacted.")
    contact_rate: float | None = Field(
        description="treated_contacted / treated_listed; null with the reason."
    )
    contact_rate_reason: str | None = Field(description="Why the rate is null; null when it is given.")
    holdout_customers: int = Field(description="Held-back customers in the population measured.")
    holdout_listed: int = Field(description="Of them, the ones whose contact status is known.")
    holdout_contacted: int = Field(description="Of them, contacted anyway.")
    contamination: float | None = Field(
        description="holdout_contacted / holdout_listed; null with the reason."
    )
    contamination_reason: str | None = Field(description="Why it is null; null when it is given.")
    rate_difference: ConfidenceValue | None = Field(
        description="Contact rate minus contamination, with a 95% interval; null when either rate is."
    )
    complier: ComplierEffect | None = Field(
        description="The effect on the customers who were contacted (secondary); null with `complier_reason`."
    )
    complier_reason: str | None = Field(
        description="Why the complier-adjusted effect is null; null when given."
    )
    notes: tuple[str, ...] = Field(default=(), description="Plain sentences worth reading beside the rates.")
    computed_at: AwareDatetime = Field(description="When the readout was computed.")


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------
def normalise_contacts(
    frame: pd.DataFrame,
    *,
    primary_key: PrimaryKey,
    contacted_column: str,
    contacted_label: str | None = None,
) -> pd.DataFrame:
    """The contact file as stored: the key column(s) and a nullable boolean `contacted`.

    A blank is unknown (null), kept as such. `contacted_label` names the value that means contacted
    (case-insensitive); without it `1/true/yes/y/sent/contacted` mean contacted and `0/false/no/n/not
    sent/not contacted` do not, and any other value is refused with a count. Raises `ContactFileError`
    for a missing column, a customer listed twice or an unreadable value.
    """
    import pandas as pd

    from engine.keys import key_text
    from engine.uplift.incrementality import _joined_keys

    columns = key_columns(primary_key)
    missing = [name for name in (*columns, contacted_column) if name not in frame.columns]
    if missing:
        raise ContactFileError(f"The contact file has no column {', '.join(repr(name) for name in missing)}.")
    keys = _joined_keys(frame, columns)
    repeated = int(keys.duplicated().sum())
    if repeated:
        raise ContactFileError(
            f"The contact file lists {repeated:,} customer(s) more than once; each customer must appear once."
        )
    values = frame[contacted_column]
    text = values.astype("string").str.strip().str.lower()
    if not pd.api.types.is_bool_dtype(values.dtype):
        numeric = pd.to_numeric(values, errors="coerce").astype("float64")
        integral = numeric.notna() & (numeric == numeric.round()) & (numeric.abs() < 2**53)
        text = text.where(~integral, numeric.round().astype("Int64").astype("string"))
    present = text.notna() & (text != "")
    if contacted_label is not None:
        label = str(contacted_label).strip().lower()
        try:
            as_number = float(label)
            if as_number.is_integer():
                label = str(int(as_number))
        except ValueError:
            pass
        contacted = text == label
    else:
        known = text.isin(_TRUE_TEXT | _FALSE_TEXT).fillna(value=False)
        unreadable = int((present & ~known).sum())
        if unreadable:
            raise ContactFileError(
                f"{unreadable:,} value(s) in {contacted_column!r} are neither yes nor no. Use 1 or 0, yes or no, "
                f"or name the value that means contacted."
            )
        contacted = text.isin(_TRUE_TEXT)
    out = pd.DataFrame({name: key_text(frame[name]).str.strip().to_numpy() for name in columns})
    out[CONTACTED_COLUMN] = contacted.astype("boolean").mask(~present.astype(bool)).to_numpy()
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Counts: contact rate and contamination
# ---------------------------------------------------------------------------
def contact_counts(
    assignment: pd.DataFrame,
    contacts: pd.DataFrame,
    *,
    primary_key: PrimaryKey,
    intended_column: str | None = "intended",
    unlisted: UnlistedRule = "unknown",
) -> dict[str, int]:
    """Exact counts of the measured population by group and contact status.

    Keys: `treated`, `treated_listed`, `treated_contacted`, `holdout`, `holdout_listed`,
    `holdout_contacted`. The population is the campaign's own (`intended`, treated or held-back); a
    customer not listed is unknown unless `unlisted == "not_contacted"`.
    """
    import numpy as np

    arm = assignment["arm"].astype("string")
    population = (
        assignment[intended_column].astype(bool).to_numpy()
        if intended_column is not None
        else np.ones(len(assignment.index), dtype=bool)
    )
    known, contacted = _status(assignment, contacts, primary_key=primary_key, unlisted=unlisted)
    out: dict[str, int] = {}
    for name, label in (("treated", "treated"), ("holdout", "holdout")):
        group = population & (arm == label).fillna(value=False).to_numpy(dtype=bool)
        out[name] = int(group.sum())
        out[f"{name}_listed"] = int((group & known).sum())
        out[f"{name}_contacted"] = int((group & contacted).sum())
    return out


def _status(
    assignment: pd.DataFrame, contacts: pd.DataFrame, *, primary_key: PrimaryKey, unlisted: UnlistedRule
) -> tuple[NDArray[np.bool_], NDArray[np.bool_]]:
    """`(known, contacted)` per assignment row: whether the file says, and whether it says contacted.

    One vectorised lookup of the assignment's ids in the file's (linear in the rows of both). A customer
    the file does not list is unknown, or not contacted when `unlisted == "not_contacted"`; a listed
    customer with a blank is unknown.
    """
    import numpy as np
    import pandas as pd

    from engine.uplift.incrementality import _joined_keys

    columns = key_columns(primary_key)
    rows = len(assignment.index)
    if contacts.empty:
        return np.full(rows, unlisted == "not_contacted"), np.zeros(rows, dtype=bool)
    index = pd.Index(_joined_keys(contacts, columns).to_numpy())
    position = index.get_indexer(_joined_keys(assignment, columns).to_numpy())
    listed = position >= 0
    values = contacts[CONTACTED_COLUMN]
    says = values.notna().to_numpy(dtype=bool)
    yes = values.fillna(value=False).to_numpy(dtype=bool) & says
    at = np.where(listed, position, 0)
    known = np.where(listed, says[at], unlisted == "not_contacted")
    contacted = np.where(listed, yes[at], False)
    return known.astype(bool), contacted.astype(bool)


# ---------------------------------------------------------------------------
# The rows the main result measured
# ---------------------------------------------------------------------------
def measured_rows(
    assignment: pd.DataFrame,
    outcomes: pd.DataFrame,
    contacts: pd.DataFrame,
    *,
    primary_key: PrimaryKey,
    outcome_column: str,
    positive_label: str | None,
    outcome_kind: Literal["binary", "continuous"],
    treatment_time: datetime,
    treatment_date_column: str | None,
    outcome_window_days: int | None,
    as_of: datetime,
    intended_column: str | None = "intended",
    unlisted: UnlistedRule = "unknown",
    offer_column: str | None = None,
    first_offer: str | None = None,
) -> tuple[pd.DataFrame, int, int]:
    """`(rows, treated_measured, control_measured)`: the customers the main result measured, with contact status.

    `rows` has `treated` (bool), `y` (the outcome as a number) and `d` (contacted as 0/1) for every
    measured customer whose contact status is known. The two counts are the measured customers *before*
    those with an unknown status are dropped; they must equal the report's `treated_rows` and
    `control_rows`, because this is `measure_incrementality`'s population, maturity and join rebuilt.
    With `offer_column`, only the first offer's customers and the held-back ones are used, as the report's
    own fields are the first offer's.
    """
    import numpy as np
    import pandas as pd

    from engine.measurement.campaign import as_scores_frame
    from engine.uplift.incrementality import (
        _aware,
        _coerce_amount,
        _coerce_outcome,
        _flag,
        _joined_keys,
        _parse_dates,
        _suppressed,
    )

    columns = key_columns(primary_key)
    scores = as_scores_frame(assignment).reset_index(drop=True)
    known_all, contacted_all = _status(scores, contacts, primary_key=primary_key, unlisted=unlisted)
    suppressed = (
        _suppressed(scores["suppressed_reason"])
        if "suppressed_reason" in scores.columns
        else pd.Series(False, index=scores.index)
    )
    eligible = ~suppressed
    in_population = eligible & _flag(scores[intended_column]) if intended_column is not None else eligible
    held_out = _flag(scores["control_group"]).to_numpy(dtype=bool)
    if offer_column is not None and first_offer is not None:
        offer = scores[offer_column].astype("string").str.strip()
        in_population = in_population & (held_out | (offer == first_offer).fillna(value=False).to_numpy())
    population = in_population.to_numpy(dtype=bool)
    treated = ~held_out & population

    score_keys = _joined_keys(scores, columns)
    outcome_keys = _joined_keys(outcomes, columns)
    converted = (
        _coerce_amount(outcomes[outcome_column], what="outcome")
        if outcome_kind == "continuous"
        else _coerce_outcome(outcomes[outcome_column], positive_label)
    )
    by_key = pd.DataFrame({"converted": converted.to_numpy()}, index=pd.Index(outcome_keys.to_numpy()))
    if treatment_date_column is not None:
        by_key["date"] = _parse_dates(outcomes[treatment_date_column]).to_numpy()
    members_keys = score_keys[population].to_numpy()
    matched = pd.Series(members_keys).isin(by_key.index).to_numpy(dtype=bool)
    joined = by_key.reindex(members_keys)
    if treatment_date_column is not None:
        dates = pd.Series(pd.to_datetime(joined["date"].to_numpy(), errors="coerce", utc=True))
    else:
        dates = pd.Series(pd.Timestamp(_aware(treatment_time)), index=pd.RangeIndex(len(members_keys)))
    date_ok = dates.notna().to_numpy()
    outcome_known = joined["converted"].notna().to_numpy()
    if outcome_window_days is None:
        mature = matched & date_ok
    else:
        ready_at = dates + pd.Timedelta(days=outcome_window_days)
        mature = matched & date_ok & (ready_at <= pd.Timestamp(_aware(as_of))).to_numpy()
    usable = mature & outcome_known
    arm = treated[population]
    known = known_all[population]
    values = joined["converted"].to_numpy()
    treated_measured = int((usable & arm).sum())
    control_measured = int((usable & ~arm).sum())
    keep = usable & known
    rows = pd.DataFrame(
        {
            "treated": arm[keep],
            "y": values[keep].astype(np.float64),
            "d": contacted_all[population][keep].astype(np.float64),
        }
    )
    return rows.reset_index(drop=True), treated_measured, control_measured


# ---------------------------------------------------------------------------
# The complier-adjusted effect (Wald ratio, Fieller interval)
# ---------------------------------------------------------------------------
def _moments(y: NDArray[np.float64], d: NDArray[np.float64]) -> tuple[float, float, float, float, float]:
    """`(mean y, mean d, var(mean y), var(mean d), cov(mean y, mean d))` of one group (unbiased variances)."""

    n = len(y)
    my, md = float(y.mean()), float(d.mean())
    dy, dd = y - my, d - md
    scale = n * (n - 1)
    return my, md, float(dy @ dy) / scale, float(dd @ dd) / scale, float(dy @ dd) / scale


def complier_effect(
    treated: NDArray[np.bool_],
    contacted: NDArray[np.float64],
    outcome: NDArray[np.float64],
    *,
    unit: Literal["rate", "amount"],
    offer: str | None = None,
) -> tuple[ComplierEffect | None, str | None]:
    """`(effect, None)` or `(None, reason)`: the Wald ratio with Fieller's 95% interval.

    `treated` marks the customers meant to be contacted, `contacted` is 0/1 and `outcome` the number
    measured, one entry per customer. Each group needs two customers. When the contact rates do not
    differ by more than chance the interval would be unbounded, and the result is null with the reason.
    """
    import numpy as np

    t = np.asarray(treated, dtype=bool)
    d = np.asarray(contacted, dtype=np.float64)
    y = np.asarray(outcome, dtype=np.float64)
    n1, n0 = int(t.sum()), int((~t).sum())
    if n1 < 2 or n0 < 2:
        return None, (
            f"Each group needs at least two customers with a known outcome and contact status; this file has "
            f"{n1:,} meant to be contacted and {n0:,} held back."
        )
    my1, md1, vy1, vd1, c1 = _moments(y[t], d[t])
    my0, md0, vy0, vd0, c0 = _moments(y[~t], d[~t])
    dy, dd = my1 - my0, md1 - md0
    vyy, vdd, vyd = vy1 + vy0, vd1 + vd0, c1 + c0
    z2 = Z_95 * Z_95
    first_low = dd - Z_95 * math.sqrt(vdd)
    first_high = dd + Z_95 * math.sqrt(vdd)
    a = dd * dd - z2 * vdd
    if dd <= 0.0 or a <= 0.0:
        return None, (
            f"The customers meant to be contacted were contacted {md1:.1%} of the time and the held-back ones "
            f"{md0:.1%}. That difference is too small to tell apart from chance, so the effect on the customers "
            f"who were contacted cannot be worked out, and any number given would be invented. Rely on the main "
            f"result."
        )
    b = dy * dd - z2 * vyd
    c = dy * dy - z2 * vyy
    discriminant = max(b * b - a * c, 0.0)  # non-negative: the point estimate lies inside the set
    root = math.sqrt(discriminant)
    low, high = (b - root) / a, (b + root) / a
    point = dy / dd
    if not low <= point <= high:  # numerical noise only; the point is always inside Fieller's set
        low, high = min(low, point), max(high, point)
    first = ConfidenceValue(value=dd, ci_low=first_low, ci_high=first_high, confidence_level=CONFIDENCE_LEVEL)
    effect = ConfidenceValue(value=point, ci_low=low, ci_high=high, confidence_level=CONFIDENCE_LEVEL)
    for_offer = f" for offer {offer!r}" if offer is not None else ""
    label = (
        f"Secondary result, not the headline. This is the change per customer who was actually contacted"
        f"{for_offer} ({'extra responders' if unit == 'rate' else 'extra amount'} per customer contacted), "
        f"worked out from the "
        f"main result and the difference in who was contacted ({md1:.1%} against {md0:.1%}). It assumes that "
        f"being picked for the campaign mattered only by getting the customer contacted. Its range is wider than "
        f"the main result's, and the fewer customers were reached the wider it is."
    )
    return (
        ComplierEffect(
            unit=unit,
            effect=effect,
            first_stage=first,
            treated_rows=n1,
            control_rows=n0,
            offer=offer,
            label=label,
        ),
        None,
    )


# ---------------------------------------------------------------------------
# The readout
# ---------------------------------------------------------------------------
def _rate(count: int, listed: int, who: str) -> tuple[float | None, str | None]:
    if listed <= 0:
        return None, f"No {who} customer is listed in the contact file, so there is no rate to give."
    return count / listed, None


def reconcile_contacts(
    assignment: pd.DataFrame,
    outcomes: pd.DataFrame | None,
    contacts: pd.DataFrame,
    *,
    campaign_id: str,
    primary_key: PrimaryKey,
    outcome_column: str | None,
    positive_label: str | None,
    outcome_kind: Literal["binary", "continuous"],
    treatment_time: datetime,
    treatment_date_column: str | None,
    outcome_window_days: int | None,
    as_of: datetime | None,
    report_treated_rows: int | None,
    report_control_rows: int | None,
    unlisted: UnlistedRule = "unknown",
    intended_column: str | None = "intended",
    offer_column: str | None = None,
    first_offer: str | None = None,
    computed_at: datetime,
) -> ContactReadout:
    """The contact readout of a campaign: counts and rates, and the complier-adjusted effect when it can be given.

    `outcomes`, `outcome_column` and `as_of` are null for a campaign with no measured result yet; the
    readout then holds the counts and rates only, with the reason the effect is missing. The report's
    counts (`report_treated_rows`, `report_control_rows`) are what `measured_rows` is checked against.
    """
    counts = contact_counts(
        assignment, contacts, primary_key=primary_key, intended_column=intended_column, unlisted=unlisted
    )
    rate, rate_reason = _rate(counts["treated_contacted"], counts["treated_listed"], "contacted-group")
    contamination, contamination_reason = _rate(
        counts["holdout_contacted"], counts["holdout_listed"], "held-back"
    )
    notes: list[str] = []
    if unlisted == "not_contacted":
        notes.append(
            "Customers the contact file does not list were counted as not contacted, because you said the file "
            "lists only the customers it sent to."
        )
    elif counts["treated"] + counts["holdout"] > counts["treated_listed"] + counts["holdout_listed"]:
        left = counts["treated"] + counts["holdout"] - counts["treated_listed"] - counts["holdout_listed"]
        notes.append(
            f"{left:,} customers in the campaign are not in the contact file. Whether they were contacted is "
            f"unknown, so they are left out of the rates and of the effect on the contacted."
        )
    listed_all = counts["treated_listed"] + counts["holdout_listed"]
    if (
        unlisted == "unknown"
        and listed_all
        and counts["treated_contacted"] + counts["holdout_contacted"] == listed_all
    ):
        notes.append(
            "Every customer in the contact file is marked as contacted. If the file is a send log that lists only "
            "the customers it sent to, say so, so that the customers it does not list are counted as not contacted."
        )
    if contamination is not None and contamination > 0.0:
        notes.append(
            f"{counts['holdout_contacted']:,} of {counts['holdout_listed']:,} held-back customers "
            f"({contamination:.1%}) were contacted anyway. This narrows the difference between the groups, so "
            f"the main result understates what contacting does."
        )
    if rate is not None and rate < 0.5:
        notes.append(
            f"Only {rate:.1%} of the customers meant to be contacted were. The main result is a difference "
            f"between everyone meant to be contacted and everyone held back, so it is small whatever the "
            f"message did to the customers it reached."
        )
    complier: ComplierEffect | None = None
    reason: str | None
    if outcomes is None or outcome_column is None or as_of is None:
        reason = (
            "The campaign has not been measured yet, so there is no result to relate to who was contacted."
        )
    else:
        rows, treated_measured, control_measured = measured_rows(
            assignment,
            outcomes,
            contacts,
            primary_key=primary_key,
            outcome_column=outcome_column,
            positive_label=positive_label,
            outcome_kind=outcome_kind,
            treatment_time=treatment_time,
            treatment_date_column=treatment_date_column,
            outcome_window_days=outcome_window_days,
            as_of=as_of,
            intended_column=intended_column,
            unlisted=unlisted,
            offer_column=offer_column,
            first_offer=first_offer,
        )
        if (treated_measured, control_measured) != (report_treated_rows, report_control_rows):
            reason = (
                "The customers the effect would rest on could not be matched to the customers of the main "
                "result, so it is withheld rather than worked out on a different set."
            )
        else:
            import numpy as np

            complier, reason = complier_effect(
                rows["treated"].to_numpy(dtype=bool),
                rows["d"].to_numpy(dtype=np.float64),
                rows["y"].to_numpy(dtype=np.float64),
                unit="amount" if outcome_kind == "continuous" else "rate",
                offer=first_offer if offer_column is not None else None,
            )
            if (
                complier is not None
                and (counts["treated_listed"] + counts["holdout_listed"])
                and len(rows) < (treated_measured + control_measured)
            ):
                notes.append(
                    f"{treated_measured + control_measured - len(rows):,} measured customers are not in the "
                    f"contact file and are left out of the effect on the contacted."
                )
    return ContactReadout(
        campaign_id=campaign_id,
        contact_rows=len(contacts.index),
        unlisted_customers=unlisted,
        treated_customers=counts["treated"],
        treated_listed=counts["treated_listed"],
        treated_contacted=counts["treated_contacted"],
        contact_rate=rate,
        contact_rate_reason=rate_reason,
        holdout_customers=counts["holdout"],
        holdout_listed=counts["holdout_listed"],
        holdout_contacted=counts["holdout_contacted"],
        contamination=contamination,
        contamination_reason=contamination_reason,
        rate_difference=first_stage_interval(
            counts["treated_contacted"],
            counts["treated_listed"],
            counts["holdout_contacted"],
            counts["holdout_listed"],
        ),
        complier=complier,
        complier_reason=reason,
        notes=tuple(notes),
        computed_at=computed_at,
    )


def first_stage_interval(
    treated_contacted: int, treated_listed: int, holdout_contacted: int, holdout_listed: int
) -> ConfidenceValue | None:
    """The difference in contact rates with Newcombe's interval, or None when a group is empty."""
    if treated_listed <= 0 or holdout_listed <= 0:
        return None
    value, low, high = newcombe_interval(treated_contacted, treated_listed, holdout_contacted, holdout_listed)
    return ConfidenceValue(value=value, ci_low=low, ci_high=high, confidence_level=CONFIDENCE_LEVEL)
