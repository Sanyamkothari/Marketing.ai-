"""Images over time: a catalogue price change, a regenerate after a rejection, a re-check at approval."""

from __future__ import annotations

from autonomy.assets.approval import ImageStatus, asset_id_for
from autonomy.assets.check import CheckContext, CheckResult, Reason, ReasonCode
from autonomy.assets.generate import Flaw, GeneratedImage
from autonomy.assets.library import AssetStatus, InMemoryAssetLibrary
from autonomy.catalog.products import Catalogue, Product
from tests.v3.images.support import catalogue, desk, make_image


def repriced(price: float) -> Catalogue:
    """The test catalogue with shoe-0 at a new price."""
    products = [p if p.item_id != "shoe-0" else p.model_copy(update={"price": price}) for p in catalogue()]
    return Catalogue(products)


def texts(item_id: str = "shoe-0", cat: Catalogue | None = None) -> list[str]:
    return [layer.text for layer in make_image(item_id, cat=cat).overlay.layers]


def test_a_price_change_names_a_new_image_with_the_new_price() -> None:
    old, new = make_image(), make_image(cat=repriced(999.0))
    assert old.image_id != new.image_id
    assert texts(cat=repriced(999.0)) == ["Runner 0", "20% off", "₹999.00", "₹799.20"]


def test_the_id_follows_the_providers_picture() -> None:
    image = make_image()
    flawed = make_image(flaws={"shoe-0": [Flaw.BROKEN_LOGO]})
    assert image.background != flawed.background
    assert image.image_id != flawed.image_id


def test_a_catalogue_change_retires_the_stale_approved_banner() -> None:
    library = InMemoryAssetLibrary()
    d = desk(library=library)
    image_id = d.submit(make_image()).image.image_id
    d.submit(make_image("shoe-1"))
    d.approve_batch("imgb_0001", "ana")
    changed = d.update_catalogue(repriced(999.0))
    assert [r.image.image_id for r in changed] == [image_id]
    record = d.get(image_id)
    assert record.status is ImageStatus.RETIRED
    assert ReasonCode.OVERLAY_TEXT_MISMATCH in {r.code for r in record.check.reasons}
    stale = library.get(asset_id_for(image_id))
    assert stale is not None and stale.status is AssetStatus.RETIRED
    assert {a.item_id for a in library.find()} == {"shoe-1"}
    assert d.summary("imgb_0001").retired == 1
    # Regenerating the job now makes a new image with the new price, pending approval.
    fresh = d.submit(make_image(cat=repriced(999.0)))
    assert fresh.status is ImageStatus.PENDING and fresh.image.image_id != image_id
    assert "₹799.20" in [layer.text for layer in fresh.image.overlay.layers]


def test_a_catalogue_change_refuses_a_stale_pending_image() -> None:
    d = desk()
    image_id = d.submit(make_image()).image.image_id
    (changed,) = d.update_catalogue(repriced(999.0))
    assert changed.status is ImageStatus.REFUSED and d.get(image_id).status is ImageStatus.REFUSED


class Flipping:
    """Passes everything until `refuse` names an item, then refuses that item (a check that learnt more)."""

    name = "flipping"

    def __init__(self) -> None:
        self.refuse: str | None = None

    def check(self, image: GeneratedImage, context: CheckContext) -> CheckResult:
        if image.item_id != self.refuse:
            return CheckResult(checker=self.name)
        return CheckResult(
            checker=self.name, reasons=(Reason(code=ReasonCode.OVERLAY_TEXT_MISMATCH, message="stale"),)
        )


def test_approval_checks_each_image_again_first() -> None:
    library = InMemoryAssetLibrary()
    checker = Flipping()
    d = desk(library=library, checker=checker)
    stale_id = d.submit(make_image()).image.image_id
    d.submit(make_image("shoe-1"))
    checker.refuse = "shoe-0"  # between submit and approval, the check stops passing shoe-0
    assets = d.approve_batch("imgb_0001", "ana")
    assert [a.item_id for a in assets] == ["shoe-1"]
    assert d.get(stale_id).status is ImageStatus.REFUSED
    assert library.get(asset_id_for(stale_id)) is None
    s = d.summary("imgb_0001")
    assert (s.approved, s.refused, s.pending) == (1, 1, 0)


def test_a_rejected_image_can_be_submitted_again() -> None:
    d = desk()
    image = make_image()
    d.reject(d.submit(image).image.image_id, "ben", "try again")
    again = d.submit(image)
    assert again.status is ImageStatus.PENDING
    assert again.batch_id == "imgb_0001"
    assert d.batch("imgb_0001") == [again]
    assert [r.status for r in d._queue.history] == [ImageStatus.REJECTED]


def test_a_refused_image_is_checked_again_when_submitted_again() -> None:
    cat = Catalogue([Product(item_id="shoe-0", name="Runner 0", category="footwear", price=1299.5)])
    d = desk(cat=Catalogue([Product(item_id="other", name="Other", price=1.0)]))
    image = make_image(cat=cat)
    assert d.submit(image).status is ImageStatus.REFUSED
    d.update_catalogue(cat)
    assert d.submit(image).status is ImageStatus.PENDING
    assert d.refused() == []


def test_a_pending_or_approved_image_submitted_again_keeps_its_record() -> None:
    d = desk()
    first = d.submit(make_image())
    assert d.submit(make_image()) == first
    d.approve_batch("imgb_0001", "ana")
    assert d.submit(make_image()).status is ImageStatus.APPROVED
