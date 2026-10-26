"""`/v2/images`: batch approval of generated images (plan-v3 decision 5; docs/v3/IMAGES.md).

| Route | What it does |
|---|---|
| `GET /v2/images/batches?open=true` | The batches (at most 20 images each); `open=true` keeps those with a pending image |
| `GET /v2/images/batches/{batch_id}` | One batch: its images with the text on them, their checks and their status |
| `POST /v2/images/batches/{batch_id}/approve` | Approve every image still pending in the batch, recording `approved_by` |
| `POST /v2/images/{image_id}/reject` | Reject one pending image, with a reason, recording who rejected it |
| `GET /v2/images/{image_id}/preview.svg` | The banner drawn: the picture with the code-written text on it |
| `GET /v2/images/refused` | Images the vision check turned away, with the reasons |

Approving writes an ordinary approved asset into the asset library; nothing pending is ever in it.
The routes read and act through an `ImageDesk` (`autonomy.assets.approval`) attached to the app with
`attach_image_services`. Without one they answer 503, which is the default: image generation is off.

Who approved or rejected is the server's identity, never a name from the request body: until the API
has real user accounts it is `ImageServices.approver`, which defaults to the same `APPROVER` that
brief approval records (`api/v2/deps.py`). An auth layer replaces it by setting `approver`.

The routes are not in `docs/v2/openapi.json` (`IN_SCHEMA` is false) while the feature is off by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import AwareDatetime, Field
from starlette.applications import Starlette

from api.v2.deps import APPROVER
from autonomy.assets.approval import (
    BatchSummary,
    ImageConflictError,
    ImageDesk,
    ImageRecord,
    ImageStatus,
    UnknownImageError,
)
from autonomy.assets.check import Reason
from autonomy.assets.generate import render_svg
from autonomy.contracts import Contract

__all__ = [
    "IN_SCHEMA",
    "ImageServices",
    "attach_image_services",
    "router",
]

IN_SCHEMA = False
STATE_KEY = "image_services"
PREFIX = "/v2/images"

router = APIRouter(prefix=PREFIX, tags=["images"], include_in_schema=IN_SCHEMA)


@dataclass
class ImageServices:
    """The desk the routes act through, and who they record as deciding (the server's identity)."""

    desk: ImageDesk
    approver: str = APPROVER


def attach_image_services(app: Starlette, services: ImageServices) -> None:
    setattr(app.state, STATE_KEY, services)


def get_image_services(request: Request) -> ImageServices:
    services = getattr(request.app.state, STATE_KEY, None)
    if not isinstance(services, ImageServices):
        raise HTTPException(status_code=503, detail="Generated images are not turned on for this server.")
    return services


ServicesDep = Annotated[ImageServices, Depends(get_image_services)]


class ImageView(Contract):
    image_id: str
    item_id: str
    customer_type: str
    status: ImageStatus
    alt_text: str
    width: int
    height: int
    texts: list[str] = Field(description="Every word and number drawn on the image, by code")
    preview_url: str
    reasons: list[Reason] = Field(default_factory=list, description="Why the check refused it")
    batch_id: str | None = None
    decided_by: str | None = None
    decided_at: AwareDatetime | None = None
    reason: str | None = None
    asset_id: str | None = None


class BatchView(Contract):
    summary: BatchSummary
    images: list[ImageView]


class RejectRequest(Contract):
    reason: str = Field(default="", max_length=1000)


class ApprovedView(Contract):
    summary: BatchSummary
    approved_asset_ids: list[str]
    approved_by: str


def _view(record: ImageRecord) -> ImageView:
    image = record.image
    return ImageView(
        image_id=image.image_id,
        item_id=image.item_id,
        customer_type=image.customer_type,
        status=record.status,
        alt_text=image.alt_text,
        width=image.width,
        height=image.height,
        texts=[layer.text for layer in image.overlay.layers],
        preview_url=f"{PREFIX}/{image.image_id}/preview.svg",
        reasons=list(record.check.reasons),
        batch_id=record.batch_id,
        decided_by=record.decided_by,
        decided_at=record.decided_at,
        reason=record.reason,
        asset_id=record.asset_id,
    )


def _not_found(err: UnknownImageError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(err))


@router.get("/batches")
def list_batches(
    services: ServicesDep, only_open: bool = Query(default=False, alias="open")
) -> list[BatchSummary]:
    return services.desk.batches(only_open=only_open)


@router.get("/batches/{batch_id}")
def get_batch(batch_id: str, services: ServicesDep) -> BatchView:
    try:
        records = services.desk.batch(batch_id)
        summary = services.desk.summary(batch_id)
    except UnknownImageError as err:
        raise _not_found(err) from err
    return BatchView(summary=summary, images=[_view(r) for r in records])


@router.post("/batches/{batch_id}/approve")
def approve_batch(batch_id: str, services: ServicesDep) -> ApprovedView:
    try:
        assets = services.desk.approve_batch(batch_id, services.approver)
        summary = services.desk.summary(batch_id)
    except UnknownImageError as err:
        raise _not_found(err) from err
    except ImageConflictError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return ApprovedView(
        summary=summary,
        approved_asset_ids=[a.asset_id for a in assets],
        approved_by=services.approver.strip(),
    )


@router.post("/{image_id}/reject")
def reject_image(image_id: str, body: RejectRequest, services: ServicesDep) -> ImageView:
    try:
        record = services.desk.reject(image_id, services.approver, body.reason)
    except UnknownImageError as err:
        raise _not_found(err) from err
    except ImageConflictError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return _view(record)


@router.get("/refused")
def list_refused(services: ServicesDep) -> list[ImageView]:
    return [_view(r) for r in services.desk.refused()]


@router.get("/{image_id}/preview.svg")
def preview(image_id: str, services: ServicesDep) -> Response:
    try:
        record = services.desk.get(image_id)
    except UnknownImageError as err:
        raise _not_found(err) from err
    return Response(content=render_svg(record.image), media_type="image/svg+xml")
