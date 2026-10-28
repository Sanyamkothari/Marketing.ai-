"""Family-D adapters: what a non-retail business would have to write to give Autonomy its data.

Autonomy takes only `Event`s (customer, time, one of 19 types, value for purchases). None of these
files is an event log: they are one row per person (bank, telco, insurance, credit), one row per
booking (hotel) or one wide row per donor with 22 mailings in columns (KDD Cup 98). Each adapter
turns the file into a long table and then into events **through the product's own `map_frame`**
(so mapping problems are the product's), and lists everything it had to invent. Nothing here is
product code; it is the customer's side.

| dataset | person | what becomes a purchase | what becomes a send | invented |
|---|---|---|---|---|
| bank_marketing | row number (no id) | `y = yes` (term deposit, value unknown -> 1.0) | the call (channel: none fits) | id, year, day, value |
| telco_churn | `customerID` | one bill per month of `tenure` at `MonthlyCharges` | none | calendar dates, bill history |
| health_insurance_xsell | `id` | the health policy at signup (`Annual_Premium`) | none | calendar dates |
| hotel_bookings | none: one booking = one "customer" | the booking (`adr` x nights) | none | id |
| uci_credit_default | `ID` | each monthly payment `PAY_AMT1..6` | none | day of month |
| kdd98 (full) | `CONTROLN` | each gift `RAMNT_k` at `RDATE_k` | each mailing at `ADATE_k` | day of month |

Channels: Autonomy's channels are email, whatsapp, sms, push, web and webhook. A phone call
(bank) and a paper mailing (KDD Cup 98) are neither, so those sends are mapped to `webhook`
("hand-off to the client's own tool") and the real channel is kept in `attrs["format"]`.

Snapshots for files with no calendar dates (telco, insurance) are an arbitrary fixed moment,
`SNAPSHOT_UNDATED`, labelled as invented.

Python 3.11 compatible; numpy and pandas only.
"""

# ruff: noqa: ARG001  (every adapter takes the runner's clock, most do not use it)
from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from d_common import DATASETS, Clock

from autonomy.contracts import Event, EventType, Goal, GoalKind, Objective
from autonomy.data.mapping import EventMapping, MappingProblem, map_frame, problem_counts

SNAPSHOT_UNDATED = datetime(2024, 1, 31, 12, 0, tzinfo=UTC)
"""The 'now' given to files that carry no dates at all. Invented; every result says so."""

MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
    )
}
DOW = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4}


@dataclass
class DoctorFile:
    """The file as the customer would upload it to the Data Doctor."""

    label: str
    read: Callable[[], bytes]
    delimiter: str = ","
    primary_key: str | None = None
    target: str | None = None


@dataclass
class DAdapted:
    name: str
    domain: str
    doctor: DoctorFile
    events: list[Event]
    problems: dict[str, int]
    now: datetime
    timezone: str
    currency: str
    goal_kind: GoalKind
    objective: Objective
    goal_text: str
    outcome: pd.Series  # customer_id -> the dataset's own outcome (0/1)
    outcome_name: str
    invented: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rows_in: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def goal(self, now: datetime) -> Goal:
        return Goal(
            goal_id=f"g_{self.name}",
            business_id="realdata",
            text=self.goal_text,
            kind=self.goal_kind,
            objective=self.objective,
            budget=1000.0,
            currency=self.currency,
            starts_at=now,
            created_at=now,
        )


# --------------------------------------------------------------------------- helpers


def _file(path: Path) -> Callable[[], bytes]:
    return path.read_bytes


def _zip_member(path: Path, member: str) -> Callable[[], bytes]:
    def read() -> bytes:
        with zipfile.ZipFile(path) as z:
            return z.read(member)

    return read


LONG_MAPPING = EventMapping(
    customer_id="customer_id",
    timestamp="ts",
    event_type_column="type",
    value_column="value",
    channel_column="channel",
    item_column="item_id",
    category_column="category",
    attrs={"format": "format", "logged_probability": "logged_probability", "promo": "promo"},
    row_key=("customer_id", "type", "ts", "key"),
)
"""The one mapping every family-D adapter's long table goes through (ISO times, our type words)."""

LONG_COLUMNS = (
    "customer_id",
    "ts",
    "type",
    "value",
    "channel",
    "item_id",
    "category",
    "format",
    "logged_probability",
    "promo",
    "key",
)


