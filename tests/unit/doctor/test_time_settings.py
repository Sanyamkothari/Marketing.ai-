"""Plan O M244 (DEC-1814): time settings, owned by Data Doctor ([REQ] 10; the design §3.5).

Acceptance (Plan O §M244):

* Data Doctor never shows a raw minutes field; the three choices each show "about N minutes".
* The progress bar's first ETA equals the promised expectation for the chosen budget.
* After each run, actual and expected are recorded per stage; on a replayed history of ten runs, the median
  actual-over-expected ratio is within 0.8-1.25.
* A file whose newest month is immature asks "Leave it out of testing?"; leaving it out makes `X_LABEL_MATURITY`
  pass.
* A time split whose cuts would leave a part empty is never proposed (`X_TIME_CUTS_FEASIBLE`).
* With no run history, the estimate is labelled "from similar public datasets", and Balanced is `check`, not `sure`.

And: the budget and the questions go through M246's protocol (AUTO when sure, one question otherwise) and the time
answers are remembered per client; with `data_doctor.unified` off, `_time_rules` and the progress bar are unchanged.
"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.agent.contracts import AgentConfidence
from engine.agent.recommend import DataFacts, recommend_settings
from engine.config import (
    ProblemType,
    ResolvedConfig,
    RunMode,
    UseCaseConfig,
    advanced_settings_schema,
    load_use_case,
)
from engine.contracts import RunRecord, RunState, RunStatus, StageKey, StageStatus
from engine.doctor import time_settings as ts
from engine.doctor.config import DataDoctorConfig
from engine.doctor.diagnose import LoadedFile
from engine.doctor.goal_brief import load_goal_brief
from engine.doctor.protocol import settle
from engine.doctor.registry import DataToolContext, DataToolError, data_registry
from engine.registry import LocalModelRegistry
from engine.storage import LocalStorage, run_key
from tests.unit.agent.egress_canary import canary_context
from tests.unit.doctor.readiness_fixtures import report
from tests.unit.run_progress_support import (  # noqa: F401 - fixtures, by name
    FakeClock,
    clock_fixture,
    registry_fixture,
    resolved_fixture,
    storage_fixture,
)
from tests.unit.test_run_train import (
    RUN_ID,
    UPLOAD_ID,
    RecordingStorage,
    StageStubs,
    make_context,
    pipeline_for,
)

LABEL = re.compile(r"^(Quick|Balanced|Thorough) \(about \d+ min(, recommended)?\)$")
T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def _on(config: UseCaseConfig, **model_search: Any) -> UseCaseConfig:
    update: dict[str, Any] = {"data_doctor": DataDoctorConfig(unified=True)}
    if model_search:
        update["model_search"] = config.model_search.model_copy(update=model_search)
    return config.model_copy(update=update)


def _loaded(frame: pd.DataFrame, config: UseCaseConfig, upload_id: str = "up-time") -> LoadedFile:
    from engine.stages import ingest

    profile = ingest.profile_dataset(
        frame,
        config,
        upload_id=upload_id,
        file_name="snapshots.csv",
        file_format="csv",
        file_size_bytes=1,
        delimiter=",",
        encoding="utf-8",
    )
    return LoadedFile(upload_id=upload_id, file_name="snapshots.csv", frame=frame, profile=profile)


# ---------------------------------------------------------------------------
# A deployment's past runs, written as the pipeline writes them
# ---------------------------------------------------------------------------
_OTHERS: dict[StageKey, float] = {
    StageKey.INGEST: 4.0,
    StageKey.VALIDATE: 6.0,
    StageKey.PREPARE: 12.0,
    StageKey.SPLIT: 3.0,
    StageKey.EVALUATE: 20.0,
    StageKey.EXPLAIN: 45.0,
    StageKey.REGISTER: 4.0,
}


def _status(run_id: str, durations: dict[StageKey, float]) -> RunStatus:
    from engine.pipeline import GROUP_LABELS, STAGE_TITLES

    return RunStatus(
        run_id=run_id,
        mode=RunMode.TRAIN,
        state=RunState.DONE,
        updated_at=T0,
        stages=tuple(
            StageStatus(
                key=key,
                title=STAGE_TITLES[key],
                group_label=GROUP_LABELS[(RunMode.TRAIN, key)],
                state=RunState.DONE,
                duration_seconds=round(durations[key], 3),
            )
            for key in ts.TRAIN_STAGE_KEYS
        ),
        progress_pct=100,
    )


def _past_run(
    storage: LocalStorage,
    run_id: str,
    config: UseCaseConfig,
    *,
    rows: int,
    columns: int,
    minutes: int,
    durations: dict[StageKey, float],
    curve_seconds: float | None = None,
    best: float = 0.85,
) -> RunStatus:
    """A finished training run: run.json, status.json, run_config.json, profile.json and its leaderboard."""
    record = RunRecord(
        run_id=run_id,
        use_case_id=config.id,
        use_case_name=config.name,
        mode=RunMode.TRAIN,
        state=RunState.DONE,
        created_at=T0,
        file_name="past.csv",
        primary_key="customer_id",
        problem_type=config.problem_type,
        model_choice="automl",
        engine_version="test",
        row_count=rows,
    )
    storage.write_model(run_key(run_id, "run.json"), record)
    status = _status(run_id, durations)
    storage.write_model(run_key(run_id, "status.json"), status)
    dumped = config.model_copy(
        update={"model_search": config.model_search.model_copy(update={"time_limit_minutes": minutes})}
    ).model_dump(mode="json")
    storage.write_bytes(run_key(run_id, "run_config.json"), json.dumps({"config": dumped}).encode())
    storage.write_bytes(
        run_key(run_id, "profile.json"), json.dumps({"row_count": rows, "column_count": columns}).encode()
    )
    if curve_seconds is not None:
        # Twelve models whose best score rises fast, then hardly at all: the diminishing returns to find.
        entries = []
        spent = curve_seconds / 12
        for index in range(12):
            score = best - 0.06 * math.exp(-index / 2.0)
            entries.append(
                {
                    "rank": index + 1,
                    "model_name": f"LightGBM_BAG_L1/T{index + 1}",
                    "validation_score": round(score, 5),
                    "fit_time_seconds": spent,
                    "stack_level": 1,
                }
            )
        storage.write_bytes(
            run_key(run_id, "leaderboard.json"),
            json.dumps({"greater_is_better": True, "entries": entries}).encode(),
        )
    return status


def _durations(train: float, scale: float = 1.0) -> dict[StageKey, float]:
    return {**{key: value * scale for key, value in _OTHERS.items()}, StageKey.TRAIN: train}


# ---------------------------------------------------------------------------
# No raw minutes field; "about N minutes"; the public prior
# ---------------------------------------------------------------------------
def test_with_no_run_history_the_estimate_is_from_similar_public_datasets_and_balanced_is_check(
    tmp_path: Path,
) -> None:
    config = _on(load_use_case("targeted-advertisement"))
    inputs = ts.time_inputs(LocalStorage(tmp_path), config, rows=7_043, columns=21)
    estimate = ts.estimate(inputs, 30)
    assert estimate.basis == "public_library" and estimate.basis_label == "from similar public datasets"
    assert estimate.runs_used == 0
    budget = ts.propose_budget(inputs)
    assert budget.confidence == "check" and budget.basis_label == "from similar public datasets"
    assert "from similar public datasets" in budget.reason
    # The telco report's own file: its search finished in about 10 minutes of a 30-minute budget.
    balanced = next(c for c in budget.choices if c.choice == "balanced")
    assert 8 <= balanced.minutes <= 14


def test_the_three_choices_each_show_about_n_minutes_and_never_a_raw_minutes_field(tmp_path: Path) -> None:
    config = _on(load_use_case("targeted-advertisement"))
    budget = ts.propose_budget(ts.time_inputs(LocalStorage(tmp_path), config, rows=50_000, columns=30))
    assert [c.choice for c in budget.choices] == ["quick", "balanced", "thorough"]
    for choice in budget.choices:
        assert LABEL.match(choice.label), choice.label
        assert choice.label == ts.choice_label(choice.choice, choice.about_minutes)
        assert choice.about_minutes == max(1, round(choice.expected_seconds / 60))
        assert choice.buys
    assert [c.recommended for c in budget.choices] == [False, True, False]
    assert budget.choices[0].minutes < budget.choices[1].minutes < budget.choices[2].minutes
    # Through M246's protocol the person picks one of three labelled options: no option takes a typed number.
    asks = ts.time_settings_asks(
        ts.TimeSettings(budget=budget, as_of=ts.AsOf(column=None, reason=""), outcome_window=_window()),
        rows=50_000,
        upload_id="up-1",
        goal_id="targeted-advertisement",
    )
    settled = settle(asks[0], now=T0)
    assert settled.how == "ask" and settled.question is not None
    assert [o.label for o in settled.question.options] == [c.label for c in budget.choices]
    assert all(LABEL.match(o.label) for o in settled.question.options)
    assert settled.question.recommended == "balanced" and not settled.question.blocking


def _window() -> ts.OutcomeWindow:
    return ts.OutcomeWindow(days=None, reason="")


def test_the_registry_tools_offer_three_choices_and_estimate_per_stage(tmp_path: Path) -> None:
    from engine.access.roles import Principal, Role

    registry = data_registry()
    agent = canary_context()
    ctx = DataToolContext(
        agent=agent,
        principal=Principal(user_id="u-a", username="a", roles=frozenset({Role.ANALYST}), kind="user"),
        caller="person",
        storage=LocalStorage(tmp_path),
    )
    estimate = registry.call("estimate_training_time", {"minutes": 5}, ctx).output
    assert estimate["minutes"] == 5 and set(estimate["stages"]) == {k.value for k in ts.TRAIN_STAGE_KEYS}
    assert estimate["expected_seconds"] == sum(estimate["stages"].values())
    proposed = registry.call("propose_training_budget", {}, ctx).output
    assert proposed["ticked"] is False and proposed["confidence"] == "check" and proposed["offered"]
    assert all(LABEL.match(c["label"]) for c in proposed["budget"]["choices"])
    offer = ts.read_offer(LocalStorage(tmp_path), "upload", agent.upload_id, agent.config.id)
    assert offer is not None and offer.budget.model_dump(mode="json") == proposed["budget"]
    settings = registry.call("propose_time_settings", {}, ctx).output
    assert settings["state"] == "pending" and settings["ticked"] is False


def test_a_column_that_is_not_a_date_is_refused_as_the_as_of_date(tmp_path: Path) -> None:
    from engine.access.roles import Principal, Role

    agent = canary_context()
    ctx = DataToolContext(
        agent=agent,
        principal=Principal(user_id="u-a", username="a", roles=frozenset({Role.ANALYST}), kind="user"),
        caller="person",
    )
    with pytest.raises(DataToolError) as refused:
        data_registry().call("propose_time_settings", {"as_of_column": "customer_id"}, ctx)
    assert refused.value.code == "TIME_COLUMN_UNREADABLE"
    assert refused.value.code in ts.TIME_CODES


# ---------------------------------------------------------------------------
# Sure with close history: Balanced pre-selected (AUTO), at the point of diminishing returns
# ---------------------------------------------------------------------------
def test_with_past_runs_close_in_size_balanced_is_sure_and_sits_where_the_gains_stop(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    config = _on(load_use_case("targeted-advertisement"))
    for index in range(4):
        _past_run(
            storage,
            f"r_20261001_0000000{index}",
            config,
            rows=20_000 + 1_000 * index,
            columns=25,
            minutes=30,
            durations=_durations(600.0 + 20 * index),
            curve_seconds=600.0 + 20 * index,
        )
    inputs = ts.time_inputs(storage, config, rows=21_000, columns=25)
    budget = ts.propose_budget(inputs)
    assert budget.basis == "this_deployment" and budget.basis_label == "from 4 past runs here"
    assert budget.confidence == "sure" and budget.curves_used == 4
    balanced = next(c for c in budget.choices if c.choice == "balanced")
    # The curves gain 0.06 * e^(-i/2) per model (50 s each): past about 6 minutes a minute buys under 0.0005.
    assert 4 <= balanced.minutes <= 10, balanced.minutes
    thorough = next(c for c in budget.choices if c.choice == "thorough")
    assert (
        thorough.buys.startswith("Thorough has found a better model on ")
        and "of 4 similar past runs" in thorough.buys
    )
    # Sure: the protocol decides it (AUTO), ticked and shown; nothing is asked.
    asks = ts.time_settings_asks(
        ts.TimeSettings(budget=budget, as_of=ts.AsOf(column=None, reason=""), outcome_window=_window()),
        rows=21_000,
        upload_id="up-1",
        goal_id="targeted-advertisement",
    )
    settled = settle(asks[0], now=T0)
    assert settled.how == "auto" and settled.item is not None
    assert settled.item.payload["value"] == balanced.minutes
    assert settled.item.payload["path"] == "model_search.time_limit_minutes"


# ---------------------------------------------------------------------------
# The promise: the first ETA equals the expectation shown; the record afterwards
# ---------------------------------------------------------------------------
def _offer_for(storage: LocalStorage, config: UseCaseConfig) -> ts.TrainingBudget:
    budget = ts.propose_budget(ts.time_inputs(storage, config, rows=40, columns=4))
    ts.save_offer(
        storage,
        ts.TimeOffer(
            use_case_id=config.id,
            source_kind="upload",
            source_id=UPLOAD_ID,
            offered_at=T0,
            rows=40,
            columns=4,
            budget=budget,
        ),
    )
    return budget


@pytest.mark.parametrize("choice", ["quick", "balanced", "thorough"])
def test_the_progress_bars_first_eta_equals_the_promise_and_the_run_records_actual_against_expected(
    choice: str,
    monkeypatch: pytest.MonkeyPatch,
    resolved: ResolvedConfig,
    storage: RecordingStorage,
    registry: LocalModelRegistry,
    clock: FakeClock,
) -> None:
    StageStubs().install(monkeypatch)
    base = _on(resolved.config)
    budget = _offer_for(storage, base)
    chosen = next(c for c in budget.choices if c.choice == choice)
    config = _on(resolved.config, time_limit_minutes=chosen.minutes)
    pipeline_for(storage, registry).run_train(make_context(resolved, storage, registry, config=config))

    estimated = [s for s in storage.statuses() if s.estimated_pct is not None]
    first = estimated[0]
    assert first.eta_seconds == chosen.expected_seconds, (first.eta_seconds, chosen.expected_seconds)
    assert first.estimate_basis == "time_budget"
    promise = storage.read_model(run_key(RUN_ID, ts.PROMISE_FILENAME), ts.TimePromise)
    assert promise.choice == choice and promise.label == chosen.label
    assert promise.expected_seconds == chosen.expected_seconds and promise.stages == chosen.stages
    # FOLLOW-UP: actual against expected, per stage, once the run ended.
    record = storage.read_model(run_key(RUN_ID, ts.RECORD_FILENAME), ts.TimeRecord)
    assert record.state == "done" and set(record.stages) == {k.value for k in ts.TRAIN_STAGE_KEYS}
    final = storage.statuses()[-1]
    for row in final.stages:
        assert record.stages[row.key.value].actual == row.duration_seconds
        assert record.stages[row.key.value].expected == chosen.stages[row.key.value]
    assert record.actual_seconds is not None and record.ratio is not None


def test_with_the_switch_off_the_progress_bar_is_m189s_and_nothing_is_promised(
    monkeypatch: pytest.MonkeyPatch,
    resolved: ResolvedConfig,
    storage: RecordingStorage,
    registry: LocalModelRegistry,
    clock: FakeClock,
) -> None:
    StageStubs().install(monkeypatch)
    budget = _offer_for(storage, _on(resolved.config))
    balanced = next(c for c in budget.choices if c.choice == "balanced")
    off = resolved.config.model_copy(
        update={
            "model_search": resolved.config.model_search.model_copy(
                update={"time_limit_minutes": balanced.minutes}
            )
        }
    )
    assert off.data_doctor.unified is False
    pipeline_for(storage, registry).run_train(make_context(resolved, storage, registry, config=off))
    first = next(s for s in storage.statuses() if s.estimated_pct is not None)
    assert first.eta_seconds != balanced.expected_seconds
    assert (
        first.eta_seconds is not None and first.eta_seconds >= balanced.minutes * 60
    )  # the budget, M189's rule
    assert not storage.exists(run_key(RUN_ID, ts.PROMISE_FILENAME))
    assert not storage.exists(run_key(RUN_ID, ts.RECORD_FILENAME))


def test_a_custom_budget_not_among_the_choices_is_promised_the_estimate_from_the_same_history(
    monkeypatch: pytest.MonkeyPatch,
    resolved: ResolvedConfig,
    storage: RecordingStorage,
    registry: LocalModelRegistry,
    clock: FakeClock,
) -> None:
    """A limit set in Settings > Advanced is none of the three choices: its ETA still comes from the same history
    and the same file the offer was made for, never from a second estimate that disagrees with the screen."""
    StageStubs().install(monkeypatch)
    budget = _offer_for(storage, _on(resolved.config))
    custom = max(c.minutes for c in budget.choices) + 7
    config = _on(resolved.config, time_limit_minutes=custom)
    expected = ts.estimate(ts.time_inputs(storage, config, rows=40, columns=4), custom)
    pipeline_for(storage, registry).run_train(make_context(resolved, storage, registry, config=config))
    first = next(s for s in storage.statuses() if s.estimated_pct is not None)
    assert first.eta_seconds == expected.expected_seconds and first.estimate_basis == "time_budget"
    promise = storage.read_model(run_key(RUN_ID, ts.PROMISE_FILENAME), ts.TimePromise)
    assert promise.choice == "custom" and promise.minutes == custom
    assert promise.label == f"Your own time limit (about {expected.about_minutes} min)"
    record = storage.read_model(run_key(RUN_ID, ts.RECORD_FILENAME), ts.TimeRecord)
    assert record.choice == "custom" and record.expected_seconds == expected.expected_seconds


def test_a_run_on_data_no_offer_was_made_for_keeps_m189s_estimate(
    monkeypatch: pytest.MonkeyPatch,
    resolved: ResolvedConfig,
    storage: RecordingStorage,
    registry: LocalModelRegistry,
    clock: FakeClock,
) -> None:
    StageStubs().install(monkeypatch)
    config = _on(resolved.config, time_limit_minutes=12)
    pipeline_for(storage, registry).run_train(make_context(resolved, storage, registry, config=config))
    first = next(s for s in storage.statuses() if s.estimated_pct is not None)
    assert first.eta_seconds is not None and first.eta_seconds >= 12 * 60  # M189's rule: the budget
    assert not storage.exists(run_key(RUN_ID, ts.PROMISE_FILENAME))


def test_a_choice_already_taken_stays_promised_after_a_newer_run_moves_the_next_offer(
    monkeypatch: pytest.MonkeyPatch,
    resolved: ResolvedConfig,
    storage: RecordingStorage,
    registry: LocalModelRegistry,
    clock: FakeClock,
) -> None:
    """The person took Balanced; before the run starts, a newer finished run changes the history and the visit offers
    again with other minutes. The earlier offer is kept, so the run is promised what the person was shown."""
    StageStubs().install(monkeypatch)
    base = _on(resolved.config)
    taken = _offer_for(storage, base)
    balanced = next(c for c in taken.choices if c.choice == "balanced")
    newer = taken.model_copy(
        update={
            "choices": tuple(
                c.model_copy(update={"minutes": c.minutes + 50, "expected_seconds": c.expected_seconds + 999})
                for c in taken.choices
            )
        }
    )
    ts.save_offer(
        storage,
        ts.TimeOffer(
            use_case_id=base.id, source_kind="upload", source_id=UPLOAD_ID, offered_at=T0 + timedelta(hours=1),
            rows=40, columns=4, budget=newer,
        ),
    )  # fmt: skip
    offers = ts.read_offers(storage, "upload", UPLOAD_ID, base.id)
    assert [o.budget for o in offers] == [newer, taken], "newest first, the earlier one kept"
    assert ts.read_offer(storage, "upload", UPLOAD_ID, base.id) == offers[0]
    config = _on(resolved.config, time_limit_minutes=balanced.minutes)
    pipeline_for(storage, registry).run_train(make_context(resolved, storage, registry, config=config))
    first = next(s for s in storage.statuses() if s.estimated_pct is not None)
    assert first.eta_seconds == balanced.expected_seconds
    promise = storage.read_model(run_key(RUN_ID, ts.PROMISE_FILENAME), ts.TimePromise)
    assert promise.choice == "balanced" and promise.offered_at == T0
    # The same choices offered again are kept once, as the newest.
    ts.save_offer(storage, offers[1].model_copy(update={"offered_at": T0 + timedelta(hours=2)}))
    again = ts.read_offers(storage, "upload", UPLOAD_ID, base.id)
    assert [o.budget for o in again] == [taken, newer]


# ---------------------------------------------------------------------------
# Guided setup's advisor: the setting it suggests and the offer are one computation
# ---------------------------------------------------------------------------
def test_the_advisors_suggested_budget_is_the_offer_and_the_run_is_promised_exactly_it(
    monkeypatch: pytest.MonkeyPatch,
    resolved: ResolvedConfig,
    storage: RecordingStorage,
    registry: LocalModelRegistry,
    clock: FakeClock,
) -> None:
    """With four finished runs here, the advisor (the only path that writes `model_search.time_limit_minutes` into a
    run's configuration) works the budget out on them, says so, and keeps the offer; the person accepts its
    suggestion, the run starts, and the bar's first ETA is the offered Balanced to the second."""
    import dataclasses

    from engine.agent.advisor import advise, run_overrides
    from engine.agent.contracts import ProposalKind, ProposalState
    from tests.unit.agent.egress_canary import canary_frame
    from tests.unit.agent.helpers import context_for

    StageStubs().install(monkeypatch)
    base = _on(resolved.config)
    frame = canary_frame(1_500)
    for index in range(4):
        _past_run(
            storage,
            f"r_20260901_0000000{index}",
            base,
            rows=1_400 + 100 * index,
            columns=len(frame.columns),
            minutes=30,
            durations=_durations(600.0 + 20 * index),
            curve_seconds=600.0 + 20 * index,
        )
    ctx = context_for(frame, base.id, target="converted_30d")
    ctx = dataclasses.replace(ctx, config=_on(ctx.config), upload_id=UPLOAD_ID, run_history=storage)
    assert ctx.config.id == resolved.config.id
    advice = advise(ctx, primary_key="customer_id", target="converted_30d")
    suggested = [
        p
        for p in advice.proposals
        if p.kind is ProposalKind.SETTING and p.path == "model_search.time_limit_minutes"
    ]
    assert len(suggested) == 1, [p.path for p in advice.proposals]
    proposal = suggested[0]
    assert "from 4 past runs here" in proposal.reason
    assert "no finished training run" not in proposal.reason
    offer = ts.read_offer(storage, "upload", UPLOAD_ID, base.id)
    assert offer is not None and offer.budget.basis == "this_deployment"
    balanced = next(c for c in offer.budget.choices if c.choice == "balanced")
    assert proposal.value == balanced.minutes and proposal.title == f"Training time: {balanced.label}"

    overrides = run_overrides([proposal.model_copy(update={"state": ProposalState.ACCEPTED})])
    config = _on(resolved.config, time_limit_minutes=overrides["model_search.time_limit_minutes"])
    pipeline_for(storage, registry).run_train(make_context(resolved, storage, registry, config=config))
    first = next(s for s in storage.statuses() if s.estimated_pct is not None)
    assert first.eta_seconds == balanced.expected_seconds, (first.eta_seconds, balanced.expected_seconds)
    promise = storage.read_model(run_key(RUN_ID, ts.PROMISE_FILENAME), ts.TimePromise)
    assert promise.choice == "balanced" and promise.basis == "this_deployment"


# ---------------------------------------------------------------------------
# Learning: ten replayed runs, median actual over expected within 0.8-1.25
# ---------------------------------------------------------------------------
def test_on_a_replayed_history_of_ten_runs_the_median_actual_over_expected_is_within_the_band(
    tmp_path: Path,
) -> None:
    """A deployment whose searches run at their own pace (not the library's), stop a little past their limit, and
    whose other stages are slower than M189's defaults. Each run: Data Doctor offers three choices on what it knows
    then, the person takes Balanced, the run happens, and actual against expected is recorded - the next estimate
    reads every earlier run."""
    storage = LocalStorage(tmp_path)
    config = _on(load_use_case("targeted-advertisement"))
    rng = np.random.default_rng(244)
    ratios: list[float] = []
    for index in range(10):
        rows = int(rng.integers(3_000, 80_000))
        columns = int(rng.integers(10, 40))
        work = rows * columns * len(config.model_search.candidates)
        natural = 1.6 * work**0.42 * float(rng.lognormal(0.0, 0.15))
        budget = ts.propose_budget(ts.time_inputs(storage, config, rows=rows, columns=columns))
        chosen = next(c for c in budget.choices if c.choice == "balanced")
        train = min(natural, chosen.minutes * 60.0 * 1.04)
        actual = _durations(train, scale=1.5 * float(rng.lognormal(0.0, 0.1)))
        run_id = f"r_20261001_{index:08d}"
        status = _past_run(
            storage,
            run_id,
            _on(config, time_limit_minutes=chosen.minutes),
            rows=rows,
            columns=columns,
            minutes=chosen.minutes,
            durations=actual,
            curve_seconds=train,
        )
        promise = ts.TimePromise(
            run_id=run_id,
            use_case_id=config.id,
            source_id=f"up-{index}",
            choice="balanced",
            minutes=chosen.minutes,
            label=chosen.label,
            expected_seconds=chosen.expected_seconds,
            stages=chosen.stages,
            base=chosen.base,
            basis=budget.basis,
            offered_at=T0,
            promised_at=T0,
        )
        record = ts.record_run_time(storage, promise, status)
        assert record.ratio is not None
        assert storage.exists(run_key(run_id, ts.RECORD_FILENAME))
        ratios.append(record.ratio)
    median = float(np.median(ratios))
    assert 0.8 <= median <= 1.25, ratios
    # And it learnt: the second half is closer than the first estimate, made on the public prior alone.
    assert abs(float(np.median(ratios[5:])) - 1.0) <= abs(ratios[0] - 1.0) + 0.05, ratios


# ---------------------------------------------------------------------------
# (b) Data time settings: maturity, cut points, the as-of date
# ---------------------------------------------------------------------------
def _monthly(
    rows_per_month: int = 300,
    months: int = 6,
    *,
    newest_yes: bool = False,
    seed: int = 0,
    first: str = "2026-04-30",
) -> pd.DataFrame:
    """Fault-prediction snapshots at month-ends, Apr to Sep 2026 (from `first`); the newest month's outcomes not
    known yet."""
    rng = np.random.default_rng(seed)
    ends = pd.date_range(first, periods=months, freq="ME")
    count = rows_per_month * months
    dates = np.repeat(ends, rows_per_month)
    faults = (rng.random(count) < 0.3).astype(int)
    if not newest_yes:
        faults[dates == ends[-1]] = 0
    return pd.DataFrame(
        {
            "asset_id": [f"A-{i:06d}" for i in range(count)],
            "snapshot_date": pd.DatetimeIndex(dates).strftime("%Y-%m-%d"),
            "error_rate_24h": rng.random(count).round(3) * 50,
            "latency_p95_1h": rng.random(count).round(1) * 400,
            "firmware_age_days": rng.integers(1, 400, count),
            "tickets_90d": rng.integers(0, 6, count),
            "device_model": rng.choice(["ONT-220", "RTR-900", "ONT-410"], count),
            "marketing_opt_in": rng.random(count) < 0.9,
            "last_contacted_at": None,
            "fault_next_72h": faults,
        }
    )


def test_a_file_whose_newest_month_is_immature_asks_leave_it_out_of_testing_and_leaving_it_out_passes() -> (
    None
):
    config = load_use_case("fault-prediction")
    frame = _monthly()
    before = report(frame, config)
    assert before.probe("X_LABEL_MATURITY").status == "fail"

    brief = load_goal_brief("fault-prediction")
    settings = ts.propose_time_settings(_loaded(frame, config), config, brief=brief)
    maturity = settings.maturity
    assert maturity is not None and not maturity.mature and maturity.fails_probe
    assert maturity.question == "Your newest month's outcomes are not final yet. Leave it out of testing?"
    assert maturity.immature_rows == 300 and maturity.immature_months == 1
    asks = ts.time_settings_asks(settings, rows=len(frame), upload_id="up-time", goal_id=brief.goal_id)
    ask = next(a for a in asks if a.code == "LABEL_MATURITY")
    settled = settle(ask, now=T0)
    assert settled.how == "ask" and settled.question is not None and settled.question.blocking
    assert settled.question.text.endswith("Leave it out of testing?")
    assert settled.question.recommended == "leave_out"

    assert maturity.immature_from is not None
    kept = ts.leave_out_immature(frame, maturity.column, maturity.immature_from)
    assert len(kept) == len(frame) - 300
    assert report(kept.reset_index(drop=True), config).probe("X_LABEL_MATURITY").status == "pass"
    # The same change as a replayable recipe step (the prescription's, M243).
    step = ts.maturity_step(maturity.column, maturity.immature_from)
    assert step.kind.value == "remove_rows" and step.params == {
        "operator": "after",
        "values": [maturity.immature_from],
    }


def test_a_mature_file_is_not_asked_about_its_newest_month() -> None:
    config = load_use_case("fault-prediction")
    frame = _monthly(newest_yes=True)
    settings = ts.propose_time_settings(_loaded(frame, config), config)  # no goal window: the outcomes decide
    assert settings.maturity is not None and settings.maturity.mature and settings.maturity.question is None
    asks = ts.time_settings_asks(settings, rows=len(frame), upload_id="u", goal_id="g")
    assert not [a for a in asks if a.code == "LABEL_MATURITY"]
    # With the goal's 3-day window the newest month-end is inside it: asked, but not blocking (its yes rate is
    # normal, so the readiness probe would pass).
    windowed = ts.propose_time_settings(
        _loaded(frame, config), config, brief=load_goal_brief("fault-prediction")
    )
    assert windowed.maturity is not None and windowed.maturity.window_days == 3
    assert not windowed.maturity.mature and not windowed.maturity.fails_probe


def test_a_time_split_whose_cuts_would_leave_a_part_empty_is_never_proposed() -> None:
    config = load_use_case("fault-prediction")
    rng = np.random.default_rng(2)
    frame = _monthly(newest_yes=True)
    frame["snapshot_date"] = np.where(rng.random(len(frame)) < 0.9, "2026-08-01", "2026-08-02")
    assert report(frame, config).probe("X_TIME_CUTS_FEASIBLE").status == "fail"
    settings = ts.propose_time_settings(
        _loaded(frame, config), config, brief=load_goal_brief("fault-prediction")
    )
    cuts = settings.cuts
    assert cuts is not None and not cuts.feasible and cuts.proposed_split == "random_stratified"
    assert cuts.validation_from is None and cuts.test_from is None
    asks = [
        a
        for a in ts.time_settings_asks(settings, rows=len(frame), upload_id="u", goal_id="g")
        if a.code == "TIME_CUTS"
    ]
    assert len(asks) == 1
    effects = [option.effect for option in asks[0].options]
    assert all(e.payload.get("split") != "time_based" for e in effects), "no time split is offered"
    settled = settle(asks[0], now=T0)
    assert settled.how == "auto" and settled.item is not None
    assert settled.item.kind == "setting"
    assert settled.item.payload == {"setting": "cuts", "path": "split.type", "value": "random_stratified"}


def test_feasible_cut_points_are_proposed_with_the_splits_own_rule() -> None:
    from engine.stages import prepare

    config = load_use_case("fault-prediction")
    frame = _monthly(newest_yes=True)
    # No outcome window given: every month is mature, so the cuts are measured on every row, as the split reads them.
    settings = ts.propose_time_settings(_loaded(frame, config), config)
    cuts = settings.cuts
    assert cuts is not None and cuts.feasible and not cuts.too_small
    rows, _plan = prepare.prepare_rows(frame, config, primary_key=("asset_id",), target="fault_next_72h")
    parts, _report = prepare.split_dataset(rows, config, run_id="r1", target="fault_next_72h")
    assert (cuts.train_rows, cuts.validation_rows, cuts.test_rows) == (
        len(parts["train"]),
        len(parts["validation"]),
        len(parts["test"]),
    )
    assert cuts.test_from == str(pd.to_datetime(parts["test"]["snapshot_date"]).min().date())


def test_two_date_columns_leave_the_as_of_date_a_question() -> None:
    config = load_use_case("targeted-advertisement")
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(50)],
            "joined_on": pd.date_range("2025-01-01", periods=50, freq="D").strftime("%Y-%m-%d"),
            "last_order": pd.date_range("2025-06-01", periods=50, freq="D").strftime("%Y-%m-%d"),
            "converted_30d": [i % 2 for i in range(50)],
        }
    )
    settings = ts.propose_time_settings(_loaded(frame, config), config)
    assert settings.as_of.column is None and set(settings.as_of.candidates) == {"joined_on", "last_order"}
    assert settings.questions == ("Which date does each row describe? Testing uses the newest dates.",)


