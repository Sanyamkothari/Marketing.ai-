"""The batched sweep decides, sends, holds and records exactly what the whole-audience sweep did."""

from __future__ import annotations

import gc
import tracemalloc
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from autonomy.channels.dispatcher import Dispatcher
from autonomy.contracts import (
    AudienceFilter,
    AutonomyLevel,
    Channel,
    CustomerState,
    Decision,
    DecisionContext,
    Event,
    EventType,
    GoalStatus,
)
from autonomy.fakes import (
    ContactEveryonePolicy,
    FakeChannelAdapter,
    InMemoryDecisionLog,
    InMemoryEventStore,
    InMemoryStateStore,
    InMemoryWorkspace,
    SimpleStateFolder,
)
from autonomy.fakes.samples import sample_brief, sample_content, sample_goal
from autonomy.runtime import service
from autonomy.runtime.service import AutonomyRuntime, RuntimeConfig
from autonomy.runtime.settings import RuntimeSettings, build_runtime
from autonomy.store import memory_engine
from tests.v3.scale import scenario as sc

SMALL = 60
"""Customers in the fast equivalence runs: enough for dozens of batches of 1 and a few of 7."""


def run(n: int = SMALL, **kw: Any) -> dict[str, Any]:
    s = sc.build(n, **kw)
    return sc.fingerprint(s, sc.play(s))


# --- the batched sweep is the whole-audience sweep ----------------------------------------------


@pytest.mark.parametrize("one_contact", [True, False])
@pytest.mark.parametrize("mandate_kind", ["none", "paced", "violation"])
def test_any_batch_size_gives_the_same_ticks_decisions_sends_and_alerts(
    one_contact: bool, mandate_kind: str
) -> None:
    """Four ticks (the sweep, a repeat, a lapse check, the next morning with a batch approved), three goals
    at the three levels, a policy that passes and raises, and a mandate with a daily cap or an exclusion."""
    whole = run(sweep_batch=None, one_contact=one_contact, mandate_kind=mandate_kind)
    summary = whole["summaries"][0]
    assert summary["decisions"] > SMALL // 2 and summary["acts"] > 0
    assert (summary["sent"] > 0) == (mandate_kind != "violation")
    assert any(a["code"] == "decide.error" for a in summary["alerts"])
    if mandate_kind != "none":
        assert whole["report_mandate_blocked"][0], "the mandate should stop some sends"
    for size in (1, 7, 25):
        assert run(sweep_batch=size, one_contact=one_contact, mandate_kind=mandate_kind) == whole, size
    # The default (5,000) is one batch at this size: this checks that leaving the setting out works.
    assert run(one_contact=one_contact, mandate_kind=mandate_kind) == whole


def test_a_later_goal_prepares_and_sweeps_on_the_population_as_the_tick_first_read_it() -> None:
    """The goals run approve, auto, shadow: the shadow goal's `prepare_day` and sweep come after the
    autopilot goal has sent, and still see the customers as they were before those sends."""
    for one_contact in (True, False):
        whole = run(sweep_batch=None, one_contact=one_contact)
        assert whole["summaries"][0]["sent"] > 0
        first_day = {gid: p[0] for gid, p in whole["prepared"].items()}
        # Nobody had been contacted before the first sweep, whichever goal prepared after the sends.
        assert all(p["contacts"] == p["contacted"] == p["contacts_7d"] == 0 for p in first_day.values())
        assert len({p["digest"] for p in first_day.values()}) == 1
        assert run(sweep_batch=3, one_contact=one_contact) == whole


def test_with_a_send_time_gateway_only_the_order_of_the_dispatch_results_follows_the_batch() -> None:
    """The gateway lists its own refusals of a call before that call's sends, so batched calls
    interleave them (docs/v3/SCALE.md). Everything else, and the results themselves, are the same."""
    whole = run(sweep_batch=None, gateway=True)
    assert any(r[3] == "opted_out" for r in whole["report_dispatch"][0]), "the gateway should refuse some"
    assert whole["summaries"][0]["sent"] > 0
    assert any(a["code"] == "dispatch.refused" for a in whole["summaries"][0]["alerts"])
    for size in (1, 7):
        batched = run(sweep_batch=size, gateway=True)
        assert [sorted(r) for r in batched.pop("report_dispatch")] == [
            sorted(r) for r in whole["report_dispatch"]
        ]
        assert batched == {k: v for k, v in whole.items() if k != "report_dispatch"}, size


