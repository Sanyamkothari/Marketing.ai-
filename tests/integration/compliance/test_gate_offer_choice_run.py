"""The pre-send gate on a real scoring run that chose the offer per customer (Plan K M134, DEC-1405).

The run is `tests/integration/decide/test_offer_choice_run.py`'s, through the product's API (upload, uplift
training, scoring under a total budget, with an explore slice of 10%), except that its two catalogue actions share
one label, "Discount": offer A is planned on SMS only, offer B on email only, and the use case configures both
SMS and email. Nothing below writes a run artefact by hand: the treat list is rebuilt by `build_treat_list` from
what the run wrote, under copies of the run's config root with `compliance.gate.enabled` on.

* With the gate on and nothing to withhold: each customer is decided on their own offer's action and channels,
  found by arm, and stays on the channel the run chose. The first review found that the gate mapped rows to
  actions by label, so with a shared label every offer had no action, was planned on the configured channels, and
  an offer-B customer reachable by SMS was moved to SMS, a channel offer B is not planned on, its message never
  checked. That fails on the unchanged code.
* With email made an opt-in channel (and no opt-in record): the email offers are withheld. The summary's
  `offer_counts`, `explore_offer_counts` and `explore_cost` count what is sent, and a withheld customer keeps no
  offer, value or runner-up (the second review's finding, which also fails on the unchanged code).
"""

# ruff: noqa: F811, F401 - the imported pytest fixtures are used by name
from __future__ import annotations

import io
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd
import pytest

from engine.decide.treat_list import TREAT_LIST_CSV, build_treat_list
from engine.storage import run_key
from tests.integration.decide.test_offer_choice_run import (
    CATALOGUE,
    Runs,
    Scored,
    app,
    make_root,
    score_twice,
)
from tests.integration.uplift.test_uplift_api import App

pytestmark = pytest.mark.integration

LABEL: Final[str] = "Discount"
ACTION_CHANNEL: Final[dict[str, str]] = {"offer_a_sms": "sms", "offer_b_email": "email"}
GATE_ON: Final[str] = "compliance:\n  gate:\n    enabled: true\n"
EMAIL_OPT_IN: Final[str] = "compliance:\n  gate:\n    enabled: true\n    opt_in_channels: [email]\n"


@pytest.fixture(scope="module")
def root(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = make_root(
        config_root, tmp_path_factory.mktemp("gate-offer-root") / "configs", explore_fraction=0.10
    )
    shared = CATALOGUE.replace("Offer A by SMS", LABEL).replace("Offer B by email", LABEL)
    (target / "decide" / "catalogue.yaml").write_text(shared, encoding="utf-8")
    return target


@dataclass(frozen=True)
class Built:
    out: pd.DataFrame
    """`treat_list.csv` as text, indexed by customer."""
    summary: Any
    decisions: pd.DataFrame


@dataclass(frozen=True)
class World:
    scored: Scored
    """The budgeted run as scoring built its treat list (the gate off)."""
    choice: pd.DataFrame
    """`offer_choice.parquet`, indexed by customer."""
    contactable: pd.DataFrame
    """`channel_contactability.parquet`, indexed by customer."""
    on: Built
    email_opt_in: Built


def _gate_root(root: Path, target: Path, document: str) -> Path:
    shutil.copytree(root, target)
    (target / "compliance" / "compliance.yaml").write_text(document, encoding="utf-8")
    return target


def _build(app: App, run_id: str, root: Path) -> Built:
    storage = app.storage
    summary = build_treat_list(storage, run_id, config_root=root)
    text = storage.read_bytes(run_key(run_id, TREAT_LIST_CSV)).decode("utf-8")
    out = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False).set_index("customer_id")
    decisions = pd.read_parquet(
        io.BytesIO(storage.read_bytes(run_key(run_id, "compliance_decisions.parquet")))
    )
    return Built(out, summary, decisions.astype({"customer_id": str}))


