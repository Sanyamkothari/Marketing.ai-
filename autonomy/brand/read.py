"""Reading the brand from the tools a shop already connected, through the connectors' own HTTP clients.

Nothing new is connected and no setting changes: these are extra reads over a connection the engine
already holds (`ConnectorEngine.context`, so tokens, pacing, retries, `Retry-After`, version pins and
secret-free errors are the connectors' own). Each read is best effort: a refusal is a note on the
kit, never a failed connection (the engine is not told, so a missing scope here never marks a
connection `needs_reconnect`).

Klaviyo (revision 2026-10-15, `autonomy.connectors.klaviyo.api`):

* `GET /api/templates?fields[template]=name,editor_type,html,text,created,updated&sort=-updated`
  (scope `templates:read`, already granted; the `templates` pacing group). Autonomy's own
  `Autonomy ·` template is left out.
* `GET /api/campaigns?filter=equals(messages.channel,'email')&include=campaign-messages&sort=-created_at`
  and the same for `'sms'`, then `GET /api/campaign-messages/{id}/template?fields[template]=html,text`
  for each sent email campaign. Needs `campaigns:read`, which `KLAVIYO_SCOPES` does not ask for yet:
  a 403 becomes a note, and the voice comes from the templates alone. Paced as Klaviyo documents
  the campaigns endpoints (10/s burst, 150/min steady). *Verify:* the campaign-message shape
  (`definition.content.subject` / `.body`, or `content.*` on older revisions, both read), the
  status words ('Sent'), and the relationship path for a message's template.

Shopify (Admin GraphQL 2026-10, `autonomy.connectors.shopify.api`):

* `AutonomyBrandShop`: `shop { name myshopifyDomain currencyCode primaryDomain { host } }`.
* `AutonomyBrandIdentity`: `shop { brand { logo, squareLogo, colors { primary, secondary } } }` (the
  shop's brand settings). *Verify* that the Admin API serves `Shop.brand` (the Storefront API
  does); a refusal is a note and the logo and colours come from the email templates instead.
* `AutonomyBrandProducts`: active products with `featuredMedia { ... on MediaImage { image } }`
  (scope `read_products`, already granted), at most 100, about 152 cost points a page.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from autonomy.brand.derive import BrandSources, ProductImage, ShopFacts
from autonomy.brand.kit import BrandColour, BrandLogo, CopySample, IncompleteRead
from autonomy.brand.text import html_to_text
from autonomy.connectors.base import Connection, ConnectionStatus, ConnectorFailure
from autonomy.contracts import Channel

__all__ = [
    "BRAND_IDENTITY_QUERY",
    "BRAND_PRODUCTS_QUERY",
    "BRAND_SHOP_QUERY",
    "KlaviyoCopy",
    "ShopifyBrand",
    "active_connection",
    "read_brand_sources",
    "read_klaviyo_copy",
    "read_shopify_brand",
]

MAX_TEMPLATES: Final[int] = 30
MAX_EMAIL_CAMPAIGNS: Final[int] = 20
MAX_SMS_CAMPAIGNS: Final[int] = 20
MAX_CAMPAIGN_PAGES: Final[int] = 3
PRODUCT_PAGE: Final[int] = 50
MAX_PRODUCT_PAGES: Final[int] = 2

BRAND_SHOP_QUERY: Final[str] = """
query AutonomyBrandShop {
  shop { name myshopifyDomain currencyCode primaryDomain { host } }
}"""

_IMAGE = "image { url altText width height }"
BRAND_IDENTITY_QUERY: Final[str] = f"""
query AutonomyBrandIdentity {{
  shop {{
    brand {{
      logo {{ {_IMAGE} }}
      squareLogo {{ {_IMAGE} }}
      colors {{ primary {{ background foreground }} secondary {{ background foreground }} }}
    }}
  }}
}}"""

BRAND_PRODUCTS_QUERY: Final[str] = f"""
query AutonomyBrandProducts($first: Int!, $after: String) {{
  products(first: $first, after: $after, query: "status:active", sortKey: UPDATED_AT, reverse: true) {{
    nodes {{
      id
      title
      productType
      featuredMedia {{ ... on MediaImage {{ id {_IMAGE} }} }}
    }}
    pageInfo {{ hasNextPage endCursor }}
  }}
}}"""

_HEX: Final[re.Pattern[str]] = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")


def active_connection(engine: Any, business_id: str, kind: str) -> Connection | None:
    """The business's oldest active connection of `kind`, or None (the platform is not connected)."""
    if engine.registry.find(kind) is None:
        return None
    found = [
        c
        for c in engine.store.connections(business_id=business_id, kind=kind)
        if c.status is ConnectionStatus.ACTIVE
    ]
    found.sort(key=lambda c: (c.created_at, c.connection_id))
    return found[0] if found else None


