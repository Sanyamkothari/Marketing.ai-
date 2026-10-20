"""X5 RetailHero (SMS RCT on 200,039 clients + 45.8M real pre-campaign purchase lines): agent vs rules.

    python3 -I scripts/v2_realdata/a_x5.py                        # all 200,039 labelled clients
    python3 -I scripts/v2_realdata/a_x5.py --customers 20000 --seeds 2   # a quick sample
    python3 -I scripts/v2_realdata/a_x5.py --scale-only           # only the product-path scale test

Steps (each timed, with peak RSS):

1. Adapter (pandas, chunked): the 45,786,568 purchase lines become one row per receipt
   (`transaction_id`, client, time, `purchase_sum`, points spent). Cached as a pickle in
   /home/user/datasets/_cache/ so a rerun skips it.
2. Product path: receipts -> `map_frame` with an `EventMapping` (type purchase, value
   `purchase_sum`, margin = 25% of it, time zone Europe/Moscow, key `transaction_id`), plus a
   signup at `first_issue_date` and an SMS `consent_granted` for every client (invented: the data
   has no consent; the control half was eligible for the same SMS) -> `InMemoryEventStore` ->
   `rebuild_states` with the production `StateFolder`.
3. Split the labelled clients 50/50 by client (seed 0). The agent learns from TRAIN only (the SMS
   SEND of each treated train client with `logged_probability` 0.5; there are no clicks in this data)
   two ways (`a_common.run_agent_variants`), and decides for TEST.
4. Baselines on the same split: none, all, random 50/50, the best segment rule and a T-learner
   uplift model on pre-period features (receipts, spend, recency, tenure, age, gender, points).
5. On TEST: IPS / SNIPS / DR of `target` (bought in the campaign window) and of an assumed profit:
   25% margin x the client's own pre-period mean receipt if they bought, minus 2 RUB per SMS. The
   data has no basket value for the campaign window, so profit is an assumption-laden secondary
   reward; `target` is the primary one. Results: /home/user/datasets/_results/a_x5*.json.

The treatment share is 0.4998 (99,981 of 200,039); the design probability 0.5 is used, as the
contest describes a random split.
"""

from __future__ import annotations

import argparse
import gc
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import a_common as C  # noqa: N812

from autonomy.contracts import (
    Action,
    Audience,
    Brief,
    Channel,
    ContentItem,
    ContentStatus,
    Decision,
    DecisionKind,
    Envelope,
    Event,
    EventType,
    Group,
    Objective,
    Reward,
    Theme,
)
from autonomy.data.mapping import EventMapping, map_frame

ROOT = C.DATASETS / "x5_retailhero"
CACHE = C.DATASETS / "_cache"
RECEIPTS = CACHE / "x5_receipts.pkl"
GOAL = "goal_x5"
ZONE = "Europe/Moscow"
T_CAMPAIGN = datetime(2019, 3, 19, 7, 0, tzinfo=UTC)  # 10:00 Moscow, the day after the last purchase
NOW = T_CAMPAIGN + timedelta(days=15)
MARGIN = 0.25
SMS_COST = 2.0

RECEIPT_MAPPING = EventMapping(
    customer_id="client_id",
    timestamp="transaction_datetime",
    timezone=ZONE,
    event_type=EventType.PURCHASE,
    value_column="purchase_sum",
    margin_column="margin",
    row_key=("transaction_id",),
    fixed_attrs={"timezone": ZONE},
)
PROFILE_MAPPING = EventMapping(
    customer_id="client_id",
    timestamp="ts",
    timezone=ZONE,
    event_type_column="event",
    event_type_values={"signup": EventType.SIGNUP, "consent": EventType.CONSENT_GRANTED},
    channel_column="channel",
    row_key=("client_id", "event"),
    fixed_attrs={"timezone": ZONE},
)