@pytest.fixture(scope="module")
def world(app: App, root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    runs: Runs = score_twice(app, "0c1340")
    scored = runs.budgeted
    choice = pd.read_parquet(
        io.BytesIO(app.storage.read_bytes(run_key(scored.run_id, "offer_choice.parquet")))
    )
    choice = choice.astype({"customer_id": str}).set_index("customer_id").loc[scored.treat.index]
    flags = pd.read_parquet(
        io.BytesIO(app.storage.read_bytes(run_key(scored.run_id, "channel_contactability.parquet")))
    )
    flags = flags.astype({"customer_id": str}).set_index("customer_id").loc[scored.treat.index]
    base = tmp_path_factory.mktemp("gate-offer-roots")
    on = _build(app, scored.run_id, _gate_root(root, base / "on", GATE_ON))
    email = _build(app, scored.run_id, _gate_root(root, base / "email", EMAIL_OPT_IN))
    yield World(scored, choice, flags, on, email)


def _arm_action(world: World) -> pd.Series:
    """The catalogue action of the offer each treated customer was given: the chosen arm's, else the explored."""
    choice, treat = world.choice, world.scored.treat
    lookup = dict(zip(choice["offer_arm"], choice["offer_action_id"], strict=True))
    lookup.pop(0, None)
    given = np.where(choice["offer_arm"] > 0, choice["offer_arm"], choice["explore_arm"])
    return pd.Series([lookup.get(int(arm)) for arm in given], index=treat.index).where(treat["treat"] == "1")


def test_each_customer_is_decided_on_their_own_offer_s_action_and_channel(world: World) -> None:
    treat = world.scored.treat
    sent = treat["treat"] == "1"
    assert set(treat.loc[sent, "offer"]) == {LABEL}, "both offers carry the one label"
    action = _arm_action(world)
    assert set(action[sent]) == {"offer_a_sms", "offer_b_email"}
    decisions = world.on.decisions
    assert decisions["customer_id"].is_unique, "each offer is planned on its one channel"
    decided = decisions.set_index("customer_id").loc[treat.index[sent]]
    assert (decided["action_id"] == action[sent]).all()
    assert (decided["channel"] == action[sent].map(ACTION_CHANNEL)).all()
    # Offer-B customers reachable by SMS too are many, and none is moved there.
    reachable_by_sms = world.contactable["contactable_sms"].astype(bool)
    assert (sent & (action == "offer_b_email") & reachable_by_sms).sum() > 20


def test_with_nothing_withheld_the_gated_list_is_the_list_the_run_built(world: World) -> None:
    before, after = world.scored.treat, world.on.out
    assert world.on.summary.compliance_withheld_rows == 0 and world.on.summary.compliance_rerouted_rows == 0
    for column in ("treat", "offer", "channel", "net_value", "runner_up_offer"):
        assert (after[column] == before[column]).all(), column
    assert world.on.summary.offer_counts == {
        LABEL: int(((before["treat"] == "1") & (world.choice["offer_arm"] > 0)).sum())
    }


def test_withheld_offers_are_neither_counted_nor_kept_on_the_list(world: World) -> None:
    before, built = world.scored.treat, world.email_opt_in
    out, summary = built.out, built.summary
    action = _arm_action(world)
    sent_before = before["treat"] == "1"
    by_email = sent_before & (action == "offer_b_email")
    assert by_email.sum() > 20 and (sent_before & (action == "offer_a_sms")).sum() > 20
    # Every email offer is withheld (no opt-in record on email); every SMS one is sent as before.
    assert (out.loc[by_email, "treat"] == "0").all()
    assert (out.loc[sent_before & ~by_email, "treat"] == "1").all()
    assert summary.compliance_withheld == {"COMPLIANCE_WHATSAPP_NO_OPT_IN": int(by_email.sum())}
    # A withheld customer keeps no offer, channel, value or runner-up: nothing was done.
    for column in ("offer", "channel", "net_value", "runner_up_offer", "runner_up_net_value"):
        assert (out.loc[by_email, column] == "").all(), column
    # The counts are of what is sent.
    sent = out["treat"] == "1"
    explored = sent & (world.choice["offer_arm"] == 0)
    assert explored.sum() > 5, "some explored customers are given an SMS offer"
    assert summary.treat_rows == int(sent.sum())
    assert sum((summary.offer_counts or {}).values()) == int(sent.sum()) - int(explored.sum())
    assert sum((summary.explore_offer_counts or {}).values()) == int(explored.sum())
    explore_cost = float(world.choice.loc[explored, "explore_total_cost"].sum())
    assert summary.explore_cost is not None and np.isclose(summary.explore_cost, explore_cost, atol=0.01)
    assert summary.channel_rows == {"sms": int(sent.sum())}
