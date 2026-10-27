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

**Connected shops** (`connectors`, docs/v2/CONNECTORS.md). With `AUTONOMY_CONNECTORS=true` (or a
`ConnectorWiring` passed in), `build_runtime` also builds the connector engine
(`build_connector_engine`) over the same database and ties the runtime to the business
(`business_id`):

* **Sending** goes through the customer's own Klaviyo by default, with our own adapters (SES, ...) as
  the fallback: a `DispatchGateway` over `wiring.send_providers`, paced by Klaviyo's Create Event
  limits (`provider_rates`). `SendRouting.refresh` runs at the start of every tick: a Klaviyo
  connection made, lost or reconnected changes the providers then, with no restart. A hand-off that
  may have reached Klaviyo (a timeout) stays pinned to Klaviyo and is never re-sent through SES.
  Consent: Klaviyo keeps its own unsubscribes and suppressions and the dispatcher checks the
  consent in state; the SQL consent registry applies only when `gateway_enabled` asks for it.
* **Holding on bad data.** `connector_data_gate` asks `HealthMonitor.ready_to_act(business_id)`
  every tick: stale, never-synced or still-loading data, or a connection that needs reconnecting,
  holds that business's decisions, approved batches and queued sends. The connector desk raises the
  one alert per problem. A connection a person disconnected never holds.
* **Identity.** With `identity_linking` (on), Klaviyo profiles are linked to Shopify customer ids
  (`autonomy.connectors.identity`); the runtime reads events and states through that link, and the
  address book hands Klaviyo the linked profile's email.
* **History** that lands from a backfill (`history_hook`) and links that change make the next tick
  rebuild state (`AutonomyRuntime.request_rebuild`).

With `gateway_enabled`, every decision is sent through the `DispatchGateway`
(`autonomy.channels.gateway`) instead of straight through the dispatcher: consent proofs from the
SQL consent registry, do-not-disturb registers read from files (`dnd_registries`, e.g.
`NCPR=/data/ncpr.txt`), and the compliance packs of the business's `countries`. It is off by
default, so a deployment that does not turn it on sends exactly as before. Mandates
(`mandates=`) and a state enricher (`state_enricher=`) are passed by argument. The nightly
negative-lift breaker is on (`negative_lift_breaker`); its sample floors are settings too.