def test_a_send_before_the_tick_first_reads_the_population_is_seen_by_the_later_sweep() -> None:
    """The old sweep read the population the first time a goal needed it. A send made earlier in the
    tick (an event trigger of a goal with no sweep due) was in that read, so a later goal's audience
    is matched on the state after it."""
    start = sc.T0 - timedelta(days=1)
    events = InMemoryEventStore()
    events.append(
        [
            Event(
                event_id=f"consent_{cid}",
                customer_id=cid,
                type=EventType.CONSENT_GRANTED,
                ts=sc.T0 - timedelta(days=30),
                source="test",
                channel=Channel.EMAIL,
            )
            for cid in ("a", "x")
        ]
        + [
            Event(
                event_id="buy_a",
                customer_id="a",
                type=EventType.PURCHASE,
                ts=sc.T0 - timedelta(days=10),
                source="test",
                value=30.0,
            )
        ]
    )
    workspace = InMemoryWorkspace()

    def put(gid: str, level: AutonomyLevel, status: GoalStatus, *filters: AudienceFilter) -> None:
        workspace.put_goal(sample_goal(start, gid).model_copy(update={"autonomy": level, "status": status}))
        brief = sample_brief(start, gid)
        audience = brief.audience.model_copy(update={"filters": filters})
        workspace.put_brief(brief.model_copy(update={"audience": audience}))
        if level is AutonomyLevel.AUTOPILOT:  # the sample content ids are the same for every goal
            workspace.put_content(sample_content(start, gid))

    bought = AudienceFilter(field="purchases", op="gte", value=1)
    put("goal_a", AutonomyLevel.AUTOPILOT, GoalStatus.ACTIVE, bought)
    put(
        "goal_b",
        AutonomyLevel.SHADOW,
        GoalStatus.PAUSED,
        bought,
        AudienceFilter(field="contacts_7d", op="lte", value=0),
    )
    adapter = FakeChannelAdapter(Channel.EMAIL)
    runtime = AutonomyRuntime(
        events=events,
        states=InMemoryStateStore(),
        decisions=InMemoryDecisionLog(),
        workspace=workspace,
        folder=SimpleStateFolder(),
        policy_factory=lambda briefs, content: ContactEveryonePolicy(),
        dispatcher=Dispatcher(
            {Channel.EMAIL: adapter}, lambda cid, ch: f"{cid}@example.com", workspace, events
        ),
        config=RuntimeConfig(one_contact_per_tick=False, sweep_batch=1),
    )
    runtime.tick(sc.SWEEP)  # goal_a sweeps and sends to a; x has not bought
    events.append(
        [
            Event(
                event_id="buy_x",
                customer_id="x",
                type=EventType.PURCHASE,
                ts=sc.SWEEP + timedelta(minutes=1),
                source="test",
                value=30.0,
            ),
            Event(
                event_id="cart_x",
                customer_id="x",
                type=EventType.CART,
                ts=sc.SWEEP + timedelta(minutes=1),
                source="test",
                item_id="sku1",
            ),
        ]
    )
    paused = next(g for g in workspace.goals() if g.goal_id == "goal_b")
    workspace.put_goal(paused.model_copy(update={"status": GoalStatus.ACTIVE}))
    report = runtime.tick(sc.SWEEP + timedelta(minutes=2))
    # goal_a reached x on the purchase before goal_b's first sweep read anyone, so goal_b's audience
    # (no contact this week) leaves x out, as it leaves out a, whom goal_a reached in the first tick.
    assert [m.customer_id for m in adapter.sent] == ["a", "x"]
    assert report.swept_goals == ["goal_b"]
    assert [d.customer_id for d in report.decisions] == ["x"]


