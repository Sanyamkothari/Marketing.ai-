"""Bench adapters: each public randomised campaign turned into Autonomy's inputs, as a customer would.

All dataset-specific code lives here (never in `autonomy/`): what a customer's connector or column
mapping would do with their own export. The logic follows `scripts/v2_realdata/a_hillstrom.py`,
`a_x5.py` and `a_features_rct.py` (family A of docs/v2/real_data/A-randomised.md), with three
changes for the scoreboard: the split and samples are by customer id hash (fixed salts), the past
campaign's window purchases are written as PURCHASE events of the train customers (a connector sees
the orders that followed a campaign, for both arms), and each customer's pre-treatment features are
handed to the agent's state as `preferences`.

Fixed sizes (a full run takes about 18 minutes on a 4-CPU box):

* Hillstrom: all 64,000 customers.
* X5 RetailHero: a fixed 100,000 of the 200,039 labelled clients (smallest hash, salt below).
* Criteo uplift v2.1: a fixed 300,000 of 13,979,592 rows (smallest SplitMix64 hash of the row number).
* MegaFon: a fixed 200,000 of 600,000 rows (smallest hash of the row id).

Adapter choices a reader should know (each is what this file does, not what the data says):

* Ids: Hillstrom, Criteo and MegaFon have none; the row number becomes the id (`h00012`, `cr123`,
  `mf123`).
* Consent: no file carries consent; every customer gets a `consent_granted` on the campaign's
  channel at signup (the control group was eligible for the same message).
* Clicks: Hillstrom and Criteo log no clicks; a treated customer's `visit` after the message is
  written as the message's CLICK (as in family A). X5 and MegaFon log no click-like signal at all.
* Money: X5 profit = 25% x the client's own pre-period mean receipt if they bought, minus 2 RUB per
  SMS. MegaFon and Criteo have no amounts: a conversion is worth the agent's own default (order
  value 50 x margin 0.4 = 20) and the contact cost is the break-even cost measured on TRAIN (20 x
  the train average effect), so blanket sending earns about nothing and only targeting pays.
  Hillstrom profit = 40% of spend minus $0.001 per e-mail (the agent's default e-mail cost).
"""

from __future__ import annotations

import gc
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from autonomy.contracts import (
    Audience,
    Brief,
    Channel,
    ContentItem,
    ContentStatus,
    Envelope,
    Event,
    EventType,
    Objective,
    Theme,
)
from scripts.v2_realbench.common import (
    SPLIT_SALT,
    AgentInputs,
    BenchData,
    Timings,
    pick_by_hash,
    salt_key,
    split_by_hash,
    splitmix64,
)

DATASETS = ("hillstrom", "x5", "criteo", "megafon")
SIZES: dict[str, int] = {"hillstrom": 64_000, "x5": 100_000, "criteo": 300_000, "megafon": 200_000}
SAMPLE_SALT = "autonomy-realbench-v1:sample"
VALUE_OF_CONVERSION = 20.0  # the agent's default: order value 50 x margin 0.4
ORDER_VALUE = 50.0
WINDOW_PURCHASE_AFTER = timedelta(days=3, hours=12)
"""When a campaign-window purchase is booked for X5, Criteo and MegaFon, whose files give no purchase
time: mid-week after the send, inside both the agent's 7-day learning window (half-open) and the
brief's 14-day proof window, as the Hillstrom loader books its window spend at +3 to +4 days."""


def _brief(goal: str, channel: Channel, at: datetime, what: str) -> Brief:
    return Brief(
        brief_id=f"brief_{goal}_1",
        goal_id=goal,
        objective=Objective.GROSS_PROFIT,
        audience=Audience(description="Every customer in the campaign"),
        horizon_days=14,
        envelope=Envelope(channels=(channel,), budget=1e12, max_contacts_per_week=2),
        rationale=f"Real-data scoreboard: {what}",
        created_at=at - timedelta(days=30),
        approved_by="realbench",
        approved_at=at - timedelta(days=30),
    )


def _content(
    goal: str, channel: Channel, at: datetime, rows: Sequence[tuple[str, Theme, str]]
) -> tuple[ContentItem, ...]:
    return tuple(
        ContentItem(
            content_id=cid,
            goal_id=goal,
            channel=channel,
            theme=theme,
            subject=subject if channel is Channel.EMAIL else None,
            body=subject,
            status=ContentStatus.APPROVED,
            created_at=at - timedelta(days=30),
        )
        for cid, theme, subject in rows
    )