@dataclass
class KlaviyoCopy:
    samples: list[CopySample] = field(default_factory=list)
    templates: int = 0
    campaigns: int = 0
    sms: int = 0
    complete: bool = True
    """False when any read was refused or failed (a 403 for a missing scope included)."""
    notes: list[str] = field(default_factory=list)


@dataclass
class ShopifyBrand:
    shop: ShopFacts | None = None
    logo: BrandLogo | None = None
    colours: list[BrandColour] = field(default_factory=list)
    products: list[ProductImage] = field(default_factory=list)
    catalogue_read: bool = False
    """True when every product page asked for came back (a refusal part way leaves it False)."""
    shop_failed: bool = False
    """True when not even the shop's details could be read."""
    notes: list[str] = field(default_factory=list)


def _campaign_pacer() -> Any:
    """A pacer for one read: Klaviyo's campaigns limits (10/s, 150/min) on top of the documented groups.

    One read makes at most about 25 campaign calls, well inside a minute's limit; the base client still
    honours a 429's `Retry-After` should several reads overlap."""
    from autonomy.connectors.klaviyo.api import ENDPOINT_LIMITS, EndpointLimit, EndpointPacer

    return EndpointPacer(limits={**ENDPOINT_LIMITS, "campaigns": EndpointLimit(10, 150)})


def read_klaviyo_copy(engine: Any, connection: Connection, now: datetime) -> KlaviyoCopy:
    """The shop's own Klaviyo templates and recent sent campaigns, as `CopySample`s."""
    from autonomy.connectors.klaviyo.api import AUTONOMY_FLOW_PREFIX, parse_klaviyo_time
    from autonomy.connectors.klaviyo.client import KlaviyoClient

    out = KlaviyoCopy()
    try:
        ctx = engine.context(connection, now)
    except ConnectorFailure as exc:
        out.notes.append(f"Klaviyo could not be read ({exc}).")
        out.complete = False
        return out
    connector = engine.registry.get("klaviyo")
    pacer = _campaign_pacer()
    client = KlaviyoClient(ctx, getattr(connector, "pacer", None) or pacer)
    campaigns = KlaviyoClient(ctx, pacer)

    try:
        items = client.collect(
            "templates",
            "templates",
            {"fields[template]": "name,editor_type,html,text,created,updated", "sort": "-updated"},
            max_pages=5,
        )
        for item in items:
            attrs = _mapping(item.get("attributes"))
            name = str(attrs.get("name") or "")
            if name.startswith(AUTONOMY_FLOW_PREFIX):
                continue
            template_html = _text(attrs.get("html"))
            text = html_to_text(template_html, _text(attrs.get("text")))
            if not text:
                continue
            out.samples.append(
                CopySample(
                    source="klaviyo_template",
                    source_id=str(item.get("id") or name),
                    name=name[:200],
                    channel=Channel.EMAIL,
                    text=text,
                    html=template_html,
                    sent_at=parse_klaviyo_time(attrs.get("updated")),
                )
            )
            out.templates += 1
            if out.templates >= MAX_TEMPLATES:
                break
    except ConnectorFailure as exc:
        out.notes.append(f"Klaviyo templates could not be read ({exc}).")
        out.complete = False

    for channel in (Channel.EMAIL, Channel.SMS):
        try:
            found = _sent_campaigns(campaigns, channel, now)
        except ConnectorFailure as exc:
            out.complete = False
            if getattr(exc, "status", None) == 403:
                out.notes.append(
                    f"Klaviyo did not let Autonomy read {channel.value} campaigns (it needs campaigns:read); "
                    "the voice comes from your templates."
                )
            else:
                out.notes.append(f"Klaviyo {channel.value} campaigns could not be read ({exc}).")
            continue
        limit = MAX_EMAIL_CAMPAIGNS if channel is Channel.EMAIL else MAX_SMS_CAMPAIGNS
        for campaign_id, sent_at, message_id, content in found[:limit]:
            subject = _text(content.get("subject"))
            if channel is Channel.SMS:
                body = _text(content.get("body")) or ""
                text = html_to_text(None, body, keep_footer=True)  # an SMS is its opt-out line too
                if text:
                    out.samples.append(
                        CopySample(
                            source="klaviyo_sms",
                            source_id=campaign_id,
                            channel=Channel.SMS,
                            text=text,
                            sent_at=sent_at,
                        )
                    )
                    out.sms += 1
                continue
            html: str | None = None
            plain: str | None = None
            try:
                doc = campaigns.get(
                    f"campaign-messages/{message_id}/template", "campaigns", {"fields[template]": "html,text"}
                )
                data = doc.get("data")
                attrs = _mapping(data.get("attributes")) if isinstance(data, Mapping) else {}
                html, plain = _text(attrs.get("html")), _text(attrs.get("text"))
            except ConnectorFailure:
                out.complete = False  # the subject alone still counts for subject length
            text = html_to_text(html, plain)
            if not text and not subject:
                continue
            out.samples.append(
                CopySample(
                    source="klaviyo_campaign",
                    source_id=campaign_id,
                    channel=Channel.EMAIL,
                    subject=subject,
                    text=text,
                    html=html,
                    sent_at=sent_at,
                )
            )
            out.campaigns += 1
    return out


