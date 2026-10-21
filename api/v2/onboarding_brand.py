"""Onboarding, brand voice: read the voice from the shop's own tools, show 3 samples, one approval.

| Route | What it does |
|---|---|
| `GET /v2/onboarding/brand` | The kit as "what we found -> what we will use", the logo, colours and product photos, and 3 sample messages written for the goal we propose first |
| `POST /v2/onboarding/brand/read` | Read Klaviyo templates and campaigns and the Shopify shop, brand settings and products now (best effort, a few seconds), and derive the kit |
| `POST /v2/onboarding/brand/approve` | The one approval: accepts the voice and the samples shown, records who and when, approves the product photos, and writes every running goal's messages in that voice (`write_goal_libraries`). 409 when the kit changed since it was shown |

Every rule traces to the shop's own copy (`autonomy.brand`); with no model configured the samples
come from the Creative's template copy put into the shop's voice. The page answers "does this sound
like us?" in one screen: the samples are the biggest block, and each finding's evidence (how many of
their messages showed it) is there for its description.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import AwareDatetime, Field
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool

from autonomy.assets.library import AssetLibrary
from autonomy.brand import (
    BrandKit,
    BrandKitStore,
    StaleKitError,
    active_connection,
    approve,
    derive_kit,
    read_brand_sources,
    record_assets,
    save_read,
)
from autonomy.brand.samples import SampleMessage, SampleSet, goal_library, sample_messages
from autonomy.contracts import Channel, Contract, Goal, GoalKind, GoalStatus

__all__ = [
    "BRAND_PREFIX",
    "IN_SCHEMA",
    "ApproveVoice",
    "BrandPage",
    "BrandServices",
    "approved_kit",
    "attach_brand_services",
    "get_brand_services",
    "router",
    "write_goal_libraries",
]

BRAND_PREFIX = "/v2/onboarding/brand"
STATE_KEY = "brand_services"
SHOWN_ASSETS = 6

TopGoal = Callable[[Any], Goal | None]


@dataclass
class BrandServices:
    """The kit store, where approved product photos go, and the model (None: template copy)."""

    store: BrandKitStore
    clock: Callable[[], datetime]
    assets: AssetLibrary | None = None
    llm: Any = None
    model_id: str | None = None
    top_goal: TopGoal | None = None
    """The goal the samples are written for, given the v2 `Services`. None: the biggest sized opportunity."""
    _samples: dict[str, SampleSet] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def attach_brand_services(app: Starlette, services: BrandServices) -> None:
    setattr(app.state, STATE_KEY, services)


def get_brand_services(request: Request) -> BrandServices:
    services = getattr(request.app.state, STATE_KEY, None)
    if not isinstance(services, BrandServices):
        raise HTTPException(status_code=503, detail="Reading your brand is not set up on this server.")
    return services


BrandDep = Annotated[BrandServices, Depends(get_brand_services)]


class FindingView(Contract):
    key: str
    label: str
    found: str
    use: str
    hard: bool = Field(description="True: every message is checked against it")
    source: str
    seen: int
    of: int


class AssetView(Contract):
    asset_id: str
    url: str
    alt_text: str
    title: str
    source: str = Field(description="Where it came from, e.g. 'Shopify product gid://shopify/Product/1'")


class GoalView(Contract):
    goal_id: str
    text: str
    kind: GoalKind


class BrandPage(Contract):
    status: Literal["not_read", "waiting", "approved"]
    words: str = Field(description="The one sentence on the card")
    shop_name: str | None = None
    read_at: AwareDatetime | None = None
    sources: list[str] = Field(default_factory=list)
    messages_read: int = 0
    found: list[FindingView] = Field(default_factory=list)
    colours: list[str] = Field(default_factory=list)
    logo_url: str | None = None
    logo_alt: str | None = None
    assets_count: int = 0
    assets: list[AssetView] = Field(default_factory=list)
    goal: GoalView | None = None
    samples: list[SampleMessage] = Field(default_factory=list)
    written_by: Literal["model", "template"] | None = None
    fingerprint: str | None = None
    approved_by: str | None = None
    approved_at: AwareDatetime | None = None
    notes: list[str] = Field(default_factory=list)


class ApproveVoice(Contract):
    fingerprint: str = Field(min_length=1, description="The fingerprint of the kit that was shown")
    approved_by: str | None = Field(default=None, max_length=200)


IN_SCHEMA = True
"""Whether the routes appear in the published OpenAPI schema (`docs/v2/openapi.json`)."""

router = APIRouter(prefix=BRAND_PREFIX, tags=["onboarding"], include_in_schema=IN_SCHEMA)


def _v2_services(request: Request) -> Any:
    from api.v2.deps import SERVICES_STATE_KEY

    return getattr(request.app.state, SERVICES_STATE_KEY, None)


def _connectors(request: Request) -> Any:
    return getattr(request.app.state, "connector_services", None)


def _business_id(request: Request) -> str:
    connectors = _connectors(request)
    if connectors is not None:
        return str(connectors.business_id)
    services = _v2_services(request)
    return str(getattr(services, "business_id", "default"))


def _proposed_goal(request: Request, services: Any, now: datetime, business_id: str) -> Goal | None:
    """The preselected goal proposal of the onboarding desk (`/v2/onboarding/proposals`), when one is attached."""
    desk = getattr(request.app.state, "v2_onboarding", None)
    if desk is None or services is None or not hasattr(desk, "scan"):
        return None
    try:
        found, _history = desk.scan(services, getattr(desk, "margin_rate", None))
        chosen = next((p for p in found.proposals if p.opportunity_id == found.preselected_id), None)
        if chosen is None:
            return None
        return Goal(
            goal_id=str(chosen.opportunity_id),
            business_id=business_id,
            text=str(chosen.goal_text),
            kind=GoalKind(chosen.kind),
            budget=0,
            starts_at=now,
            created_at=now,
        )
    except Exception:  # the proposals read history; the samples must not fail with them
        return None


def _default_top_goal(request: Request, services: Any, now: datetime, business_id: str) -> Goal:
    """The preselected proposal, else the biggest sized opportunity, else the first suggestion, else a
    plain repeat-purchase goal (the samples' framing only; no number comes from it)."""
    proposed = _proposed_goal(request, services, now, business_id)
    if proposed is not None:
        return proposed
    sized = _safe(lambda: services.sized_opportunities()) if services is not None else []
    if sized:
        best: Goal = max(sized, key=lambda o: o.audience_n).goal
        return best
    ideas: list[Goal] = _safe(lambda: services.opportunities()) if services is not None else []
    if ideas:
        return ideas[0]
    return Goal(
        goal_id="sample_repeat_purchase",
        business_id=business_id,
        text="Bring one-time buyers back for a second order",
        kind=GoalKind.REPEAT_PURCHASE,
        budget=0,
        starts_at=now,
        created_at=now,
    )


def _channels(request: Request, business_id: str) -> tuple[Channel, ...]:
    connectors = _connectors(request)
    if connectors is None:
        return (Channel.EMAIL,)
    klaviyo = active_connection(connectors.engine, business_id, "klaviyo")
    if klaviyo is not None and klaviyo.config.get("sms", "off") == "on":
        return (Channel.EMAIL, Channel.SMS)
    return (Channel.EMAIL,)


def _samples(request: Request, brand: BrandServices, kit: BrandKit, goal_id: str | None) -> SampleSet:
    now = brand.clock()
    services = _v2_services(request)
    goal: Goal | None = None
    if goal_id and services is not None:
        known = [*_safe(lambda: services.opportunities()), *_safe(lambda: services.list_goals())]
        goal = next((g for g in known if g.goal_id == goal_id), None)
        if goal is None:
            raise HTTPException(status_code=404, detail=f"No goal {goal_id!r}.")
    if goal is None and brand.top_goal is not None:
        goal = brand.top_goal(services)
    if goal is None:
        goal = _default_top_goal(request, services, now, kit.business_id)
    with brand._lock:
        cached = brand._samples.get(kit.business_id)
    if cached is not None and cached.fingerprint == kit.fingerprint() and cached.goal_id == goal.goal_id:
        return cached
    made = sample_messages(
        kit,
        goal,
        now=now,
        llm=brand.llm,
        model_id=brand.model_id,
        channels=_channels(request, kit.business_id),
    )
    with brand._lock:
        brand._samples[kit.business_id] = made
    return made


def _safe(fn: Callable[[], Any]) -> list[Any]:
    try:
        return list(fn())
    except Exception:
        return []


def _page(kit: BrandKit | None, samples: SampleSet | None) -> BrandPage:
    if kit is None:
        return BrandPage(
            status="not_read",
            words="Read your voice from Klaviyo and Shopify; nothing is sent.",
        )
    approved = kit.approved and kit.approval is not None
    n = len(samples.samples) if samples else 0
    words = (
        f"Approved by {kit.approval.approved_by}; every message is checked against it."
        if approved and kit.approval is not None
        else (
            f"Approve these {n} messages and Autonomy writes like this from now on."
            if n
            else "Approve this voice and Autonomy writes like this from now on."
        )
    )
    notes = [*kit.notes, *(samples.notes if samples else ())]
    return BrandPage(
        status="approved" if approved else "waiting",
        words=words,
        shop_name=kit.shop_name,
        read_at=kit.read_at,
        sources=list(kit.sources),
        messages_read=kit.messages_read,
        found=[
            FindingView(
                key=f.key,
                label=f.label,
                found=f.found,
                use=f.use,
                hard=f.hard,
                source=f.evidence.source,
                seen=f.evidence.seen,
                of=f.evidence.of,
            )
            for f in kit.findings
        ],
        colours=[c.hex for c in kit.colours],
        logo_url=kit.logo.url if kit.logo else None,
        logo_alt=kit.logo.alt_text if kit.logo else None,
        assets_count=len(kit.assets),
        assets=[
            AssetView(
                asset_id=a.asset_id,
                url=a.url,
                alt_text=a.alt_text,
                title=a.title,
                source=f"Shopify product {a.source.record_id}",
            )
            for a in kit.assets[:SHOWN_ASSETS]
        ],
        goal=(
            GoalView(goal_id=samples.goal_id, text=samples.goal_text, kind=samples.goal_kind)
            if samples
            else None
        ),
        samples=list(samples.samples) if samples else [],
        written_by=samples.written_by if samples else None,
        fingerprint=kit.fingerprint(),
        approved_by=kit.approval.approved_by if approved and kit.approval else None,
        approved_at=kit.approval.approved_at if approved and kit.approval else None,
        notes=list(dict.fromkeys(notes)),
    )


def approved_kit(request: Request) -> tuple[BrandServices, BrandKit] | None:
    """The brand services and the business's kit, when the kit is approved."""
    brand = getattr(request.app.state, STATE_KEY, None)
    if not isinstance(brand, BrandServices):
        return None
    kit = brand.store.get(_business_id(request))
    return (brand, kit) if kit is not None and kit.approved else None


def write_goal_libraries(
    app_state: Any, brand: BrandServices, kit: BrandKit, *, goal_ids: tuple[str, ...] | None = None
) -> list[str]:
    """Write the messages of each running goal (or `goal_ids`) that has an approved plan and no approved
    message yet, in the approved kit's voice (`autonomy.brand.samples.goal_library`); returns notes.

    This is what makes the one voice approval enough: the goal's library passes the Critic's rules and
    the kit's under that approval, so nobody reviews messages one by one. A goal that already has
    approved messages keeps them. Best effort: a goal whose library fails gets a note, never an error."""
    from api.v2.deps import SERVICES_STATE_KEY

    services = getattr(app_state, SERVICES_STATE_KEY, None)
    if services is None or kit.approval is None:
        return []
    now = brand.clock()
    assets = brand.assets.find() if brand.assets is not None else []
    notes: list[str] = []
    for goal in services.workspace.goals():
        if goal_ids is not None and goal.goal_id not in goal_ids:
            continue
        if goal_ids is None and goal.status not in (GoalStatus.ACTIVE, GoalStatus.PAUSED):
            continue
        brief = services.latest_brief(goal.goal_id)
        if brief is None or not brief.approved:
            continue
        if services.workspace.content(goal_id=goal.goal_id):
            continue  # approved messages exist already
        try:
            library = goal_library(kit, brief, now=now, assets=assets, llm=brand.llm, model_id=brand.model_id)
        except Exception as exc:  # the voice approval stands; the goal says why it has nothing to send
            notes.append(f"The messages for '{goal.text}' could not be written ({type(exc).__name__}).")
            continue
        services.workspace.put_content(list(library.items))
        notes.extend(library.notes)
    return notes


@router.get("", response_model=BrandPage)
async def brand_page(
    request: Request, brand: BrandDep, goal_id: Annotated[str | None, Query(max_length=200)] = None
) -> BrandPage:
    """The kit and 3 samples for the goal we propose first (or `goal_id`)."""

    def work() -> BrandPage:
        kit = brand.store.get(_business_id(request))
        return _page(kit, _samples(request, brand, kit, goal_id) if kit is not None else None)

    return await run_in_threadpool(work)


@router.post("/read", response_model=BrandPage)
async def read_brand(
    request: Request, brand: BrandDep, goal_id: Annotated[str | None, Query(max_length=200)] = None
) -> BrandPage:
    """Read the voice now from the connected Klaviyo and Shopify, and derive the kit."""
    connectors = _connectors(request)
    if connectors is None:
        raise HTTPException(status_code=503, detail="Connecting platforms is not set up on this server.")
    business_id = _business_id(request)
    engine = connectors.engine
    if (
        active_connection(engine, business_id, "klaviyo") is None
        and active_connection(engine, business_id, "shopify") is None
    ):
        raise HTTPException(status_code=409, detail="Connect Klaviyo or Shopify first.")

    def work() -> BrandPage:
        now = brand.clock()
        sources = read_brand_sources(engine, business_id, now)
        kit = save_read(brand.store, derive_kit(sources, llm=brand.llm, model_id=brand.model_id))
        if brand.assets is not None:
            record_assets(brand.assets, kit)
        return _page(kit, _samples(request, brand, kit, goal_id))

    return await run_in_threadpool(work)


@router.post("/approve", response_model=BrandPage)
async def approve_voice(
    request: Request, brand: BrandDep, body: Annotated[ApproveVoice, Body()]
) -> BrandPage:
    """The one approval of the voice, its rules, the samples shown and the product photos."""

    def work() -> BrandPage:
        business_id = _business_id(request)
        current = brand.store.get(business_id)
        with brand._lock:
            shown = brand._samples.get(business_id)
        sample_ids: tuple[str, ...] = ()
        goal: str | None = None
        if current is not None and shown is not None and shown.fingerprint == body.fingerprint:
            sample_ids = tuple(s.content_id for s in shown.samples)
            goal = shown.goal_id
        try:
            kit = approve(
                brand.store,
                business_id,
                approved_by=body.approved_by or "the owner",
                now=brand.clock(),
                fingerprint=body.fingerprint,
                sample_ids=sample_ids,
                goal_id=goal,
            )
        except StaleKitError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if brand.assets is not None:
            record_assets(brand.assets, kit)
        written = write_goal_libraries(request.app.state, brand, kit)
        page = _page(kit, shown if shown is not None and shown.fingerprint == kit.fingerprint() else None)
        if written:
            page = page.model_copy(update={"notes": list(dict.fromkeys([*page.notes, *written]))})
        return page

    return await run_in_threadpool(work)
