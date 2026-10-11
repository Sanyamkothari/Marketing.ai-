"""The gate plans each customer of a run that chose the offer on the offer's own action, by arm (Plan K M134).

The first review's major finding: the gate worked out a row's catalogue action from its offer's *label*. Two
actions that share a label then both mapped to no action, so their customers were planned on the use case's
configured channels instead of their offer's, a customer the run sent by SMS could be moved to another channel
that offer is not planned on, and the message was never checked.

Here the run's own reader (`_read_offer_choice`), the list's own choice (`_apply_offer_choice`) and the row
actions the treat list hands the gate (`_row_actions`) run over an `offer_choice.parquet` and `offer_choice.json`
written as the scoring run writes them, then `gate_treat_list` decides, writes and signs. Two catalogue actions
share the label "10% off": `disc_sms` (SMS only) and `disc_email` (email only); a third offer has no catalogue
action and is planned, by the choice and by the gate, on the configured channels (push, then SMS). One customer
is explored onto the email offer, which no customer was given by the choice: its action comes from the summary's
`arms`, not from the file's `offer_action_id`.

On the unchanged code (the label lookup) every row of the two shared-label offers has no action, the SMS and
email customers are planned on push and SMS, and the email customers are moved to push: these tests fail there.
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.agentic.ledger import DecisionLedger, signer_for_data_dir
from engine.compliance.handoff import gate_treat_list
from engine.config import load_use_case
from engine.decide.catalogue import catalogue_sha256, clear_catalogue_cache
from engine.decide.offer_run import OfferArmSummary, OfferChoiceSummary
from engine.decide.treat_list import (
    _apply_offer_choice,
    _arm_actions,
    _join_keys,
    _read_offer_choice,
    _row_actions,
)
from engine.platform_db import sqlite_engine
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now
from tests.unit.compliance.switch import set_gate_switch

RUN = "r_20261011_0c134a01"
KEY = "customer_id"
CONFIGURED = ("push", "sms")
CATALOGUE = """\
actions:
  - action_id: disc_sms
    label: 10% off
    channels: [sms]
    offer_cost: 5.0
    contact_cost: 0.2
  - action_id: disc_email
    label: 10% off
    channels: [email]
    offer_cost: 5.0
    contact_cost: 0.1
"""
PLANNED = {"disc_sms": ("sms",), "disc_email": ("email",)}
ARMS: dict[int, tuple[str, str | None, tuple[str, ...]]] = {
    1: ("sms_discount", "disc_sms", ("sms",)),
    2: ("email_discount", "disc_email", ("email",)),
    3: (
        "10% off",
        None,
        CONFIGURED,
    ),  # no catalogue action: its label is its level, its channels the configured
}
"""Per offer position: level, catalogue action, channels (as `plan_arms` plans them)."""

# customer: (offer_arm, offer channel, explore_arm, explored, explore channel)
ROWS: dict[str, tuple[int, str | None, int, bool, str | None]] = {
    "C1": (1, "sms", 0, False, None),
    "C2": (1, "sms", 0, False, None),
    "C3": (3, "push", 0, False, None),
    "C4": (0, None, 2, True, "email"),  # explored onto the email offer, which the choice gave nobody
    "C5": (0, None, 0, False, None),  # no offer, not explored: not contacted
    "C6": (
        3,
        "sms",
        0,
        False,
        None,
    ),  # the action-less offer, on SMS: the run found them not reachable on push
}
PUSH_OK = {"C3"}
"""Per the run's `channel_contactability.parquet`: only C3 is reachable on push; everyone on SMS and email."""


@pytest.fixture
def root(tmp_path: Path, config_root: Path) -> Path:
    target = tmp_path / "configs"
    shutil.copytree(config_root, target)
    (target / "decide" / "catalogue.yaml").write_text(CATALOGUE, encoding="utf-8")
    set_gate_switch(target, on=True)
    clear_catalogue_cache()
    return target


def _write_choice(storage: LocalStorage, *, summary: bool) -> None:
    keys = list(ROWS)
    arm = np.array([ROWS[k][0] for k in keys], dtype=np.int64)
    explore_arm = np.array([ROWS[k][2] for k in keys], dtype=np.int64)
    level = {0: None, **{k: v[0] for k, v in ARMS.items()}}
    action = {0: None, **{k: v[1] for k, v in ARMS.items()}}
    label = {0: None, 1: "10% off", 2: "10% off", 3: "10% off"}
    table = pd.DataFrame(
        {
            KEY: keys,
            "offer_arm": arm,
            "offer_level": [level[int(a)] for a in arm],
            "offer_action_id": [action[int(a)] for a in arm],
            "offer_label": [label[int(a)] for a in arm],
            "offer_channel": [ROWS[k][1] for k in keys],
            "offer_net_value": np.where(arm > 0, 40.0, np.nan),
            "offer_total_cost": np.where(arm > 0, 5.0, 0.0),
            "runner_up_arm": np.zeros(len(keys), dtype=np.int64),
            "runner_up_label": [None] * len(keys),
            "runner_up_channel": [None] * len(keys),
            "runner_up_net_value": np.full(len(keys), np.nan),
            "offer_reason": ["offer" if a > 0 else "below_cost" for a in arm],
            "explore_arm": explore_arm,
            "explore_label": [label[int(a)] for a in explore_arm],
            "explore_channel": [ROWS[k][4] for k in keys],
            "explore_net_value": np.where(explore_arm > 0, -2.0, np.nan),
            "explore_total_cost": np.where(explore_arm > 0, 5.1, 0.0),
        }
    )
    buffer = io.BytesIO()
    table.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(RUN, "offer_choice.parquet"), buffer.getvalue())
    flags = pd.DataFrame(
        {
            KEY: keys,
            "contactable_push": [k in PUSH_OK for k in keys],
            "contactable_sms": [True] * len(keys),
            "contactable_email": [True] * len(keys),
        }
    )
    buffer = io.BytesIO()
    flags.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(RUN, "channel_contactability.parquet"), buffer.getvalue())
    if not summary:
        return
    arms = tuple(
        OfferArmSummary(
            position=position,
            level=level_name,
            action_id=action_id,
            label="10% off",
            channels=channels,
            offer_cost=5.0,
            contact_cost=0.1,
            cost_source="catalogue" if action_id else "run_settings",
            eligible_rows=len(keys),
            sleeping_dog_rows=0,
            offered_rows=int((arm == position).sum()),
            net_value_total=0.0,
            cost_total=0.0,
        )
        for position, (level_name, action_id, channels) in ARMS.items()
    )
    storage.write_model(
        run_key(RUN, "offer_choice.json"),
        OfferChoiceSummary(
            run_id=RUN,
            use_case_id="win-back-campaign",
            chosen=True,
            rows=len(keys),
            levels=("none", *(item[0] for item in ARMS.values())),
            arms=arms,
            no_offer_rows=int((arm == 0).sum()),
            reasons={},
            budget=None,
            budget_contacts=None,
            spent=0.0,
            net_value_total=0.0,
            value_per_conversion=100.0,
            value_column=None,
            catalogue_sha256=None,
            created_at=utc_now(),
        ),
    )


