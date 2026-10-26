"""Batch approval: pending images are never usable; approval is the one way into the library."""

from __future__ import annotations

import pytest

from autonomy.assets import AssetKind, AssetStatus, candidates, validate
from autonomy.assets.approval import ImageConflictError, ImageStatus, UnknownImageError, asset_id_for
from autonomy.assets.generate import Flaw
from autonomy.assets.library import InMemoryAssetLibrary
from autonomy.catalog.products import Catalogue, Product
from autonomy.contracts import Channel, Theme
from tests.v3.images.support import CONFIG, NOW, STYLE, desk, make_image


def big_catalogue(n: int) -> Catalogue:
    return Catalogue(
        Product(item_id=f"p{i:02d}", name=f"Item {i}", category="gear", price=100.0 + i) for i in range(n)
    )


def submit_many(n: int, library: InMemoryAssetLibrary) -> tuple[object, list[str]]:
    cat = big_catalogue(n)
    d = desk(library=library, cat=cat)
    ids = [d.submit(make_image(f"p{i:02d}", cat=cat)).image.image_id for i in range(n)]
    return d, ids


def test_a_good_image_waits_as_pending_and_is_not_in_the_library() -> None:
    library = InMemoryAssetLibrary()
    d = desk(library=library)
    record = d.submit(make_image())
    assert record.status is ImageStatus.PENDING
    assert record.batch_id == "imgb_0001"
    assert library.find(approved_only=False) == []
    assert library.find() == []
    assert candidates(library, kind=AssetKind.IMAGE, item_id="shoe-0") == []
    assert library.get(asset_id_for(record.image.image_id)) is None


def test_a_bad_image_is_refused_with_its_reasons_and_never_queued() -> None:
    library = InMemoryAssetLibrary()
    d = desk(library=library)
    record = d.submit(make_image(flaws={"shoe-0": [Flaw.GARBLED_TEXT]}))
    assert record.status is ImageStatus.REFUSED
    assert record.batch_id is None
    assert "text" in record.check.words
    assert d.batches() == []
    assert [r.image.image_id for r in d.refused()] == [record.image.image_id]
    with pytest.raises(ImageConflictError):
        d.reject(record.image.image_id, "ana", "x")
    assert library.find(approved_only=False) == []


def test_submitting_the_same_image_twice_gives_one_record() -> None:
    d = desk()
    first = d.submit(make_image())
    assert d.submit(make_image()) == first
    assert d.summary("imgb_0001").size == 1


def test_batches_hold_at_most_20_images() -> None:
    d, ids = submit_many(45, InMemoryAssetLibrary())
    summaries = d.batches()  # type: ignore[attr-defined]
    assert [s.size for s in summaries] == [20, 20, 5]
    assert all(s.pending == s.size for s in summaries)
    assert len(ids) == 45


def test_a_batch_size_above_20_is_not_allowed() -> None:
    from pydantic import ValidationError

    from autonomy.assets.generate import ImageConfig

    with pytest.raises(ValidationError):
        ImageConfig(batch_size=21)


def test_approve_all_writes_approved_offer_banners_with_approved_by() -> None:
    library = InMemoryAssetLibrary()
    d = desk(library=library)
    d.submit(make_image("shoe-0"))
    d.submit(make_image("shoe-1"))
    assets = d.approve_batch("imgb_0001", "  Asha Rao ")
    assert len(assets) == 2
    for asset in assets:
        assert asset.status is AssetStatus.APPROVED
        assert asset.approved_by == "Asha Rao"
        # Its own kind: the banner states a price and an offer, so it is not a general image.
        assert asset.kind is AssetKind.OFFER_BANNER
        assert asset.alt_text and asset.url.startswith("https://")
        assert (asset.width, asset.height) == (CONFIG.width, CONFIG.height)
        assert asset.mime_type == "image/png" and asset.size_bytes
        assert library.get(asset.asset_id) == asset
    assert candidates(library, kind=AssetKind.IMAGE, item_id="shoe-0") == []
    found = candidates(library, kind=AssetKind.OFFER_BANNER, item_id="shoe-0")
    assert [a.item_id for a in found] == ["shoe-0"]
    # The general validator refuses it for any channel: it goes out only through `approved_for`.
    assert any(p.code.value == "wrong_kind" for p in validate(found[0], Channel.EMAIL))
    summary = d.summary("imgb_0001")
    assert (summary.approved, summary.pending, summary.closed) == (2, 0, True)
    record = d.get(assets[0].asset_id.removeprefix("gen_"))
    assert record.status is ImageStatus.APPROVED
    assert (record.decided_by, record.decided_at, record.asset_id) == ("Asha Rao", NOW, assets[0].asset_id)