def long_events(
    frame: pd.DataFrame, source: str, timezone: str = "UTC"
) -> tuple[list[Event], list[MappingProblem]]:
    """Fill the optional columns and run the product's `map_frame` on a long table."""
    f = frame.copy()
    for c in LONG_COLUMNS:
        if c not in f.columns:
            f[c] = None
    f = f.astype(object).where(pd.notna(f), None)
    mapping = LONG_MAPPING.model_copy(update={"timezone": timezone})
    return map_frame(f.reset_index(drop=True), mapping, source)


def _iso(ts: pd.Series) -> pd.Series:
    return pd.to_datetime(ts).dt.strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------------------- bank marketing


def bank_dates(frame: pd.DataFrame) -> pd.Series:
    """Reconstructed call dates. The file has `month` and `day_of_week` but no year or day; the UCI
    page says rows are in time order from May 2008 to November 2010. So the year goes up by one
    whenever the month goes backwards in row order, and within a month the week goes up whenever
    the weekday goes backwards. Approximate by construction (a week can be off by one)."""
    import calendar

    year, week = 2008, 0
    prev_m, prev_d = None, None
    anchor = datetime(2008, 1, 1)
    out: list[datetime] = []
    for m, d in zip(frame["month"].map(MONTHS), frame["day_of_week"].map(DOW), strict=True):
        if prev_m is not None and m < prev_m:
            year += 1
        if m != prev_m:
            week = 0
            first = datetime(year, m, 1)
            # The Monday of the first week, chosen so the month's first call falls on or after the 1st.
            anchor = first - timedelta(days=first.weekday())
            if anchor + timedelta(days=d) < first:
                anchor += timedelta(days=7)
        elif prev_d is not None and d < prev_d:
            week += 1
        prev_m, prev_d = m, d
        day = anchor + timedelta(days=7 * week + d)
        last = datetime(year, m, calendar.monthrange(year, m)[1])
        out.append(min(day, last).replace(hour=11))
    return pd.Series(out, index=frame.index)


def adapt_bank(clock: Clock | None = None) -> DAdapted:
    path = DATASETS / "bank_marketing" / "bank-additional-full.csv"
    raw = pd.read_csv(path, sep=";")
    raw["customer_id"] = [f"bank{i:05d}" for i in range(len(raw))]
    raw["call_at"] = bank_dates(raw)
    rows: list[pd.DataFrame] = []
    # This campaign's last call: one send per client, the channel kept as the format.
    rows.append(
        pd.DataFrame(
            {
                "customer_id": raw.customer_id,
                "ts": _iso(raw.call_at),
                "type": "send",
                "channel": "webhook",
                "format": raw.contact,
                "key": "call",
                "promo": "this_campaign",
            }
        )
    )
    yes = raw[raw.y == "yes"]
    # The deposit: a purchase needs a value and the file has none -> 1.0 (a count, in no currency).
    rows.append(
        pd.DataFrame(
            {
                "customer_id": yes.customer_id,
                "ts": _iso(yes.call_at + pd.Timedelta(hours=1)),
                "type": "purchase",
                "value": 1.0,
                "item_id": "term_deposit",
                "category": "deposit",
                "key": "deposit",
            }
        )
    )
    # The previous campaign: `pdays` days before (999 = never contacted), outcome `poutcome`.
    prev = raw[raw.pdays != 999]
    prev_at = prev.call_at - pd.to_timedelta(prev.pdays, unit="D")
    rows.append(
        pd.DataFrame(
            {
                "customer_id": prev.customer_id,
                "ts": _iso(prev_at),
                "type": "send",
                "channel": "webhook",
                "format": "unknown",
                "key": "prev_call",
                "promo": "previous_campaign",
            }
        )
    )
    won = prev[prev.poutcome == "success"]
    won_at = won.call_at - pd.to_timedelta(won.pdays, unit="D") + pd.Timedelta(hours=1)
    rows.append(
        pd.DataFrame(
            {
                "customer_id": won.customer_id,
                "ts": _iso(won_at),
                "type": "purchase",
                "value": 1.0,
                "item_id": "previous_product",
                "key": "prev_win",
            }
        )
    )
    long = pd.concat(rows, ignore_index=True)
    events, problems = long_events(long, "bank_marketing", "Europe/Lisbon")
    last = raw.call_at.max()
    return DAdapted(
        name="bank_marketing",
        domain="retail bank, outbound telemarketing for term deposits",
        doctor=DoctorFile("bank-additional-full.csv", _file(path), delimiter=";", target="y"),
        events=events,
        problems=problem_counts(problems),
        now=(last + pd.Timedelta(days=1)).to_pydatetime().replace(tzinfo=UTC),
        timezone="Europe/Lisbon",
        currency="EUR",
        goal_kind=GoalKind.CUSTOM,
        objective=Objective.CONVERSION,
        goal_text="Get existing clients to open a term deposit",
        outcome=pd.Series((raw.y == "yes").astype(int).values, index=raw.customer_id),
        outcome_name="y",
        invented=[
            "customer id = row number (the file has none; a client called twice would be two people)",
            "year and day of the call, from row order (the file has month and weekday only)",
            "value 1.0 for a deposit (the file has no amount)",
            "channel 'webhook' for a phone call (no call channel exists)",
            "previous-campaign call dated pdays before this call; pdays=999 read as 'never called'",
        ],
        notes=[
            "every client in the file was called in this campaign: there is no uncontacted group",
            "`duration` is known only after the call; UCI says to drop it for a realistic model",
        ],
        rows_in=len(raw),
        extra={"raw": raw},
    )