def test_assigning_groups_to_every_state_gives_the_same_result_in_batches() -> None:
    whole = run(sweep_batch=None, groups=True)
    assert run(sweep_batch=4, groups=True) == whole
    assert any(st["group"] == "holdout" for st in whole["states"])
    assert whole != run(sweep_batch=None)


def test_the_sweep_pages_through_sql_stores_the_same_way() -> None:
    """The state store pages by customer id and the decision log writes in batches of 10 here."""
    whole = run(sweep_batch=None, sql=True, mandate_kind="paced")
    assert whole["summaries"][0]["decisions"] > SMALL // 2
    for size in (1, 9, 25):
        assert run(sweep_batch=size, sql=True, mandate_kind="paced") == whole, size


def test_the_batch_size_is_a_setting_and_is_checked() -> None:
    assert RuntimeSettings.from_env({"AUTONOMY_SWEEP_BATCH": "250"}).config().sweep_batch == 250
    assert RuntimeSettings().config().sweep_batch == RuntimeConfig().sweep_batch == 5_000
    with pytest.raises(ValueError):
        RuntimeConfig(sweep_batch=0)
    with pytest.raises(ValueError):
        RuntimeConfig(report_decisions=-1)
    with pytest.raises(ValueError):
        RuntimeSettings.from_env({"AUTONOMY_SWEEP_BATCH": "0"})
    # A built runtime (the worker) keeps a bounded few of a tick's decisions; a runtime made in
    # code keeps them all unless told otherwise.
    assert RuntimeSettings().report_decisions == 1_000 and RuntimeConfig().report_decisions is None
    assert RuntimeSettings.from_env({"AUTONOMY_REPORT_DECISIONS": "50"}).report_decisions == 50
    with pytest.raises(ValueError):
        RuntimeSettings.from_env({"AUTONOMY_REPORT_DECISIONS": "-1"})


def test_the_scenario_covers_every_part_of_the_sweep() -> None:
    whole = run(sweep_batch=None)
    summaries = whole["summaries"]
    assert summaries[0]["swept_goals"] and summaries[0]["batches"], "a sweep that holds a batch for approval"
    assert summaries[2]["decisions"] > 0, "a lapse check that decides"
    assert summaries[3]["batches_sent"], "an approved batch that is sent"
    assert all(p for p in whole["prepared"].values()), "prepare_day ran for every goal"
    assert any(e["type"] == "send" for e in whole["events"])


# --- a batch is all that is held at a time -------------------------------------------------------


def test_the_enricher_and_the_policy_see_one_batch_at_a_time() -> None:
    s = sc.build(SMALL, sweep_batch=8, enrich=True)
    s.runtime.tick(sc.SWEEP)
    assert s.enriched_calls and max(s.enriched_calls) <= 8
    whole = sc.build(SMALL, sweep_batch=None, enrich=True)
    whole.runtime.tick(sc.SWEEP)
    assert sum(s.enriched_calls) == sum(whole.enriched_calls)


def test_a_pass_of_many_batches_scans_the_event_window_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Above `ENRICH_PER_CUSTOMER_MAX` a batch's recent events come from a scan of the lookback window.
    Only a pass's first batch scans; the later ones read per customer, with the same result."""
    monkeypatch.setattr(service, "ENRICH_PER_CUSTOMER_MAX", 2)
    whole = run(sweep_batch=None, enrich=True)
    s = sc.build(SMALL, sweep_batch=5, enrich=True)
    window_scans = 0
    scan = s.events.scan

    def counting_scan(*, since: datetime | None = None, until: datetime | None = None) -> Iterator[Event]:
        nonlocal window_scans
        if since is not None and until is not None and until - since == s.runtime.config.enrich_lookback:
            window_scans += 1
        return scan(since=since, until=until)

    s.events.scan = counting_scan  # type: ignore[method-assign]
    assert sc.fingerprint(s, sc.play(s)) == whole
    assert len(s.enriched_calls) > 3 * 4, "many batches"
    assert 0 < window_scans <= 3 * 4, window_scans  # at most one per goal per tick


