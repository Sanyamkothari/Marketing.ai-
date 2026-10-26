"""Batch approval: generated images wait here, and only an approved one reaches the asset library.

`ImageDesk.submit` checks a `GeneratedImage` with a `VisionChecker`. A failing image is REFUSED with
its reasons and goes no further. A passing one is PENDING and joins the open batch of at most 20.
A person then approves a batch (every image still pending in it) or rejects single images.

Approval is the one way in. It writes an ordinary `Asset` with `status=approved` and `approved_by`
into the `AssetLibrary` (docs/v2/FORMATS.md), so lookups, validation and the audit trail downstream
are untouched. Pending, rejected and refused images are never written to the library at all, not even
as drafts, so no lookup can return them by mistake.

State is in memory (`InMemoryImageQueue`); a database-backed queue is a second class with the same
methods. The clock is passed in.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum

from pydantic import AwareDatetime, Field

from autonomy.assets.check import CheckContext, CheckResult, VisionChecker
from autonomy.assets.generate import GeneratedImage, ImageConfig
from autonomy.assets.library import Asset, AssetKind, AssetLibrary, AssetStatus
from autonomy.catalog.products import Catalogue
from autonomy.contracts.base import Contract

__all__ = [
    "BatchSummary",
    "ImageConflictError",
    "ImageDesk",
    "ImageRecord",
    "ImageStatus",
    "InMemoryImageQueue",
    "UnknownImageError",
    "asset_id_for",
]


class UnknownImageError(LookupError):
    """No image or batch with that id."""


class ImageConflictError(RuntimeError):
    """The image is not in a state the action needs (say, approving one that was rejected)."""


class ImageStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REFUSED = "refused"


class ImageRecord(Contract):
    """One image and where it stands. `batch_id` is set for every image that reached a person."""

    image: GeneratedImage
    status: ImageStatus
    check: CheckResult
    batch_id: str | None = None
    submitted_at: AwareDatetime
    decided_by: str | None = None
    decided_at: AwareDatetime | None = None
    reason: str | None = Field(default=None, description="Why it was rejected, as the reviewer wrote it")
    asset_id: str | None = Field(default=None, description="Set once approved: the library asset")


class BatchSummary(Contract):
    batch_id: str
    size: int
    pending: int
    approved: int
    rejected: int
    closed: bool


def asset_id_for(image_id: str) -> str:
    """The library asset id of a generated image."""
    return f"gen_{image_id}"


class InMemoryImageQueue:
    """Records by image id and the batches they sit in. Not safe to share without `ImageDesk`'s lock."""

    def __init__(self) -> None:
        self.records: dict[str, ImageRecord] = {}
        self.batches: dict[str, list[str]] = {}
        self.closed: set[str] = set()

    def open_batch(self, batch_size: int) -> str:
        """The batch a new pending image joins: the latest one if it is open and not full, else a new one."""
        if self.batches:
            latest = list(self.batches)[-1]
            if latest not in self.closed and len(self.batches[latest]) < batch_size:
                return latest
        batch_id = f"imgb_{len(self.batches) + 1:04d}"
        self.batches[batch_id] = []
        return batch_id


