"""Synthetic raw client tables for the Phase 2 onboarding flow (Phase 2 plan section 11).

Everything here is **synthetic**: every value comes from a seeded pseudo-random number generator
and describes no real subscriber, invoice, ticket or call. It exists so the onboarding flow has a
client-shaped pile of CSVs to profile, map, snapshot, label and build from, and so
`scripts/bench_large_file.py`-style measurements have a file of a chosen size to run on. It is
never a source of numbers shown to a user.

Why the tables look wrong on purpose
------------------------------------
A client does not send the engine's column names. `customers.csv` therefore carries `CUST_ID`,
`TNR_MNTHS`, `Plan Type`, `DND_FLAG`, `SIGNUP_DT`, `REGION` and `SEGMENT`; the dates in it are
`dd/mm/yyyy`; the plan is spelled `PRE`/`POST`; and `DND_FLAG` is **inverted** - `Y` means *do not
contact*, so it maps onto the standard `marketing_opt_in` only through a `negate` transform. Those
are the mapping screen's reason to exist, so the fixture has to have them.

The generating process, precisely
---------------------------------
Every customer gets three independent latent stresses, each drawn uniformly on [0, 1] and each
**persistent** for the whole extract:

* ``s_complaints`` sets the customer's complaint intensity: tickets arrive as a Poisson process at
  ``BASE_COMPLAINTS_PER_YEAR + STRESS_COMPLAINTS_PER_YEAR * s_complaints`` per year, spread
  uniformly over the extract.
* ``s_late`` sets the probability that any one invoice is paid after its due date:
  ``BASE_LATE_RATE + STRESS_LATE_RATE * s_late``. A late payment is 1-30 days late, an on-time one
  is 0-5 days early.
* ``s_usage`` sets a linear decline in consumption: a usage row ``d`` days into the extract is
  scaled by ``1 - USAGE_DECAY_MAX * s_usage * d / SPAN_DAYS``, so a customer at ``s_usage = 1``
  ends the extract on roughly a sixth of the volume they started with, and the last 30 days sit
  below the 60 before them.

Churn is then drawn once per customer from those three stresses and nothing else::

    logit = CHURN_INTERCEPT
          + W_COMPLAINTS * (s_complaints - 0.5)
          + W_LATE       * (s_late - 0.5)
          + W_USAGE      * (s_usage - 0.5)
          + Normal(0, CHURN_NOISE_SD)

A customer drawn as churned stops appearing in `activity.csv` on their *decision date*, drawn
uniformly between ``DECISION_WINDOW_DAYS`` and ``LABEL_HORIZON_DAYS`` before the end of the extract;
a retained customer keeps going to the end. Nothing stops inside the final ``LABEL_HORIZON_DAYS``,
so every churner's silence is long enough to be seen. **No table carries a churn column.** The outcome exists only as an absence, which is what
the `event_absence` label type is for, and a reader checking that a model learned the planted
signal rather than an artefact can read the three coefficients above straight off.

Bills, payments, complaints and usage deliberately keep flowing for a churned customer after their
decision date. Silencing every feed at once would make "this customer's tables went quiet" a
near-perfect predictor of "no activity in the next 60 days", the built dataset would fail Phase 1's
leakage check, and the planted coefficients above would never be the thing a model found. Only the
activity log stops, because the activity log is the label.

`REMARKS` on a complaint carries a planted phone number or e-mail address in about two thirds of
rows - NANP's fictional 555-555 exchange and RFC 2606's `.invalid` domain, the same obviously-fake
shapes `tests/fixtures/make_data.py` uses - so the PII path has something real to find.

Determinism
-----------
Every draw comes from a :class:`numpy.random.Generator` seeded by a BLAKE2b digest of
``(seed, purpose)``. No global RNG, no clock reads and no dict-ordering dependence, so two runs at
the same seed and the same sizes produce byte-identical files, and adding a draw to one table
cannot shift the values of another.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Final

import numpy as np
import numpy.typing as npt
import pandas as pd

# ---------------------------------------------------------------------------
# Defaults and the extract's calendar
# ---------------------------------------------------------------------------
DEFAULT_CUSTOMERS: Final[int] = 2_000
DEFAULT_USAGE_ROWS: Final[int] = 50_000
DEFAULT_SEED: Final[int] = 20_260_922

DATA_START: Final[date] = date(2023, 1, 1)
DATA_END: Final[date] = date(2025, 2, 28)
SPAN_DAYS: Final[int] = (DATA_END - DATA_START).days

#: How far back from `DATA_END` a churner's decision date may fall.
DECISION_WINDOW_DAYS: Final[int] = 380
#: How long after a decision date the extract must still run. The label the telco-churn use case
#: declares is "no activity in the 60 days after the snapshot", so a decision date inside the last
#: 60 days would leave a churner whose outcome the extract cannot show; none is drawn there.
LABEL_HORIZON_DAYS: Final[int] = 60

# --- the planted signal ----------------------------------------------------
BASE_COMPLAINTS_PER_YEAR: Final[float] = 2.0
STRESS_COMPLAINTS_PER_YEAR: Final[float] = 10.0
BASE_LATE_RATE: Final[float] = 0.06
STRESS_LATE_RATE: Final[float] = 0.55
USAGE_DECAY_MAX: Final[float] = 0.85

CHURN_INTERCEPT: Final[float] = -0.55
W_COMPLAINTS: Final[float] = 5.0
W_LATE: Final[float] = 2.4
W_USAGE: Final[float] = 2.2
CHURN_NOISE_SD: Final[float] = 0.5

# --- table shapes ----------------------------------------------------------
BILL_DUE_DAYS: Final[int] = 15
MAX_DAYS_LATE: Final[int] = 30
UNPAID_RATE: Final[float] = 0.04
COMPLAINT_UNRESOLVED_RATE: Final[float] = 0.35
ACTIVITY_STEP_DAYS: Final[int] = 3
ACTIVITY_RATE_LOW: Final[float] = 0.20
ACTIVITY_RATE_HIGH: Final[float] = 0.60

#: Digits in a `CUST_ID`; `make_leading_zero_keys` pads to twice this width.
KEY_DIGITS: Final[int] = 7
FIRST_KEY: Final[int] = 1_000_001

REGIONS: Final[tuple[str, ...]] = ("North", "South", "East", "West")
SEGMENTS: Final[tuple[str, ...]] = ("Gold", "Silver", "Bronze")
PLAN_TYPES: Final[tuple[str, ...]] = ("PRE", "POST")
PAYMENT_METHODS: Final[tuple[str, ...]] = ("UPI", "CARD", "NETBANKING", "CASH")
COMPLAINT_CATEGORIES: Final[tuple[str, ...]] = ("network", "billing", "service", "roaming")
COMPLAINT_SEVERITIES: Final[tuple[str, ...]] = ("Low", "Medium", "High")
ACTIVITY_TYPES: Final[tuple[str, ...]] = ("login", "recharge", "call", "app_open")

#: Distinct fake contacts the remarks draw from. Fewer than one per row on purpose: a contact
#: detail that never repeats would also look like a per-row identifier.
CONTACT_POOL: Final[int] = 400

_PHONE_REMARKS: Final[tuple[str, ...]] = (
    "customer asked for a callback on {contact}",
    "alternate number given during the call: {contact}",
    "reachable after 6pm on {contact}",
)
_EMAIL_REMARKS: Final[tuple[str, ...]] = (
    "asked us to send the invoice copy to {contact}",
    "resolution to be confirmed by mail to {contact}",
    "escalation acknowledged, replied to {contact}",
)
_PLAIN_REMARKS: Final[tuple[str, ...]] = (
    "tower outage reported in the area, engineer visit raised",
    "unhappy with the last bill and asked for a waiver",
    "data speed slow since the plan change, retested on site",
    "handset settings corrected, satisfied for now",
)

CUSTOMERS_FILE: Final[str] = "customers.csv"
BILLS_FILE: Final[str] = "bills.csv"
PAYMENTS_FILE: Final[str] = "payments.csv"
COMPLAINTS_FILE: Final[str] = "complaints.csv"
USAGE_FILE: Final[str] = "usage.csv"
ACTIVITY_FILE: Final[str] = "activity.csv"


@dataclass(frozen=True, slots=True)
class RawTables:
    """Where one generated set of client tables landed."""

    root: Path
    customers: Path
    bills: Path
    payments: Path
    complaints: Path
    usage: Path
    activity: Path

    @property
    def paths(self) -> tuple[Path, ...]:
        """Every written file, in the order the generator writes them."""
        return (self.customers, self.bills, self.payments, self.complaints, self.usage, self.activity)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------
def _rng(seed: int, purpose: str) -> np.random.Generator:
    """A generator for one named draw. Independent of PYTHONHASHSEED and of call order."""
    payload = f"{seed}|{purpose}".encode()
    return np.random.default_rng(int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big"))


def _iso(offsets: npt.NDArray[np.int64]) -> npt.NDArray[np.str_]:
    """Day offsets from `DATA_START` rendered as `YYYY-MM-DD`."""
    days = np.datetime64(DATA_START, "D") + offsets.astype("timedelta64[D]")
    rendered: npt.NDArray[np.str_] = days.astype(str)
    return rendered


def _ddmmyyyy(offsets: npt.NDArray[np.int64]) -> list[str]:
    """Day offsets from `DATA_START` rendered the way the client's customer master writes dates."""
    days = np.datetime64(DATA_START, "D") + offsets.astype("timedelta64[D]")
    return list(pd.DatetimeIndex(days).strftime("%d/%m/%Y"))