def _sent_campaigns(
    client: Any, channel: Channel, now: datetime
) -> list[tuple[str, datetime | None, str, Mapping[str, Any]]]:
    """(campaign id, sent at, message id, message content) for sent campaigns, newest first."""
    from autonomy.connectors.http import jsonapi_next
    from autonomy.connectors.klaviyo.api import parse_klaviyo_time

    doc = client.get(
        "campaigns",
        "campaigns",
        {
            "filter": f"equals(messages.channel,'{channel.value}')",
            "include": "campaign-messages",
            "sort": "-created_at",
        },
    )
    rows: list[tuple[str, datetime | None, str, Mapping[str, Any]]] = []
    for _ in range(MAX_CAMPAIGN_PAGES):
        messages = {
            str(i.get("id")): _mapping(i.get("attributes"))
            for i in _list(doc.get("included"))
            if isinstance(i, Mapping) and i.get("type") == "campaign-message"
        }
        for item in _list(doc.get("data")):
            if not isinstance(item, Mapping):
                continue
            attrs = _mapping(item.get("attributes"))
            status = str(attrs.get("status") or "").strip().lower()
            sent_at = parse_klaviyo_time(attrs.get("send_time")) or parse_klaviyo_time(
                attrs.get("scheduled_at")
            )
            if status not in ("sent", "sending") and not (
                not status and sent_at is not None and sent_at <= now
            ):
                continue
            if attrs.get("archived") is True:
                continue
            related = _mapping(_mapping(item.get("relationships")).get("campaign-messages")).get("data")
            for ref in _list(related):
                message_id = str(_mapping(ref).get("id") or "")
                message = messages.get(message_id)
                if not message_id or message is None:
                    continue
                definition = _mapping(message.get("definition")) or message
                content = _mapping(definition.get("content")) or _mapping(message.get("content"))
                rows.append((str(item.get("id")), sent_at, message_id, content))
        nxt = jsonapi_next(doc)
        if nxt is None:
            break
        doc = client.get(nxt, "campaigns")
    rows.sort(key=lambda r: (r[1] is None, -(r[1].timestamp() if r[1] else 0.0), r[0]))
    return rows


