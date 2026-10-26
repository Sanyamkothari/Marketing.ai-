"""Hillstrom e-mail RCT (64,000 customers, 3 arms of 1/3): the agent vs simple policies, off-policy.

    python3 -I scripts/v2_realdata/a_hillstrom.py                 # full run (about 10 minutes)
    python3 -I scripts/v2_realdata/a_hillstrom.py --limit 6000 --seeds 2   # quick check

What it does, in order:

1. Reads `/home/user/datasets/hillstrom/hillstorm_no_indices.csv.gz` (the full public file, which
   has no customer id) and adds `customer_id = h<row>` (the one adapter step a customer would need).
2. Maps rows to events with the repo loader `autonomy.data.loaders.hillstrom_events` (signup,
   e-mail consent, an approximate purchase from `history`/`recency`, the SEND for treated rows, the
   visit and purchase outcomes). Checks what the warm start reads from the loader's SEND as-is.
3. Splits customers 50/50 (seed 0). Customer states are folded from PRE-campaign events only (no
   send, no outcome) for everyone, with the production `StateFolder`, as of 15 days after the
   campaign (so the train outcomes are in the agent's past).
4. The agent learns from TRAIN only, two ways (see `a_common.run_agent_variants`): the default click
   model warm-started from the train sends and their visits (taken as the e-mail's click), and the
   purchase-uplift bundle fed the train RCT through `learn_from_rewards`. Men's and women's e-mails
   are two approved content items; the content model has no "product line" label, so they carry
   two different themes (quality = men's, novelty = women's) only so the agent can tell them apart.
5. On TEST, every policy is scored with the repo's IPS / SNIPS / DR; the gain over sending nothing
   is a paired DR difference with a 95% interval.

Rewards: conversion, spend (US$ over two weeks) and profit = 40% x spend - cost per e-mail, under
two costs: $0.001 (the agent's default e-mail cost) and $0.25 (a costly contact, to see targeting).
The agent is told the same cost. Results: /home/user/datasets/_results/a_hillstrom.json.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

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
from autonomy.data.loaders import HILLSTROM_CAMPAIGN_AT, hillstrom_decision_id, hillstrom_events
from autonomy.learning.observations import logged_sends

PATH = C.DATASETS / "hillstrom" / "hillstorm_no_indices.csv.gz"
ARMS = ("No E-Mail", "Mens E-Mail", "Womens E-Mail")
GOAL = "goal_hillstrom"
T0 = HILLSTROM_CAMPAIGN_AT
NOW = (T0 + timedelta(days=15)).replace(hour=10)
CONTENT_OF_ARM = {1: "hill_mens", 2: "hill_womens"}
ARM_OF_CONTENT = {v: k for k, v in CONTENT_OF_ARM.items()}
MARGIN = 0.4
COSTS = (0.001, 0.25)


def brief() -> Brief:
    return Brief(
        brief_id=f"brief_{GOAL}_1",
        goal_id=GOAL,
        objective=Objective.GROSS_PROFIT,
        audience=Audience(description="Every customer in the test"),
        horizon_days=14,
        envelope=Envelope(channels=(Channel.EMAIL,), budget=1e9, max_contacts_per_week=2),
        rationale="Real-data replay: who to e-mail, and which e-mail.",
        created_at=T0 - timedelta(days=30),
        approved_by="realdata",
        approved_at=T0 - timedelta(days=30),
    )


def content() -> list[ContentItem]:
    rows = [
        ("hill_mens", Theme.QUALITY, "The men's collection"),
        ("hill_womens", Theme.NOVELTY, "The women's collection"),
    ]
    return [
        ContentItem(
            content_id=cid,
            goal_id=GOAL,
            channel=Channel.EMAIL,
            theme=theme,
            subject=subject,
            body="New this season.",
            status=ContentStatus.APPROVED,
            created_at=T0 - timedelta(days=30),
        )
        for cid, theme, subject in rows
    ]


def arm_of(d: Decision) -> int:
    if d.kind is DecisionKind.NOTHING or d.action is None:
        return 0
    return ARM_OF_CONTENT[d.action.content_id or ""]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="use only the first N rows (quick check)")
    ap.add_argument("--seeds", type=int, default=4, help="agent decisions per test customer")
    ap.add_argument("--variants", default="click,purchase")
    args = ap.parse_args()
    timings = C.Timings()
    payload: dict[str, Any] = {"dataset": "hillstrom", "command": " ".join(["python3 -I", *sys.argv])}

    with timings.step("read csv"):
        frame = pd.read_csv(PATH)
        if args.limit:
            frame = frame.iloc[: args.limit].copy()
        frame["customer_id"] = [f"h{i:05d}" for i in range(len(frame))]
    n = len(frame)
    arm = frame["segment"].map({a: i for i, a in enumerate(ARMS)}).to_numpy(dtype=np.int64)
    feats = frame[
        ["recency", "history", "mens", "womens", "newbie", "history_segment", "zip_code", "channel"]
    ].copy()
    rct = C.Rct(
        name="hillstrom",
        customer_ids=frame["customer_id"].to_numpy(),
        arm=arm,
        propensity=np.full(n, 1 / 3),
        arm_names=ARMS,
        features=feats.reset_index(drop=True),
        outcomes={
            "visit": frame["visit"].to_numpy(dtype=float),
            "conversion": frame["conversion"].to_numpy(dtype=float),
            "spend": frame["spend"].to_numpy(dtype=float),
        },
    )
    tr_idx, te_idx = C.split_customers(n, seed=0)
    train, test = rct.take(tr_idx), rct.take(te_idx)
    payload["split"] = {"train": train.n, "test": test.n, "seed": 0}
    print(f"{n} customers: train {train.n}, test {test.n}")

    # --- events through the repo loader -------------------------------------------------------
    with timings.step("map rows to events (repo loader hillstrom_events)"):
        loaded = hillstrom_events(frame, campaign_at=T0, include_outcomes=True)
    payload["loader"] = {"events": len(loaded.events), "problems": len(loaded.problems)}
    print(f"    {len(loaded.events)} events, {len(loaded.problems)} problems")
    by_c: dict[str, list[Event]] = {}
    for e in loaded.events:
        by_c.setdefault(e.customer_id, []).append(e)

    # what the warm start reads from the loader's SEND as it is
    sample_ids = list(train.customer_ids[:3000])
    as_is = logged_sends({c: by_c[c] for c in sample_ids}, {})
    payload["loader_send_check"] = {
        "sends": len(as_is),
        "with_propensity": sum(1 for s in as_is if s.propensity is not None),
        "with_theme": sum(1 for s in as_is if s.theme is not None),
    }
    print(f"    loader SENDs as the warm start reads them: {payload['loader_send_check']}")

    def is_outcome(e: Event) -> bool:
        return bool(e.attrs.get("outcome"))

    pre = [e for e in loaded.events if e.type is not EventType.SEND and not is_outcome(e)]
    with timings.step("fold pre-campaign states (rebuild_states, StateFolder)"):
        states = C.fold_states(pre, NOW)
    train_states = [states[c] for c in train.customer_ids]
    test_states = [states[c] for c in test.customer_ids]
    seg_counts = pd.Series([s.segment for s in states.values()]).value_counts().to_dict()
    payload["state_segments_folder"] = seg_counts
    print(f"    {len(states)} states; folder segments {seg_counts}")

    # --- the train log as the agent's learning API takes it ------------------------------------
    train_set = set(train.customer_ids)
    history: dict[str, list[Event]] = {}
    decisions: dict[str, Decision] = {}
    rewards: list[Reward] = []
    for i, cid in enumerate(train.customer_ids):
        a = int(train.arm[i])
        evs = []
        did = hillstrom_decision_id(cid) if a else f"rct_{cid}"
        for e in by_c[cid]:
            if e.type is EventType.SEND:
                attrs = {**e.attrs, "content_id": CONTENT_OF_ARM[a], "logged_probability": 1 / 3}
                evs.append(e.model_copy(update={"attrs": attrs}))
            elif e.type is EventType.VISIT and is_outcome(e) and a:
                # the visit after a treated customer's e-mail is the closest thing to its click
                evs.append(
                    e.model_copy(
                        update={
                            "type": EventType.CLICK,
                            "channel": Channel.EMAIL,
                            "decision_id": did,
                            "event_id": e.event_id + "_click",
                        }
                    )
                )
            else:
                evs.append(e)
        history[cid] = evs
        spend = float(train.outcomes["spend"][i])
        if a:
            act = Action(goal_id=GOAL, channel=Channel.EMAIL, content_id=CONTENT_OF_ARM[a], send_at=T0)
            decisions[did] = Decision(
                decision_id=did,
                customer_id=cid,
                made_at=T0,
                kind=DecisionKind.ACT,
                action=act,
                group=Group.AGENT,
                probability=1 / 3,
                reason="RCT arm",
                policy_version="hillstrom-rct",
            )
        else:
            decisions[did] = Decision(
                decision_id=did,
                customer_id=cid,
                made_at=T0,
                kind=DecisionKind.NOTHING,
                group=Group.HOLDOUT,
                probability=1.0,
                reason="RCT control",
                policy_version="hillstrom-rct",
            )
        rewards.append(
            Reward(
                decision_id=did,
                customer_id=cid,
                goal_id=GOAL if a else None,
                window_end=T0 + timedelta(days=14),
                converted=bool(train.outcomes["conversion"][i]),
                revenue=spend,
                margin=MARGIN * spend,
                cost=0.0,
                net=MARGIN * spend,
            )
        )
    assert train_set == set(history)
    train_log = C.TrainLog(history=history, decisions=decisions, rewards=rewards)

    variants = [v for v in args.variants.split(",") if v]
    seeds = list(range(1, args.seeds + 1))
    payload["economics"] = {}
    for cost in COSTS:
        econ = C.Economics(MARGIN, cost, "USD", f"profit = {MARGIN} x spend - {cost} per e-mail")
        key = f"cost_{cost:g}"
        print(f"\n===== economics: {econ.note} =====")
        runs = C.run_agent_variants(
            variants=variants,
            train=train_log,
            train_states=train_states,
            test_states=test_states,
            briefs=[brief()],
            content=content(),
            now=NOW,
            arm_of=arm_of,
            n_arms=3,
            seeds=seeds,
            costs={Channel.EMAIL: cost},
            horizon_days=14,
            timings=timings,
        )
        profit_tr = econ.profit(train.outcomes["spend"], train.arm)
        profit_te = econ.profit(test.outcomes["spend"], test.arm)
        with timings.step("baselines on train"):
            seg_pol, seg_table = C.best_segment_rule(
                train,
                profit_tr,
                [
                    ["history_segment"],
                    ["recency"],
                    ["mens", "womens"],
                    ["zip_code"],
                    ["newbie"],
                    ["channel"],
                    ["history_segment", "mens", "womens"],
                    ["recency", "mens", "womens"],
                    ["newbie", "mens", "womens"],
                    ["history_segment", "newbie"],
                    ["recency", "newbie"],
                ],
            )
            design = C.Design(
                numeric=("recency", "history"),
                categorical=("mens", "womens", "newbie", "history_segment", "zip_code", "channel"),
            )
            up_pi, _ = C.uplift_t_learner(train, test, profit_tr, design)
        k = 3
        match = np.zeros((test.n, k))
        womens_only = (test.features["womens"].to_numpy() == 1) & (test.features["mens"].to_numpy() == 0)
        match[np.arange(test.n), np.where(womens_only, 2, 1)] = 1.0
        policies = {
            "send to none": C.fixed(test.n, k, 0),
            "send all men's e-mail": C.fixed(test.n, k, 1),
            "send all women's e-mail": C.fixed(test.n, k, 2),
            "random (uniform over 3 arms)": C.uniform(test.n, k),
            "rule: e-mail matching past category": match,
            f"best segment rule ({'+'.join(seg_pol.columns)})": seg_pol(test.logged(profit_te, "p")),
            "uplift model (T-learner, ridge)": up_pi,
        }
        for v, run in runs.items():
            policies[f"AGENT ({v} model)"] = run.pi
        block: dict[str, object] = {
            "economics": econ.note,
            "segment_rule_validation": seg_table,
            "segment_rule_columns": list(seg_pol.columns),
            "agent": {v: C.agent_summary(r) for v, r in runs.items()},
        }
        C.save_matrices(f"a_hillstrom_{key}", policies)
        for rname, rew, rew_tr in (
            ("conversion", test.outcomes["conversion"], train.outcomes["conversion"]),
            ("spend", test.outcomes["spend"], train.outcomes["spend"]),
            ("profit", profit_te, profit_tr),
        ):
            if rname != "profit" and cost != COSTS[0]:
                continue  # conversion and spend do not depend on the cost; report them once
            ref_arm = C.best_blanket_arm(train, rew_tr)
            block[f"{rname}_best_blanket_arm_on_train"] = ARMS[ref_arm]
            rows = C.evaluate_policies(test, rew, rname, policies, reference=C.fixed(test.n, k, ref_arm))
            C.print_table(f"TEST {rname} ({econ.note if rname == 'profit' else 'per customer'})", rows)
            block[rname] = rows
        # content choice: among customers we e-mail anyway, which e-mail?
        emailed = {"random content (50/50)": np.tile([0.0, 0.5, 0.5], (test.n, 1))}
        for name, pi in policies.items():
            if name in ("send to none", "random (uniform over 3 arms)"):
                continue
            send = pi[:, 1:].sum(axis=1)
            choice = np.where(send[:, None] > 0, pi[:, 1:] / np.maximum(send[:, None], 1e-12), 0.5)
            m = np.zeros_like(pi)
            m[:, 1:] = choice
            emailed[f"{name}: its content pick"] = m
        ref_c = 1 + int(np.argmax([profit_tr[train.arm == a].mean() for a in (1, 2)]))
        rows = C.evaluate_policies(test, profit_te, "profit", emailed, reference=C.fixed(test.n, k, ref_c))
        C.print_table("TEST content choice (always e-mail; the policy only picks men's vs women's)", rows)
        block["content_choice_profit"] = rows
        payload["economics"][key] = block

    payload["timings"] = timings.rows
    C.save("a_hillstrom" + (f"_limit{args.limit}" if args.limit else ""), payload)


if __name__ == "__main__":
    main()
