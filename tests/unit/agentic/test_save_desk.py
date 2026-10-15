"""The save-desk card on small scoring runs written by the product's own code (Plan K M144, DEC-1411).

`tests/fixtures/decide/treat_runs.write_run` writes a finished scoring run with Phase 1's `apply_actions` or
M97's `apply_uplift_actions`, the explain stage's reasons and M92's holdout assignment; `build_treat_list` builds
the treat list. Nothing here hand-writes an artefact. The real train-and-score run, the route, the sign-in and
the logs are in `tests/agentic/test_save_desk.py`.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from engine.agentic.errors import AgenticError
from engine.agentic.save_desk import (
    CAUTION,
    CardRateLimiter,
    CardRequest,
    SaveDeskCard,
    build_card,
    load_settings,
)
from engine.decide.treat_list import TREAT_LIST_PARQUET, build_treat_list
from engine.pilot.plain import jargon_in
from engine.storage import LocalStorage, run_key
from engine.uplift.contracts import SegmentThresholds
from tests.fixtures.decide.treat_runs import write_run


def _treat_list(storage: LocalStorage, run_id: str) -> pd.DataFrame:
    return pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run_id, TREAT_LIST_PARQUET))))


def _card(storage: LocalStorage, run_id: str, key: str) -> SaveDeskCard:
    return build_card(storage, CardRequest(run_id=run_id, customer_key=key))


@pytest.fixture
def uplift_value(tmp_path: Path) -> Iterator[tuple[LocalStorage, str, pd.DataFrame]]:
    storage = LocalStorage(tmp_path)
    run_id = "r_20261001_0a000014"
    write_run(storage, run_id, kind="uplift", value=True, holdout=True, channels=True, rows=60)
    build_treat_list(storage, run_id)
    yield storage, run_id, _treat_list(storage, run_id)


def test_a_treated_customer_gets_the_offer_channel_and_value_the_treat_list_holds(
    uplift_value: tuple[LocalStorage, str, pd.DataFrame],
) -> None:
    storage, run_id, treat = uplift_value
    rows = treat[treat["treat"] & treat["net_value"].notna()]
    assert not rows.empty
    row = rows.iloc[0]
    card = _card(storage, run_id, str(row["customer_id"]))
    assert card.recommendation.make_offer is True
    assert card.recommendation.offer == row["offer"] and card.recommendation.channel == row["channel"]
    assert card.net_value.customer_net_value == pytest.approx(float(row["net_value"]))
    assert card.net_value.gross_value is None, "an uplift run's money is net value, not gross"
    expected_reasons = [row[name] for name in ("reason_1", "reason_2", "reason_3") if row[name] is not None]
    assert [line.text for line in card.reasons] == expected_reasons
    assert card.text_source == "rules" and card.caution == CAUTION


def test_every_line_carries_a_label_and_a_source_and_none_is_causal(
    uplift_value: tuple[LocalStorage, str, pd.DataFrame],
) -> None:
    storage, run_id, treat = uplift_value
    for key in treat["customer_id"].head(25):
        card = _card(storage, run_id, str(key))
        lines: list[Any] = [card.recommendation, card.net_value, card.stay_anyway, *card.reasons]
        if card.kept_back is not None:
            lines.append(card.kept_back)
        for line in lines:
            assert line.label in ("Estimated", "Descriptive"), line
            assert line.source.startswith(("treat_list.parquet", "policy_recommendation")), line
    reasons_only = {line.label for line in _card(storage, run_id, str(treat["customer_id"].iloc[0])).reasons}
    assert reasons_only <= {"Estimated"}


def test_a_customer_outside_the_stay_anyway_group_is_not_flagged(
    uplift_value: tuple[LocalStorage, str, pd.DataFrame],
) -> None:
    """The fixture's customers all have a chance of staying below the sure-thing cut, so none is one; the
    flag for a real sure thing is tested on the trained world in `tests/agentic/test_save_desk.py`."""
    storage, run_id, treat = uplift_value
    assert not (treat["segment"] == "sure_thing").any()
    for key in treat["customer_id"].head(8):
        card = _card(storage, run_id, str(key))
        assert card.stay_anyway.flag is False and card.stay_anyway.segment is not None
        assert (
            card.stay_anyway.reason
            == "The model does not place this customer among those who would stay anyway."
        )


def test_a_kept_back_customer_gets_no_offer_whatever_the_model_says(
    uplift_value: tuple[LocalStorage, str, pd.DataFrame],
) -> None:
    """I4: the holdout and the run's own control group are never given an offer by the card."""
    storage, run_id, treat = uplift_value
    held = treat[(treat["holdout"].fillna(False).astype(bool)) | treat["control_group"].astype(bool)]
    assert len(held) >= 3
    for _, row in held.iterrows():
        card = _card(storage, run_id, str(row["customer_id"]))
        assert card.recommendation.make_offer is False
        assert card.recommendation.offer is None and card.recommendation.channel is None
        assert card.kept_back is not None and card.kept_back.label == "Descriptive"
        assert card.kept_back.kind == ("control_group" if bool(row["control_group"]) else "holdout")
        assert "Make no offer" in card.recommendation.advice
        assert card.net_value.customer_net_value is None and card.net_value.list_low is None