def read_shopify_brand(engine: Any, connection: Connection, now: datetime) -> ShopifyBrand:
    """The shop's name and public domain, its brand settings (logo, colours) and its product images."""
    from autonomy.connectors.shopify import gql
    from autonomy.connectors.shopify.normalize import numeric_id

    out = ShopifyBrand()
    try:
        ctx = engine.context(connection, now)
        shop = _mapping(gql.query(ctx, BRAND_SHOP_QUERY).get("shop"))
    except ConnectorFailure as exc:
        out.notes.append(f"Shopify could not be read ({exc}).")
        out.shop_failed = True
        return out
    out.shop = ShopFacts(
        name=_text(shop.get("name")) or connection.label,
        domain=_text(shop.get("myshopifyDomain")) or connection.external_id,
        primary_host=_text(_mapping(shop.get("primaryDomain")).get("host")),
        currency=_text(shop.get("currencyCode")),
        connection_id=connection.connection_id,
    )
    try:
        brand = _mapping(_mapping(gql.query(ctx, BRAND_IDENTITY_QUERY).get("shop")).get("brand"))
        image = _mapping(_mapping(brand.get("logo")).get("image")) or _mapping(
            _mapping(brand.get("squareLogo")).get("image")
        )
        url = _text(image.get("url"))
        if url and url.startswith("https://"):
            out.logo = BrandLogo(
                url=url,
                alt_text=_text(image.get("altText")) or "",
                source="your Shopify brand settings",
                width=_int(image.get("width")),
                height=_int(image.get("height")),
            )
        colours = _mapping(brand.get("colors"))
        roles: Sequence[tuple[str, Any]] = (
            ("primary", colours.get("primary")),
            ("secondary", colours.get("secondary")),
        )
        for role, groups in roles:
            first = _mapping(_list(groups)[0]) if _list(groups) else {}
            value = _hex(first.get("background"))
            if value and all(c.hex != value for c in out.colours):
                out.colours.append(
                    BrandColour(
                        hex=value,
                        role="primary" if role == "primary" else "secondary",
                        source="your Shopify brand settings",
                    )
                )
    except ConnectorFailure:
        out.notes.append(
            "Shopify did not give your brand settings; the logo and colours come from your email templates."
        )
    try:
        after: str | None = None
        for _ in range(MAX_PRODUCT_PAGES):
            page = _mapping(
                gql.query(ctx, BRAND_PRODUCTS_QUERY, {"first": PRODUCT_PAGE, "after": after}).get("products")
            )
            for node in _list(page.get("nodes")):
                node = _mapping(node)
                media = _mapping(node.get("featuredMedia"))
                image = _mapping(media.get("image"))
                url, pid = _text(image.get("url")), numeric_id(node.get("id"))
                if not url or pid is None:
                    continue
                out.products.append(
                    ProductImage(
                        product_id=pid,
                        record_id=str(node.get("id")),
                        title=_text(node.get("title")) or f"Product {pid}",
                        product_type=_text(node.get("productType")),
                        url=url,
                        alt_text=_text(image.get("altText")) or "",
                        width=_int(image.get("width")),
                        height=_int(image.get("height")),
                        media_id=_text(media.get("id")),
                    )
                )
            info = _mapping(page.get("pageInfo"))
            after = _text(info.get("endCursor"))
            if not info.get("hasNextPage") or not after:
                break
        out.catalogue_read = True
    except ConnectorFailure as exc:
        out.notes.append(f"Shopify products could not be read ({exc}); no product photos yet.")
    return out


def read_brand_sources(engine: Any, business_id: str, now: datetime) -> BrandSources:
    """Everything the brand kit is derived from, read from the business's active connections."""
    notes: list[str] = []
    klaviyo = active_connection(engine, business_id, "klaviyo")
    shopify = active_connection(engine, business_id, "shopify")
    copy = KlaviyoCopy()
    if klaviyo is not None:
        copy = read_klaviyo_copy(engine, klaviyo, now)
        notes.extend(copy.notes)
    else:
        notes.append("Klaviyo is not connected, so the voice uses our plain defaults until it is.")
    brand = ShopifyBrand()
    if shopify is not None:
        brand = read_shopify_brand(engine, shopify, now)
        notes.extend(brand.notes)
    else:
        notes.append("Shopify is not connected, so there are no product photos yet.")
    incomplete: list[IncompleteRead] = []
    if klaviyo is not None and not copy.complete:
        incomplete.append("klaviyo")
    if shopify is not None and brand.shop_failed:
        incomplete.append("shopify")
    if shopify is not None and not brand.catalogue_read:
        incomplete.append("shopify_products")
    return BrandSources(
        business_id=business_id,
        read_at=now,
        copy_samples=tuple(copy.samples),
        klaviyo_connection_id=klaviyo.connection_id if klaviyo else None,
        klaviyo_label=klaviyo.label if klaviyo else None,
        templates_read=copy.templates,
        campaigns_read=copy.campaigns,
        sms_read=copy.sms,
        shop=brand.shop,
        shop_logo=brand.logo,
        shop_colours=tuple(brand.colours),
        products=tuple(brand.products),
        catalogue_read=brand.catalogue_read,
        incomplete=tuple(incomplete),
        notes=tuple(notes),
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _hex(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _HEX.match(value.strip())
    if match is None:
        return None
    v = match.group(1).lower()
    return "#" + ("".join(c * 2 for c in v) if len(v) == 3 else v)