# --------------------------------------------------------------------------- telco churn


def adapt_telco(clock: Clock | None = None) -> DAdapted:
    path = DATASETS / "telco_churn" / "Telco-Customer-Churn.csv"
    raw = pd.read_csv(path, dtype={"customerID": str})
    snap = pd.Timestamp(SNAPSHOT_UNDATED).tz_localize(None)
    churn = raw.Churn == "Yes"
    rows = []
    signup = [snap - pd.DateOffset(months=int(t)) for t in raw.tenure]
    rows.append(
        pd.DataFrame(
            {
                "customer_id": raw.customerID,
                "ts": _iso(pd.Series(signup)),
                "type": "signup",
                "key": "signup",
            }
        )
    )
    # One bill per month of tenure; a churner left "within the last month", so their bills stop a
    # month before the snapshot. Invented history: the file only has tenure and today's charge.
    reps = raw.loc[raw.index.repeat(raw.tenure.clip(lower=0))].copy()
    reps["k"] = reps.groupby(level=0).cumcount()
    offset = reps.tenure - reps.k - 1 + churn.loc[reps.index].astype(int)
    reps["bill_at"] = [snap - pd.DateOffset(months=int(o)) for o in offset]
    rows.append(
        pd.DataFrame(
            {
                "customer_id": reps.customerID,
                "ts": _iso(reps.bill_at),
                "type": "purchase",
                "value": reps.MonthlyCharges,
                "item_id": "plan_" + reps.Contract.str.replace(" ", "_"),
                "category": reps.InternetService,
                "key": reps.k.astype(str),
            }
        )
    )
    long = pd.concat(rows, ignore_index=True)
    events, problems = long_events(long, "telco_churn")
    return DAdapted(
        name="telco_churn",
        domain="telecom subscription (phone + internet), churn",
        doctor=DoctorFile("Telco-Customer-Churn.csv", _file(path), primary_key="customerID", target="Churn"),
        events=events,
        problems=problem_counts(problems),
        now=SNAPSHOT_UNDATED,
        timezone="America/Los_Angeles",
        currency="USD",
        goal_kind=GoalKind.RETENTION,
        objective=Objective.RETENTION,
        goal_text="Reduce churn: keep subscribers from cancelling",
        outcome=pd.Series(churn.astype(int).values, index=raw.customerID),
        outcome_name="Churn",
        invented=[
            f"calendar: snapshot {SNAPSHOT_UNDATED.date()} (the file has no dates)",
            "a monthly bill purchase for every month of tenure at today's MonthlyCharges",
            "churners' last bill one month before the snapshot",
        ],
        notes=["11 customers have tenure 0 and a blank TotalCharges: no bills"],
        rows_in=len(raw),
        extra={"raw": raw},
    )


# --------------------------------------------------------------------------- health insurance