class ImageDesk:
    """Submit, review and approve generated images. The routes in `api/v2/images.py` are thin over this."""

    def __init__(
        self,
        library: AssetLibrary,
        *,
        catalogue: Catalogue,
        checker: VisionChecker,
        config: ImageConfig,
        url_for: Callable[[GeneratedImage], str],
        clock: Callable[[], datetime],
        queue: InMemoryImageQueue | None = None,
    ) -> None:
        self.library = library
        self.config = config
        self._context = CheckContext(catalogue=catalogue, config=config)
        self._checker = checker
        self._url_for = url_for
        self._clock = clock
        self._queue = queue if queue is not None else InMemoryImageQueue()
        self._lock = threading.Lock()

    # --- intake --------------------------------------------------------------------------------

    def submit(self, image: GeneratedImage) -> ImageRecord:
        """Check the image. Refused with reasons, or pending in the open batch. Same image twice: same record."""
        result = self._checker.check(image, self._context)
        now = self._clock()
        with self._lock:
            existing = self._queue.records.get(image.image_id)
            if existing is not None:
                return existing
            if not result.passed:
                record = ImageRecord(image=image, status=ImageStatus.REFUSED, check=result, submitted_at=now)
            else:
                batch_id = self._queue.open_batch(self.config.batch_size)
                self._queue.batches[batch_id].append(image.image_id)
                record = ImageRecord(
                    image=image, status=ImageStatus.PENDING, check=result, batch_id=batch_id, submitted_at=now
                )
            self._queue.records[image.image_id] = record
            return record

    # --- reading -------------------------------------------------------------------------------

    def get(self, image_id: str) -> ImageRecord:
        record = self._queue.records.get(image_id)
        if record is None:
            raise UnknownImageError(f"no image {image_id!r}")
        return record

    def batch(self, batch_id: str) -> list[ImageRecord]:
        """The images of a batch in the order they joined (at most `batch_size`)."""
        ids = self._queue.batches.get(batch_id)
        if ids is None:
            raise UnknownImageError(f"no batch {batch_id!r}")
        return [self._queue.records[i] for i in ids]

    def summary(self, batch_id: str) -> BatchSummary:
        records = self.batch(batch_id)
        count = {s: sum(1 for r in records if r.status is s) for s in ImageStatus}
        return BatchSummary(
            batch_id=batch_id,
            size=len(records),
            pending=count[ImageStatus.PENDING],
            approved=count[ImageStatus.APPROVED],
            rejected=count[ImageStatus.REJECTED],
            closed=batch_id in self._queue.closed,
        )

    def batches(self, *, only_open: bool = False) -> list[BatchSummary]:
        """Every batch, oldest first; with `only_open`, those that still have a pending image."""
        found = [self.summary(b) for b in self._queue.batches]
        return [s for s in found if s.pending > 0] if only_open else found

    def refused(self) -> list[ImageRecord]:
        """Images the check turned away, oldest first, with their reasons."""
        return [r for r in self._queue.records.values() if r.status is ImageStatus.REFUSED]

    # --- deciding ------------------------------------------------------------------------------

    def approve_batch(self, batch_id: str, approved_by: str) -> list[Asset]:
        """Approve every image still pending in the batch; return the assets now in the library.

        Images already rejected stay rejected. Raises `ImageConflictError` when nothing is pending.
        """
        who = _person(approved_by, "approved_by")
        with self._lock:
            records = self.batch(batch_id)
            pending = [r for r in records if r.status is ImageStatus.PENDING]
            if not pending:
                raise ImageConflictError(f"batch {batch_id!r} has no pending image to approve")
            now = self._clock()
            assets: list[Asset] = []
            for record in pending:
                asset = self._asset(record.image, who, now)
                self.library.put(asset)
                assets.append(asset)
                self._queue.records[record.image.image_id] = record.model_copy(
                    update={
                        "status": ImageStatus.APPROVED,
                        "decided_by": who,
                        "decided_at": now,
                        "asset_id": asset.asset_id,
                    }
                )
            self._queue.closed.add(batch_id)
            return assets

    def reject(self, image_id: str, rejected_by: str, reason: str) -> ImageRecord:
        """Reject one pending image. It never enters the library. Raises `ImageConflictError` otherwise."""
        who = _person(rejected_by, "rejected_by")
        with self._lock:
            record = self.get(image_id)
            if record.status is not ImageStatus.PENDING:
                raise ImageConflictError(
                    f"image {image_id!r} is {record.status.value}; only pending images can be rejected"
                )
            rejected = record.model_copy(
                update={
                    "status": ImageStatus.REJECTED,
                    "decided_by": who,
                    "decided_at": self._clock(),
                    "reason": reason.strip() or None,
                }
            )
            self._queue.records[image_id] = rejected
            return rejected

    def _asset(self, image: GeneratedImage, approved_by: str, now: datetime) -> Asset:
        return Asset(
            asset_id=asset_id_for(image.image_id),
            kind=AssetKind.IMAGE,
            url=self._url_for(image),
            alt_text=image.alt_text,
            width=image.width,
            height=image.height,
            item_id=image.item_id,
            category=image.category,
            themes=(image.style.theme,) if image.style.theme is not None else (),
            status=AssetStatus.APPROVED,
            approved_by=approved_by,
            created_at=now,
        )


def _person(name: str, field: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise ValueError(f"{field} must name a person")
    return cleaned