def _group(events: Sequence[Event]) -> dict[str, list[Event]]:
    out: dict[str, list[Event]] = {}
    for e in events:
        out.setdefault(e.customer_id, []).append(e)
    return out


# --- Hillstrom -----------------------------------------------------------------------------------

HILLSTROM_ARMS = ("No E-Mail", "Mens E-Mail", "Womens E-Mail")
HILLSTROM_MARGIN = 0.4
HILLSTROM_EMAIL_COST = 0.001


def hillstrom_frame(path: Path, limit: int | None = None) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if limit:
        frame = frame.iloc[:limit].copy()
    frame["customer_id"] = [f"h{i:05d}" for i in range(len(frame))]
    return frame


def hillstrom(data_dir: Path, *, limit: int | None = None, frame: pd.DataFrame | None = None) -> BenchData:
    """Hillstrom e-mail RCT: men's / women's / no e-mail, 1/3 each, through the repo loader."""
    from autonomy.data.loaders import HILLSTROM_CAMPAIGN_AT, hillstrom_events

    if frame is None:
        frame = hillstrom_frame(data_dir / "hillstrom" / "hillstorm_no_indices.csv.gz", limit)
    goal, t0 = "goal_hillstrom", HILLSTROM_CAMPAIGN_AT
    now = (t0 + timedelta(days=15)).replace(hour=10)
    arm: npt.NDArray[np.int64] = np.asarray(
        frame["segment"].map({a: i for i, a in enumerate(HILLSTROM_ARMS)}).to_numpy(), dtype=np.int64
    )
    arm_content = {1: "hill_mens", 2: "hill_womens"}
    loaded = hillstrom_events(frame, campaign_at=t0, include_outcomes=True)
    by_c = _group(loaded.events)
    arm_of_c = dict(zip(frame["customer_id"].astype(str), arm.tolist(), strict=True))

    def is_outcome(e: Event) -> bool:
        return bool(e.attrs.get("outcome"))

    def pre_events(ids: Sequence[str]) -> dict[str, list[Event]]:
        return {
            c: [e for e in by_c.get(c, []) if e.type is not EventType.SEND and not is_outcome(e)] for c in ids
        }

    def campaign_events(ids: Sequence[str]) -> dict[str, list[Event]]:
        out: dict[str, list[Event]] = {}
        for c in ids:
            a = arm_of_c[c]
            evs: list[Event] = []
            send_did: str | None = None
            for e in by_c.get(c, []):
                if e.type is EventType.SEND:
                    attrs = dict(e.attrs)
                    attrs["content_id"] = arm_content[a]
                    attrs.setdefault("logged_probability", 1 / 3)
                    evs.append(e.model_copy(update={"attrs": attrs}))
                    send_did = e.decision_id
            for e in by_c.get(c, []):
                if not is_outcome(e):
                    continue
                if e.type is EventType.VISIT and a and send_did is not None:
                    # no click data: the visit after the e-mail is taken as its click (as family A)
                    upd = {
                        "type": EventType.CLICK,
                        "channel": Channel.EMAIL,
                        "decision_id": send_did,
                        "event_id": e.event_id + "_click",
                    }
                    evs.append(e.model_copy(update=upd))
                else:
                    evs.append(e)
            out[c] = evs
        return out

    feats = frame[
        ["recency", "history", "mens", "womens", "newbie", "history_segment", "zip_code", "channel"]
    ]
    spend = frame["spend"].to_numpy(dtype=np.float64)
    agent = AgentInputs(
        briefs=(_brief(goal, Channel.EMAIL, t0, "who to e-mail, and which e-mail"),),
        content=_content(
            goal,
            Channel.EMAIL,
            t0,
            [
                ("hill_mens", Theme.QUALITY, "The men's collection"),
                ("hill_womens", Theme.NOVELTY, "The women's collection"),
            ],
        ),
        costs={Channel.EMAIL: HILLSTROM_EMAIL_COST},
        channel=Channel.EMAIL,
        goal_id=goal,
        campaign_at=t0,
        now=now,
        arm_content=arm_content,
        pre_events=pre_events,
        campaign_events=campaign_events,
    )
    return BenchData(
        name="hillstrom",
        customer_ids=frame["customer_id"].astype(str).to_numpy(),
        arm=arm,
        propensity=np.full(len(frame), 1 / 3),
        arm_names=HILLSTROM_ARMS,
        features=feats.reset_index(drop=True),
        numeric=("recency", "history"),
        categorical=("mens", "womens", "newbie", "history_segment", "zip_code", "channel"),
        conversion=frame["conversion"].to_numpy(dtype=np.float64),
        margin_value=HILLSTROM_MARGIN * spend,
        arm_cost=(0.0, HILLSTROM_EMAIL_COST, HILLSTROM_EMAIL_COST),
        conversion_value=None,
        segment_candidates=(
            ("history_segment",),
            ("recency",),
            ("mens", "womens"),
            ("zip_code",),
            ("newbie",),
            ("channel",),
            ("history_segment", "mens", "womens"),
            ("recency", "mens", "womens"),
            ("newbie", "mens", "womens"),
            ("history_segment", "newbie"),
            ("recency", "newbie"),
        ),
        economics=f"profit = {HILLSTROM_MARGIN} x spend (USD, 2 weeks) - {HILLSTROM_EMAIL_COST} per e-mail",
        agent=agent,
        notes=(
            "men's and women's e-mails carry themes quality / novelty only so the agent can tell them apart",
            "a treated customer's visit is written as the e-mail's click (no click data)",
            f"repo loader hillstrom_events: {len(loaded.events)} events, {len(loaded.problems)} problems",
        ),
        sizes={"customers": len(frame), "fixed": limit is None},
    )