def adapt_health(clock: Clock | None = None, max_rows: int | None = None) -> DAdapted:
    path = DATASETS / "health_insurance_xsell" / "train.csv"
    raw = pd.read_csv(path, dtype={"id": str})
    if max_rows is not None:
        raw = raw.iloc[:max_rows]
    snap = pd.Timestamp(SNAPSHOT_UNDATED).tz_localize(None)
    signup = snap - pd.to_timedelta(raw.Vintage, unit="D")
    rows = [
        pd.DataFrame({"customer_id": raw.id, "ts": _iso(signup), "type": "signup", "key": "s"}),
        pd.DataFrame(
            {
                "customer_id": raw.id,
                "ts": _iso(signup + pd.Timedelta(minutes=5)),
                "type": "purchase",
                "value": raw.Annual_Premium,
                "item_id": "health_policy",
                "category": "health",
                "key": "p",
            }
        ),
    ]
    events, problems = long_events(pd.concat(rows, ignore_index=True), "health_insurance_xsell")
    return DAdapted(
        name="health_insurance_xsell",
        domain="health insurer cross-selling vehicle insurance",
        doctor=DoctorFile("train.csv", _file(path), primary_key="id", target="Response"),
        events=events,
        problems=problem_counts(problems),
        now=SNAPSHOT_UNDATED,
        timezone="Asia/Kolkata",
        currency="INR",
        goal_kind=GoalKind.CROSS_SELL,
        objective=Objective.CONVERSION,
        goal_text="Cross-sell vehicle insurance to our health policy holders",
        outcome=pd.Series(raw.Response.values, index=raw.id),
        outcome_name="Response",
        invented=[
            f"calendar: snapshot {SNAPSHOT_UNDATED.date()}; signup = snapshot - Vintage days",
            "the health policy as one purchase at signup worth Annual_Premium",
            "currency INR and time zone Asia/Kolkata (the source does not say; Analytics Vidhya, India)",
        ],
        notes=[
            "Response = said they are interested, not a purchase",
            "age, gender, region, vehicle age and damage cannot reach CustomerState",
        ],
        rows_in=len(raw),
    )


# --------------------------------------------------------------------------- hotel bookings


def hotel_arrival(raw: pd.DataFrame) -> pd.Series:
    month = raw.arrival_date_month.str[:3].str.lower().map(MONTHS)
    return pd.to_datetime(
        pd.DataFrame({"year": raw.arrival_date_year, "month": month, "day": raw.arrival_date_day_of_month})
    )


def adapt_hotel(clock: Clock | None = None) -> DAdapted:
    path = DATASETS / "hotel_bookings" / "hotels.csv"
    raw = pd.read_csv(path, keep_default_na=False, na_values=[""], low_memory=False)
    raw["customer_id"] = [f"bk{i:06d}" for i in range(len(raw))]
    arrival = hotel_arrival(raw)
    booked = arrival - pd.to_timedelta(raw.lead_time, unit="D") + pd.Timedelta(hours=12)
    nights = raw.stays_in_weekend_nights + raw.stays_in_week_nights
    value = raw.adr * nights
    rows = [
        pd.DataFrame(
            {
                "customer_id": raw.customer_id,
                "ts": _iso(booked),
                "type": "purchase",
                "value": value,
                "item_id": raw.hotel + "/" + raw.reserved_room_type,
                "category": raw.hotel,
                "key": "b",
            }
        )
    ]
    canc = raw[raw.is_canceled == 1]
    rows.append(
        pd.DataFrame(
            {
                "customer_id": canc.customer_id,
                "ts": _iso(pd.to_datetime(canc.reservation_status_date) + pd.Timedelta(hours=13)),
                "type": "refund",
                "value": value[canc.index],
                "key": "c",
            }
        )
    )
    events, problems = long_events(pd.concat(rows, ignore_index=True), "hotel_bookings", "Europe/Lisbon")
    last = max(e.ts for e in events)
    return DAdapted(
        name="hotel_bookings",
        domain="two Portuguese hotels (resort + city), bookings and cancellations",
        doctor=DoctorFile("hotels.csv", _file(path), target="is_canceled"),
        events=events,
        problems=problem_counts(problems),
        now=last.replace(hour=10) + pd.Timedelta(days=1).to_pytimedelta(),
        timezone="Europe/Lisbon",
        currency="EUR",
        goal_kind=GoalKind.RETENTION,
        objective=Objective.RETENTION,
        goal_text="Bring guests back for another stay and cut cancellations",
        outcome=pd.Series(raw.is_canceled.values, index=raw.customer_id),
        outcome_name="is_canceled",
        invented=[
            "customer id = booking row (the file has no guest id)",
            "booking time = arrival - lead_time, noon",
        ],
        notes=[
            f"{int(raw.is_repeated_guest.sum()):,} bookings are flagged repeat guests, but their earlier stays cannot be linked",
            "adr x nights is the booking value; 0-night and negative-adr bookings exist",
        ],
        rows_in=len(raw),
        extra={"repeat_flagged": int(raw.is_repeated_guest.sum()), "zero_value": int((value <= 0).sum())},
    )


