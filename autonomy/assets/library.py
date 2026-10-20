"""The brand's asset library: the images and videos the agent may put in a message.

Format is chosen the safe way first. The agent never generates an image or a video; it picks
from assets a person at the brand has approved. So every asset carries its approval status and
who approved it, and every lookup returns approved assets only unless the caller asks otherwise.

Two implementations share the `AssetLibrary` protocol:

- `InMemoryAssetLibrary`: for the Gym and tests.
- `JsonDirAssetLibrary`: one JSON file per asset in a directory, so a brand's library can live
  next to its config, be reviewed in a pull request and be edited by hand.

Lookups return assets sorted by `asset_id`, so the same library always gives the same answer.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from autonomy.contracts.base import Contract
from autonomy.contracts.content import Theme

_ASSET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")
"""Safe file-name characters only, because the id is also the file name in a JSON directory."""


class AssetKind(StrEnum):
    """What the file is. A carousel card is an image meant to sit in a row of cards."""

    IMAGE = "image"
    VIDEO = "video"
    CAROUSEL_CARD = "carousel_card"


class AssetStatus(StrEnum):
    """Only APPROVED assets are ever sent. RETIRED keeps the record for the audit trail."""

    APPROVED = "approved"
    DRAFT = "draft"
    RETIRED = "retired"


class Asset(Contract):
    """One approved (or waiting, or retired) image or video, labelled so the agent can find it.

    `alt_text` has no default: an image without a description shuts out screen-reader users and
    shows as a blank box when images are blocked. It may still arrive blank from a hand-edited
    file, which is why `autonomy.assets.support.validate` reports blank alt text as a problem.
    """

    asset_id: str = Field(
        pattern=_ASSET_ID.pattern,
        max_length=200,
        description="Letters, digits, '_', '.', '-'; it is also the file name in a JSON directory",
    )
    kind: AssetKind
    url: str = Field(min_length=1, description="Where the channel fetches the file from (https)")
    alt_text: str = Field(description="What the image shows, in plain words")
    width: int | None = Field(default=None, gt=0, description="Pixels")
    height: int | None = Field(default=None, gt=0, description="Pixels")
    duration_s: float | None = Field(default=None, gt=0, description="Video only: length in seconds")
    size_bytes: int | None = Field(default=None, gt=0, description="File size, for channel size limits")
    mime_type: str | None = Field(
        default=None, description="e.g. image/jpeg; guessed from the URL when missing"
    )
    item_id: str | None = Field(default=None, description="The product it shows, if it shows one")
    category: str | None = Field(default=None, description="The product category it fits")
    themes: tuple[Theme, ...] = Field(default=(), description="The content themes it suits")
    group_id: str | None = Field(default=None, description="Carousel cards: the set this card belongs to")
    position: int = Field(default=0, ge=0, description="Carousel cards: the card's place in its set")
    status: AssetStatus = AssetStatus.DRAFT
    approved_by: str | None = Field(default=None, description="Who at the brand approved it")
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _check(self) -> Asset:
        # An approved asset with no approver would break the audit trail the safe-first rule rests on.
        if self.status is AssetStatus.APPROVED and not (self.approved_by or "").strip():
            raise ValueError(f"asset {self.asset_id!r} is approved but has no approved_by")
        if self.duration_s is not None and self.kind is not AssetKind.VIDEO:
            raise ValueError(f"asset {self.asset_id!r}: duration_s is for videos only")
        return self

    @property
    def is_approved(self) -> bool:
        return self.status is AssetStatus.APPROVED

    @property
    def aspect_ratio(self) -> float | None:
        """Width over height, or None when the size is unknown."""
        if self.width is None or self.height is None:
            return None
        return self.width / self.height


@runtime_checkable
class AssetLibrary(Protocol):
    """Where the agent looks up assets. Every read defaults to approved assets only."""

    def put(self, asset: Asset) -> None:
        """Add an asset, or replace the one with the same `asset_id` (to approve or retire it)."""
        ...

    def get(self, asset_id: str) -> Asset | None:
        """One asset by id, whatever its status, so a past decision's asset can still be shown."""
        ...

    def find(
        self,
        *,
        kind: AssetKind | None = None,
        item_id: str | None = None,
        category: str | None = None,
        theme: Theme | None = None,
        group_id: str | None = None,
        approved_only: bool = True,
    ) -> list[Asset]:
        """Assets matching every filter given, sorted by `asset_id`."""
        ...


def matches(
    asset: Asset,
    *,
    kind: AssetKind | None = None,
    item_id: str | None = None,
    category: str | None = None,
    theme: Theme | None = None,
    group_id: str | None = None,
    approved_only: bool = True,
) -> bool:
    """True when the asset passes every filter given. Shared by both libraries so they agree."""
    if approved_only and not asset.is_approved:
        return False
    if kind is not None and asset.kind is not kind:
        return False
    if item_id is not None and asset.item_id != item_id:
        return False
    if category is not None and asset.category != category:
        return False
    if theme is not None and theme not in asset.themes:
        return False
    return group_id is None or asset.group_id == group_id