# --- X5 RetailHero -------------------------------------------------------------------------------

X5_ZONE = "Europe/Moscow"
X5_CAMPAIGN = datetime(2019, 3, 19, 7, 0, tzinfo=UTC)  # 10:00 Moscow, the day after the last receipt
X5_MARGIN = 0.25
X5_SMS_COST = 2.0


def x5_receipts(data_dir: Path, timings: Timings) -> pd.DataFrame:
    """45.8M purchase lines -> one row per receipt, cached in `<data-dir>/_cache` (as a_x5.py)."""
    cache = data_dir / "_cache" / "x5_receipts.pkl"
    if cache.exists():
        with timings.step("x5: load cached receipts"):
            rec: pd.DataFrame = pd.read_pickle(cache)
            return rec
    cols = [
        "client_id",
        "transaction_id",
        "transaction_datetime",
        "purchase_sum",
        "regular_points_spent",
        "express_points_spent",
    ]
    parts: list[pd.DataFrame] = []
    with timings.step("x5: purchase lines -> receipts (pandas, chunked)"):
        path = data_dir / "x5_retailhero" / "purchases.csv.gz"
        for chunk in pd.read_csv(path, usecols=cols, chunksize=2_000_000):
            parts.append(chunk.drop_duplicates("transaction_id"))
        rec = pd.concat(parts, ignore_index=True).drop_duplicates("transaction_id")
        rec["points_spent"] = -(rec["regular_points_spent"].fillna(0) + rec["express_points_spent"].fillna(0))
        rec = rec.drop(columns=["regular_points_spent", "express_points_spent"])
        cache.parent.mkdir(parents=True, exist_ok=True)
        rec.to_pickle(cache)
    return rec


def x5_features(rec: pd.DataFrame, clients: pd.DataFrame) -> pd.DataFrame:
    """Pre-period features per client: only what was known before the campaign."""
    ts = pd.to_datetime(rec["transaction_datetime"])
    end = pd.Timestamp("2019-03-19")
    g = rec.assign(ts=ts).groupby("client_id")
    f = pd.DataFrame(
        {
            "n_receipts": g.size(),
            "total": g["purchase_sum"].sum(),
            "mean_receipt": g["purchase_sum"].mean(),
            "recency_days": (end - g["ts"].max()).dt.days,
            "span_days": (g["ts"].max() - g["ts"].min()).dt.days,
            "points_spent": g["points_spent"].sum(),
        }
    )
    c = clients.set_index("client_id")
    f = f.join(c[["age", "gender"]], how="right")
    issue = pd.to_datetime(c["first_issue_date"], errors="coerce")
    f["card_days"] = (end - issue).dt.days
    f["redeemed"] = c["first_redeem_date"].notna().astype(int)
    f["age"] = f["age"].clip(0, 100)
    for col in ("n_receipts", "total", "mean_receipt", "points_spent", "span_days"):
        f[col] = f[col].fillna(0)
    f["recency_days"] = f["recency_days"].fillna(999)
    f["card_days"] = f["card_days"].fillna(f["card_days"].median())
    f["gender"] = f["gender"].fillna("U").astype(str)
    return f