def test_the_theme_of_the_style_carries_to_the_asset() -> None:
    library = InMemoryAssetLibrary()
    d = desk(library=library)
    image = make_image().model_copy(update={"style": STYLE.model_copy(update={"theme": Theme.SAVINGS})})
    d.submit(image)
    (asset,) = d.approve_batch("imgb_0001", "ana")
    assert asset.themes == (Theme.SAVINGS,)


def test_rejecting_single_images_keeps_them_out_and_approve_takes_the_rest() -> None:
    library = InMemoryAssetLibrary()
    d = desk(library=library)
    ids = [d.submit(make_image(f"shoe-{i}")).image.image_id for i in range(3)]
    rejected = d.reject(ids[1], "ben", "  background too dark ")
    assert (rejected.status, rejected.decided_by, rejected.reason) == (
        ImageStatus.REJECTED,
        "ben",
        "background too dark",
    )
    assert library.find(approved_only=False) == []
    assets = d.approve_batch("imgb_0001", "ana")
    assert {a.asset_id for a in assets} == {asset_id_for(ids[0]), asset_id_for(ids[2])}
    assert library.get(asset_id_for(ids[1])) is None
    assert d.get(ids[1]).status is ImageStatus.REJECTED
    s = d.summary("imgb_0001")
    assert (s.approved, s.rejected, s.pending) == (2, 1, 0)


def test_nothing_pending_is_ever_usable() -> None:
    library = InMemoryAssetLibrary()
    d, ids = submit_many(25, library)
    d.approve_batch("imgb_0001", "ana")  # type: ignore[attr-defined]
    approved = {a.asset_id for a in library.find(approved_only=False)}
    assert len(approved) == 20
    for image_id in ids[20:]:  # the second batch is still pending
        assert asset_id_for(image_id) not in approved
        assert d.get(image_id).status is ImageStatus.PENDING  # type: ignore[attr-defined]
    # Every asset a lookup can see came through approval with an approver.
    assert all(a.is_approved and a.approved_by == "ana" for a in library.find())
    assert library.find(approved_only=False) == library.find()


def test_conflicts_and_unknowns_are_reported() -> None:
    d = desk()
    record = d.submit(make_image())
    d.approve_batch("imgb_0001", "ana")
    with pytest.raises(ImageConflictError):
        d.approve_batch("imgb_0001", "ana")
    with pytest.raises(ImageConflictError):
        d.reject(record.image.image_id, "ana", "too late")
    with pytest.raises(UnknownImageError):
        d.approve_batch("imgb_9999", "ana")
    with pytest.raises(UnknownImageError):
        d.reject("img_nope", "ana", "x")


def test_a_blank_approver_is_refused_and_changes_nothing() -> None:
    library = InMemoryAssetLibrary()
    d = desk(library=library)
    record = d.submit(make_image())
    with pytest.raises(ValueError):
        d.approve_batch("imgb_0001", "   ")
    with pytest.raises(ValueError):
        d.reject(record.image.image_id, "", "x")
    assert d.get(record.image.image_id).status is ImageStatus.PENDING
    assert library.find(approved_only=False) == []


def test_a_closed_batch_takes_no_new_images() -> None:
    d = desk()
    d.submit(make_image("shoe-0"))
    d.approve_batch("imgb_0001", "ana")
    later = d.submit(make_image("shoe-1"))
    assert later.batch_id == "imgb_0002"