def _cancellations(rows: int = 30_000, seed: int = 7) -> pd.DataFrame:
    """A fault file whose only date column is the outcome's own date: filled (10%) only where the fault happened."""
    rng = np.random.default_rng(seed)
    faults = (rng.random(rows) < 0.1).astype(int)
    days = pd.Timestamp("2025-01-01") + pd.to_timedelta(rng.integers(0, 620, rows), unit="D")
    return pd.DataFrame(
        {
            "asset_id": [f"A-{i:06d}" for i in range(rows)],
            "error_rate_24h": rng.random(rows).round(3) * 50,
            "device_model": rng.choice(["ONT-220", "RTR-900", "ONT-410"], rows),
            "cancelled_on": np.where(faults == 1, days.strftime("%Y-%m-%d"), None),
            "fault_next_72h": faults,
        }
    )


def test_a_sparse_outcome_date_is_never_taken_as_the_date_each_row_describes() -> None:
    """The reviewer's case: the outcome's own date, filled only on the 10% of rows where the fault happened, read as
    the as-of date for sure, cut the test set by the outcome and left 27,000 rows out - and accepted it unasked.
    """
    config = load_use_case("fault-prediction")
    frame = _cancellations()
    brief = load_goal_brief("fault-prediction")
    settings = ts.propose_time_settings(_loaded(frame, config), config, brief=brief)
    as_of = settings.as_of
    assert as_of.column is None and not as_of.sure and as_of.candidates == ()
    assert as_of.left_out == ("cancelled_on",)
    assert "outcome's own date" in as_of.reason and "10% of rows" in as_of.reason
    assert settings.cuts is None and settings.maturity is None
    assert settings.questions == ("Which date does each row describe? Testing uses the newest dates.",)
    asks = ts.time_settings_asks(settings, rows=len(frame), upload_id="u", goal_id=brief.goal_id)
    assert not [a for a in asks if a.code == "TIME_CUTS"], "no time split is decided on it"