def _filtered(
    assets: Iterable[Asset],
    *,
    kind: AssetKind | None,
    item_id: str | None,
    category: str | None,
    theme: Theme | None,
    group_id: str | None,
    approved_only: bool,
) -> list[Asset]:
    found = [
        a
        for a in assets
        if matches(
            a,
            kind=kind,
            item_id=item_id,
            category=category,
            theme=theme,
            group_id=group_id,
            approved_only=approved_only,
        )
    ]
    return sorted(found, key=lambda a: a.asset_id)


class InMemoryAssetLibrary:
    """A dict of assets. For the Gym, tests and demos."""

    def __init__(self, assets: Iterable[Asset] = ()) -> None:
        self._assets: dict[str, Asset] = {}
        for asset in assets:
            self.put(asset)

    def put(self, asset: Asset) -> None:
        self._assets[asset.asset_id] = asset

    def get(self, asset_id: str) -> Asset | None:
        return self._assets.get(asset_id)

    def find(
        self,
        *,
        kind: AssetKind | None = None,
        item_id: str | None = None,
        category: str | None = None,
        theme: Theme | None = None,
        group_id: str | None = None,
        approved_only: bool = True,
    ) -> list[Asset]:
        return _filtered(
            self._assets.values(),
            kind=kind,
            item_id=item_id,
            category=category,
            theme=theme,
            group_id=group_id,
            approved_only=approved_only,
        )


class JsonDirAssetLibrary:
    """One `<asset_id>.json` file per asset in a directory.

    The directory is read on every lookup, so a person approving an asset by editing its file
    is seen at once, without a restart. A file that is not valid JSON, or not a valid `Asset`,
    raises with its name: a broken library should fail loudly, not quietly lose assets. Writes
    go to a temporary file first and are then renamed, so a reader never sees half a file.
    """

    SUFFIX = ".json"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, asset_id: str) -> Path:
        return self.root / f"{asset_id}{self.SUFFIX}"

    @staticmethod
    def _load(path: Path) -> Asset:
        try:
            asset = Asset.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValidationError) as err:
            raise ValueError(f"asset file {path.name} is not a valid asset: {err}") from err
        if path.name != f"{asset.asset_id}{JsonDirAssetLibrary.SUFFIX}":
            raise ValueError(
                f"asset file {path.name} holds asset_id {asset.asset_id!r}; the names must match"
            )
        return asset

    def put(self, asset: Asset) -> None:
        text = json.dumps(asset.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-", suffix=self.SUFFIX)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            Path(tmp).replace(self._path(asset.asset_id))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def get(self, asset_id: str) -> Asset | None:
        # An id that could never be valid (say "../x" from an API path) is simply not here; it
        # must never turn into a read outside the library's directory.
        if not _ASSET_ID.fullmatch(asset_id):
            return None
        path = self._path(asset_id)
        if not path.is_file():
            return None
        return self._load(path)

    def all_assets(self) -> list[Asset]:
        """Every asset in the directory, any status, sorted by id. Temporary files are skipped."""
        paths = sorted(p for p in self.root.glob(f"*{self.SUFFIX}") if not p.name.startswith("."))
        return [self._load(p) for p in paths]

    def find(
        self,
        *,
        kind: AssetKind | None = None,
        item_id: str | None = None,
        category: str | None = None,
        theme: Theme | None = None,
        group_id: str | None = None,
        approved_only: bool = True,
    ) -> list[Asset]:
        return _filtered(
            self.all_assets(),
            kind=kind,
            item_id=item_id,
            category=category,
            theme=theme,
            group_id=group_id,
            approved_only=approved_only,
        )


def candidates(
    library: AssetLibrary,
    *,
    kind: AssetKind,
    item_id: str | None = None,
    category: str | None = None,
    theme: Theme | None = None,
) -> list[Asset]:
    """Approved assets of one kind for a message, most specific first.

    A picture of the very product beats a picture of its category, which beats a picture that
    only suits the message's theme. Each asset appears once, at its most specific tier; within a
    tier the order is by `asset_id`. Empty means "no fitting asset": send text instead.

    The broader tiers never offer an asset that contradicts the message: a picture of a
    different product (its `item_id` is set and is not `item_id`), or one filed under a different
    category. A message about one shoe must not show another shoe, even one tagged "savings".
    """

    def fits(asset: Asset) -> bool:
        if item_id is not None and asset.item_id is not None and asset.item_id != item_id:
            return False
        return category is None or asset.category is None or asset.category == category

    tiers: list[list[Asset]] = []
    if item_id is not None:
        tiers.append(library.find(kind=kind, item_id=item_id))
    if category is not None:
        tiers.append([a for a in library.find(kind=kind, category=category) if fits(a)])
    if theme is not None:
        tiers.append([a for a in library.find(kind=kind, theme=theme) if fits(a)])
    seen: set[str] = set()
    ordered: list[Asset] = []
    for tier in tiers:
        for asset in tier:
            if asset.asset_id not in seen:
                seen.add(asset.asset_id)
                ordered.append(asset)
    return ordered


def carousel_set(library: AssetLibrary, group_id: str) -> list[Asset]:
    """The approved cards of one carousel, in card order (`position`, then `asset_id`)."""
    cards = library.find(kind=AssetKind.CAROUSEL_CARD, group_id=group_id)
    return sorted(cards, key=lambda a: (a.position, a.asset_id))