def test_a_suppressed_customer_is_told_not_to_be_contacted_and_why(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    write_run(storage, "r_20261001_0a000015", kind="propensity", channels=True, rows=60)
    build_treat_list(storage, "r_20261001_0a000015")
    treat = _treat_list(storage, "r_20261001_0a000015")
    suppressed = treat[treat["suppression_reason"].notna()]
    assert not suppressed.empty
    row = suppressed.iloc[0]
    card = _card(storage, "r_20261001_0a000015", str(row["customer_id"]))
    assert card.kept_back is not None and card.kept_back.kind == "suppressed"
    assert card.recommendation.make_offer is False and card.recommendation.advice.startswith("Do not contact")


def test_a_run_that_ranks_by_chance_to_respond_cannot_say_who_stays_anyway(tmp_path: Path) -> None:
    """Null with a plain reason: a propensity run has bands, not the effect-of-an-offer segments."""
    storage = LocalStorage(tmp_path)
    write_run(storage, "r_20261001_0a000016", kind="propensity", value=True, rows=40)
    build_treat_list(storage, "r_20261001_0a000016")
    treat = _treat_list(storage, "r_20261001_0a000016")
    card = _card(storage, "r_20261001_0a000016", str(treat["customer_id"].iloc[0]))
    assert card.stay_anyway.flag is None and card.stay_anyway.segment is None
    assert "cannot say who would stay anyway" in card.stay_anyway.reason
    valued = treat[treat["expected_gross_value"].notna() & ~treat["control_group"]]
    got = _card(storage, "r_20261001_0a000016", str(valued["customer_id"].iloc[0]))
    assert got.net_value.customer_net_value is None and got.net_value.gross_value is not None
    assert "not incremental" in got.net_value.note and got.net_value.list_low is None


def test_a_run_without_a_value_says_net_value_was_not_configured(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    write_run(storage, "r_20261001_0a000017", kind="uplift", rows=40)
    build_treat_list(storage, "r_20261001_0a000017")
    treat = _treat_list(storage, "r_20261001_0a000017")
    card = _card(storage, "r_20261001_0a000017", str(treat["customer_id"].iloc[0]))
    money = card.net_value
    assert money.customer_net_value is None and money.gross_value is None
    assert money.list_low is None and money.list_high is None
    assert "not configured" in money.note


def test_a_composite_key_is_given_in_the_runs_order_joined_by_a_bar(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    write_run(storage, "r_20261001_0a000018", kind="propensity", composite=True, rows=40)
    build_treat_list(storage, "r_20261001_0a000018")
    treat = _treat_list(storage, "r_20261001_0a000018")
    row = treat.iloc[3]
    key = f"{row['customer_id']}|{row['snapshot_date']}"
    assert _card(storage, "r_20261001_0a000018", key).run_id == "r_20261001_0a000018"
    with pytest.raises(AgenticError) as caught:
        _card(storage, "r_20261001_0a000018", str(row["customer_id"]))
    assert caught.value.code == "SAVE_DESK_KEY_INVALID" and caught.value.status == 422


def test_refusals_have_codes_and_never_name_the_customer(
    uplift_value: tuple[LocalStorage, str, pd.DataFrame], tmp_path: Path
) -> None:
    storage, run_id, _ = uplift_value
    # A customer key, not a credential (named so the repository's secret scan does not read it as one).
    typed_key = "ZZ-no-such-customer-914"
    with pytest.raises(AgenticError) as unknown:
        _card(storage, run_id, typed_key)
    assert unknown.value.code == "SAVE_DESK_CUSTOMER_NOT_FOUND" and unknown.value.status == 404
    assert typed_key not in unknown.value.message
    with pytest.raises(AgenticError) as no_run:
        _card(storage, "r_20261001_0affffff", typed_key)
    assert no_run.value.code == "RUN_NOT_FOUND" and typed_key not in no_run.value.message
    other = LocalStorage(tmp_path / "other")
    write_run(other, "r_20261001_0a000019", kind="uplift", rows=20)
    with pytest.raises(AgenticError) as unbuilt:
        _card(other, "r_20261001_0a000019", "C-001")
    assert unbuilt.value.code == "SAVE_DESK_NO_TREAT_LIST" and unbuilt.value.status == 409


def test_a_repeated_key_is_not_guessed_at(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    run_id = "r_20261001_0a000020"
    write_run(storage, run_id, kind="propensity", composite=True, rows=40)
    build_treat_list(storage, run_id)
    frame = _treat_list(storage, run_id)
    doubled = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    buffer = io.BytesIO()
    doubled.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(run_id, TREAT_LIST_PARQUET), buffer.getvalue())
    first = frame.iloc[0]
    with pytest.raises(AgenticError) as caught:
        _card(storage, run_id, f"{first['customer_id']}|{first['snapshot_date']}")
    assert caught.value.code == "SAVE_DESK_KEY_AMBIGUOUS"


def test_a_zero_padded_key_finds_the_customer_the_ledger_would(tmp_path: Path) -> None:
    """`00104` and `104` are one customer to the consent ledger (`principal_key`); the card agrees."""
    from tests.fixtures.decide.treat_runs import write_run as write

    storage = LocalStorage(tmp_path)
    run_id = "r_20261001_0a000021"
    write(storage, run_id, kind="propensity", rows=20)
    build_treat_list(storage, run_id)
    frame = _treat_list(storage, run_id)
    frame["customer_id"] = [str(100 + position) for position in range(len(frame))]
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(run_id, TREAT_LIST_PARQUET), buffer.getvalue())
    assert _card(storage, run_id, "0105").run_id == run_id


def test_a_key_that_reads_as_typed_wins_over_the_ledgers_form_of_another_customer(tmp_path: Path) -> None:
    """A text key column may hold `00104` and `104` as two customers: each is found by its own text."""
    storage = LocalStorage(tmp_path)
    run_id = "r_20261001_0a000022"
    write_run(storage, run_id, kind="propensity", rows=20)
    build_treat_list(storage, run_id)
    frame = _treat_list(storage, run_id)
    frame["customer_id"] = [str(200 + position) for position in range(len(frame))]
    frame.loc[0, "customer_id"] = "00104"
    frame.loc[1, "customer_id"] = "104"
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(run_id, TREAT_LIST_PARQUET), buffer.getvalue())
    padded = _card(storage, run_id, "00104")
    plain = _card(storage, run_id, "104")
    assert padded.recommendation.advice or padded.recommendation.why_not
    assert plain.run_id == run_id
    # Two rows with the very same text are still not guessed at.
    frame.loc[1, "customer_id"] = "00104"
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(run_id, TREAT_LIST_PARQUET), buffer.getvalue())
    with pytest.raises(AgenticError) as caught:
        _card(storage, run_id, "00104")
    assert caught.value.code == "SAVE_DESK_KEY_AMBIGUOUS"


