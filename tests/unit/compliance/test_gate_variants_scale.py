"""The gate with message arms in force, at scale (Plan K M145, DEC-1412; design §11: 1 million customers in 60 s).

`gate_treat_list` is timed on the whole hand-off for 1,000,000 customers planned on SMS then e-mail under India's
region rule (SMS needs a DLT template), each drawn into the control template or one of three variants (two groups,
twelve variant templates, each with its own registered catalogue action), against a contactability file of the
same customers in another order. The best of three runs at each size, the two sizes taking turns, is held under
the budget and against a 100,000-customer run (linear work takes about ten times as long for ten times the
customers, quadratic work about a hundred times), and against the same hand-off without arms: the variants' words
are checked once per arm, group and channel, so the arms add column work only.
"""

from __future__ import annotations

import io
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engine.compliance.variants import VariantMessages, VariantWords
from tests.unit.compliance.switch import set_gate_switch

ROWS = 1_000_000
SMALL_ROWS = 100_000
BUDGET_SECONDS = 60.0
RUN = "r_20261011_0c145b01"
USE_CASE = "win-back-campaign"
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
LEVELS = ("control_template", "variant_a", "variant_b", "variant_c")
UNITS = ("High", "Medium")
CHANNELS = ("sms", "email")
TEXT = {
    "variant_a": "Hi {{first_name}}, your plan is back at shop.example.in/back\nReply STOP to opt out",
    "variant_b": "Hi {{first_name}}, your line is ready whenever you are.\nReply STOP to opt out",
    "variant_c": "Hi {{first_name}}, your old plan is waiting at deals-x.in/back\nReply STOP to opt out",
}
TEMPLATE = {
    "variant_a": "Hi {#var#}, your plan is back at {#var#}\\nReply STOP to opt out",
    "variant_b": "Hi {#var#}, your line is ready when you like.\\nReply STOP to opt out",
    "variant_c": "Hi {#var#}, your old plan is waiting at {#var#}\\nReply STOP to opt out",
}


def _catalogue() -> str:
    lines = [
        "region: IN",
        "actions:",
        "  - action_id: winback",
        "    label: Win-back",
        "    channels: [sms, email]",
        '    dlt_template_id: "1107000000000000001"',
        "    message_category: promotional",
        '    dlt_template_text: "Dear customer, come back for Rs {#numeric#} off - Acme"',
        '    message_text: "Dear customer, come back for Rs 200 off - Acme"',
    ]
    for index, level in enumerate(LEVELS[1:]):
        for channel in CHANNELS:
            lines += [
                f"  - action_id: {level}_{channel}",
                f"    label: {level} on {channel}",
                f"    channels: [{channel}]",
                f'    template_ref: "copy:{index}{channel[0]}{"0" * 14}"',
                f'    dlt_template_id: "11070000000000001{index}"',
                "    message_category: promotional",
            ]
            if channel == "sms":
                lines.append(f'    dlt_template_text: "{TEMPLATE[level]}"')
    return "\n".join(lines) + "\n"


def _world(tmp_path: Path, config_root: Path) -> tuple[Path, Path]:
    root = tmp_path / "configs"
    shutil.copytree(config_root, root)
    (root / "decide" / "catalogue.yaml").write_text(_catalogue(), encoding="utf-8")
    (root / "compliance" / "url_whitelist.yaml").write_text(
        "channels: [sms, email]\nurls: [shop.example.in]\n", encoding="utf-8"
    )
    set_gate_switch(root, on=True)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return root, data_dir


def _variants(root: Path, arm: np.ndarray, unit: np.ndarray) -> VariantMessages:
    from engine.decide.catalogue import catalogue_sha256

    words = {
        (level, group, channel): VariantWords(
            arm=level,
            unit=group,
            channel=channel,
            template_id=f"{group}-{channel}-{level[-1].upper()}",
            template_ref=f"copy:{index}{channel[0]}{'0' * 14}",
            catalogue_action_id=f"{level}_{channel}",
            subject=None,
            text=TEXT[level],
        )
        for index, level in enumerate(LEVELS[1:])
        for group in UNITS
        for channel in CHANNELS
    }
    return VariantMessages(
        arm=arm,
        unit=unit,
        words=words,
        levels=LEVELS,
        batch_id="b1",
        batch_created_at=NOW.isoformat(),
        catalogue_sha256=str(catalogue_sha256(root)),
        templates=tuple(
            sorted((w.arm, w.unit, w.channel, w.template_ref, w.catalogue_action_id) for w in words.values())
        ),
    )