def test_a_chosen_date_column_with_undated_rows_counts_them_and_asks() -> None:
    """When the person picks a sparsely filled date column anyway, the rows it has no date for are counted in the
    sentence and the cut points are a question - never accepted by the rules."""
    config = load_use_case("fault-prediction")
    frame = _cancellations()
    undated = int(frame["cancelled_on"].isna().sum())
    settings = ts.propose_time_settings(_loaded(frame, config), config, as_of_column="cancelled_on")
    cuts = settings.cuts
    assert settings.as_of.column == "cancelled_on" and cuts is not None and cuts.feasible
    assert cuts.undated_rows == undated
    assert cuts.train_rows + cuts.validation_rows + cuts.test_rows == len(frame) - undated
    assert f"{undated:,} rows have no date in 'cancelled_on' and are not in any part." in cuts.reason
    ask = next(a for a in ts.time_settings_asks(settings, rows=len(frame), upload_id="u", goal_id="g")
               if a.code == "TIME_CUTS")  # fmt: skip
    assert ask.proven is None
    settled = settle(ask, now=T0)
    assert settled.how == "ask" and settled.item is None and settled.question is not None
    assert settled.question.recommended is None or settled.question.recommended != "dates"


def test_an_event_file_offers_monthly_or_weekly_snapshots() -> None:
    config = load_use_case("targeted-advertisement")
    days = pd.date_range("2025-01-01", periods=400, freq="D")
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{i % 50}" for i in range(400)],
            "event_date": days.strftime("%Y-%m-%d"),
            "amount": np.arange(400) % 70,
        }
    )
    # The only date column is not decided for the person: nothing names it, so it is a question.
    undecided = ts.propose_time_settings(_loaded(frame, config), config, primary_key="customer_id")
    assert undecided.as_of.column is None and undecided.as_of.candidates == ("event_date",)
    assert not undecided.as_of.sure and undecided.cadence is None
    assert undecided.questions == ("Which date does each row describe? Testing uses the newest dates.",)
    settings = ts.propose_time_settings(
        _loaded(frame, config), config, as_of_column="event_date", primary_key="customer_id", window_days=30
    )
    cadence = settings.cadence
    assert cadence is not None and cadence.ask and cadence.proposed == "monthly"
    assert cadence.weekly_snapshots > cadence.monthly_snapshots >= ts.MIN_SNAPSHOTS
    asks = ts.time_settings_asks(settings, rows=400, upload_id="u", goal_id="g")
    ask = next(a for a in asks if a.code == "SNAPSHOT_CADENCE")
    assert [o.label for o in ask.options] == ["Every month", "Every week"]


