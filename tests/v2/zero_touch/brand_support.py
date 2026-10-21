"""Brand-read fakes: `FakeKlaviyo` plus campaigns and message templates, `FakeShopify` plus the shop's
brand settings and product images, and one of the shop's own emails. Used by the brand kit tests and
the end-to-end rehearsal (`scripts/v2_zero_touch_rehearsal.py`)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import httpx

from tests.v2.connectors.klaviyo_support import REVISION, iso, jsonapi_error
from tests.v2.zero_touch.wiring_support import T0, TeardownKlaviyo, TeardownShopify

__all__ = ["GREEN", "LOGO", "BrandKlaviyo", "BrandShopify", "email_html"]

LOGO = "https://cdn.acme.example/acme-logo.png"
GREEN = "#1f6f43"


def email_html(i: int, *, discount: str = "", bang: str = "", emoji: str = "") -> str:
    """One of the shop's own emails: a greeting with Klaviyo's name tag, their words, a sign-off, a footer."""
    return f"""<html><head><title>Acme</title><style>p {{ margin: 0 }}</style></head><body>
<table><tr><td><img src="{LOGO}" alt="Acme Outdoors logo" width="120"></td></tr>
<tr><td style="background-color:{GREEN}">
<p>Hey {{{{ first_name|default:'there' }}}},</p>
<p>The trail is calling{bang}{emoji} We&#39;ve restocked the gear you&#39;ll want for weekend adventures.</p>
<p>{discount or "New jackets just landed for the season."}</p>
<a href="https://acme.example/trail" style="background-color:{GREEN};color:#ffffff">See the trail kit</a>
<p>Cheers,</p><p>The Acme team</p>
<p>No longer want these emails? {{% unsubscribe %}}</p>
<p>&copy; 2026 Acme Outdoors, 1 Trail Road</p></td></tr></table></body></html>"""


class BrandKlaviyo(TeardownKlaviyo):
    """FakeKlaviyo plus Get Campaigns (with included campaign-messages) and a message's template."""

    campaigns: list[dict[str, Any]]
    messages: dict[str, dict[str, Any]]
    message_templates: dict[str, str]
    campaigns_denied: bool

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.campaigns = []
        self.messages = {}
        self.message_templates = {}
        self.campaigns_denied = False

    def add_template(self, tid: str, name: str, html: str) -> None:
        self.templates[tid] = {
            "type": "template",
            "id": tid,
            "attributes": {
                "name": name,
                "editor_type": "CODE",
                "html": html,
                "text": None,
                "updated": iso(T0),
            },
        }

    def add_campaign(
        self,
        cid: str,
        *,
        channel: str = "email",
        subject: str | None = None,
        html: str | None = None,
        body: str | None = None,
        status: str = "Sent",
        days_ago: int = 1,
    ) -> None:
        mid = f"M{cid}"
        content: dict[str, Any] = {"subject": subject, "preview_text": "", "from_label": "Acme"}
        if channel == "sms":
            content = {"body": body}
        self.campaigns.append(
            {
                "type": "campaign",
                "id": cid,
                "attributes": {
                    "name": f"Campaign {cid}",
                    "status": status,
                    "archived": False,
                    "send_time": iso(T0 - timedelta(days=days_ago)),
                    "created_at": iso(T0 - timedelta(days=days_ago + 1)),
                    "channel_hint": channel,
                },
                "relationships": {"campaign-messages": {"data": [{"type": "campaign-message", "id": mid}]}},
            }
        )
        self.messages[mid] = {
            "type": "campaign-message",
            "id": mid,
            "attributes": {"definition": {"channel": channel, "label": cid, "content": content}},
        }
        if html is not None:
            self.message_templates[mid] = html

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/campaigns" or path.startswith("/api/campaign-messages/"):
            self.calls.append(request)
            if request.headers.get("revision") != REVISION:
                return jsonapi_error(400, "invalid_revision", "a revision header is required")
            if self.campaigns_denied:
                return jsonapi_error(
                    403, "permission_denied", "You do not have permission to perform this action."
                )
            if path == "/api/campaigns":
                wanted = "sms" if "'sms'" in (request.url.params.get("filter") or "") else "email"
                rows = [c for c in self.campaigns if c["attributes"]["channel_hint"] == wanted]
                ids = [r["relationships"]["campaign-messages"]["data"][0]["id"] for r in rows]
                return httpx.Response(
                    200,
                    json={
                        "data": rows,
                        "included": [self.messages[i] for i in ids],
                        "links": {"self": str(request.url), "next": None},
                    },
                )
            mid = path.split("/")[3]
            html = self.message_templates.get(mid)
            if html is None:
                return jsonapi_error(404, "not_found", "no template")
            return httpx.Response(
                200,
                json={
                    "data": {"type": "template", "id": f"T{mid}", "attributes": {"html": html, "text": None}}
                },
            )
        return super().handle(request)


class BrandShopify(TeardownShopify):
    """FakeShopify plus the brand reads: shop details, brand settings and product images."""

    brand_denied: bool = False
    products_denied: bool = False
    product_images: list[dict[str, Any]]

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.product_images = []

    def add_image_product(self, pid: int, title: str, ptype: str, *, alt: str = "") -> None:
        self.product_images.append(
            {
                "id": f"gid://shopify/Product/{pid}",
                "title": title,
                "productType": ptype,
                "featuredMedia": {
                    "id": f"gid://shopify/MediaImage/{pid}00",
                    "image": {
                        "url": f"https://cdn.shopify.com/s/files/acme/{pid}.jpg",
                        "altText": alt or None,
                        "width": 1200,
                        "height": 1200,
                    },
                },
            }
        )

    def _op_AutonomyBrandShop(self, variables: Mapping[str, Any], query: str) -> Any:  # noqa: N802
        return {
            "shop": {
                "name": "Acme Outdoors",
                "myshopifyDomain": "acme-goods.myshopify.com",
                "currencyCode": "USD",
                "primaryDomain": {"host": "acme.example"},
            }
        }

    def _op_AutonomyBrandIdentity(self, variables: Mapping[str, Any], query: str) -> Any:  # noqa: N802
        if self.brand_denied:
            return self._answer(
                {},
                errors=[{"message": "Field 'brand' doesn't exist", "extensions": {"code": "undefinedField"}}],
            )
        return {
            "shop": {
                "brand": {
                    "logo": {
                        "image": {
                            "url": "https://cdn.shopify.com/brand/logo.png",
                            "altText": "Acme",
                            "width": 400,
                            "height": 100,
                        }
                    },
                    "squareLogo": None,
                    "colors": {
                        "primary": [{"background": "#1F6F43", "foreground": "#FFFFFF"}],
                        "secondary": [{"background": "#F2B705", "foreground": "#111111"}],
                    },
                }
            }
        }

    def _op_AutonomyBrandProducts(self, variables: Mapping[str, Any], query: str) -> Any:  # noqa: N802
        if self.products_denied:
            return self._answer(
                {}, errors=[{"message": "Throttled", "extensions": {"code": "INTERNAL_SERVER_ERROR"}}]
            )
        return {
            "products": {"nodes": self.product_images, "pageInfo": {"hasNextPage": False, "endCursor": None}}
        }
