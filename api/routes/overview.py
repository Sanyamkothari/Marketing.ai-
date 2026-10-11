"""`GET /overview`: the Overview page's four parts in one call (Plan M M175, DEC-1605).

What needs attention, the value proven to date, the campaigns that are live and the first-visit checklist, built by
`engine.measurement.overview` **only** from what other routes already read: `GET /campaigns/summary`'s own builder
(every figure traced to an artefact), the campaign store and each listed campaign's registered test plan, the saved
connections and the uploads. Nothing here measures, trains or computes a number of its own, reads a customer row or
writes a file beyond what the summary route itself writes (DEC-1402, with pooling on).

A figure of the summary that cannot be traced is not a reason to hide the campaigns: the answer says
``summary_checked: false`` and leaves the cards and the headline out, where `GET /campaigns/summary` answers 500
`SUMMARY_NOT_TRACEABLE`. The route is Viewer, as the summary and the campaign list are.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Request, Response

from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, RegistryDep, SettingsDep, StorageDep
from api.routes.campaigns import get_campaign_store
from engine.access.roles import Role
from engine.connections.store import ConnectionStore
from engine.measurement.campaign import TEST_PLAN_FILENAME, campaign_key
from engine.measurement.overview import Overview, build_overview
from engine.measurement.plan import TestPlan
from engine.measurement.summary import CampaignSummary, SummaryTraceError, build_summary
from engine.storage import Storage, StorageError
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = ["router"]

router: APIRouter = APIRouter(tags=["overview"])

_LOGGER = get_logger(__name__)
_NO_STORE: Final[str] = "no-store"

register(
    {
        ("GET", "/overview"): RoutePolicy(
            role=Role.VIEWER, action="overview.read", purpose="see the overview"
        ),
    }
)


def _plan(storage: Storage, campaign_id: str) -> TestPlan | None:
    try:
        return storage.read_model(campaign_key(campaign_id, TEST_PLAN_FILENAME), TestPlan)
    except StorageError:
        return None


@router.get(
    "/overview",
    response_model=Overview,
    summary="The Overview page: what needs attention, value proven to date, live campaigns, first-visit checklist",
)
def read_overview(
    request: Request,
    response: Response,
    root: ConfigRootDep,
    storage: StorageDep,
    registry: RegistryDep,
    settings: SettingsDep,
) -> Overview:
    response.headers["Cache-Control"] = _NO_STORE
    store = get_campaign_store(request)
    campaigns = store.list(limit=None)
    summary: CampaignSummary | None
    try:
        summary = build_summary(storage, campaigns, registry=registry, root=root, store=store)
    except SummaryTraceError as exc:
        _LOGGER.error(
            "overview: %d figure(s) of the summary did not trace; cards and headline left out",
            len(exc.failures),
        )
        summary = None
    connected = bool(ConnectionStore(storage, settings).list()) or bool(storage.list_keys("uploads/"))
    return build_overview(
        summary=summary,
        campaigns=campaigns,
        plan_of=lambda campaign_id: _plan(storage, campaign_id),
        connected=connected,
        now=utc_now(),
    )