def build_receipts(timings: C.Timings) -> pd.DataFrame:
    if RECEIPTS.exists():
        with timings.step("load cached receipts"):
            return pd.read_pickle(RECEIPTS)
    cols = [
        "client_id",
        "transaction_id",
        "transaction_datetime",
        "purchase_sum",
        "regular_points_spent",
        "express_points_spent",
    ]
    parts: list[pd.DataFrame] = []
    lines = 0
    with timings.step("adapter: 45.8M purchase lines -> receipts (pandas, chunked)"):
        for chunk in pd.read_csv(ROOT / "purchases.csv.gz", usecols=cols, chunksize=2_000_000):
            lines += len(chunk)
            parts.append(chunk.drop_duplicates("transaction_id"))
        rec = pd.concat(parts, ignore_index=True).drop_duplicates("transaction_id")
        rec["points_spent"] = -(rec["regular_points_spent"].fillna(0) + rec["express_points_spent"].fillna(0))
        rec = rec.drop(columns=["regular_points_spent", "express_points_spent"])
        print(f"    {lines} lines -> {len(rec)} receipts")
        CACHE.mkdir(parents=True, exist_ok=True)
        rec.to_pickle(RECEIPTS)
    return rec


def features(rec: pd.DataFrame, clients: pd.DataFrame) -> pd.DataFrame:
    """Pre-period features per client (pandas): only what was known before the campaign."""
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


def to_events(
    rec: pd.DataFrame, clients: pd.DataFrame, timings: C.Timings, label: str
) -> tuple[list[Event], int]:
    with timings.step(f"product path: map_frame receipts ({label}: {len(rec)} rows)"):
        frame = rec.assign(margin=MARGIN * rec["purchase_sum"])
        events, problems = map_frame(frame, RECEIPT_MAPPING, "x5")
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
    with timings.step(f"product path: map_frame signup + consent ({len(prof)} rows)"):
        ev2, p2 = map_frame(prof, PROFILE_MAPPING, "x5-profile")
    probs = len(problems) + len(p2)
    print(f"    {len(events) + len(ev2)} events, {probs} problems")
    return events + ev2, probs


def brief() -> Brief:
    return Brief(
        brief_id=f"brief_{GOAL}_1",
        goal_id=GOAL,
        objective=Objective.GROSS_PROFIT,
        audience=Audience(description="Every labelled client"),
        horizon_days=14,
        envelope=Envelope(channels=(Channel.SMS,), budget=1e12, max_contacts_per_week=2),
        rationale="Real-data replay: who should get the SMS.",
        created_at=T_CAMPAIGN - timedelta(days=30),
        approved_by="realdata",
        approved_at=T_CAMPAIGN - timedelta(days=30),
    )


CONTENT = [
    ContentItem(
        content_id="x5_sms",
        goal_id=GOAL,
        channel=Channel.SMS,
        theme=Theme.REMINDER,
        body="Points offer this week.",
        status=ContentStatus.APPROVED,
        created_at=T_CAMPAIGN - timedelta(days=30),
    )
]


def arm_of(d: Decision) -> int:
    return 0 if d.kind is DecisionKind.NOTHING else 1