def test_an_empty_or_over_long_key_is_refused_without_holding_it(
    uplift_value: tuple[LocalStorage, str, pd.DataFrame],
) -> None:
    storage, run_id, _ = uplift_value
    for key in ("   ", "SECRET-" + "9" * 250):
        with pytest.raises(AgenticError) as caught:
            _card(storage, run_id, key)
        assert caught.value.code == "SAVE_DESK_KEY_INVALID" and caught.value.status == 422
        assert "SECRET" not in caught.value.message


def test_a_customer_picked_at_random_to_learn_from_is_never_told_not_to_be_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M92's explore slice draws among customers who are not selected, and sure things can be drawn. The
    treat list then holds `treat = True` for one the model expects to stay anyway. The card must not say "Do
    not discount": following it would undo the random draw that off-policy evaluation depends on."""
    import tests.fixtures.decide.treat_runs as fixture

    monkeypatch.setattr(
        fixture,
        "THRESHOLDS",
        SegmentThresholds(
            persuadable_min_uplift=0.02,
            sleeping_dog_max_uplift=-0.01,
            sure_thing_min_probability=0.25,
            sure_thing_from_base_rate=False,
        ),
    )
    storage = LocalStorage(tmp_path)
    run_id = "r_20261001_0a000023"
    write_run(storage, run_id, kind="uplift", holdout=True, rows=400, value=True, channels=True)
    build_treat_list(storage, run_id)
    treat = _treat_list(storage, run_id)
    sure = treat[treat["segment"] == "sure_thing"]
    explored = sure[sure["explore"].fillna(False).astype(bool) & sure["treat"].astype(bool)]
    plain = sure[~sure["treat"].astype(bool)]
    assert len(explored) >= 1 and len(plain) >= 1, "the run has both kinds of sure thing"
    for key in explored["customer_id"].astype(str):
        card = _card(storage, run_id, key)
        assert card.recommendation.make_offer is True
        assert card.explored is not None and card.explored.label == "Descriptive"
        assert "picked at random" in card.explored.text
        assert card.stay_anyway.flag is True, "the model's view stays on the card"
        assert "Do not discount" not in card.stay_anyway.reason
        assert "do not discount" not in card.recommendation.advice.lower()
    for key in plain["customer_id"].astype(str).head(5):
        card = _card(storage, run_id, key)
        assert card.explored is None and "Do not discount" in card.stay_anyway.reason