# --------------------------------------------------------------------------- credit card default


def adapt_credit(clock: Clock | None = None) -> DAdapted:
    path = DATASETS / "uci_credit_default" / "UCI_Credit_Card.csv"
    raw = pd.read_csv(path, dtype={"ID": str})
    # PAY_AMT1 is the payment in September 2005 ... PAY_AMT6 in April 2005 (UCI documentation).
    month_of = {1: 9, 2: 8, 3: 7, 4: 6, 5: 5, 6: 4}
    rows = []
    for k, m in month_of.items():
        paid = raw[raw[f"PAY_AMT{k}"] > 0]
        rows.append(
            pd.DataFrame(
                {
                    "customer_id": paid.ID,
                    "ts": f"2005-{m:02d}-15T12:00:00",
                    "type": "purchase",
                    "value": paid[f"PAY_AMT{k}"],
                    "item_id": "card_payment",
                    "category": "payment",
                    "key": str(k),
                }
            )
        )
    events, problems = long_events(pd.concat(rows, ignore_index=True), "uci_credit_default", "Asia/Taipei")
    return DAdapted(
        name="uci_credit_default",
        domain="Taiwanese card issuer, payment default risk",
        doctor=DoctorFile(
            "UCI_Credit_Card.csv", _file(path), primary_key="ID", target="default.payment.next.month"
        ),
        events=events,
        problems=problem_counts(problems),
        now=datetime(2005, 10, 1, 10, 0, tzinfo=UTC),
        timezone="Asia/Taipei",
        currency="TWD",
        goal_kind=GoalKind.CUSTOM,
        objective=Objective.RETENTION,
        goal_text="Keep cardholders paying: prevent default next month",
        outcome=pd.Series(raw["default.payment.next.month"].values, index=raw.ID),
        outcome_name="default.payment.next.month",
        invented=[
            "each month's payment on the 15th (the file has the month only)",
            "a card payment is mapped to a purchase: no event type means 'paid a bill'",
        ],
        notes=[
            "wide file: 6 bill and 6 payment columns had to be melted in code",
            "LIMIT_BAL, age, sex, education, marriage and the PAY_k delay codes cannot reach state",
        ],
        rows_in=len(raw),
    )


# --------------------------------------------------------------------------- KDD Cup 98


KDD_PROMOS = {
    2: "97NK",
    3: "96NK",
    4: "96TK",
    5: "96SK",
    6: "96LL",
    7: "96G1",
    8: "96GK",
    9: "96CC",
    10: "96WL",
    11: "96X1",
    12: "96XK",
    13: "95FS",
    14: "95NK",
    15: "95TK",
    16: "95LL",
    17: "95G1",
    18: "95GK",
    19: "95CC",
    20: "95WL",
    21: "95X1",
    22: "95XK",
    23: "94FS",
    24: "94NK",
}
KDD_ZIP = DATASETS / "kdd98_full" / "cup98LRN.zip"


def read_kdd98(max_rows: int | None = None) -> pd.DataFrame:
    with zipfile.ZipFile(KDD_ZIP) as z:
        data = z.read("cup98LRN.txt")
    return pd.read_csv(io.BytesIO(data), low_memory=False, nrows=max_rows)