def _write(frame: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, lineterminator="\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The latent customer
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Traits:
    """The per-customer latents every table is generated from. Never written to a file."""

    keys: list[str]
    stress_complaints: npt.NDArray[np.float64]
    stress_late: npt.NDArray[np.float64]
    stress_usage: npt.NDArray[np.float64]
    #: Last day offset on which the customer appears in `activity.csv`.
    stop_offset: npt.NDArray[np.int64]
    signup_offset: npt.NDArray[np.int64]
    monthly_charge: npt.NDArray[np.float64]


def _traits(customers: int, seed: int) -> _Traits:
    """Draw the three stresses, then churn from them, then the day the activity log goes quiet."""
    keys = [f"{FIRST_KEY + index:0{KEY_DIGITS}d}" for index in range(customers)]
    stress = _rng(seed, "stress").random((3, customers))
    complaints, late, usage = stress[0], stress[1], stress[2]
    logit = (
        CHURN_INTERCEPT
        + W_COMPLAINTS * (complaints - 0.5)
        + W_LATE * (late - 0.5)
        + W_USAGE * (usage - 0.5)
        + _rng(seed, "churn_noise").normal(0.0, CHURN_NOISE_SD, size=customers)
    )
    churned = _rng(seed, "churn_draw").random(customers) < 0.5 * (1.0 + np.tanh(0.5 * logit))
    reach = _rng(seed, "decision").integers(LABEL_HORIZON_DAYS, DECISION_WINDOW_DAYS, size=customers)
    decision = SPAN_DAYS - reach
    return _Traits(
        keys=keys,
        stress_complaints=complaints,
        stress_late=late,
        stress_usage=usage,
        stop_offset=np.where(churned, decision, SPAN_DAYS).astype(np.int64),
        signup_offset=-_rng(seed, "signup").integers(30, 1_800, size=customers).astype(np.int64),
        monthly_charge=np.round(_rng(seed, "charge").uniform(200.0, 1_500.0, size=customers), 2),
    )


# ---------------------------------------------------------------------------
# The six tables
# ---------------------------------------------------------------------------
def _customers_frame(traits: _Traits, seed: int) -> pd.DataFrame:
    """The customer master, in the client's own vocabulary.

    `TNR_MNTHS` is derived from `SIGNUP_DT` rather than drawn, so the standard schema's
    `months_between(signup_date, snapshot_date)` derivation has something to agree with.
    """
    count = len(traits.keys)
    rng = _rng(seed, "customers")
    tenure = np.floor((SPAN_DAYS - traits.signup_offset) / 30.44).astype(np.int64)
    return pd.DataFrame(
        {
            "CUST_ID": traits.keys,
            "TNR_MNTHS": tenure,
            "Plan Type": rng.choice(PLAN_TYPES, count),
            "DND_FLAG": rng.choice(("Y", "N"), count, p=(0.3, 0.7)),
            "SIGNUP_DT": _ddmmyyyy(traits.signup_offset),
            "REGION": rng.choice(REGIONS, count),
            "SEGMENT": rng.choice(SEGMENTS, count),
        }
    )


def _bill_offsets() -> npt.NDArray[np.int64]:
    """First-of-month offsets whose due date plus the worst possible delay still fits the extract."""
    latest = SPAN_DAYS - BILL_DUE_DAYS - MAX_DAYS_LATE
    months: list[int] = []
    year, month = DATA_START.year, DATA_START.month
    while (offset := (date(year, month, 1) - DATA_START).days) <= latest:
        months.append(offset)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return np.array(months, dtype=np.int64)


def _bills_and_payments(traits: _Traits, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One invoice per customer-month, and one payment for every invoice that was paid.

    Built together because a payment is the settlement of a known invoice: generating them apart
    would let `PAID_DT` and `PAY_DT` disagree, and a client's two feeds do not disagree about that.
    """
    count = len(traits.keys)
    months = _bill_offsets()
    shape = (count, len(months))
    bill_offset = np.repeat(months[None, :], count, axis=0)
    due_offset = bill_offset + BILL_DUE_DAYS

    late_rate = BASE_LATE_RATE + STRESS_LATE_RATE * traits.stress_late
    rng = _rng(seed, "bills")
    is_late = rng.random(shape) < late_rate[:, None]
    days_late = np.where(
        is_late,
        rng.integers(1, MAX_DAYS_LATE + 1, shape),
        -rng.integers(0, 6, shape),
    ).astype(np.int64)
    unpaid = rng.random(shape) < UNPAID_RATE
    paid_offset = due_offset + days_late
    amount = np.round(traits.monthly_charge[:, None] * rng.uniform(0.8, 1.2, shape), 2)

    keys = np.repeat(np.array(traits.keys), len(months))
    flat_paid = ~unpaid.ravel()
    paid_iso = _iso(paid_offset.ravel())
    bills = pd.DataFrame(
        {
            "CUST_ID": keys,
            "BILL_DT": _iso(bill_offset.ravel()),
            "AMT": amount.ravel(),
            "DUE_DT": _iso(due_offset.ravel()),
            "PAID_DT": np.where(flat_paid, paid_iso, ""),
            "STATUS": np.where(flat_paid, "PAID", "UNPAID"),
        }
    )
    payments = pd.DataFrame(
        {
            "CUST_ID": keys[flat_paid],
            "PAY_DT": paid_iso[flat_paid],
            "AMT": amount.ravel()[flat_paid],
            "METHOD": _rng(seed, "methods").choice(PAYMENT_METHODS, int(flat_paid.sum())),
            "DAYS_LATE": days_late.ravel()[flat_paid],
        }
    )
    return bills, payments


def _remarks(count: int, seed: int) -> list[str]:
    """Free text with a planted phone number or e-mail address in about two rows in three."""
    rng = _rng(seed, "remarks")
    kinds = rng.integers(0, 3, size=count)
    picks = rng.integers(0, 12, size=count)
    contacts = rng.integers(0, CONTACT_POOL, size=count)
    lines: list[str] = []
    for kind, pick, contact in zip(kinds, picks, contacts, strict=True):
        if kind == 0:
            template = _PHONE_REMARKS[int(pick) % len(_PHONE_REMARKS)]
            lines.append(template.format(contact=f"+1-555-555-{int(contact):04d}"))
        elif kind == 1:
            template = _EMAIL_REMARKS[int(pick) % len(_EMAIL_REMARKS)]
            lines.append(template.format(contact=f"subscriber{int(contact):05d}@example.invalid"))
        else:
            lines.append(_PLAIN_REMARKS[int(pick) % len(_PLAIN_REMARKS)])
    return lines


def _complaints_frame(traits: _Traits, seed: int) -> pd.DataFrame:
    """A Poisson ticket stream whose intensity is the customer's complaint stress."""
    count = len(traits.keys)
    years = SPAN_DAYS / 365.25
    rate = BASE_COMPLAINTS_PER_YEAR + STRESS_COMPLAINTS_PER_YEAR * traits.stress_complaints
    tickets = _rng(seed, "complaint_counts").poisson(rate * years, size=count)
    total = int(tickets.sum())
    owner: npt.NDArray[np.int64] = np.repeat(np.arange(count, dtype=np.int64), tickets)
    raised = _rng(seed, "complaint_days").integers(0, SPAN_DAYS + 1, size=total).astype(np.int64)
    order = np.lexsort((raised, owner))
    owner, raised = owner[order], raised[order]

    rng = _rng(seed, "complaint_fields")
    resolved = raised + rng.integers(1, 11, size=total).astype(np.int64)
    open_still = rng.random(total) < COMPLAINT_UNRESOLVED_RATE
    return pd.DataFrame(
        {
            "CUST_ID": np.array(traits.keys)[owner],
            "TICKET_DT": _iso(raised),
            "CATEGORY": rng.choice(COMPLAINT_CATEGORIES, total),
            "RESOLVED_DT": np.where(open_still, "", _iso(resolved)),
            "SEVERITY": rng.choice(COMPLAINT_SEVERITIES, total),
            "REMARKS": _remarks(total, seed),
        }
    )


def _usage_frame(traits: _Traits, usage_rows: int, seed: int) -> pd.DataFrame:
    """Exactly `usage_rows` rows, spread as evenly over customers as the count divides.

    The row count is a parameter because the two callers want opposite things from it: a benchmark
    wants millions of rows to time the read, a test wants a few hundred so it stays fast.
    """
    count = len(traits.keys)
    per_customer = np.full(count, usage_rows // count, dtype=np.int64)
    per_customer[: usage_rows % count] += 1
    owner: npt.NDArray[np.int64] = np.repeat(np.arange(count, dtype=np.int64), per_customer)
    rng = _rng(seed, "usage")
    day = rng.integers(0, SPAN_DAYS + 1, size=usage_rows).astype(np.int64)
    order = np.lexsort((day, owner))
    owner, day = owner[order], day[order]

    decay = 1.0 - USAGE_DECAY_MAX * traits.stress_usage[owner] * (day / SPAN_DAYS)
    volume = _rng(seed, "usage_base").lognormal(math.log(800.0), 0.6, size=count)[owner]
    return pd.DataFrame(
        {
            "CUST_ID": np.array(traits.keys)[owner],
            "USAGE_DT": _iso(day),
            "DATA_MB": np.round(volume * decay * rng.uniform(0.5, 1.5, usage_rows), 1),
            "MINUTES": rng.poisson(np.clip(30.0 * decay, 0.0, None)).astype(np.int64),
            "SESSIONS": rng.poisson(np.clip(4.0 * decay, 0.0, None)).astype(np.int64) + 1,
        }
    )


def _activity_frame(traits: _Traits, seed: int) -> pd.DataFrame:
    """The log the label is read out of: a per-customer hit rate on a fixed day grid, cut at `stop_offset`.

    The rate is drawn per customer and is *not* a function of the three stresses, so "no activity in
    the next 60 days" stays genuinely uncertain given the features - a busy customer who churns goes
    silent, a quiet one who stays can look silent for a while by chance.
    """
    count = len(traits.keys)
    grid = np.arange(0, SPAN_DAYS + 1, ACTIVITY_STEP_DAYS, dtype=np.int64)
    rates = _rng(seed, "activity_rates").uniform(ACTIVITY_RATE_LOW, ACTIVITY_RATE_HIGH, size=count)
    rng = _rng(seed, "activity")
    hit = (rng.random((count, grid.size)) < rates[:, None]) & (grid[None, :] <= traits.stop_offset[:, None])
    rows, columns = np.nonzero(hit)
    return pd.DataFrame(
        {
            "CUST_ID": np.array(traits.keys)[rows],
            "EVENT_DT": _iso(grid[columns]),
            "EVENT_TYPE": rng.choice(ACTIVITY_TYPES, rows.size),
        }
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def make_raw(
    out_dir: Path,
    *,
    customers: int = DEFAULT_CUSTOMERS,
    usage_rows: int = DEFAULT_USAGE_ROWS,
    seed: int = DEFAULT_SEED,
) -> RawTables:
    """Write the six client tables into `out_dir` and return where each one landed."""
    if customers < 1:
        raise ValueError(f"customers must be >= 1, got {customers}")
    if usage_rows < customers:
        raise ValueError(f"usage_rows must be >= customers ({customers}), got {usage_rows}")
    traits = _traits(customers, seed)
    bills, payments = _bills_and_payments(traits, seed)
    return RawTables(
        root=out_dir,
        customers=_write(_customers_frame(traits, seed), out_dir / CUSTOMERS_FILE),
        bills=_write(bills, out_dir / BILLS_FILE),
        payments=_write(payments, out_dir / PAYMENTS_FILE),
        complaints=_write(_complaints_frame(traits, seed), out_dir / COMPLAINTS_FILE),
        usage=_write(_usage_frame(traits, usage_rows, seed), out_dir / USAGE_FILE),
        activity=_write(_activity_frame(traits, seed), out_dir / ACTIVITY_FILE),
    )


# ---------------------------------------------------------------------------
# Broken variants - one function, one directory, one defect
# ---------------------------------------------------------------------------
#: Share of `activity.csv` rows `make_unmatched_keys` points at a customer that does not exist.
UNMATCHED_SHARE: Final[float] = 0.40
#: How many `customers.csv` rows `make_duplicate_entities` repeats.
DUPLICATE_ROWS: Final[int] = 25


def make_unmatched_keys(
    out_dir: Path,
    *,
    customers: int = DEFAULT_CUSTOMERS,
    usage_rows: int = DEFAULT_USAGE_ROWS,
    seed: int = DEFAULT_SEED,
) -> RawTables:
    """`unmatched_keys/`: 40% of activity rows name a customer the master has never heard of.

    The bad keys are still seven digits and still look like keys, which is the case worth having:
    a key that was obviously rubbish would be caught by the column's own profile, whereas this one
    only shows up as `JOIN_KEY_COVERAGE_LOW` once the two sources are compared.
    """
    tables = make_raw(out_dir / "unmatched_keys", customers=customers, usage_rows=usage_rows, seed=seed)
    frame = pd.read_csv(tables.activity, dtype=str)
    rng = _rng(seed, "unmatched")
    stranger = rng.random(len(frame)) < UNMATCHED_SHARE
    foreign = FIRST_KEY + customers + rng.integers(0, max(customers, 1), size=len(frame))
    frame["CUST_ID"] = np.where(
        stranger, [f"{int(value):0{KEY_DIGITS}d}" for value in foreign], frame["CUST_ID"]
    )
    _write(frame, tables.activity)
    return tables


def make_leading_zero_keys(
    out_dir: Path,
    *,
    customers: int = DEFAULT_CUSTOMERS,
    usage_rows: int = DEFAULT_USAGE_ROWS,
    seed: int = DEFAULT_SEED,
) -> RawTables:
    """`leading_zero_keys/`: `usage.csv` pads its keys to twice the width, and nothing else does.

    The classic export accident - one system writes the key as a fixed-width string, another as a
    number - and the one `KEY_FORMAT_MISMATCH` exists for, since `lstrip_zeros` repairs the join.
    """
    tables = make_raw(out_dir / "leading_zero_keys", customers=customers, usage_rows=usage_rows, seed=seed)
    frame = pd.read_csv(tables.usage, dtype=str)
    frame["CUST_ID"] = [value.zfill(KEY_DIGITS * 2) for value in frame["CUST_ID"]]
    _write(frame, tables.usage)
    return tables


def make_ambiguous_dates(
    out_dir: Path,
    *,
    customers: int = DEFAULT_CUSTOMERS,
    usage_rows: int = DEFAULT_USAGE_ROWS,
    seed: int = DEFAULT_SEED,
) -> RawTables:
    """`ambiguous_dates/`: every date in `bills.csv` is `dd/mm/yyyy` with a day of 12 or less.

    No parser can tell `03/04/2024` from `04/03/2024`, and no amount of extra rows will settle it,
    so the format has to be asked about rather than inferred. Days above 12 are folded back into
    1-12 instead of being dropped, so the table keeps its full row count and only the dates move.
    """
    tables = make_raw(out_dir / "ambiguous_dates", customers=customers, usage_rows=usage_rows, seed=seed)
    frame = pd.read_csv(tables.bills, dtype=str)
    for column in ("BILL_DT", "DUE_DT", "PAID_DT"):
        frame[column] = [_ambiguous(value) for value in frame[column].fillna("")]
    _write(frame, tables.bills)
    return tables


def _ambiguous(iso: str) -> str:
    """One ISO date as `dd/mm/yyyy` with its day folded into 1-12; blank stays blank."""
    if not iso:
        return ""
    parsed = date.fromisoformat(iso)
    return f"{(parsed.day - 1) % 12 + 1:02d}/{parsed.month:02d}/{parsed.year}"


def make_duplicate_entities(
    out_dir: Path,
    *,
    customers: int = DEFAULT_CUSTOMERS,
    usage_rows: int = DEFAULT_USAGE_ROWS,
    seed: int = DEFAULT_SEED,
) -> RawTables:
    """`duplicate_entities/`: `customers.csv` repeats its first rows, so the entity key is not unique.

    The rows are exact duplicates rather than conflicting ones: an entity table that is one row per
    entity *except* for a handful of re-exported rows is what a client's master actually looks like.
    """
    tables = make_raw(out_dir / "duplicate_entities", customers=customers, usage_rows=usage_rows, seed=seed)
    frame = pd.read_csv(tables.customers, dtype=str)
    repeated = min(DUPLICATE_ROWS, len(frame))
    _write(pd.concat([frame, frame.head(repeated)], ignore_index=True), tables.customers)
    return tables


def make_unparseable_time(
    out_dir: Path,
    *,
    customers: int = DEFAULT_CUSTOMERS,
    usage_rows: int = DEFAULT_USAGE_ROWS,
    seed: int = DEFAULT_SEED,
) -> RawTables:
    """`unparseable_time/`: `usage.csv` dates its rows by fiscal week, which is not a date at all.

    `FY24-W32` names a seven-day bucket in the client's financial calendar. It is not that no
    parser can read it - it is that the row genuinely has no day, so the source cannot take part in
    a snapshot at all until someone says which day of the week it should stand for.
    """
    tables = make_raw(out_dir / "unparseable_time", customers=customers, usage_rows=usage_rows, seed=seed)
    frame = pd.read_csv(tables.usage, dtype=str)
    frame["USAGE_DT"] = [_fiscal_week(value) for value in frame["USAGE_DT"]]
    _write(frame, tables.usage)
    return tables


def _fiscal_week(iso: str) -> str:
    parsed = date.fromisoformat(iso)
    return f"FY{parsed.strftime('%y')}-W{parsed.isocalendar().week:02d}"


BrokenMaker = Callable[..., RawTables]

BROKEN: Final[Mapping[str, BrokenMaker]] = MappingProxyType(
    {
        "unmatched_keys": make_unmatched_keys,
        "leading_zero_keys": make_leading_zero_keys,
        "ambiguous_dates": make_ambiguous_dates,
        "duplicate_entities": make_duplicate_entities,
        "unparseable_time": make_unparseable_time,
    }
)
"""Every broken variant by the directory it writes. The CLI and the tests both sweep this."""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def default_output_dir() -> Path:
    """Where the tables go unless a caller says otherwise: outside the checkout, so never staged."""
    return Path(tempfile.gettempdir()) / "marketing-ai-synthetic-raw"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="make_raw",
        description="Regenerate the synthetic raw client tables the onboarding flow is exercised on.",
    )
    parser.add_argument("--out-dir", type=Path, default=None, help=f"default: {default_output_dir()}")
    parser.add_argument("--customers", type=int, default=DEFAULT_CUSTOMERS)
    parser.add_argument("--usage-rows", type=int, default=DEFAULT_USAGE_ROWS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--broken", action="append", dest="broken", metavar="NAME", help="repeatable")
    parser.add_argument("--list-broken", action="store_true", help="print the broken variants and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    if args.list_broken:
        width = max(len(name) for name in BROKEN)
        for name, maker in BROKEN.items():
            summary = (maker.__doc__ or "").strip().splitlines()[0]
            print(f"{name:<{width}}  {summary}")
        return 0
    out_dir: Path = args.out_dir if args.out_dir is not None else default_output_dir()
    sizes = {"customers": args.customers, "usage_rows": args.usage_rows, "seed": args.seed}
    written = [make_raw(out_dir / "clean", **sizes)]
    for name in tuple(args.broken) if args.broken else tuple(BROKEN):
        if name not in BROKEN:
            print(f"unknown broken variant {name!r}; known: {', '.join(BROKEN)}", file=sys.stderr)
            return 2
        written.append(BROKEN[name](out_dir, **sizes))
    for tables in written:
        for path in tables.paths:
            print(f"{path}  ({math.trunc(path.stat().st_size / 1024)} KiB)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