# ---------------------------------------------------------------------------
# The switch: `_time_rules` byte-identical off; the budget replaces it on
# ---------------------------------------------------------------------------
def _facts(rows: int) -> DataFacts:
    return DataFacts(
        rows=rows,
        positive_rate=0.3,
        time_columns=(),
        consent_candidates=(),
        evidence_ids=("e1",),
        columns=("customer_id", "visits_last_7d", "plan_tier", "converted_30d"),
    )


def _time_recs(config: UseCaseConfig, rows: int) -> list[Any]:
    schema = advanced_settings_schema(
        config, columns=_facts(rows).columns, primary_key="customer_id", target="converted_30d"
    )
    found = recommend_settings(config, _facts(rows), schema, holdout=None)
    return [r for r in found if r.path == "model_search.time_limit_minutes"]


def test_with_the_switch_off_time_rules_are_unchanged() -> None:
    config = load_use_case("targeted-advertisement")
    small = _time_recs(config, 1_200)
    assert [(r.value, r.title, r.confidence) for r in small] == [
        (10, "Limit the model search to 10 minutes", AgentConfidence.SURE)
    ]
    assert _time_recs(config, 50_000) == []


def test_with_the_switch_on_the_budget_replaces_the_file_size_rule() -> None:
    config = _on(load_use_case("targeted-advertisement"))
    recs = _time_recs(config, 1_200)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.title.startswith("Training time: Balanced (about ") and rec.title.endswith(
        " min, recommended)"
    )
    assert rec.confidence is AgentConfidence.CHECK  # no run history: from similar public datasets
    assert "Quick (about" in rec.reason and "Thorough (about" in rec.reason
    assert "from similar public datasets" in rec.reason
    # No store was read: nothing is known about this deployment's runs, so nothing is claimed about them.
    assert "no finished training run" not in rec.reason