def yymm(values: pd.Series) -> pd.Series:
    """YYMM numbers (9706, or 9606.0 from a column with blanks) as the 1st of that month."""
    v = pd.to_numeric(values, errors="coerce")
    year = 1900 + (v // 100)
    month = v % 100
    ok = v.notna() & month.between(1, 12)
    out = pd.Series(pd.NaT, index=values.index, dtype="datetime64[ns]")
    out[ok] = pd.to_datetime(
        pd.DataFrame({"year": year[ok].astype(int), "month": month[ok].astype(int), "day": 1})
    )
    return out


def kdd_long(raw: pd.DataFrame, *, include_97nk: bool = False) -> pd.DataFrame:
    """Melt the 22 past mailings (and gifts) into a long table: one send per mailing received, one
    purchase per gift. `include_97nk` adds the target mailing's send (everyone got it)."""
    cid = raw.CONTROLN.astype(str)
    rows = []
    for k, code in KDD_PROMOS.items():
        if k == 2 and not include_97nk:
            continue
        sent = yymm(raw[f"ADATE_{k}"])
        m = sent.notna()
        rows.append(
            pd.DataFrame(
                {
                    "customer_id": cid[m],
                    "ts": _iso(sent[m] + pd.Timedelta(hours=9)),
                    "type": "send",
                    "channel": "webhook",
                    "format": "mail_" + code[2:],
                    "promo": code,
                    "key": code,
                }
            )
        )
        if k == 2:
            continue
        got = yymm(raw[f"RDATE_{k}"])
        amt = pd.to_numeric(raw[f"RAMNT_{k}"], errors="coerce")
        g = got.notna() & (amt > 0)
        rows.append(
            pd.DataFrame(
                {
                    "customer_id": cid[g],
                    "ts": _iso(got[g] + pd.Timedelta(days=14, hours=12)),
                    "type": "purchase",
                    "value": amt[g],
                    "item_id": "gift",
                    "category": "donation",
                    "promo": code,
                    "key": code,
                }
            )
        )
    return pd.concat(rows, ignore_index=True)


def adapt_kdd98(clock: Clock | None = None, max_rows: int | None = None) -> DAdapted:
    raw = read_kdd98(max_rows)
    long = kdd_long(raw)
    events, problems = long_events(long, "kdd98", "America/New_York")
    now = datetime(1997, 6, 1, 14, 0, tzinfo=UTC)  # the 97NK mailing month; history is before it
    events = [e for e in events if e.ts < now]
    return DAdapted(
        name="kdd98",
        domain="US veterans charity (PVA), direct-mail donation appeals to lapsed donors",
        doctor=DoctorFile(
            "cup98LRN.txt", _zip_member(KDD_ZIP, "cup98LRN.txt"), primary_key="CONTROLN", target="TARGET_B"
        ),
        events=events,
        problems=problem_counts(problems),
        now=now,
        timezone="America/New_York",
        currency="USD",
        goal_kind=GoalKind.WIN_BACK,
        objective=Objective.REVENUE,
        goal_text="Win back lapsed donors with the June 1997 appeal",
        outcome=pd.Series(raw.TARGET_B.values, index=raw.CONTROLN.astype(str)),
        outcome_name="TARGET_B",
        invented=[
            "mailing on the 1st and gift on the 15th of the YYMM month (the file has months only)",
            "channel 'webhook' for a paper mailing (no mail channel exists)",
        ],
        notes=[
            "wide file: 22 mailings x (date, RFA, gift date, gift amount) columns melted in code",
            "gifts before the 22 promotions (lifetime NGIFTALL, FISTDATE) are summaries, not events",
            "every donor in the file received the 97NK mailing: no unmailed group",
        ],
        rows_in=len(raw),
        extra={"raw": raw},
    )


ADAPTERS: dict[str, Callable[..., DAdapted]] = {
    "bank_marketing": adapt_bank,
    "telco_churn": adapt_telco,
    "health_insurance_xsell": adapt_health,
    "hotel_bookings": adapt_hotel,
    "uci_credit_default": adapt_credit,
    "kdd98": adapt_kdd98,
}
FAMILY_D = tuple(ADAPTERS)


def adapt(name: str, clock: Clock | None = None, **kw: Any) -> DAdapted:
    return ADAPTERS[name](clock, **kw)


def describe_events(events: list[Event]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    for e in events:
        by_type[e.type.value] = by_type.get(e.type.value, 0) + 1
    vals = np.array([e.value for e in events if e.type is EventType.PURCHASE and e.value is not None])
    return {
        "events": len(events),
        "by_type": dict(sorted(by_type.items())),
        "customers": len({e.customer_id for e in events}),
        "purchase_value_p50": float(np.median(vals)) if len(vals) else None,
        "first": min(e.ts for e in events) if events else None,
        "last": max(e.ts for e in events) if events else None,
    }