def x5_events(rec: pd.DataFrame, clients: pd.DataFrame) -> list[Event]:
    """Receipts and profiles through the product's column mapping (`map_frame`)."""
    from autonomy.data.mapping import EventMapping, map_frame

    receipt_mapping = EventMapping(
        customer_id="client_id",
        timestamp="transaction_datetime",
        timezone=X5_ZONE,
        event_type=EventType.PURCHASE,
        value_column="purchase_sum",
        margin_column="margin",
        row_key=("transaction_id",),
        fixed_attrs={"timezone": X5_ZONE},
    )
    profile_mapping = EventMapping(
        customer_id="client_id",
        timestamp="ts",
        timezone=X5_ZONE,
        event_type_column="event",
        event_type_values={"signup": EventType.SIGNUP, "consent": EventType.CONSENT_GRANTED},
        channel_column="channel",
        row_key=("client_id", "event"),
        fixed_attrs={"timezone": X5_ZONE},
    )
    events, _ = map_frame(rec.assign(margin=X5_MARGIN * rec["purchase_sum"]), receipt_mapping, "x5")
    issue = pd.to_datetime(clients["first_issue_date"], errors="coerce").fillna(pd.Timestamp("2017-01-01"))
    prof = pd.DataFrame(
        {
            "client_id": np.repeat(clients["client_id"].to_numpy(), 2),
            "ts": np.repeat(issue.dt.strftime("%Y-%m-%d %H:%M:%S").to_numpy(), 2),
            "event": np.tile(["signup", "consent"], len(clients)),
            "channel": np.tile(["", "sms"], len(clients)),
        }
    )
    prof["channel"] = prof["channel"].replace("", None)
    ev2, _ = map_frame(prof, profile_mapping, "x5-profile")
    return [*events, *ev2]


