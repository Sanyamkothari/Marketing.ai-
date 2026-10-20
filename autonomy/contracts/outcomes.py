"""Outcomes, proof, priors, health and briefings: what the system learned and what it tells people."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from autonomy.contracts.base import Contract


class Reward(Contract):
    """The realised result of one decision, once its outcome window has closed."""

    decision_id: str
    customer_id: str
    goal_id: str | None
    window_end: AwareDatetime
    converted: bool
    revenue: float = 0.0
    margin: float = 0.0
    cost: float = Field(default=0.0, description="Offer + channel cost actually spent")
    net: float = Field(description="margin - cost (or revenue - cost, by objective)")
    unsubscribed: bool = False
    complained: bool = False


class Estimate(Contract):
    """A number with its 95% interval. Every money figure shown to a person is one of these."""

    point: float
    low: float
    high: float

    @model_validator(mode="after")
    def _ordered(self) -> Estimate:
        if not self.low <= self.point <= self.high:
            raise ValueError("an estimate needs low <= point <= high")
        return self


class ProofSummary(Contract):
    goal_id: str | None = Field(description="None: the whole programme")
    as_of: AwareDatetime
    agent_n: int
    baseline_n: int
    holdout_n: int
    incremental_revenue: Estimate | None = Field(description="Agent vs holdout, per the agent group")
    incremental_profit: Estimate | None
    lift_vs_baseline: Estimate | None = Field(description="Agent vs simple baseline, profit per customer")
    spend: float = 0.0
    roi: Estimate | None = None
    notes: tuple[str, ...] = ()


class PriorSource(StrEnum):
    GYM = "gym"
    PUBLIC = "public"
    CLIENTS = "clients"


MIN_CLIENTS_FOR_SHARED_PRIOR = 5


class Prior(Contract):
    """A pooled pattern used as a starting point (plan-v2 section 7). Never customer rows."""

    key: str = Field(
        description="e.g. 'fatigue/win_back', 'send_hour/email', 'theme_lift/win_back/education'"
    )
    source: PriorSource
    n_clients: int = Field(default=0, ge=0)
    n_observations: int = Field(default=0, ge=0)
    values: dict[str, float]
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _pooled_enough(self) -> Prior:
        if self.source is PriorSource.CLIENTS and self.n_clients < MIN_CLIENTS_FOR_SHARED_PRIOR:
            raise ValueError(f"a client-sourced prior needs at least {MIN_CLIENTS_FOR_SHARED_PRIOR} clients")
        return self


class Alert(Contract):
    alert_id: str
    at: AwareDatetime
    goal_id: str | None = None
    severity: Literal["info", "warning", "critical"]
    code: str
    message: str
    auto_paused: bool = False


class HealthStatus(Contract):
    as_of: AwareDatetime
    delivery_rate: float | None = None
    complaint_rate: float | None = None
    unsubscribe_rate: float | None = None
    block_rate: float | None = None
    contacts_per_customer_week: float | None = None
    alerts: tuple[Alert, ...] = ()


class BriefingSection(Contract):
    title: str
    body: str = Field(description="Plain English; every number in it appears in `numbers`")
    numbers: dict[str, float] = Field(default_factory=dict, description="Computed by code, never by an LLM")


class Briefing(Contract):
    briefing_id: str
    goal_id: str | None = None
    as_of: AwareDatetime
    period: Literal["daily", "weekly"]
    headline: str
    sections: tuple[BriefingSection, ...] = ()


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Job(Contract):
    job_id: str
    kind: str = Field(description="e.g. 'decide_sweep', 'learn_nightly', 'briefing_daily', 'webhook_event'")
    payload: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    run_at: AwareDatetime
    status: JobStatus = JobStatus.QUEUED
    attempts: int = 0
    last_error: str | None = None
