"""Production wiring: settings from the environment, and `build_runtime` that puts the pieces together.

`RuntimeSettings` reads `AUTONOMY_*` environment variables (or a mapping passed in, for tests). It
holds *references* to secrets, never the secrets: `whatsapp_access_token_ref` names the environment
variable (or mounted secret) that holds the token, so settings can be logged and shown safely.

`build_runtime(settings)` wires the SQL stores (`autonomy.store`, Postgres or SQLite), the
production `StateFolder` (`autonomy.data.folder`), the batch, alert and briefing stores, the channel
adapters the settings configure, the dispatcher, the circuit breakers and the per-goal policy, and
returns an `AutonomyRuntime`. The business has one proof split (`holdout_share`, `baseline_share`):
customers are assigned with it, and briefs that ask for other shares are refused. Send jobs belong
to the worker process, so the runtime is built with `run_send_jobs=False`. Anything can be
overridden by argument, which is how tests and the demo swap in fakes. Heavy or optional packages
(boto3 for SES, httpx for WhatsApp and webhooks) are imported only when that channel is configured.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from autonomy.agents.orchestrator import AdjustFn, AnalystFn
from autonomy.channels.dispatcher import AddressBook, Dispatcher, ProfileBook
from autonomy.contracts import Brief, Channel, ContentItem, GoalKind
from autonomy.data.folder import StateFolder as ProductionStateFolder
from autonomy.interfaces import ChannelAdapter, Policy, TextSignals, Workspace
from autonomy.proof.split import group_assigner
from autonomy.runtime.batches import SqlBatchStore, create_batch_tables
from autonomy.runtime.service import (
    AutonomyRuntime,
    BreakersFn,
    PolicyFactory,
    RuntimeConfig,
    SpendFn,
    standard_breakers,
)
from autonomy.runtime.stores import SqlAlertStore, SqlBriefingStore, create_record_tables
from autonomy.store import (
    SqlDecisionLog,
    SqlEventStore,
    SqlJobQueue,
    SqlStateStore,
    SqlWorkspace,
    create_all,
    memory_engine,
)

__all__ = [
    "ENV_PREFIX",
    "RuntimeSettings",
    "agent_policy_factory",
    "build_adapters",
    "build_runtime",
    "engine_for",
    "webhook_address_book",
    "workspace_policy_factory",
]

ENV_PREFIX = "AUTONOMY_"
_LIST_FIELDS = frozenset({"ses_topic_arns"})


class RuntimeSettings(BaseModel):
    """Everything the runtime needs to know about its deployment. No secret values live here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    database_url: str = Field(
        default="sqlite:///autonomy.db", description="SQLAlchemy URL; Postgres in production"
    )
    timezone: str = Field(default="UTC", description="The business's time zone: sweep and report hours")
    sweep_hour: int = Field(default=10, ge=0, le=23)
    nightly_hour: int = Field(default=2, ge=0, le=23)
    weekly_weekday: int = Field(default=0, ge=0, le=6, description="0 is Monday")
    weekly_hour: int = Field(default=7, ge=0, le=23)
    tick_every_minutes: int = Field(default=5, ge=1)
    seed: int = 0
    split_salt: str = Field(default="autonomy-split", min_length=1)
    holdout_share: float = Field(
        default=0.10, ge=0, le=0.5, description="The business's one holdout share; briefs must match it"
    )
    baseline_share: float = Field(
        default=0.10, ge=0, le=0.5, description="The business's one baseline share; briefs must match it"
    )
    plan_days: int = Field(default=30, ge=1, description="Days a goal with no end date paces its budget over")
    late_window_hours: float = Field(default=48.0, ge=0)
    trigger_window_hours: float = Field(default=24.0, ge=0)
    batch_max_age_hours: float = Field(default=72.0, gt=0)

    # Email through Amazon SES.
    ses_from_address: str | None = None
    ses_configuration_set: str | None = None
    ses_region: str = "us-east-1"
    ses_unsubscribe_url: str | None = None
    ses_topic_arns: tuple[str, ...] = ()

    # WhatsApp Cloud API. The *_ref fields name the environment variables holding the secrets.
    whatsapp_phone_number_id: str | None = None
    whatsapp_access_token_ref: str = "WHATSAPP_ACCESS_TOKEN"
    whatsapp_app_secret_ref: str = "WHATSAPP_APP_SECRET"
    whatsapp_verify_token_ref: str = "WHATSAPP_VERIFY_TOKEN"
    whatsapp_language: str = "en"

    # The client's own tool: a signed webhook, or else a CSV file they import.
    webhook_tool_url: str | None = None
    webhook_tool_secret_ref: str = "WEBHOOK_TOOL_SECRET"
    csv_export_path: str | None = None

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as err:
            raise ValueError(f"unknown time zone {value!r}") from err
        return value

    @model_validator(mode="after")
    def _shares(self) -> RuntimeSettings:
        if self.holdout_share + self.baseline_share >= 1:
            raise ValueError("holdout + baseline must leave room for the agent")
        return self

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None, *, prefix: str = ENV_PREFIX
    ) -> RuntimeSettings:
        """Settings from `AUTONOMY_<FIELD>` variables; unset ones keep their defaults.

        List fields take comma-separated values. Unknown `AUTONOMY_*` variables are ignored, so a
        deployment can carry variables for other parts of the system under the same prefix.
        """
        env = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        for name in cls.model_fields:
            raw = env.get(prefix + name.upper())
            if raw is None:
                continue
            if name in _LIST_FIELDS:
                values[name] = tuple(part.strip() for part in raw.split(",") if part.strip())
            elif raw.strip() == "" and cls.model_fields[name].default is None:
                values[name] = None
            else:
                values[name] = raw.strip()
        return cls.model_validate(values)

    def config(self) -> RuntimeConfig:
        """The runtime's schedule and windows."""
        return RuntimeConfig(
            sweep_hour=self.sweep_hour,
            nightly_hour=self.nightly_hour,
            weekly_weekday=self.weekly_weekday,
            weekly_hour=self.weekly_hour,
            timezone=self.timezone,
            late_window=timedelta(hours=self.late_window_hours),
            trigger_window=timedelta(hours=self.trigger_window_hours),
            batch_max_age=timedelta(hours=self.batch_max_age_hours),
            plan_days=self.plan_days,
            seed=self.seed,
        )

    @staticmethod
    def secret(ref: str, environ: Mapping[str, str] | None = None) -> str | None:
        """The value a secret reference points at (an environment variable), or None if unset."""
        env = os.environ if environ is None else environ
        value = env.get(ref)
        return value if value else None


