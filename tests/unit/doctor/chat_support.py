"""What the Data Doctor chat's tests share: an orders file with test accounts, a case at its first rest, the chat
port over a scripted model, and a way to say something to the Doctor."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from engine.access.roles import Principal, Role
from engine.agent.tools import AgentContext
from engine.config import BudgetConfig, LlmConfig, RunMode, load_use_case
from engine.doctor import visit
from engine.doctor.agent_chat import doctor_converse
from engine.doctor.ai_guard import AiGuard
from engine.doctor.case import Case, CaseStore
from engine.doctor.config import AiLimitsConfig
from engine.doctor.diagnose import LoadedFile, Offer
from engine.storage import LocalStorage
from tests.fixtures.doctor_eval.chat_model import ChatScript
from tests.unit.agent.egress_canary import guardrails
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.test_visit import _MEMORY, _Cached
from tests.unit.doctor.visit_support import ANALYST, CLIENT, FIRST, GOAL_A, World, deps_for

ASHA = Principal(user_id=ANALYST, username="asha", roles=frozenset({Role.ANALYST}), kind="user")
TEST_ACCOUNTS = 36
"""Customers whose ID starts with 999 in the orders file: 3% of its rows."""
OLD_ORDERS = 42
"""Orders from last year, the rest are from this year (so "only this year's orders" leaves out about 3.5%)."""

_WORLD: dict[str, World] = {}


def orders_frame(rows: int = 1_200) -> pd.DataFrame:
    """One row per order: six-digit customer IDs (every 33rd is a test account starting 999), an order amount, a date."""
    frame = vs.customers(rows=rows, seed=11)
    ids = [str(100_000 + i) for i in range(rows)]
    for n in range(TEST_ACCOUNTS):
        ids[n * 33] = str(999_000 + n)
    frame["customer_id"] = ids
    rng = np.random.default_rng(5)
    frame["order_amount"] = np.round(rng.gamma(2.0, 300.0, rows), 2)
    dates = pd.Series(pd.Timestamp("2026-01-02") + pd.to_timedelta(rng.integers(0, 270, rows), unit="D"))
    dates.iloc[7 :: rows // OLD_ORDERS] = pd.Timestamp("2025-11-15")
    frame["order_date"] = dates.dt.strftime("%Y-%m-%d").to_numpy()
    frame["channel"] = ["Email", "email ", "EMAIL", "SMS", "sms", "Sms"] * (rows // 6)
    return frame


def orders_world() -> World:
    if "w" not in _WORLD:
        config = load_use_case(GOAL_A)
        _WORLD["w"] = World(
            files={FIRST: vs.loaded(FIRST, "orders.csv", orders_frame(), config)},
            offers=(Offer(config=config, stage="Acquire"),),
        )
    world = _WORLD["w"]
    world.ticks = 0
    return world


def monthly_world() -> World:
    """The orders file in two halves, as two exports of the same report (different customers, so nothing conflicts)."""
    if "m" not in _WORLD:
        config = load_use_case(GOAL_A)
        frame = orders_frame()
        _WORLD["m"] = World(
            files={
                "up-orders-1": vs.loaded(
                    "up-orders-1", "orders_part_1.csv", frame.iloc[:600].reset_index(drop=True), config
                ),
                "up-orders-2": vs.loaded(
                    "up-orders-2", "orders_part_2.csv", frame.iloc[600:].reset_index(drop=True), config
                ),
            },
            offers=(Offer(config=config, stage="Acquire"),),
        )
    world = _WORLD["m"]
    world.ticks = 0
    return world


class Down(ChatScript):
    """A connected AI service whose every call fails with `error` (an expired key, an empty answer ...)."""

    def __init__(self, error: Exception) -> None:
        super().__init__({})
        self.error = error
        self.attempts = 0

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        self.attempts += 1
        raise self.error


def guard_for(client: ChatScript, *, consent: bool = True, limits: AiLimitsConfig | None = None) -> AiGuard:
    return AiGuard(
        client,
        job_id="test",
        llm=LlmConfig(),
        budget=BudgetConfig(cache=False, max_calls_per_run=100_000, max_cost_usd_per_run=1_000.0),
        limits=limits or AiLimitsConfig(),
        consented=lambda _client: consent,
    )


def chat_port(
    client: ChatScript,
    storage: LocalStorage | None = None,
    *,
    consent: bool = True,
    limits: AiLimitsConfig | None = None,
    config_id: str = GOAL_A,
) -> visit.ConversePort:
    def context_for(case: Case, files: tuple[LoadedFile, ...]) -> AgentContext:
        return AgentContext(
            use_case_id=config_id,
            config=load_use_case(config_id),
            config_root=None,
            upload_id=files[0].upload_id,
            mode=RunMode.TRAIN,
            profile=files[0].profile,
            frame=files[0].frame,
        )

    return doctor_converse(
        context_for,
        meter=guard_for(client, consent=consent, limits=limits),
        guardrails=guardrails(),
        principal=ASHA,
        registry=_Cached(visit.visit_registry(), _MEMORY),
        storage=storage,
    )


class Talk:
    """A person talking to the Doctor about one case, with the chat port over a scripted model."""

    def __init__(
        self,
        storage: LocalStorage,
        world: World,
        client: ChatScript | None,
        *,
        files: tuple[str, ...] = (FIRST,),
        key: str | None = "customer_id",
        **port: Any,
    ) -> None:
        self.storage, self.world, self.client = storage, world, client
        self.converse = None if client is None else chat_port(client, storage, **port)
        opened = CaseStore(storage, clock=world.clock).create(
            client_id=CLIENT, created_by=ANALYST, files=[vs.case_file(f, world) for f in files]
        )
        self.case = visit.resume(CLIENT, opened.case_id, self.deps(), actor=ANALYST)
        if key is not None:
            q = vs.question(self.case, "KEY_AMBIGUOUS")
            self.case = visit.answer(
                CLIENT, self.case.case_id, q.question_id, vs.option(q, key), self.deps(),
                expected_version=self.case.version, actor=ANALYST,
            )  # fmt: skip

    def goal(self, goal_id: str = GOAL_A) -> Case:
        self.case = visit.set_goal(
            CLIENT, self.case.case_id, goal_id, self.deps(), expected_version=self.case.version, actor=ANALYST
        )
        return self.case

    def deps(self) -> visit.VisitDeps:
        return deps_for(
            self.storage,
            self.world,
            registry=_Cached(visit.visit_registry(), _MEMORY),
            converse=self.converse,
        )

    def say(self, text: str) -> Case:
        self.case = visit.chat(
            CLIENT, self.case.case_id, text, self.deps(), expected_version=self.case.version, actor=ANALYST
        )
        return self.case

    def decide(self, item_id: str, accept: bool) -> Case:
        self.case = visit.decide(
            CLIENT, self.case.case_id, item_id, accept, self.deps(),
            expected_version=self.case.version, actor=ANALYST,
        )  # fmt: skip
        return self.case

    def answer(self, question_id: str, option_id: str) -> Case:
        self.case = visit.answer(
            CLIENT, self.case.case_id, question_id, option_id, self.deps(),
            expected_version=self.case.version, actor=ANALYST,
        )  # fmt: skip
        return self.case

    @property
    def reply(self) -> Any:
        return self.case.chat[-1]

    def timeline(self) -> list[Any]:
        return list(CaseStore(self.storage).timeline(CLIENT, self.case.case_id))


def ai_items(case: Case) -> list[Any]:
    return [i for i in case.items if i.source.value == "ai"]