def test_the_settings_defaults_are_left_out_of_a_dump() -> None:
    config = load_use_case("targeted-advertisement")
    assert "data_doctor" not in config.model_dump(mode="json")
    assert config.data_doctor.time_settings.score_step_per_minute == 0.0005
    assert config.data_doctor.time_settings.quick_ratio == 0.34
    assert config.data_doctor.time_settings.thorough_ratio == 2.5


def test_the_curve_reads_a_leaderboard_in_fitting_order() -> None:
    board = {
        "greater_is_better": False,
        "entries": [
            {
                "model_name": "XGBoost_BAG_L1/T2",
                "validation_score": 0.30,
                "fit_time_seconds": 5,
                "stack_level": 1,
            },
            {
                "model_name": "WeightedEnsemble_L2",
                "validation_score": 0.28,
                "fit_time_seconds": 1,
                "stack_level": 2,
            },
            {
                "model_name": "LightGBM_BAG_L1/T1",
                "validation_score": 0.35,
                "fit_time_seconds": 4,
                "stack_level": 1,
            },
        ],
    }
    assert ts.curve_of(board) == ((4.0, -0.35), (9.0, -0.30), (10.0, -0.28))


def test_offers_promises_and_records_live_in_registered_stores() -> None:
    assert ts.offer_key("upload", "u1", "uc").startswith("uploads/u1/")
    assert ts.offer_key("dataset", "d1", "uc").startswith("datasets/d1/")
    assert run_key("r1", ts.PROMISE_FILENAME).startswith("runs/r1/")
    assert run_key("r1", ts.RECORD_FILENAME).startswith("runs/r1/")


