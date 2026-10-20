"""The asset library, the channel x format matrix and asset validation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from autonomy.assets import (
    CAROUSEL_TEMPLATE,
    SUPPORT,
    Asset,
    AssetKind,
    AssetLibrary,
    AssetStatus,
    InMemoryAssetLibrary,
    JsonDirAssetLibrary,
    ProblemCode,
    Severity,
    blocking,
    candidates,
    carousel_set,
    formats_for,
    is_sendable,
    mime_type_of,
    supports,
    validate,
    validate_carousel,
)
from autonomy.contracts import Channel, Format, Theme

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
MB = 1024 * 1024


def make(asset_id: str = "img_1", **over: Any) -> Asset:
    """An approved, well-formed JPEG unless the test says otherwise."""
    fields: dict[str, Any] = {
        "asset_id": asset_id,
        "kind": AssetKind.IMAGE,
        "url": f"https://cdn.example.com/{asset_id}.jpg",
        "alt_text": "A blue running shoe on a white background",
        "width": 1200,
        "height": 628,
        "size_bytes": 300_000,
        "status": AssetStatus.APPROVED,
        "approved_by": "maya@brand.example",
        "created_at": NOW,
    }
    fields.update(over)
    return Asset(**fields)


def video(asset_id: str = "vid_1", **over: Any) -> Asset:
    base: dict[str, Any] = {
        "kind": AssetKind.VIDEO,
        "url": f"https://cdn.example.com/{asset_id}.mp4",
        "width": 1920,
        "height": 1080,
        "duration_s": 20.0,
        "size_bytes": 8 * MB,
    }
    base.update(over)
    return make(asset_id, **base)


def card(asset_id: str, position: int, **over: Any) -> Asset:
    base: dict[str, Any] = {
        "kind": AssetKind.CAROUSEL_CARD,
        "width": 1080,
        "height": 1080,
        "group_id": "summer",
        "position": position,
    }
    base.update(over)
    return make(asset_id, **base)


def codes(problems: list[Any]) -> set[ProblemCode]:
    return {p.code for p in problems}


# --- the support matrix ---------------------------------------------------------------------------


def test_formats_per_channel() -> None:
    assert formats_for(Channel.EMAIL) == (Format.TEXT, Format.TEXT_IMAGE, Format.CAROUSEL)
    assert formats_for(Channel.WHATSAPP) == (Format.TEXT, Format.TEXT_IMAGE, Format.VIDEO)
    assert formats_for(Channel.SMS) == (Format.TEXT,)
    assert formats_for(Channel.PUSH) == (Format.TEXT, Format.TEXT_IMAGE)


def test_whatsapp_carousel_needs_the_template_flag() -> None:
    assert not supports(Channel.WHATSAPP, Format.CAROUSEL)
    assert supports(Channel.WHATSAPP, Format.CAROUSEL, carousel_template=True)
    assert Format.CAROUSEL in formats_for(Channel.WHATSAPP, carousel_template=True)
    assert SUPPORT[(Channel.WHATSAPP, Format.CAROUSEL)].requires_flag == CAROUSEL_TEMPLATE
    # The flag only unlocks WhatsApp carousels; it never adds formats elsewhere.
    assert formats_for(Channel.SMS, carousel_template=True) == (Format.TEXT,)


def test_every_channel_carries_text_and_the_matrix_is_read_only() -> None:
    for channel in Channel:
        assert formats_for(channel)[0] is Format.TEXT
        assert not SUPPORT[(channel, Format.TEXT)].needs_asset
    with pytest.raises(TypeError):
        SUPPORT[(Channel.SMS, Format.VIDEO)] = SUPPORT[(Channel.SMS, Format.TEXT)]  # type: ignore[index]


# --- validation ----------------------------------------------------------------------------------


def test_good_assets_have_no_problems() -> None:
    assert validate(make(), Channel.WHATSAPP) == []
    assert validate(make(), Channel.EMAIL) == []
    assert validate(video(), Channel.WHATSAPP) == []
    assert validate(make(width=1024, height=512), Channel.PUSH) == []


def test_sms_takes_no_image() -> None:
    problems = validate(make(), Channel.SMS)
    assert codes(problems) == {ProblemCode.FORMAT_NOT_SUPPORTED}
    assert not is_sendable(make(), Channel.SMS)


def test_video_is_not_for_email_or_push() -> None:
    assert ProblemCode.FORMAT_NOT_SUPPORTED in codes(validate(video(), Channel.EMAIL))
    assert ProblemCode.FORMAT_NOT_SUPPORTED in codes(validate(video(), Channel.PUSH))


def test_a_text_format_takes_no_asset() -> None:
    assert codes(validate(make(), Channel.EMAIL, Format.TEXT)) == {ProblemCode.NO_ASSET_NEEDED}


def test_wrong_kind_for_the_format() -> None:
    assert ProblemCode.WRONG_KIND in codes(validate(make(), Channel.WHATSAPP, Format.VIDEO))


def test_hard_size_limits_block() -> None:
    big_image = make(size_bytes=6 * MB)
    assert ProblemCode.TOO_LARGE in codes(blocking(validate(big_image, Channel.WHATSAPP)))
    big_video = video(size_bytes=17 * MB)
    assert ProblemCode.TOO_LARGE in codes(blocking(validate(big_video, Channel.WHATSAPP)))
    # Firebase's 1 MB notification image limit.
    push_image = make(width=1024, height=512, size_bytes=2 * MB)
    assert ProblemCode.TOO_LARGE in codes(blocking(validate(push_image, Channel.PUSH)))


def test_email_size_is_only_guidance() -> None:
    problems = validate(make(size_bytes=2 * MB), Channel.EMAIL)
    assert codes(problems) == {ProblemCode.LARGE}
    assert blocking(problems) == []
    assert is_sendable(make(size_bytes=2 * MB), Channel.EMAIL)


def test_unknown_size_is_a_warning() -> None:
    problems = validate(make(size_bytes=None), Channel.WHATSAPP)
    assert codes(problems) == {ProblemCode.SIZE_UNKNOWN}
    assert problems[0].severity is Severity.WARNING


def test_video_duration_guidance() -> None:
    long_one = validate(video(duration_s=90.0), Channel.WHATSAPP)
    assert codes(long_one) == {ProblemCode.LONG}
    assert blocking(long_one) == []
    assert ProblemCode.DURATION_UNKNOWN in codes(validate(video(duration_s=None), Channel.WHATSAPP))


def test_blank_alt_text_blocks() -> None:
    problems = validate(make(alt_text="   "), Channel.EMAIL)
    assert ProblemCode.MISSING_ALT_TEXT in codes(blocking(problems))


def test_only_approved_assets_are_sendable() -> None:
    for status in (AssetStatus.DRAFT, AssetStatus.RETIRED):
        problems = validate(make(status=status, approved_by=None), Channel.EMAIL)
        assert ProblemCode.NOT_APPROVED in codes(blocking(problems))


def test_url_must_be_https() -> None:
    problems = validate(make(url="http://cdn.example.com/a.jpg"), Channel.EMAIL)
    assert ProblemCode.NOT_HTTPS in codes(blocking(problems))


def test_file_type_rules() -> None:
    gif = make(url="https://cdn.example.com/a.gif")
    assert ProblemCode.FILE_TYPE in codes(blocking(validate(gif, Channel.WHATSAPP)))
    assert validate(gif, Channel.EMAIL) == []
    unknown = make(url="https://cdn.example.com/render?id=7")
    assert codes(validate(unknown, Channel.WHATSAPP)) == {ProblemCode.FILE_TYPE_UNKNOWN}
    declared = make(url="https://cdn.example.com/render?id=7", mime_type="image/PNG")
    assert mime_type_of(declared) == "image/png"
    assert validate(declared, Channel.WHATSAPP) == []


def test_aspect_ratio_and_width_guidance() -> None:
    square = make(width=1000, height=1000)
    problems = validate(square, Channel.WHATSAPP)
    assert codes(problems) == {ProblemCode.ASPECT_RATIO}
    assert "1.91:1" in problems[0].message
    assert blocking(problems) == []
    narrow = make(width=300, height=157)
    assert codes(validate(narrow, Channel.EMAIL)) == {ProblemCode.TOO_NARROW}
    assert ProblemCode.DIMENSIONS_UNKNOWN in codes(validate(make(width=None), Channel.PUSH))


def test_whatsapp_carousel() -> None:
    cards = [card("c1", 0), card("c2", 1), card("c3", 2)]
    assert codes(validate_carousel(cards, Channel.WHATSAPP)) == {ProblemCode.NEEDS_FLAG}
    assert validate_carousel(cards, Channel.WHATSAPP, carousel_template=True) == []
    mixed = [*cards[:2], card("c3", 2, width=1910, height=1000)]
    assert ProblemCode.MIXED_ASPECT in codes(
        validate_carousel(mixed, Channel.WHATSAPP, carousel_template=True)
    )
    one = validate_carousel(cards[:1], Channel.WHATSAPP, carousel_template=True)
    assert codes(one) == {ProblemCode.CARD_COUNT}
    eleven = [card(f"c{i}", i) for i in range(11)]
    assert ProblemCode.CARD_COUNT in codes(
        validate_carousel(eleven, Channel.WHATSAPP, carousel_template=True)
    )


def test_email_carousel_is_stacked_images() -> None:
    # Plain images are fine as stacked cards, and email cards may differ in shape.
    cards = [make("a", width=1200, height=600), make("b", width=1200, height=1200)]
    assert validate_carousel(cards, Channel.EMAIL) == []
    assert ProblemCode.FORMAT_NOT_SUPPORTED in codes(validate_carousel(cards, Channel.SMS))


# --- the Asset model ------------------------------------------------------------------------------


def test_asset_rules() -> None:
    with pytest.raises(ValidationError):
        make(approved_by=None)  # approved needs an approver
    with pytest.raises(ValidationError):
        make(duration_s=5.0)  # only videos have a length
    with pytest.raises(ValidationError):
        make("../escape")  # ids are file names in a JSON directory
    with pytest.raises(ValidationError):
        Asset(asset_id="x", kind=AssetKind.IMAGE, url="https://a/x.jpg", created_at=NOW)  # type: ignore[call-arg]
    assert make(width=1200, height=600).aspect_ratio == 2.0
    assert make(width=None).aspect_ratio is None


# --- the libraries --------------------------------------------------------------------------------


def _assets() -> list[Asset]:
    return [
        make("shoe_item", item_id="sku_1", category="shoes", themes=(Theme.QUALITY,)),
        make("shoe_cat", category="shoes", themes=(Theme.SAVINGS,)),
        make("savings_any", themes=(Theme.SAVINGS,)),
        make("draft_shoe", item_id="sku_1", category="shoes", status=AssetStatus.DRAFT, approved_by=None),
        video("shoe_vid", item_id="sku_1", category="shoes"),
        card("card_b", 1),
        card("card_a", 0),
        card("card_c", 2, status=AssetStatus.RETIRED),
    ]


@pytest.fixture(params=["memory", "json"])
def library(request: pytest.FixtureRequest, tmp_path: Path) -> AssetLibrary:
    if request.param == "memory":
        return InMemoryAssetLibrary(_assets())
    lib = JsonDirAssetLibrary(tmp_path / "assets")
    for asset in _assets():
        lib.put(asset)
    return lib


def ids(assets: list[Asset]) -> list[str]:
    return [a.asset_id for a in assets]


def test_library_satisfies_protocol(library: AssetLibrary) -> None:
    assert isinstance(library, AssetLibrary)


def test_find_is_approved_only_by_default(library: AssetLibrary) -> None:
    assert "draft_shoe" not in ids(library.find())
    assert "card_c" not in ids(library.find())
    assert "draft_shoe" in ids(library.find(approved_only=False))
    # get() returns any status, so a past decision's asset can still be shown.
    found = library.get("draft_shoe")
    assert found is not None
    assert found.status is AssetStatus.DRAFT
    assert library.get("missing") is None


def test_find_filters(library: AssetLibrary) -> None:
    assert ids(library.find(item_id="sku_1")) == ["shoe_item", "shoe_vid"]
    assert ids(library.find(item_id="sku_1", kind=AssetKind.IMAGE)) == ["shoe_item"]
    assert ids(library.find(category="shoes", kind=AssetKind.IMAGE)) == ["shoe_cat", "shoe_item"]
    assert ids(library.find(theme=Theme.SAVINGS)) == ["savings_any", "shoe_cat"]
    assert ids(library.find(kind=AssetKind.VIDEO)) == ["shoe_vid"]


def test_candidates_most_specific_first(library: AssetLibrary) -> None:
    found = candidates(library, kind=AssetKind.IMAGE, item_id="sku_1", category="shoes", theme=Theme.SAVINGS)
    assert ids(found) == ["shoe_item", "shoe_cat", "savings_any"]
    assert candidates(library, kind=AssetKind.IMAGE, item_id="nothing") == []


def test_candidates_never_show_another_product_or_category() -> None:
    lib = InMemoryAssetLibrary(
        [
            make("own", item_id="sku_1", category="shoes"),
            make("other_shoe", item_id="sku_2", category="shoes", themes=(Theme.SAVINGS,)),
            make("shoe_banner", category="shoes", themes=(Theme.SAVINGS,)),
            make("bag_banner", category="bags", themes=(Theme.SAVINGS,)),
            make("any_savings", themes=(Theme.SAVINGS,)),
        ]
    )
    found = candidates(lib, kind=AssetKind.IMAGE, item_id="sku_1", category="shoes", theme=Theme.SAVINGS)
    assert ids(found) == ["own", "shoe_banner", "any_savings"]
    # Without a product, any shoe picture suits a shoe message; a bag banner still does not.
    found = candidates(lib, kind=AssetKind.IMAGE, category="shoes", theme=Theme.SAVINGS)
    assert ids(found) == ["other_shoe", "own", "shoe_banner", "any_savings"]


def test_json_dir_get_rejects_unsafe_ids(tmp_path: Path) -> None:
    lib = JsonDirAssetLibrary(tmp_path / "lib")
    (tmp_path / "outside.json").write_text(json.dumps(make("outside").model_dump(mode="json")))
    assert lib.get("../outside") is None
    assert lib.get("") is None


def test_carousel_set_in_card_order(library: AssetLibrary) -> None:
    assert ids(carousel_set(library, "summer")) == ["card_a", "card_b"]


def test_put_replaces_to_approve(library: AssetLibrary) -> None:
    draft = library.get("draft_shoe")
    assert draft is not None
    library.put(draft.model_copy(update={"status": AssetStatus.APPROVED, "approved_by": "maya"}))
    assert "draft_shoe" in ids(library.find(item_id="sku_1"))


def test_json_dir_files_and_errors(tmp_path: Path) -> None:
    lib = JsonDirAssetLibrary(tmp_path)
    lib.put(make("a"))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.json"]
    assert JsonDirAssetLibrary(tmp_path).get("a") == make("a")  # a fresh reader sees the same asset

    (tmp_path / "b.json").write_text(json.dumps(make("not_b").model_dump(mode="json")))
    with pytest.raises(ValueError, match="names must match"):
        lib.find()
    (tmp_path / "b.json").write_text("{not json")
    with pytest.raises(ValueError, match=r"b\.json"):
        lib.find()