def x5(data_dir: Path, timings: Timings, *, limit: int | None = None) -> BenchData:
    """X5 RetailHero: a randomised SMS (50/50) on a fixed 100,000 labelled clients, with real receipts."""
    root = data_dir / "x5_retailhero"
    n_clients = limit or SIZES["x5"]
    rec = x5_receipts(data_dir, timings)
    with timings.step(f"x5: pick a fixed {n_clients} labelled clients, features, events (map_frame)"):
        up = pd.read_csv(root / "uplift_train.csv.gz")
        clients = pd.read_csv(root / "clients.csv.gz")
        keep = pick_by_hash(up["client_id"].astype(str).to_numpy(), n_clients, salt=SAMPLE_SALT + ":x5")
        up = up.iloc[keep].reset_index(drop=True)
        labelled = set(up["client_id"])
        rec = rec[rec["client_id"].isin(labelled)].reset_index(drop=True)
        clients = clients[clients["client_id"].isin(labelled)].reset_index(drop=True)
        gc.collect()
        feats = x5_features(rec, clients).reindex(up["client_id"]).reset_index(drop=True)
        events = x5_events(rec, clients)
        n_receipts = len(rec)
        del rec
        by_c = _group(events)
        del events
        gc.collect()
    ids = up["client_id"].astype(str).to_numpy()
    arm = up["treatment_flg"].to_numpy(dtype=np.int64)
    target = up["target"].to_numpy(dtype=np.float64)
    mean_receipt = feats["mean_receipt"].to_numpy(dtype=np.float64)
    goal = "goal_x5"
    info = {
        c: (int(a), float(t), float(m)) for c, a, t, m in zip(ids, arm, target, mean_receipt, strict=True)
    }

    def pre_events(sel: Sequence[str]) -> dict[str, list[Event]]:
        return {c: by_c.get(c, []) for c in sel}

    def campaign_events(sel: Sequence[str]) -> dict[str, list[Event]]:
        out: dict[str, list[Event]] = {}
        for c in sel:
            a, t, m = info[c]
            evs: list[Event] = []
            did = f"x5_rct_{c}"
            if a:
                evs.append(
                    Event(
                        event_id=f"x5_send_{c}",
                        customer_id=c,
                        type=EventType.SEND,
                        ts=X5_CAMPAIGN,
                        source="x5-rct",
                        channel=Channel.SMS,
                        decision_id=did,
                        attrs={"content_id": "x5_sms", "logged_probability": 0.5},
                    )
                )
            if t > 0:
                # bought in the campaign window; the data has no basket value or time: the client's own
                # mean receipt, booked mid-week (see WINDOW_PURCHASE_AFTER)
                evs.append(
                    Event(
                        event_id=f"x5_buy_{c}",
                        customer_id=c,
                        type=EventType.PURCHASE,
                        ts=X5_CAMPAIGN + WINDOW_PURCHASE_AFTER,
                        source="x5-rct",
                        value=m,
                        margin=X5_MARGIN * m,
                        attrs={"timezone": X5_ZONE},
                    )
                )
            out[c] = evs
        return out

    agent = AgentInputs(
        briefs=(_brief(goal, Channel.SMS, X5_CAMPAIGN, "who should get the SMS"),),
        content=_content(
            goal, Channel.SMS, X5_CAMPAIGN, [("x5_sms", Theme.REMINDER, "Points offer this week.")]
        ),
        costs={Channel.SMS: X5_SMS_COST},
        channel=Channel.SMS,
        goal_id=goal,
        campaign_at=X5_CAMPAIGN,
        now=X5_CAMPAIGN + timedelta(days=15),
        arm_content={1: "x5_sms"},
        pre_events=pre_events,
        campaign_events=campaign_events,
    )
    value = X5_MARGIN * mean_receipt
    return BenchData(
        name="x5",
        customer_ids=ids,
        arm=arm,
        propensity=np.full(len(ids), 0.5),
        arm_names=("no SMS", "SMS"),
        features=feats,
        numeric=(
            "n_receipts",
            "total",
            "mean_receipt",
            "recency_days",
            "span_days",
            "points_spent",
            "age",
            "card_days",
        ),
        categorical=("gender", "redeemed"),
        conversion=target,
        margin_value=value * target,
        arm_cost=(0.0, X5_SMS_COST),
        conversion_value=value,
        segment_candidates=(
            ("recency_days",),
            ("n_receipts",),
            ("mean_receipt",),
            ("age",),
            ("gender",),
            ("redeemed",),
            ("card_days",),
            ("points_spent",),
            ("recency_days", "n_receipts"),
            ("n_receipts", "mean_receipt"),
            ("age", "gender"),
            ("recency_days", "redeemed"),
        ),
        economics=f"profit = {X5_MARGIN} x own pre-period mean receipt (RUB) x bought - {X5_SMS_COST} RUB per SMS",
        agent=agent,
        notes=(
            "SMS consent invented for every client; the SMS campaign logged no clicks",
            "campaign date assumed 2019-03-19 10:00 Moscow (the data has none)",
            "a campaign-window purchase is booked at the client's own pre-period mean receipt, "
            "3.5 days after the SMS (the data has no purchase time)",
        ),
        sizes={
            "labelled_clients_in_file": 200_039,
            "clients": len(ids),
            "receipts": n_receipts,
            "fixed": limit is None,
        },
    )


# --- feature-only RCTs (Criteo, MegaFon) ---------------------------------------------------------

FEATURE_T0 = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)  # these files have no dates; any fixed moment works


