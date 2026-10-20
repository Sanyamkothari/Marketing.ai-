"""Learning keys carry the format, so an image send teaches the image option, not the plain message.

Hand-built events and content; no Gym run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from autonomy.contracts import Channel, ContentItem, ContentStatus, Event, EventType, Format, Theme
from autonomy.learning import format_of, logged_sends

NOW = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)


def item(content_id: str, fmt: Format) -> ContentItem:
    return ContentItem(
        content_id=content_id,
        channel=Channel.EMAIL,
        format=fmt,
        theme=Theme.EDUCATION,
        offer_id="none",
        subject="Hello",
        body="Hi {{first_name}}, a few tips.",
        slots=("first_name",),
        asset_url="https://assets.demo.invalid/a.png" if fmt is not Format.TEXT else None,
        status=ContentStatus.APPROVED,
        created_at=NOW,
    )


def send(event_id: str, ts: datetime, **attrs: str) -> Event:
    return Event(
        event_id=event_id,
        customer_id="c1",
        type=EventType.SEND,
        ts=ts,
        source="csv",
        channel=Channel.EMAIL,
        attrs=dict(attrs),
    )


def test_format_of_reads_the_library() -> None:
    library = {"img": item("img", Format.TEXT_IMAGE), "txt": item("txt", Format.TEXT)}
    assert format_of("img", library) == "text_image"
    assert format_of("txt", library) == "text"
    assert format_of("missing", library) is None
    assert format_of(None, library) is None


def test_logged_sends_key_an_image_send_by_its_format() -> None:
    library = {"img": item("img", Format.TEXT_IMAGE), "txt": item("txt", Format.TEXT)}
    history = {
        "c1": [
            send("s1", NOW - timedelta(days=9), content_id="img", offer_id="none"),
            send("s2", NOW - timedelta(days=6), content_id="txt", offer_id="none"),
            send("s3", NOW - timedelta(days=3), offer_id="none", theme="education", format="video"),
            send("s4", NOW - timedelta(days=1), offer_id="none", theme="education"),
        ]
    }
    keys = [s.action_key() for s in logged_sends(history, library)]
    assert keys == [
        "email|none|education|text_image",
        "email|none|education",
        "email|none|education|video",
        "email|none|education",
    ]