def test_the_card_writes_nothing(
    uplift_value: tuple[LocalStorage, str, pd.DataFrame], tmp_path: Path
) -> None:
    storage, run_id, treat = uplift_value
    root = Path(storage.root) if hasattr(storage, "root") else None
    assert root is not None

    def snapshot() -> dict[str, tuple[int, int]]:
        return {
            str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    before = snapshot()
    for key in treat["customer_id"].head(10):
        _card(storage, run_id, str(key))
    assert snapshot() == before


def test_every_sentence_the_card_writes_is_plain_and_holds_no_digit_of_its_own(
    uplift_value: tuple[LocalStorage, str, pd.DataFrame],
) -> None:
    import engine.agentic.save_desk as module

    templates = [
        module.CAUTION,
        module._NO_REASONS,
        module._NO_SEGMENT,
        module._STAY_YES,
        module._STAY_NO,
        module._CONTROL,
        module._HOLDOUT,
        module._NOT_CONFIGURED,
        module._GROSS_NOTE,
        module._NET_NOTE,
        module._LIST_NOTE,
        module._HELD_BACK_VALUE,
        *module._ACTION_ADVICE.values(),
        *module._SUPPRESSED.values(),
    ]
    for text in templates:
        assert jargon_in(text) == (), text
        assert not any(char.isdigit() for char in text), f"a template holds a number of its own: {text}"
    storage, run_id, treat = uplift_value
    for key in treat["customer_id"].head(20):
        card = _card(storage, run_id, str(key))
        for sentence in (card.recommendation.advice, card.stay_anyway.reason, card.net_value.note):
            assert jargon_in(sentence) == (), sentence


def test_the_card_calls_no_language_model() -> None:
    """Rules only (DEC-1411 (a)): M139's renderer is not built, and nothing here may reach a model."""
    import subprocess
    import sys

    probe = (
        "import sys\n"
        "import engine.agentic.save_desk\n"
        "bad = sorted(m for m in sys.modules if m.startswith(('engine.llm', 'engine.generative', 'boto3', 'anthropic')))\n"
        "print(bad)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).parents[3],
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "[]"


# ---------------------------------------------------------------------------
# The rate limit, with an injected clock
# ---------------------------------------------------------------------------
class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def test_the_limiter_refuses_past_the_limit_and_lets_the_minute_pass() -> None:
    clock = _Clock()
    limiter = CardRateLimiter(3, clock=clock)
    assert [limiter.allow("u_asha") for _ in range(3)] == [None, None, None]
    wait = limiter.allow("u_asha")
    assert wait == 60
    assert limiter.allow("u_ravi") is None, "another person has their own allowance"
    clock.now += timedelta(seconds=30)
    assert limiter.allow("u_asha") == 30
    clock.now += timedelta(seconds=30)
    assert limiter.allow("u_asha") is None


def test_a_refused_ask_is_not_counted_against_the_person() -> None:
    clock = _Clock()
    limiter = CardRateLimiter(1, clock=clock)
    assert limiter.allow("u") is None
    for _ in range(5):
        assert limiter.allow("u") == 60
    clock.now += timedelta(seconds=60)
    assert limiter.allow("u") is None, "five refusals did not push the window out"


def test_the_limiter_keeps_a_bounded_table(monkeypatch: pytest.MonkeyPatch) -> None:
    import engine.agentic.save_desk as module

    monkeypatch.setattr(module, "_MAX_TRACKED", 5)
    limiter = CardRateLimiter(1, clock=_Clock())
    for person in range(50):
        limiter.allow(f"u_{person}")
    assert len(limiter._seen) <= 5


def test_the_limiter_forgets_the_least_recently_seen_not_the_first_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import engine.agentic.save_desk as module

    monkeypatch.setattr(module, "_MAX_TRACKED", 3)
    clock = _Clock()
    limiter = CardRateLimiter(2, clock=clock)
    for person in ("u_a", "u_b", "u_c"):
        assert limiter.allow(person) is None
    assert limiter.allow("u_a") is None, "u_a is in the middle of a burst"
    assert limiter.allow("u_d") is None, "a fourth person pushes the oldest out"
    assert set(limiter._seen) == {"u_a", "u_c", "u_d"}, "u_b was the least recently seen"
    assert limiter.allow("u_a") == 60, "u_a's count was kept, so the burst is still limited"


def test_the_limit_is_read_from_the_file_with_a_default(config_root: Path, tmp_path: Path) -> None:
    assert load_settings(config_root).rate_limit_per_minute == 30
    assert load_settings(tmp_path).rate_limit_per_minute == 30, "no file: the default"
    (tmp_path / "agentic").mkdir()
    (tmp_path / "agentic" / "save_desk.yaml").write_text("rate_limit_per_minute: 0\n", encoding="utf-8")
    with pytest.raises(AgenticError) as caught:
        load_settings(tmp_path)
    assert caught.value.code == "CONFIG_INVALID"


def _large_treat_list(storage: LocalStorage, run_id: str, rows: int) -> None:
    """A treat list of `rows` customers written by the treat list's own typed writer (schema and parquet)."""
    import numpy as np

    from engine.decide import treat_list as writer

    frame = pd.DataFrame(
        {
            "customer_id": np.array([f"C{position:09d}" for position in range(rows)], dtype=object),
            "use_case": "win-back-campaign",
            "model_version": "m_1",
            "segment": "persuadable",
            "treat": True,
            "holdout": pd.array([None] * rows, dtype="boolean"),
            "explore": pd.array([None] * rows, dtype="boolean"),
            "suppression_reason": None,
            "offer": "Treat",
            "channel": "sms",
            "contactable_channels": "sms,email",
            "runner_up_offer": None,
            "runner_up_net_value": np.nan,
            "offer_reason": None,
            "control_group": False,
            "score": 0.5,
            "net_value": 1.5,
            "expected_gross_value": np.nan,
            "reason_1": "age up",
            "reason_2": None,
            "reason_3": None,
        }
    )
    table = writer._table(frame, True, ("customer_id",))
    storage.write_bytes(run_key(run_id, TREAT_LIST_PARQUET), writer._parquet_bytes(table))


def test_a_lookup_costs_time_linear_in_the_size_of_the_list(tmp_path: Path) -> None:
    """Best of three at 100,000 and 400,000 customers: four times the rows is nowhere near sixteen times the time."""
    import time

    best: dict[int, float] = {}
    for rows in (100_000, 400_000):
        storage = LocalStorage(tmp_path / str(rows))
        run_id = "r_20261001_0a000030"
        write_run(storage, run_id, kind="uplift", rows=20)
        _large_treat_list(storage, run_id, rows)
        timings = []
        for _ in range(3):
            started = time.perf_counter()
            card = _card(storage, run_id, f"C{rows // 2:09d}")
            timings.append(time.perf_counter() - started)
        assert card.recommendation.make_offer is True
        best[rows] = min(timings)
    assert best[400_000] < 8.0 * best[100_000], best