def scale_test(rec: pd.DataFrame, clients: pd.DataFrame, timings: C.Timings, sizes: list[int]) -> list[dict]:
    """Product path (map_frame + store + rebuild_states) on growing client samples."""
    rows = []
    ids = clients["client_id"].to_numpy()
    rng = np.random.default_rng(7)
    order = rng.permutation(len(ids))
    for n in sizes:
        pick = set(ids[order[:n]])
        r = rec[rec["client_id"].isin(pick)]
        cl = clients[clients["client_id"].isin(pick)]
        t0 = time.perf_counter()
        events, _ = to_events(r, cl, timings, f"scale {n} clients")
        t1 = time.perf_counter()
        with timings.step(f"product path: rebuild_states for {n} clients ({len(events)} events)"):
            states = C.fold_states(events, NOW)
        t2 = time.perf_counter()
        rows.append(
            {
                "clients": n,
                "receipts": len(r),
                "events": len(events),
                "map_seconds": round(t1 - t0, 1),
                "fold_seconds": round(t2 - t1, 1),
                "states": len(states),
                "peak_rss_mb": round(C.peak_rss_mb(), 0),
            }
        )
        print(f"    scale row {rows[-1]}")
        del events, states
        gc.collect()
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--customers", type=int, default=0, help="sample this many labelled clients (0 = all)")
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--variants", default="click,purchase")
    ap.add_argument("--scale-only", action="store_true")
    ap.add_argument("--scale-sizes", default="10000,50000,100000")
    args = ap.parse_args()
    timings = C.Timings()
    payload: dict[str, object] = {"dataset": "x5_retailhero", "command": " ".join(["python3 -I", *sys.argv])}

    rec = build_receipts(timings)
    payload["receipts"] = len(rec)
    with timings.step("read uplift_train + clients"):
        up = pd.read_csv(ROOT / "uplift_train.csv.gz")
        clients = pd.read_csv(ROOT / "clients.csv.gz")
    payload["labelled"] = len(up)
    payload["treated_share"] = float(up["treatment_flg"].mean())

    if args.scale_only:
        sizes = [int(x) for x in args.scale_sizes.split(",")]
        payload["scale"] = scale_test(rec, clients, timings, sizes)
        payload["timings"] = timings.rows
        C.save("a_x5_scale", payload)
        return

    if args.customers:
        up = up.sample(n=args.customers, random_state=0).sort_values("client_id")
    up = up.reset_index(drop=True)
    labelled = set(up["client_id"])
    rec = rec[rec["client_id"].isin(labelled)].reset_index(drop=True)
    clients = clients[clients["client_id"].isin(labelled)].reset_index(drop=True)
    gc.collect()
    with timings.step("pre-period features (pandas) for the baselines"):
        feats = features(rec, clients).reindex(up["client_id"]).reset_index(drop=True)

    arm = up["treatment_flg"].to_numpy(dtype=np.int64)
    rct = C.Rct(
        name="x5_retailhero",
        customer_ids=up["client_id"].to_numpy(),
        arm=arm,
        propensity=np.full(len(up), 0.5),
        arm_names=("no SMS", "SMS"),
        features=feats,
        outcomes={
            "target": up["target"].to_numpy(dtype=float),
            "mean_receipt": feats["mean_receipt"].to_numpy(dtype=float),
        },
    )
    tr_idx, te_idx = C.split_customers(rct.n, seed=0)
    train, test = rct.take(tr_idx), rct.take(te_idx)
    payload["split"] = {"train": train.n, "test": test.n, "seed": 0}
    print(f"{rct.n} labelled clients: train {train.n}, test {test.n}; {len(rec)} receipts")

    events, n_prob = to_events(rec, clients, timings, "labelled clients")
    payload["mapping_problems"] = n_prob
    payload["events"] = len(events)
    del rec
    gc.collect()
    with timings.step(f"product path: rebuild_states ({len(events)} events)"):
        states = C.fold_states(events, NOW)
    payload["states"] = len(states)
    payload["state_segments_folder"] = (
        pd.Series([s.segment for s in states.values()]).value_counts().to_dict()
    )
    missing = [c for c in rct.customer_ids if c not in states]
    payload["clients_without_state"] = len(missing)
    print(
        f"    {len(states)} states; {len(missing)} labelled clients have no events; segments {payload['state_segments_folder']}"
    )
    by_c: dict[str, list[Event]] = {}
    for e in events:
        by_c.setdefault(e.customer_id, []).append(e)
    del events
    gc.collect()

    # the train log
    history: dict[str, list[Event]] = {}
    decisions: dict[str, Decision] = {}
    rewards: list[Reward] = []
    for i, cid in enumerate(train.customer_ids):
        a = int(train.arm[i])
        did = f"x5_rct_{cid}"
        evs = list(by_c.get(cid, []))
        if a:
            evs.append(
                Event(
                    event_id=f"x5_send_{cid}",
                    customer_id=cid,
                    type=EventType.SEND,
                    ts=T_CAMPAIGN,
                    source="x5-rct",
                    channel=Channel.SMS,
                    decision_id=did,
                    attrs={"content_id": "x5_sms", "logged_probability": 0.5},
                )
            )
            act = Action(goal_id=GOAL, channel=Channel.SMS, content_id="x5_sms", send_at=T_CAMPAIGN)
            decisions[did] = Decision(
                decision_id=did,
                customer_id=cid,
                made_at=T_CAMPAIGN,
                kind=DecisionKind.ACT,
                action=act,
                group=Group.AGENT,
                probability=0.5,
                reason="RCT arm",
                policy_version="x5-rct",
            )
        else:
            decisions[did] = Decision(
                decision_id=did,
                customer_id=cid,
                made_at=T_CAMPAIGN,
                kind=DecisionKind.NOTHING,
                group=Group.HOLDOUT,
                probability=1.0,
                reason="RCT control",
                policy_version="x5-rct",
            )
        history[cid] = evs
        conv = bool(train.outcomes["target"][i])
        rewards.append(
            Reward(
                decision_id=did,
                customer_id=cid,
                goal_id=GOAL if a else None,
                window_end=T_CAMPAIGN + timedelta(days=14),
                converted=conv,
                net=0.0,
            )
        )
    del by_c
    gc.collect()
    train_log = C.TrainLog(history=history, decisions=decisions, rewards=rewards)
    from autonomy.contracts import CustomerState

    def state_of(cid: str) -> CustomerState:
        s = states.get(cid)  # noqa: F821 (called before the del below)
        return s if s is not None else CustomerState(customer_id=cid, as_of=NOW)

    train_states = [state_of(c) for c in train.customer_ids]
    test_states = [state_of(c) for c in test.customer_ids]
    runs = C.run_agent_variants(
        variants=[v for v in args.variants.split(",") if v],
        train=train_log,
        train_states=train_states,
        test_states=test_states,
        briefs=[brief()],
        content=CONTENT,
        now=NOW,
        arm_of=arm_of,
        n_arms=2,
        seeds=list(range(1, args.seeds + 1)),
        costs={Channel.SMS: SMS_COST},
        horizon_days=14,
        timings=timings,
    )
    del history, train_log, states
    gc.collect()

    def profit(r: C.Rct) -> np.ndarray:
        return MARGIN * r.outcomes["mean_receipt"] * r.outcomes["target"] - SMS_COST * (r.arm != 0)

    payload["agent"] = {v: C.agent_summary(r) for v, r in runs.items()}
    cands = [
        ["recency_days"],
        ["n_receipts"],
        ["mean_receipt"],
        ["age"],
        ["gender"],
        ["redeemed"],
        ["card_days"],
        ["points_spent"],
        ["recency_days", "n_receipts"],
        ["n_receipts", "mean_receipt"],
        ["age", "gender"],
        ["recency_days", "redeemed"],
    ]
    design = C.Design(
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
    )
    for rname, rew_tr, rew_te in (
        ("target", train.outcomes["target"], test.outcomes["target"]),
        ("profit", profit(train), profit(test)),
    ):
        with timings.step(f"baselines for {rname}"):
            seg_pol, seg_table = C.best_segment_rule(train, rew_tr, cands)
            up_pi, _ = C.uplift_t_learner(train, test, rew_tr, design)
        policies = {
            "send to none": C.fixed(test.n, 2, 0),
            "send to all": C.fixed(test.n, 2, 1),
            "random 50/50": C.uniform(test.n, 2),
            f"best segment rule ({'+'.join(seg_pol.columns)})": seg_pol(test.logged(rew_te, rname)),
            "uplift model (T-learner, ridge)": up_pi,
        }
        for v, run in runs.items():
            policies[f"AGENT ({v} model)"] = run.pi
        ref_arm = C.best_blanket_arm(train, rew_tr)
        payload[f"{rname}_best_blanket_arm_on_train"] = ref_arm
        C.save_matrices(f"a_x5_{rname}" + (f"_n{args.customers}" if args.customers else ""), policies)
        rows = C.evaluate_policies(test, rew_te, rname, policies, reference=C.fixed(test.n, 2, ref_arm))
        C.print_table(f"TEST {rname}", rows)
        payload[rname] = rows
        payload[f"{rname}_segment_rule_validation"] = seg_table
    payload["economics"] = (
        f"profit = {MARGIN} x own pre-period mean receipt (RUB) x target - {SMS_COST} RUB per SMS (assumptions)"
    )
    payload["timings"] = timings.rows
    C.save("a_x5" + (f"_n{args.customers}" if args.customers else ""), payload)


if __name__ == "__main__":
    main()