def engine_for(url: str) -> Engine:
    """An engine for the URL. An in-memory SQLite URL gets one shared connection, so stores agree."""
    if url in ("sqlite://", "sqlite:///:memory:"):
        return memory_engine()
    return create_engine(url, pool_pre_ping=True)


def webhook_address_book(customer_id: str, channel: Channel) -> str | None:
    """The default address book: the client's tool knows customers by our id; nothing else is known.

    Email addresses and phone numbers come from the client's contact table, which has no v2 contract
    yet; pass `address_book=` to `build_runtime` to send email or WhatsApp.
    """
    return customer_id if channel is Channel.WEBHOOK else None


def agent_policy_factory(
    briefs: Sequence[Brief],
    content: Sequence[ContentItem],
    *,
    kinds: Mapping[str, GoalKind] | None = None,
) -> Policy:
    """The agent's policy (`autonomy.runtime.agent.build_agent_policy`), imported when first needed.

    `kinds` gives each goal's kind (cart recovery fades fast), as the demo passes it too. The agent
    keeps its own default channel costs and baseline policy."""
    module = importlib.import_module("autonomy.runtime.agent")
    build = module.build_agent_policy
    if kinds is None:
        return cast(Policy, build(list(briefs), list(content)))
    return cast(Policy, build(list(briefs), list(content), kinds=dict(kinds)))


def workspace_policy_factory(workspace: Workspace) -> PolicyFactory:
    """`agent_policy_factory` with each brief's goal kind read from the workspace when it is built."""

    def build(briefs: Sequence[Brief], content: Sequence[ContentItem]) -> Policy:
        wanted = {b.goal_id for b in briefs}
        kinds = {g.goal_id: g.kind for g in workspace.goals() if g.goal_id in wanted}
        return agent_policy_factory(briefs, content, kinds=kinds)

    return build


def _required(settings: RuntimeSettings, ref: str, what: str, environ: Mapping[str, str] | None) -> str:
    value = settings.secret(ref, environ)
    if value is None:
        raise ValueError(f"{what} is configured but the secret {ref} is not set")
    return value