**Plan-v3** (`v3`, `AUTONOMY_V3`, docs/v3/PRESET.md). Off by default. On, the agent is built with the v3
preset (look-alike, who, frequency, moments, journeys, offer pricing and the experiments' wrapper with no
experiment running) and the nightly run fills every state's profile (`RuntimeConfig.nightly_profile`), which
the look-alike model, who and the tiredness model read. The sweep's batches and the bounded tick report stay
as they are.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from autonomy.agents.orchestrator import AdjustFn, AnalystFn
from autonomy.channels.breakers import DEFAULT_GUARDS, Guards, LiftGuards
from autonomy.channels.dispatcher import AddressBook, Dispatcher, ProfileBook
from autonomy.contracts import Brief, Channel, ContentItem, GoalKind
from autonomy.data.folder import StateFolder as ProductionStateFolder
from autonomy.interfaces import (
    ChannelAdapter,
    EventStore,
    JobQueue,
    Policy,
    StateStore,
    TextSignals,
    Workspace,
)
from autonomy.proof.split import group_assigner
from autonomy.runtime.batches import SqlBatchStore, create_batch_tables
from autonomy.runtime.service import (
    AutonomyRuntime,
    BreakersFn,
    DataGate,
    DataHold,
    MandateSource,
    PolicyFactory,
    RuntimeConfig,
    SpendFn,
    StateEnricher,
    TickHook,
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

if TYPE_CHECKING:
    from autonomy.channels.consent import ConsentRegistry, DndRegistry
    from autonomy.channels.gateway import DispatchGateway, Provider
    from autonomy.channels.klaviyo import DecisionDetails
    from autonomy.channels.ratelimit import RateLimiter
    from autonomy.connectors.health import AlertSink, HealthMonitor
    from autonomy.connectors.http import Clock
    from autonomy.connectors.identity import IdentityLinker, IdentityMap, IdentityStore
    from autonomy.connectors.registry import ConnectorRegistry
    from autonomy.connectors.shopify import ProductSink, ShopifyPrivacyDesk
    from autonomy.connectors.sync import ConnectorEngine, TransportFor
    from autonomy.connectors.tokens import SecretCipher
    from autonomy.runtime.v3 import V3Preset

__all__ = [
    "ENV_PREFIX",
    "ConnectorWiring",
    "RuntimeSettings",
    "SendRouting",
    "agent_policy_factory",
    "build_adapters",
    "build_connector_engine",
    "build_gateway",
    "build_runtime",
    "connected_address_book",
    "connector_data_gate",
    "engine_for",
    "webhook_address_book",
    "workspace_policy_factory",
]

ENV_PREFIX = "AUTONOMY_"
_LIST_FIELDS = frozenset({"ses_topic_arns", "countries", "dnd_registries"})


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
    sweep_batch: int = Field(
        default=5_000,
        ge=1,
        description="Customers the daily sweep decides and sends at a time (memory bound)",
    )
    report_decisions: int | None = Field(
        default=1_000,
        ge=0,
        description=(
            "Decisions a built runtime's tick report keeps in full; the rest are counted (and all "
            "are logged). Bounded so a worker's tick does not hold every decision of a large sweep"
        ),
    )
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

    # The send-time gateway (autonomy.channels.gateway). Off: the dispatcher sends directly.
    gateway_enabled: bool = False
    countries: tuple[str, ...] = Field(
        default=(), description="ISO 3166 alpha-2 codes the business sends to; they pick the compliance packs"
    )
    consent_registry: Literal["sql", "none"] = Field(
        default="sql", description="'sql': a promotional send needs a live consent proof on file"
    )
    dnd_registries: tuple[str, ...] = Field(
        default=(),
        description="Do-not-disturb registers as REGISTER=path (or COUNTRY=path), e.g. NCPR=/data/ncpr.txt",
    )
    gateway_purpose: Literal["promotional", "transactional", "service"] = "promotional"

    # The business, and the SaaS connectors it is connected through (docs/v2/CONNECTORS.md).
    business_id: str = Field(
        default="default", min_length=1, description="The business this deployment runs for (one per runtime)"
    )
    connectors: bool = Field(
        default=False,
        description="Build the connector engine: Shopify and Klaviyo data in, Klaviyo sends out",
    )
    public_url: str | None = Field(
        default=None,
        description="https://host the platforms call back (OAuth, webhooks); AUTONOMY_PUBLIC_URL",
    )
    connections_key_ref: str = Field(
        default="MARKETING_AI_CONNECTIONS_KEY", description="The variable holding the token encryption key"
    )
    identity_linking: bool = Field(
        default=True, description="Link Klaviyo profiles to Shopify customers by email and phone"
    )
    hold_on_stale_data: bool = Field(
        default=True,
        description="Hold decisions and sends while connected data is stale or needs reconnecting",
    )

    # The nightly negative-lift breaker (autonomy.channels.breakers.negative_lift).
    negative_lift_breaker: bool = True
    lift_min_group: int = Field(default=30, ge=2, description="Measured customers per group before judging")
    lift_min_closed: int = Field(default=100, ge=1, description="Closed outcome windows before judging")

    # The shop's contribution margin: one setting for the value model, the proof and the 90-day value label.
    margin_rate: float | None = Field(
        default=None,
        gt=0,
        le=1,
        description=(
            "Contribution margin of the shop's orders as a share of their value, after shipping, gateway "
            "fees and returns to origin (AUTONOMY_MARGIN_RATE, 0.35 for 35%). Used for every purchase that "
            "carries no margin of its own, by the agent's value model, the proof's profit and the "
            "90-day value label. Unset: the built-in 0.4 for the value model, full order value for the "
            "proof, and the proof page says the margin is not set"
        ),
    )

    # Look-alike learning (plan-v3 foundation B, docs/v3/LOOKALIKE.md). Off: the agent's click model alone.
    lookalike_model: bool = Field(
        default=False,
        description="Put the look-alike response model on top of the agent's click model",
    )

    # Plan-v3 as a whole (docs/v3/PRESET.md). Off: the plain v2 agent, or whichever single v3 setting is on.
    v3: bool = Field(
        default=False,
        description=(
            "Build the agent with every plan-v3 piece on and fill the nightly profile they read "
            "(AUTONOMY_V3)"
        ),
    )

    @field_validator("countries")
    @classmethod
    def _countries(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        out = tuple(dict.fromkeys(c.strip().upper() for c in value if c.strip()))
        bad = [c for c in out if len(c) != 2 or not c.isalpha()]
        if bad:
            raise ValueError(f"countries are ISO 3166 alpha-2 codes such as IN or US, not {bad}")
        return out

    @field_validator("dnd_registries")
    @classmethod
    def _dnd(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for entry in value:
            key, sep, path = entry.partition("=")
            if not sep or not key.strip() or not path.strip():
                raise ValueError(f"a DND register is REGISTER=path, e.g. NCPR=/data/ncpr.txt, not {entry!r}")
        return value

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

    def lift_guards(self) -> LiftGuards | None:
        """The negative-lift breaker's guards, or None when it is turned off."""
        if not self.negative_lift_breaker:
            return None
        return LiftGuards(min_group=self.lift_min_group, min_closed=self.lift_min_closed)

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
            sweep_batch=self.sweep_batch,
            seed=self.seed,
            nightly_profile=self.v3,  # the profile v3 reads (look-alike, who, tiredness)
        )

    def v3_preset(self) -> V3Preset | None:
        """The agent's v3 preset when `v3` is on (the shop's calendar is its one country, if it has one), else None."""
        if not self.v3:
            return None
        from autonomy.runtime.v3 import V3Preset

        return V3Preset(moments_country=self.countries[0] if len(self.countries) == 1 else None)

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
    lookalike: bool = False,
    v3: V3Preset | bool = False,
    margin_rate: float | None = None,
) -> Policy:
    """The agent's policy (`autonomy.runtime.agent.build_agent_policy`), imported when first needed.

    `kinds` gives each goal's kind (cart recovery fades fast), as the demo passes it too. The agent
    keeps its own default channel costs. The baseline group is decided by the simple rule a careful
    marketer would set up (`autonomy.gym.baselines.RuleBaseline`: e-mail customers who have not bought
    in 60 days, at most every 14 days): without it the baseline group was never contacted, and the
    proof's "agent vs the simple rule" comparison compared the agent with doing nothing."""
    module = importlib.import_module("autonomy.runtime.agent")
    build = module.build_agent_policy
    extra: dict[str, Any] = {"lookalike": True} if lookalike else {}
    if v3:
        extra["v3"] = v3
    if margin_rate is not None:
        extra["margin_rate"] = margin_rate
    if kinds is None:
        built = build(list(briefs), list(content), **extra)
    else:
        built = build(list(briefs), list(content), kinds=dict(kinds), **extra)
    use_baseline = getattr(built, "use_baseline", None)
    if callable(use_baseline):
        use_baseline(importlib.import_module("autonomy.gym.baselines").RuleBaseline())
    return cast(Policy, built)


def workspace_policy_factory(
    workspace: Workspace,
    *,
    lookalike: bool = False,
    v3: V3Preset | bool = False,
    margin_rate: float | None = None,
) -> PolicyFactory:
    """`agent_policy_factory` with each brief's goal kind read from the workspace when it is built."""

    def build(briefs: Sequence[Brief], content: Sequence[ContentItem]) -> Policy:
        wanted = {b.goal_id for b in briefs}
        kinds = {g.goal_id: g.kind for g in workspace.goals() if g.goal_id in wanted}
        return agent_policy_factory(
            briefs, content, kinds=kinds, lookalike=lookalike, v3=v3, margin_rate=margin_rate
        )

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


def build_gateway(
    settings: RuntimeSettings,
    adapters: Mapping[Channel, ChannelAdapter],
    *,
    engine: Engine,
    queue: JobQueue,
    events: EventStore,
    workspace: Workspace,
    address_book: AddressBook,
    profiles: ProfileBook | None = None,
    consent: ConsentRegistry | None = None,
    dnd: DndRegistry | Mapping[str, DndRegistry] | None = None,
    providers: Mapping[Channel, Sequence[Provider]] | None = None,
    limiter: RateLimiter | None = None,
    consent_from_settings: bool = True,
) -> DispatchGateway:
    """The send-time gateway over the configured adapters, with a dispatcher attached.

    One provider per channel (named after the channel, which is what its webhooks are addressed
    by), unless `providers` gives the chain (the connectors' Klaviyo-then-SES). Country rules come
    from the compliance packs of the business's `countries` (a recipient elsewhere gets the
    strictest of those packs); with no countries, every shipped pack is loaded. `consent` and `dnd`
    override the registries the settings describe; `consent_from_settings=False` leaves the SQL
    consent registry out unless `consent` is given. Held decisions go on the runtime's own queue,
    so the one worker claims them."""
    from autonomy.channels.consent import FileDndRegistry, SqlConsentRegistry
    from autonomy.channels.gateway import DispatchGateway, Provider, pack_rules
    from autonomy.compliance_packs import packs_for

    if consent is None and consent_from_settings and settings.consent_registry == "sql":
        consent = SqlConsentRegistry(engine)
    if dnd is None and settings.dnd_registries:
        registers: dict[str, DndRegistry] = {}
        for entry in settings.dnd_registries:
            key, _, path = entry.partition("=")
            registers[key.strip()] = FileDndRegistry(path.strip())
        dnd = registers
    packs = packs_for(settings.countries) if settings.countries else None
    extra: dict[str, Any] = {"default_country": settings.countries[0]} if settings.countries else {}
    chain: Mapping[Channel, Sequence[Provider]] = (
        providers
        if providers is not None
        else {channel: [Provider(channel.value, adapter)] for channel, adapter in adapters.items()}
    )
    gateway = DispatchGateway(
        chain,
        consent=consent,
        dnd=dnd,
        rules=pack_rules(packs=packs),
        limiter=limiter,
        queue=queue,
        events=events,
        decision_purpose=settings.gateway_purpose,
        **extra,
    )
    gateway.attach_dispatcher(address_book, workspace, events, profiles=profiles)
    return gateway


# --- connected shops (docs/v2/CONNECTORS.md) --------------------------------------------------------


@dataclass
class ConnectorWiring:
    """The connector side of a deployment, as `build_runtime` ties it to the runtime.

    `events` is what the runtime reads: the event store seen through the identity links (or the
    plain store when linking is off). `routing` is set by `build_runtime`."""

    engine: ConnectorEngine
    business_id: str
    events: EventStore
    identity_store: IdentityStore | None = None
    identity: IdentityMap | None = None
    linker: IdentityLinker | None = None
    privacy_desk: ShopifyPrivacyDesk | None = None
    routing: SendRouting | None = None


def build_connector_engine(
    settings: RuntimeSettings,
    *,
    db: Engine,
    events: EventStore,
    alerts: AlertSink,
    environ: Mapping[str, str] | None = None,
    clock: Clock | None = None,
    cipher: SecretCipher | None = None,
    transport_for: TransportFor | None = None,
    registry: ConnectorRegistry | None = None,
    product_sink: ProductSink | None = None,
    holder: str = "worker-1",
) -> ConnectorWiring:
    """The connector engine over the deployment's database, wired for this business.

    Tokens are encrypted with the key `connections_key_ref` names (v1's credential-store key);
    our OAuth apps come from `AUTONOMY_<KIND>_CLIENT_ID` / `_CLIENT_SECRET`; alerts go to `alerts`
    (the runtime's alert store), one per problem. With `identity_linking`, Klaviyo profiles are
    linked to Shopify customers as records arrive (`IdentityLinker` as the engine's `record_hook`),
    the Klaviyo connector links engagement through the same view, and Shopify's privacy desk also
    erases the linked profiles' events. `clock`, `cipher`, `transport_for` and `registry` are for
    tests (a fake platform); production leaves them out."""
    from autonomy.connectors import (
        AppCredentials,
        ConnectorEngine,
        ConnectorStore,
        HealthMonitor,
        IncidentDesk,
        SystemClock,
        create_connector_tables,
        production_registry,
    )
    from autonomy.connectors.identity import (
        AliasedEvents,
        IdentityLinker,
        IdentityMap,
        IdentityStore,
        create_identity_tables,
        identity_eraser,
    )
    from autonomy.connectors.shopify import ShopifyPrivacyDesk
    from autonomy.connectors.tokens import FernetCipher, SqlTokenVault

    env = os.environ if environ is None else environ
    create_all(db)
    create_record_tables(db)
    create_connector_tables(db)
    if cipher is None:
        key = settings.secret(settings.connections_key_ref, env)
        if key is None:
            raise ValueError(
                f"connectors are on but {settings.connections_key_ref} is not set: it holds the key "
                "connector tokens are encrypted with"
            )
        cipher = FernetCipher(key.encode())
    business = settings.business_id
    view: EventStore = events
    identity_store: IdentityStore | None = None
    identity: IdentityMap | None = None
    linker: IdentityLinker | None = None
    if settings.identity_linking:
        create_identity_tables(db)
        identity_store = IdentityStore(db)
        identity = IdentityMap(identity_store, business)
        view = AliasedEvents(events, identity)
        linker = IdentityLinker(identity_store, maps=[identity])
    registry = registry or production_registry(view, product_sink=product_sink)
    store = ConnectorStore(db)
    public = (settings.public_url or env.get("AUTONOMY_PUBLIC_URL") or "").strip().rstrip("/") or None
    monitor = HealthMonitor(store, registry, desk=IncidentDesk(store, alerts))
    engine = ConnectorEngine(
        registry=registry,
        store=store,
        vault=SqlTokenVault(store, cipher),
        sink=events.append,
        clock=clock or SystemClock(),
        apps={k: a for k in registry.kinds() if (a := AppCredentials.from_env(k, env)) is not None},
        transport_for=transport_for,
        monitor=monitor,
        webhook_base_url=f"{public}/v2/connectors/webhooks" if public else None,
        holder=holder,
        record_hook=linker,
    )
    erasers = [identity_eraser(identity_store, db, business)] if identity_store is not None else []
    desk = ShopifyPrivacyDesk(store, db, desk=monitor.desk, erasers=erasers)
    return ConnectorWiring(
        engine=engine,
        business_id=business,
        events=view,
        identity_store=identity_store,
        identity=identity,
        linker=linker,
        privacy_desk=desk,
    )


class SendRouting:
    """Which tool sends each channel for the business, re-read at the start of every tick.

    The customer's active Klaviyo connection first (email, and SMS with `sms=on`), our own adapters
    second (`wiring.send_providers`). `refresh` compares the Klaviyo connection in use (id and the
    channels it sends) with the one last seen and rebuilds the providers only when it changed: a
    connection made, lost or reconnected takes effect on the next tick, with no restart. In-flight
    decisions are never sent twice: the gateway keeps the provider an ambiguous attempt went to
    (a hand-off may have arrived) and retries only there, with the same `unique_id`, so a Klaviyo
    connection lost meanwhile makes that one decision fail rather than go out again through SES;
    and the dispatcher never re-sends a decision that has a SEND."""

    def __init__(
        self,
        engine: ConnectorEngine,
        business_id: str,
        *,
        fallback: Mapping[Channel, ChannelAdapter],
        details: DecisionDetails | None = None,
    ) -> None:
        self.engine = engine
        self.business_id = business_id
        self.fallback = dict(fallback)
        self.details = details
        self.gateway: DispatchGateway | None = None
        self.dispatcher: Dispatcher | None = None
        self.seen: tuple[str, ...] | None = None
        self.changes = 0

    def current(self) -> tuple[str, ...]:
        """(Klaviyo connection id, the channels it sends ...), or () when the fallback sends alone."""
        from autonomy.connectors.klaviyo.connector import channels_for
        from autonomy.connectors.wiring import klaviyo_connection

        connection = klaviyo_connection(self.engine, self.business_id)
        if connection is None:
            return ()
        return (connection.connection_id, *sorted(c.value for c in channels_for(connection.config)))

    def providers(self) -> dict[Channel, Sequence[Provider]]:
        from autonomy.connectors.wiring import send_providers

        return send_providers(self.engine, self.business_id, fallback=self.fallback, details=self.details)

    def adapters(self) -> dict[Channel, ChannelAdapter]:
        from autonomy.connectors.wiring import send_adapters

        return send_adapters(self.engine, self.business_id, fallback=self.fallback, details=self.details)

    def bind(self, *, gateway: DispatchGateway | None = None, dispatcher: Dispatcher | None = None) -> None:
        self.gateway = gateway
        self.dispatcher = dispatcher if dispatcher is not None else (gateway.dispatcher if gateway else None)
        self.seen = self.current()

    def refresh(self, now: datetime | None = None) -> bool:  # noqa: ARG002 - a tick hook's shape
        """Rebuild the providers if the business's Klaviyo connection changed; True when it did."""
        seen = self.current()
        if seen == self.seen:
            return False
        self.seen = seen
        if self.gateway is not None:
            self.gateway.providers = {c: tuple(chain) for c, chain in self.providers().items()}
            if self.gateway.dispatcher is not None:
                self.gateway.dispatcher.adapters = self.gateway.channel_adapters()
        elif self.dispatcher is not None:
            self.dispatcher.adapters = self.adapters()
        self.changes += 1
        return True


def connector_data_gate(monitor: HealthMonitor, business_id: str) -> DataGate:
    """The runtime's `data_gate`: hold the business while its connected data cannot be trusted.

    `ready_to_act` says whether to hold and why. For each active connection that holds, the
    monitor's `evaluate` is asked too, so the one alert for the problem exists even if no sync has
    run since it began (the incident desk raises it once; asking again raises nothing)."""
    from autonomy.connectors.base import ConnectionStatus

    def gate(now: datetime) -> DataHold | None:
        verdict = monitor.ready_to_act(business_id, now)
        if verdict.can_act:
            return None
        raised = []
        for health in verdict.connections:
            if health.can_act or health.status is not ConnectionStatus.ACTIVE:
                continue  # a reconnect or uninstall alert was raised when the status changed
            connection = monitor.store.get_connection(health.connection_id)
            if connection is not None:
                raised.extend(monitor.evaluate(connection, now))
        return DataHold(reasons=verdict.reasons, alerts=tuple(raised))

    return gate


def connected_address_book(
    identity: IdentityMap | None, fallback: AddressBook = webhook_address_book
) -> AddressBook:
    """The address book for a shop that sends through Klaviyo.

    With identity linking: the linked Klaviyo profile's email (or phone), see
    `autonomy.connectors.identity.address_book`. Without it: the customer id itself, which the
    Klaviyo hand-off names the profile by (`external_id`). Other channels ask `fallback`."""
    if identity is not None:
        from autonomy.connectors.identity import address_book

        return address_book(identity, fallback)

    def book(customer_id: str, channel: Channel) -> str | None:
        if channel in (Channel.EMAIL, Channel.SMS):
            return customer_id
        return fallback(customer_id, channel)

    return book


def _guards_for(wiring: ConnectorWiring | None) -> Guards:
    """The breakers' minimums. When the shop's Klaviyo may send, a delivery is known only once the
    engagement stream reads it back (hourly), so a send counts toward the delivery rate only after
    Klaviyo's read-back window (the delivery watch's: 6 h email, 18 h SMS). With the default hour,
    one evening batch still in flight tripped the breaker in the end-to-end rehearsal: it paused
    email and dropped Autopilot to Approve, though 145 of the 146 messages were delivered."""
    if wiring is None:
        return DEFAULT_GUARDS
    from autonomy.channels.klaviyo import DEFAULT_WINDOWS

    return replace(DEFAULT_GUARDS, channel_grace=tuple(DEFAULT_WINDOWS.items()))


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
    state_enricher: StateEnricher | None = None,
    mandates: MandateSource | None = None,
    consent: ConsentRegistry | None = None,
    dnd: DndRegistry | Mapping[str, DndRegistry] | None = None,
    connectors: ConnectorWiring | None = None,
    before_tick: Sequence[TickHook] = (),
) -> AutonomyRuntime:
    """The production runtime on SQL stores. Creates the tables it needs (safe to call twice).

    Spend defaults to the runtime's `SpendLedger` (channel cost per send, priced by
    `channel_costs`, plus offer cost per redemption) and planned spend to `EvenPacing`; the
    standard breakers get both. `run_send_jobs=False` leaves queued sends to the worker process
    (`python -m autonomy.runtime.worker`), their one owner; pass None for a single process that
    ticks with no worker.

    `settings.gateway_enabled` sends through `build_gateway` (with `consent` and `dnd` overriding
    the registries from the settings); `mandates` and `state_enricher` go to the runtime as they are.

    `connectors` (or `settings.connectors`, which builds them with `build_connector_engine`) ties the
    runtime to the connected shop: Klaviyo-first sending with `SendRouting` refreshed every tick,
    `connector_data_gate` holding the business on stale data, reads through the identity links, and
    `request_rebuild` as the engine's `history_hook` and the linker's `on_change`. `before_tick`
    hooks run first in every tick."""
    settings = settings or RuntimeSettings.from_env(environ)
    engine = engine or engine_for(settings.database_url)
    create_all(engine)
    create_batch_tables(engine)
    create_record_tables(engine)

    events: EventStore = SqlEventStore(engine)
    states: StateStore = SqlStateStore(engine)
    decisions = SqlDecisionLog(engine)
    workspace = SqlWorkspace(engine)
    queue = SqlJobQueue(engine)
    alert_store = SqlAlertStore(engine)
    wiring = connectors
    if wiring is None and settings.connectors:
        wiring = build_connector_engine(
            settings, db=engine, events=events, alerts=alert_store, environ=environ
        )
    hooks: list[TickHook] = list(before_tick)
    if wiring is not None:
        events = wiring.events
        if wiring.identity is not None:
            from autonomy.connectors.identity import AliasedStates

            states = AliasedStates(states, wiring.identity)
            hooks.append(wiring.identity.refresh)
    chosen = dict(adapters) if adapters is not None else build_adapters(settings, environ)
    book = address_book or (
        connected_address_book(wiring.identity) if wiring is not None else webhook_address_book
    )
    gateway: DispatchGateway | None = None
    if wiring is not None:
        from autonomy.channels.klaviyo import details_from, provider_rates
        from autonomy.channels.ratelimit import RateLimiter

        routing = SendRouting(
            wiring.engine, wiring.business_id, fallback=chosen, details=details_from(decisions, workspace)
        )
        gateway = build_gateway(
            settings,
            chosen,
            engine=engine,
            queue=queue,
            events=events,
            workspace=workspace,
            address_book=book,
            profiles=profiles,
            consent=consent,
            dnd=dnd,
            providers=routing.providers(),
            limiter=RateLimiter(provider_rates=provider_rates()),
            # Klaviyo keeps the shop's own unsubscribes and suppressions, and the dispatcher checks
            # consent in state; a proof registry applies only when the deployment turned it on.
            consent_from_settings=settings.gateway_enabled,
        )
        routing.bind(gateway=gateway)
        wiring.routing = routing
        hooks.insert(0, routing.refresh)
        dispatcher = gateway.dispatcher
    elif settings.gateway_enabled:
        gateway = build_gateway(
            settings,
            chosen,
            engine=engine,
            queue=queue,
            events=events,
            workspace=workspace,
            address_book=book,
            profiles=profiles,
            consent=consent,
            dnd=dnd,
        )
        dispatcher = gateway.dispatcher
    else:
        dispatcher = Dispatcher(chosen, book, workspace, events, queue, profiles=profiles)
    data_gate: DataGate | None = None
    if wiring is not None and settings.hold_on_stale_data:
        data_gate = connector_data_gate(wiring.engine.monitor, wiring.business_id)
    runtime = AutonomyRuntime(
        events=events,
        states=states,
        decisions=decisions,
        workspace=workspace,
        folder=ProductionStateFolder(),
        policy_factory=policy_factory
        or workspace_policy_factory(
            workspace,
            lookalike=settings.lookalike_model,
            v3=settings.v3_preset() or False,
            margin_rate=settings.margin_rate,
        ),
        queue=queue,
        dispatcher=dispatcher,
        batches=SqlBatchStore(engine),
        breakers_fn=breakers_fn
        or standard_breakers(
            baselines=baselines, spend_fn=spend_fn, planned_fn=planned_fn, guards=_guards_for(wiring)
        ),
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
            # v3 scale: the worker never reads a tick's decisions back, so it keeps a bounded few.
            report_decisions=settings.report_decisions,
        ),
        alerts=alert_store,
        briefings=SqlBriefingStore(engine),
        channel_costs=channel_costs,
        state_enricher=state_enricher,
        mandates=mandates,
        gateway=gateway,
        lift_guards=settings.lift_guards(),
        margin_rate=settings.margin_rate,
        business_id=wiring.business_id if wiring is not None else settings.business_id,
        data_gate=data_gate,
        before_tick=hooks,
    )
    if wiring is not None:
        # Connector history older than the late window, and changed identity links, rebuild state.
        wiring.engine.history_hook = runtime.request_rebuild
        if wiring.linker is not None:
            wiring.linker.on_change = runtime.request_rebuild
    return runtime