def test_prepare_day_gets_the_whole_population_without_the_runtime_holding_it() -> None:
    s = sc.build(SMALL, sweep_batch=8)
    s.runtime.tick(sc.SWEEP)
    seen: list[Any] = []

    class Spy(ContactEveryonePolicy):
        def prepare_day(self, states: Sequence[CustomerState], ctx: DecisionContext) -> None:
            seen.append(states)
            first = [x.customer_id for x in states]
            second = [x.customer_id for x in states]
            assert first == second == sorted(first)
            assert len(states) == len(first) == s.states.count()
            assert states[0].customer_id == first[0] and states[-1].customer_id == first[-1]
            assert len(states[1:3]) == 2

    spy = Spy()
    t = sc.build(SMALL, sweep_batch=8)
    t.runtime.policy_factory = lambda briefs, content: spy
    t.runtime.tick(sc.SWEEP)
    assert seen and not isinstance(seen[0], list)


def test_the_report_keeps_only_as_many_decisions_as_asked_and_counts_the_rest() -> None:
    full = sc.build(SMALL, sweep_batch=7)
    whole = full.runtime.tick(sc.SWEEP)
    capped = sc.build(SMALL, sweep_batch=7, report_decisions=10)
    report = capped.runtime.tick(sc.SWEEP)
    assert len(report.decisions) == 10 and report.decisions == whole.decisions[:10]
    assert report.decision_count == len(whole.decisions) and report.act_count == len(whole.acts)
    assert report.summary()["decisions"] == whole.summary()["decisions"]
    assert report.summary()["acts"] == whole.summary()["acts"]
    assert report.summary()["sent"] == whole.summary()["sent"]
    # Everything is still logged and acted on; only the report is shorter.
    assert {d.decision_id for d in capped.decisions.scan()} == {d.decision_id for d in full.decisions.scan()}
    assert len(capped.adapter.sent) == len(full.adapter.sent) > 0


def test_a_mandate_block_is_written_back_to_the_logged_decision_in_every_batch() -> None:
    s = sc.build(SMALL, sweep_batch=5, mandate_kind="violation")
    report = s.runtime.tick(sc.SWEEP)
    assert report.mandate_blocked
    for decision_id in report.mandate_blocked:
        logged = s.decisions.get(decision_id)
        assert logged is not None and "Not sent" in logged.reason
    shown = {d.decision_id: d for d in report.decisions}
    assert all("Not sent" in shown[i].reason for i in report.mandate_blocked)
    assert sum(1 for a in report.alerts if a.code.startswith("mandate.")) == 1  # one alert, not one per batch


def test_a_failing_sender_is_one_alert_for_the_goal_however_many_batches() -> None:
    s = sc.build(SMALL, sweep_batch=5)

    def boom(*_: object, **__: object) -> None:
        raise ConnectionError("provider down")

    assert s.runtime.dispatcher is not None
    s.runtime.dispatcher.dispatch = boom  # type: ignore[assignment,method-assign]
    report = s.runtime.tick(sc.SWEEP)
    errors = [a for a in report.alerts if a.code == "dispatch.error"]
    assert len(errors) == 1 and errors[0].goal_id == sc.AUTO_GOAL
    assert "nothing was sent this tick" in errors[0].message
    assert s.adapter.sent == []


# --- bounded memory ------------------------------------------------------------------------------


class _CountingLog:
    """A decision log that counts and keeps nothing, so the sweep's own memory is what is measured."""

    def __init__(self) -> None:
        self.n = 0

    def record(self, decisions: Iterable[Decision]) -> None:
        self.n += sum(1 for _ in decisions)

    def get(self, decision_id: str) -> Decision | None:
        return None

    def scan(self, *, since: datetime | None = None, until: datetime | None = None) -> Iterator[Decision]:
        return iter(())

    def for_customer(self, customer_id: str) -> list[Decision]:
        return []