def build_adapters(
    settings: RuntimeSettings, environ: Mapping[str, str] | None = None
) -> dict[Channel, ChannelAdapter]:
    """The channel adapters the settings configure. A half-configured channel is an error, not a skip."""
    adapters: dict[Channel, ChannelAdapter] = {}
    if settings.ses_from_address or settings.ses_configuration_set:
        if not (settings.ses_from_address and settings.ses_configuration_set):
            raise ValueError("SES needs both ses_from_address and ses_configuration_set")
        from autonomy.channels.ses import SesEmailAdapter

        # No SNS verifier is wired yet, so SES webhooks are refused until one is (see channels.md).
        adapters[Channel.EMAIL] = SesEmailAdapter.from_boto3(
            settings.ses_from_address,
            settings.ses_configuration_set,
            region_name=settings.ses_region,
            topic_arns=settings.ses_topic_arns,
            unsubscribe_url=settings.ses_unsubscribe_url,
        )
    if settings.whatsapp_phone_number_id:
        from autonomy.channels.whatsapp import WhatsAppCloudAdapter

        adapters[Channel.WHATSAPP] = WhatsAppCloudAdapter(
            settings.whatsapp_phone_number_id,
            _required(settings, settings.whatsapp_access_token_ref, "WhatsApp", environ),
            _required(settings, settings.whatsapp_app_secret_ref, "WhatsApp", environ),
            verify_token=settings.secret(settings.whatsapp_verify_token_ref, environ),
            language=settings.whatsapp_language,
        )
    if settings.webhook_tool_url:
        from autonomy.channels.client_tool import WebhookToolAdapter

        adapters[Channel.WEBHOOK] = WebhookToolAdapter(
            settings.webhook_tool_url,
            _required(settings, settings.webhook_tool_secret_ref, "The webhook tool", environ),
        )
    elif settings.csv_export_path:
        from autonomy.channels.client_tool import CsvExportAdapter

        adapters[Channel.WEBHOOK] = CsvExportAdapter(settings.csv_export_path)
    return adapters


def build_runtime(
    settings: RuntimeSettings | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    engine: Engine | None = None,
    policy_factory: PolicyFactory | None = None,
    adapters: Mapping[Channel, ChannelAdapter] | None = None,
    address_book: AddressBook | None = None,
    profiles: ProfileBook | None = None,
    text_signals: TextSignals | None = None,
    analyst: AnalystFn | None = None,
    strategist: AdjustFn | None = None,
    spend_fn: SpendFn | None = None,
    planned_fn: SpendFn | None = None,
    breakers_fn: BreakersFn | None = None,
    baselines: Mapping[str, float] | None = None,
    channel_costs: Mapping[Channel, float] | None = None,
    run_send_jobs: bool | None = False,
) -> AutonomyRuntime:
    """The production runtime on SQL stores. Creates the tables it needs (safe to call twice).

    Spend defaults to the runtime's `SpendLedger` (channel cost per send, priced by
    `channel_costs`, plus offer cost per redemption) and planned spend to `EvenPacing`; the
    standard breakers get both. `run_send_jobs=False` leaves queued sends to the worker process
    (`python -m autonomy.runtime.worker`), their one owner; pass None for a single process that
    ticks with no worker."""
    settings = settings or RuntimeSettings.from_env(environ)
    engine = engine or engine_for(settings.database_url)
    create_all(engine)
    create_batch_tables(engine)
    create_record_tables(engine)

    events = SqlEventStore(engine)
    states = SqlStateStore(engine)
    decisions = SqlDecisionLog(engine)
    workspace = SqlWorkspace(engine)
    queue = SqlJobQueue(engine)
    chosen = dict(adapters) if adapters is not None else build_adapters(settings, environ)
    dispatcher = Dispatcher(
        chosen,
        address_book or webhook_address_book,
        workspace,
        events,
        queue,
        profiles=profiles,
    )
    return AutonomyRuntime(
        events=events,
        states=states,
        decisions=decisions,
        workspace=workspace,
        folder=ProductionStateFolder(),
        policy_factory=policy_factory or workspace_policy_factory(workspace),
        queue=queue,
        dispatcher=dispatcher,
        batches=SqlBatchStore(engine),
        breakers_fn=breakers_fn
        or standard_breakers(baselines=baselines, spend_fn=spend_fn, planned_fn=planned_fn),
        text_signals=text_signals,
        analyst=analyst,
        strategist=strategist,
        spend_fn=spend_fn,
        planned_fn=planned_fn,
        assign_group=group_assigner(
            settings.split_salt,
            holdout_share=settings.holdout_share,
            baseline_share=settings.baseline_share,
        ),
        # The same shares the customers were split with: a brief asking for others is refused.
        config=replace(
            settings.config(),
            holdout_share=settings.holdout_share,
            baseline_share=settings.baseline_share,
            run_send_jobs=run_send_jobs,
        ),
        alerts=SqlAlertStore(engine),
        briefings=SqlBriefingStore(engine),
        channel_costs=channel_costs,
    )