def _gate(root: Path, tmp_path: Path, *, summary: bool) -> tuple[Any, np.ndarray, pd.DataFrame]:
    storage = LocalStorage(tmp_path / "data")
    _write_choice(storage, summary=summary)
    scores = pd.DataFrame({KEY: list(ROWS)})
    n = len(scores.index)
    chosen = _read_offer_choice(storage, RUN, KEY, _join_keys(scores, KEY))
    assert chosen is not None
    explore = np.array([ROWS[k][3] for k in ROWS], dtype=bool)
    applied = _apply_offer_choice(
        chosen,
        suppressed=np.zeros(n, dtype=bool),
        held_out=np.zeros(n, dtype=bool),
        explore=explore,
        fallback_treat=np.zeros(n, dtype=bool),
        fallback_offer=pd.Series([None] * n, dtype="object"),
        fallback_channel=pd.Series([None] * n, dtype="object"),
        fallback_net_value=pd.Series(np.full(n, np.nan)),
        contactability_written=False,
    )
    actions = _row_actions(
        scores,
        load_use_case("win-back-campaign"),
        is_uplift=True,
        offer_choice=chosen,
        applied=applied,
        arm_actions=_arm_actions(storage, RUN, chosen),
    )
    outcome = gate_treat_list(
        storage,
        run_id=RUN,
        use_case_id="win-back-campaign",
        client_id=None,
        key_frame=scores.astype("object"),
        treat=applied.treat,
        channel=applied.channel.to_numpy(dtype=object),
        actions=actions,
        planned_by_action=PLANNED,
        configured=CONFIGURED,
        stamp_sha256=catalogue_sha256(root),
        config_root=root,
        ledger=DecisionLedger(
            sqlite_engine(tmp_path / "ledger.db"),
            signer=signer_for_data_dir(tmp_path / "keys"),
            policy_version="p" * 64,
        ),
    )
    decisions = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(RUN, "compliance_decisions.parquet"))))
    return outcome, actions, decisions


def test_each_row_takes_its_offer_s_own_action_by_arm_never_by_a_shared_label(
    root: Path, tmp_path: Path
) -> None:
    outcome, actions, decisions = _gate(root, tmp_path, summary=True)
    assert list(actions) == ["disc_sms", "disc_sms", None, "disc_email", None, None]
    planned = {
        key: list(zip(group["channel"], group["action_id"], strict=True))
        for key, group in decisions.groupby(KEY, sort=False)
    }
    assert planned == {
        "C1": [("sms", "disc_sms")],
        "C2": [("sms", "disc_sms")],
        "C3": [("push", None), ("sms", None)],
        "C4": [("email", "disc_email")],
        "C6": [("push", None), ("sms", None)],
    }
    closed = decisions[~decisions["allowed"]]
    assert list(zip(closed[KEY], closed["channel"], strict=True)) == [("C6", "push")]
    # The catalogue action's message is the one referenced; the action-less offer has none.
    refs = dict(zip(decisions[KEY] + "/" + decisions["channel"], decisions["catalogue_ref"], strict=True))
    sha = catalogue_sha256(root)
    assert refs["C1/sms"] == f"{sha}#disc_sms" and refs["C4/email"] == f"{sha}#disc_email"
    assert refs["C3/push"] is None
    # Nothing is withheld here, so every customer stays on the channel the run chose: none is moved.
    assert list(outcome.treat) == [True, True, True, True, False, True]
    assert list(outcome.channel) == ["sms", "sms", "push", "email", None, "sms"]
    assert outcome.summary.rerouted_rows == 0 and outcome.summary.withheld_rows == 0


def test_without_the_choice_summary_an_offered_arm_s_action_comes_from_the_file(
    root: Path, tmp_path: Path
) -> None:
    _outcome, actions, decisions = _gate(root, tmp_path, summary=False)
    # Offer 1 is the file's own `offer_action_id`; offer 2, given to nobody by the choice, is named by no row
    # of the file and the summary is gone: its action is not known (logged), and it is planned as no action.
    assert list(actions[:3]) == ["disc_sms", "disc_sms", None]
    assert actions[3] is None
    sms = decisions[decisions[KEY] == "C1"]
    assert list(sms["channel"]) == ["sms"] and list(sms["action_id"]) == ["disc_sms"]
