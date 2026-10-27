"""`POST /v2/import/orders`: upload a shop's order-history CSV and get the import report back.

| Route | What it does |
|---|---|
| `POST /v2/import/orders?shop=...&dry_run=true` | Read the CSV in the request body, say what it holds and how much of it is new |
| `POST /v2/import/orders?shop=...` | The same, and store the new events and fold them into customer state |

The body is the raw CSV (`Content-Type: text/csv`), the way `POST /v2/webhooks/{channel}` takes its raw body, so
the v2 CI job needs no multipart parser. Optional query fields: `timezone` (IANA zone for times with no offset,
default UTC), `currency`, `date_order` (`day_first` or `month_first`) and `exported_at` (ISO 8601 with offset).
`shop` defaults to the server's business id. The answer is the same `ImportReport` the command line
(`python -m autonomy.data.order_import`) prints: rows read, orders, customers, refunds, skipped rows by reason, and
how many customers are contactable. Re-uploading the same or an overlapping export adds nothing twice
(docs/v4/ORDER_IMPORT.md). 400 for a file that is not a known order list, 413 above `MAX_UPLOAD_BYTES`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from api.v2.deps import Services, get_services
from autonomy.data.order_import import ImportReport, OrderImportError, import_orders

__all__ = ["IN_SCHEMA", "MAX_UPLOAD_BYTES", "router"]

MAX_UPLOAD_BYTES: Final[int] = 64 * 1024 * 1024
"""The largest CSV accepted in one request."""

IN_SCHEMA: Final[bool] = False
"""Kept out of docs/v2/openapi.json (a shared file) until the integrator publishes the schema again."""

router = APIRouter(prefix="/v2/import", tags=["import"], include_in_schema=IN_SCHEMA)

ServicesDep = Annotated[Services, Depends(get_services)]


@router.post("/orders")
async def import_order_history(
    request: Request,
    services: ServicesDep,
    shop: str | None = None,
    dry_run: bool = False,
    timezone: str = "UTC",
    currency: str | None = None,
    date_order: Annotated[Literal["day_first", "month_first"] | None, Query()] = None,
    exported_at: datetime | None = None,
) -> ImportReport:
    """Import the order CSV in the body; `dry_run=true` reports and writes nothing."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="That file is too large to import in one request.")
    body = await request.body()
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="That file is too large to import in one request.")

    def run() -> ImportReport:
        report, _ = import_orders(
            body,
            shop=shop or services.business_id,
            store=services.events,
            ingest=services.ingest,
            dry_run=dry_run,
            timezone=timezone,
            currency=currency,
            date_order=date_order,
            exported_at=exported_at,
        )
        return report

    try:
        return await run_in_threadpool(run)
    except OrderImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