def _feature_rct(
    name: str,
    ids: np.ndarray,
    arm: np.ndarray,
    p_treated: float,
    feats: pd.DataFrame,
    conversion: np.ndarray,
    click: np.ndarray | None,
    candidates: tuple[tuple[str, ...], ...],
    notes: tuple[str, ...],
    sizes: dict[str, Any],
) -> BenchData:
    goal = f"goal_{name}"
    t0 = FEATURE_T0
    signup = t0 - timedelta(days=200)
    # break-even contact cost, measured on TRAIN only (the split is a pure function of the id)
    train, _ = split_by_hash(ids, salt=SPLIT_SALT)
    c_tr, a_tr = conversion[train], arm[train]
    ate = float(c_tr[a_tr == 1].mean() - c_tr[a_tr == 0].mean())
    cost = max(VALUE_OF_CONVERSION * ate, 0.001)
    info = {
        str(c): (int(a), float(v), bool(k))
        for c, a, v, k in zip(
            ids, arm, conversion, click if click is not None else np.zeros(len(ids)), strict=True
        )
    }

    def pre_events(sel: Sequence[str]) -> dict[str, list[Event]]:
        return {
            c: [
                Event(event_id=f"su_{c}", customer_id=c, type=EventType.SIGNUP, ts=signup, source=name),
                Event(
                    event_id=f"co_{c}",
                    customer_id=c,
                    type=EventType.CONSENT_GRANTED,
                    ts=signup,
                    source=name,
                    channel=Channel.EMAIL,
                ),
            ]
            for c in sel
        }

    def campaign_events(sel: Sequence[str]) -> dict[str, list[Event]]:
        out: dict[str, list[Event]] = {}
        for c in sel:
            a, conv, clicked = info[c]
            did = f"{name}_rct_{c}"
            evs: list[Event] = []
            if a:
                evs.append(
                    Event(
                        event_id=f"send_{c}",
                        customer_id=c,
                        type=EventType.SEND,
                        ts=t0,
                        source=name,
                        channel=Channel.EMAIL,
                        decision_id=did,
                        attrs={"content_id": f"{name}_msg", "logged_probability": p_treated},
                    )
                )
            if clicked:
                if a:
                    evs.append(
                        Event(
                            event_id=f"click_{c}",
                            customer_id=c,
                            type=EventType.CLICK,
                            ts=t0 + timedelta(hours=5),
                            source=name,
                            channel=Channel.EMAIL,
                            decision_id=did,
                        )
                    )
                else:
                    evs.append(
                        Event(
                            event_id=f"visit_{c}",
                            customer_id=c,
                            type=EventType.VISIT,
                            ts=t0 + timedelta(hours=5),
                            source=name,
                        )
                    )
            if conv > 0:
                evs.append(
                    Event(
                        event_id=f"buy_{c}",
                        customer_id=c,
                        type=EventType.PURCHASE,
                        ts=t0 + WINDOW_PURCHASE_AFTER,
                        source=name,
                        value=ORDER_VALUE,
                        margin=VALUE_OF_CONVERSION,
                    )
                )
            out[c] = evs
        return out

    agent = AgentInputs(
        briefs=(_brief(goal, Channel.EMAIL, t0, "who to contact"),),
        content=_content(goal, Channel.EMAIL, t0, [(f"{name}_msg", Theme.REMINDER, "Offer.")]),
        costs={Channel.EMAIL: cost},
        channel=Channel.EMAIL,
        goal_id=goal,
        campaign_at=t0,
        now=t0 + timedelta(days=15),
        arm_content={1: f"{name}_msg"},
        pre_events=pre_events,
        campaign_events=campaign_events,
    )
    numeric = tuple(str(c) for c in feats.columns)
    return BenchData(
        name=name,
        customer_ids=ids,
        arm=arm.astype(np.int64),
        propensity=np.where(arm == 1, p_treated, 1.0 - p_treated),
        arm_names=("control", "treated"),
        features=feats.reset_index(drop=True),
        numeric=numeric,
        categorical=(),
        conversion=conversion.astype(np.float64),
        margin_value=VALUE_OF_CONVERSION * conversion.astype(np.float64),
        arm_cost=(0.0, cost),
        conversion_value=np.full(len(ids), VALUE_OF_CONVERSION),
        segment_candidates=candidates,
        economics=(
            f"profit = {VALUE_OF_CONVERSION:g} x conversion - {cost:.5f} per contact "
            f"(break-even cost on train: {VALUE_OF_CONVERSION:g} x train effect {ate:+.5f})"
        ),
        agent=agent,
        notes=(
            *notes,
            "consent invented for every customer; no history, no dates, no amounts in the file",
            "a conversion is booked as a purchase 3.5 days after the campaign (the data has no time)",
        ),
        sizes={**sizes, "train_effect": ate, "break_even_cost": cost},
    )