def test_with_no_storage_a_budget_is_still_offered_from_the_prior() -> None:
    config = _on(load_use_case("targeted-advertisement"))
    budget = ts.propose_budget(ts.time_inputs(None, config, rows=10_000, columns=12))
    assert budget.basis == "public_library" and len(budget.choices) == 3


def test_an_uplift_learner_without_a_time_limit_has_no_budget_to_choose() -> None:
    from engine.uplift.config import UpliftBaseModel

    config = load_use_case("targeted-advertisement")
    uplift = config.model_copy(
        update={
            "problem_type": ProblemType.UPLIFT,
            "uplift": config.uplift.model_copy(update={"base_model": UpliftBaseModel.LIGHTGBM}),
        }
    )
    with pytest.raises(ts.TimeSettingsError) as refused:
        ts.setting_path(uplift)
    assert refused.value.code == "TIME_BUDGET_NOT_USED"


def test_the_promise_survives_a_week_old_offer(tmp_path: Path) -> None:
    """An offer is kept with the data, whenever it was made; the promise names when it was offered."""
    storage = LocalStorage(tmp_path)
    config = _on(load_use_case("targeted-advertisement"))
    budget = ts.propose_budget(ts.time_inputs(storage, config, rows=100, columns=4))
    offered = T0 - timedelta(days=7)
    ts.save_offer(
        storage,
        ts.TimeOffer(
            use_case_id=config.id, source_kind="upload", source_id="u9", offered_at=offered, rows=100, columns=4,
            budget=budget,
        ),
    )  # fmt: skip
    found = ts.read_offer(storage, "upload", "u9", config.id)
    assert found is not None and found.offered_at == offered