def _hand_off(root: Path, data_dir: Path, rows: int, seed: int, *, arms: bool) -> float:
    from engine.agentic.ledger import DecisionLedger, signer_for_data_dir
    from engine.compliance.handoff import gate_treat_list
    from engine.decide.catalogue import catalogue_sha256, clear_catalogue_cache
    from engine.platform_db import sqlite_engine
    from engine.storage import LocalStorage, run_key

    rng = np.random.default_rng(seed)
    ids = np.char.add("C-", np.arange(rows).astype(str)).astype(object)
    storage = LocalStorage(data_dir)
    order = rng.permutation(rows)
    sms_ok = rng.random(rows) < 0.8
    contactability = pd.DataFrame(
        {
            "customer_id": ids[order],
            "contactable_sms": sms_ok[order],
            "contactable_email": np.ones(rows, bool),
        }
    )
    buffer = io.BytesIO()
    contactability.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(RUN, "channel_contactability.parquet"), buffer.getvalue())
    clear_catalogue_cache()
    arm = np.asarray(LEVELS, dtype=object)[rng.integers(0, len(LEVELS), rows)]
    unit = np.asarray(UNITS, dtype=object)[rng.integers(0, len(UNITS), rows)]
    ledger = DecisionLedger(
        sqlite_engine(data_dir / f"ledger_{rows}_{seed}_{arms}.db"),
        signer=signer_for_data_dir(data_dir),
        policy_version="p" * 64,
    )
    start = time.perf_counter()
    outcome = gate_treat_list(
        storage,
        run_id=RUN,
        use_case_id=USE_CASE,
        client_id=None,
        key_frame=pd.DataFrame({"customer_id": ids}),
        treat=np.ones(rows, dtype=bool),
        channel=np.full(rows, "sms", dtype=object),
        actions=np.full(rows, "winback", dtype=object),
        planned_by_action={"winback": CHANNELS},
        configured=("sms",),
        stamp_sha256=catalogue_sha256(root),
        config_root=root,
        ledger=ledger,
        at=NOW,
        variants=_variants(root, arm, unit) if arms else None,
    )
    elapsed = time.perf_counter() - start
    summary = outcome.summary
    assert summary.customers == rows and summary.rows == 2 * rows
    if arms:
        # B breaks its SMS template and C's link is not listed (on SMS and e-mail alike): B goes by e-mail, where
        # no template is needed; C is withheld; A and the control template go on SMS where contactable.
        assert summary.withheld_by_arm is not None
        assert summary.withheld_by_arm["variant_c"] == int((arm == "variant_c").sum())
        assert summary.withheld_by_arm["variant_a"] == summary.withheld_by_arm["variant_b"] == 0
        sent = np.asarray(outcome.channel, dtype=object)
        assert (sent[arm == "variant_b"] == "email").all()
    return elapsed


@pytest.mark.slow
def test_a_million_customers_with_message_arms_go_through_the_hand_off_within_the_budget_and_linearly(
    tmp_path: Path, config_root: Path
) -> None:
    root, data_dir = _world(tmp_path, config_root)
    small = large = plain = float("inf")
    for attempt in range(3):
        small = min(small, _hand_off(root, data_dir, SMALL_ROWS, 10 + attempt, arms=True))
        large = min(large, _hand_off(root, data_dir, ROWS, 20 + attempt, arms=True))
        plain = min(plain, _hand_off(root, data_dir, ROWS, 30 + attempt, arms=False))
    print(
        f"\n[Perf] compliance gate hand-off with message arms: {ROWS:,} customers x 2 channels {large:.2f}s "
        f"(without arms {plain:.2f}s; budget {BUDGET_SECONDS:.0f}s); {SMALL_ROWS:,} customers {small:.2f}s"
    )
    assert large < BUDGET_SECONDS, f"{ROWS:,} customers took {large:.2f}s"
    assert large < 30 * max(small, 0.05), "ten times the customers took far more than ten times as long"
    assert large < 3 * max(plain, 0.05), "the arms add column work, not work per customer"


def test_a_hundred_thousand_customers_with_message_arms_take_seconds_not_minutes(
    tmp_path: Path, config_root: Path
) -> None:
    """The fast suite's guard: the 100k hand-off with arms alone, well inside a tenth of the 1M budget."""
    root, data_dir = _world(tmp_path, config_root)
    elapsed = min(_hand_off(root, data_dir, SMALL_ROWS, 7 + attempt, arms=True) for attempt in range(2))
    print(f"\n[Perf] compliance gate hand-off with message arms: {SMALL_ROWS:,} customers {elapsed:.2f}s")
    assert elapsed < BUDGET_SECONDS / 10