def criteo_sample(data_dir: Path, n_rows: int, timings: Timings) -> pd.DataFrame:
    """A fixed sample of Criteo rows: the `n_rows` smallest SplitMix64 hashes of the row number.

    The file is in 13 treated/control blocks (its first 5.57M rows are all treated), so a head slice
    is not a sample. Cached in `<data-dir>/_cache` (outside the repo)."""
    cache = data_dir / "_cache" / f"realbench_criteo_{n_rows}.pkl"
    if cache.exists():
        with timings.step("criteo: load cached sample"):
            cached: pd.DataFrame = pd.read_pickle(cache)
            return cached
    total = 13_979_592
    key = salt_key(SAMPLE_SALT + ":criteo")
    h = splitmix64(np.arange(total, dtype=np.uint64) ^ key)
    keep = np.zeros(total, dtype=bool)
    keep[np.argpartition(h, n_rows)[:n_rows]] = True
    del h
    dtypes: dict[str, Any] = {f"f{i}": np.float32 for i in range(12)} | {
        "treatment": np.int8,
        "visit": np.int8,
        "conversion": np.int8,
        "exposure": np.int8,
    }
    parts: list[pd.DataFrame] = []
    start = 0
    with timings.step(f"criteo: read all rows in chunks, keep a fixed {n_rows}"):
        path = data_dir / "criteo_uplift" / "criteo-research-uplift-v2.1.csv.gz"
        for chunk in pd.read_csv(path, dtype=dtypes, chunksize=1_000_000):
            m = keep[start : start + len(chunk)]
            part = chunk[m].copy()
            part.insert(0, "row", np.flatnonzero(m) + start)
            parts.append(part)
            start += len(chunk)
        if start != total:
            raise ValueError(f"criteo: expected {total} rows, read {start}")
        frame = pd.concat(parts, ignore_index=True)
        cache.parent.mkdir(parents=True, exist_ok=True)
        frame.to_pickle(cache)
    return frame


def criteo(data_dir: Path, timings: Timings, *, limit: int | None = None) -> BenchData:
    """Criteo uplift v2.1 (85/15): a fixed 300,000-row sample; treatment = eligible for ads."""
    n_rows = limit or SIZES["criteo"]
    frame = criteo_sample(data_dir, n_rows, timings)
    ids = np.array([f"cr{int(r)}" for r in frame["row"]], dtype=object)
    arm = frame["treatment"].to_numpy(dtype=np.int64)
    feats = frame[[f"f{i}" for i in range(12)]].astype(np.float64)
    return _feature_rct(
        "criteo",
        ids,
        arm,
        0.85,
        feats,
        frame["conversion"].to_numpy(dtype=np.float64),
        frame["visit"].to_numpy() > 0,
        (("f0",), ("f2",), ("f4",), ("f8",), ("f9",), ("f2", "f8")),
        (
            "treatment is eligibility for ads (85/15 by design), not a message",
            "a treated customer's visit is written as the message's click; a control visit as a visit",
        ),
        {"rows_in_file": 13_979_592, "rows": len(ids), "fixed": limit is None},
    )


def megafon(data_dir: Path, timings: Timings, *, limit: int | None = None) -> BenchData:
    """MegaFon uplift (generated by MegaFon from telecom data, 50/50): a fixed 200,000-row sample."""
    n_rows = limit or SIZES["megafon"]
    with timings.step("megafon: read and pick a fixed sample"):
        frame = pd.read_csv(data_dir / "megafon" / "megafon_dataset.csv.gz")
        all_ids = np.array([f"mf{i}" for i in range(len(frame))], dtype=object)
        keep = pick_by_hash(all_ids, n_rows, salt=SAMPLE_SALT + ":megafon")
        frame = frame.iloc[keep].reset_index(drop=True)
        ids = all_ids[keep]
    cols = [f"X_{i}" for i in range(1, 51)]
    arm = (frame["treatment_group"] == "treatment").to_numpy().astype(np.int64)
    return _feature_rct(
        "megafon",
        ids,
        arm,
        0.5,
        frame[cols].astype(np.float64),
        frame["conversion"].to_numpy(dtype=np.float64),
        None,
        (("X_1",), ("X_2",), ("X_3",), ("X_1", "X_2"), ("X_4",), ("X_5",)),
        ("generated by MegaFon from real telecom data; 50/50 by design; no click-like signal",),
        {"rows_in_file": 600_000, "rows": len(ids), "fixed": limit is None},
    )


def load(name: str, data_dir: Path, timings: Timings, *, limit: int | None = None) -> BenchData:
    """The bench adapter for `name` (one of `DATASETS`)."""
    if name == "hillstrom":
        with timings.step("hillstrom: read, add customer ids, map rows (repo loader)"):
            return hillstrom(data_dir, limit=limit)
    if name == "x5":
        return x5(data_dir, timings, limit=limit)
    if name == "criteo":
        return criteo(data_dir, timings, limit=limit)
    if name == "megafon":
        return megafon(data_dir, timings, limit=limit)
    raise ValueError(f"unknown dataset {name!r}; choose from {', '.join(DATASETS)}")