def _shadow_sweep(n: int, batch: int | None, report_decisions: int | None = 0) -> tuple[int, int]:
    """(bytes the sweep peaked at above where it started, bytes still held when it ended) for `n` customers.

    What is still held is the day's decided-set (`_Day`), about half a KB per decision: the one
    thing the sweep keeps per customer. The rest of the peak is the batch in flight.

    One goal in shadow, the states already folded (a first tick before the sweep hour), a log that
    keeps nothing, and a report that keeps `report_decisions` decisions (None: all, as before)."""
    t0 = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)
    events = InMemoryEventStore()
    rows: list[Event] = []
    for i in range(n):
        cid = f"c{i:07d}"
        rows.append(
            Event(
                event_id=f"k{i}",
                customer_id=cid,
                type=EventType.CONSENT_GRANTED,
                ts=t0 - timedelta(days=200),
                source="test",
                channel=Channel.EMAIL,
            )
        )
        rows.append(
            Event(
                event_id=f"b{i}",
                customer_id=cid,
                type=EventType.PURCHASE,
                ts=t0 - timedelta(days=90),
                source="test",
                value=40.0,
                category="shoes",
            )
        )
    events.append(rows)
    del rows
    workspace = InMemoryWorkspace()
    start = t0 - timedelta(days=1)
    workspace.put_goal(
        sample_goal(start).model_copy(update={"autonomy": AutonomyLevel.SHADOW, "status": GoalStatus.ACTIVE})
    )
    workspace.put_brief(sample_brief(start))
    workspace.put_content(sample_content(start))
    log = _CountingLog()
    policy = ContactEveryonePolicy()
    runtime = AutonomyRuntime(
        events=events,
        states=InMemoryStateStore(),
        decisions=log,
        workspace=workspace,
        folder=SimpleStateFolder(),
        policy_factory=lambda briefs, content: policy,
        config=RuntimeConfig(sweep_batch=batch, report_decisions=report_decisions),
    )
    runtime.tick(t0)  # folds every event into a state; before the sweep hour, so it decides nothing
    assert log.n == 0
    gc.collect()
    tracemalloc.start()
    try:
        before, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        report = runtime.tick(t0.replace(hour=10))
        after, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert report.decision_count == n == log.n
    return peak - before, after - before


def test_a_small_sweep_in_batches_holds_less_than_the_same_sweep_in_one() -> None:
    batched, _ = _shadow_sweep(2_000, 100)
    whole, _ = _shadow_sweep(2_000, None)
    assert batched < whole / 3, (batched, whole)


@pytest.mark.slow
def test_the_sweeps_peak_memory_follows_the_batch_not_the_customer_count() -> None:
    """With batches of 500, the memory in flight is the same at 5,000 customers and at 20,000, and the
    20,000-customer sweep peaks far below the same sweep in one batch."""
    small, small_kept = _shadow_sweep(5_000, 500)
    big, big_kept = _shadow_sweep(20_000, 500)
    whole, _ = _shadow_sweep(20_000, None)
    assert big - big_kept < (small - small_kept) * 1.5 + 1_000_000, (small, small_kept, big, big_kept)
    assert big_kept < 1_000 * 20_000, big_kept  # the decided-set: under 1 KB a customer
    assert big < whole / 4, (big, whole)
    assert big < 40 * 1024 * 1024, big


@pytest.mark.slow
def test_a_worker_built_from_the_default_settings_holds_a_fraction_of_the_old_sweep() -> None:
    """The deployed defaults (batches of 5,000, a report of 1,000 decisions) against the old sweep
    (the whole audience, every decision kept in the report) on 20,000 customers."""
    settings = RuntimeSettings()
    deployed, deployed_kept = _shadow_sweep(20_000, settings.sweep_batch, settings.report_decisions)
    old, old_kept = _shadow_sweep(20_000, None, None)
    assert deployed < old * 0.6, (deployed, old)  # 42 MB against 88 MB when measured
    assert deployed_kept < old_kept / 3, (deployed_kept, old_kept)  # 12 MB against 49 MB


def test_a_runtime_built_from_settings_keeps_a_bounded_report() -> None:
    runtime = build_runtime(
        RuntimeSettings(database_url="sqlite://", report_decisions=7, sweep_batch=3),
        engine=memory_engine(),
        policy_factory=lambda briefs, content: ContactEveryonePolicy(),
    )
    assert runtime.config.report_decisions == 7 and runtime.config.sweep_batch == 3
    assert (
        build_runtime(
            RuntimeSettings(database_url="sqlite://"), engine=memory_engine()
        ).config.report_decisions
        == 1_000
    )
