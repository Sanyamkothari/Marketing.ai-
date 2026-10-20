"""A fake Shopify for the connector tests, answering in the shapes shopify.dev documents.

`FakeShopify` serves, over `httpx.MockTransport` (no network):

* the OAuth token endpoint on the shop's own host (`/admin/oauth/access_token`): code exchange
  (expiring offline tokens only when the exchange carries `expiring=1`), refresh with rotation, and
  `401 {"error": "invalid_request"}` for a dead refresh token; client id and secret in the form;
* the Admin GraphQL API at `/admin/api/2026-10/graphql.json` (the version is in the path; any other
  is a 404), authenticated with `X-Shopify-Access-Token`, with `extensions.cost.throttleStatus` on
  every answer, `updated_at` search filters, `sortKey: UPDATED_AT` paging with opaque cursors,
  bulk operations (start, status, a JSONL file whose child lines carry `__parentId`), webhook
  subscriptions (`uri`), discount codes (unique per shop: a second create is `TAKEN`) and tags;
* the bulk file on another host (`storage.example`), honouring `Range: bytes=N-`.

Records are kept in GraphQL node shape; tests change them and inject failures (`failures`, a queue
of responses served before normal answers). Every request is recorded.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx

from autonomy.connectors import AppCredentials, Connection
from autonomy.connectors.shopify import ShopifyConnector
from autonomy.connectors.shopify.api import VERSION
from autonomy.connectors.webhooks import sign
from autonomy.contracts import Job

from .base_support import Rig, iso, make_rig, parse_ts

SHOP = "acme.myshopify.com"
STORAGE = "storage.example"
CLIENT_ID = "shp_client_1"
CLIENT_SECRET = "shp_secret_1"
APP = AppCredentials(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
GQL_PATH = f"/admin/api/{VERSION}/graphql.json"
CALLBACK = "https://autonomy.example/v2/connectors/oauth/shopify/callback"

_FILTER = re.compile(r"updated_at:(>=|<)'([^']+)'")
_BULK_ROOT = re.compile(r"\{\s*(\w+)\(query:\s*(\"(?:[^\"\\]|\\.)*\")\)")


def gid(kind: str, value: str | int) -> str:
    return f"gid://shopify/{kind}/{value}"


def money(amount: float, currency: str = "USD") -> dict[str, Any]:
    return {"shopMoney": {"amount": f"{amount:.2f}", "currencyCode": currency}}


def customer_node(
    cid: int,
    *,
    created: datetime,
    updated: datetime | None = None,
    email_state: str | None = "SUBSCRIBED",
    email_at: datetime | None = None,
    sms_state: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": gid("Customer", cid),
        "createdAt": iso(created),
        "updatedAt": iso(updated or created),
        "state": "ENABLED",
        "numberOfOrders": "2",
        "amountSpent": {"amount": "40.00", "currencyCode": "USD"},
        "defaultEmailAddress": (
            {
                "marketingState": email_state,
                "marketingOptInLevel": "SINGLE_OPT_IN",
                "marketingUpdatedAt": iso(email_at) if email_at else None,
            }
            if email_state
            else None
        ),
        "defaultPhoneNumber": (
            {
                "marketingState": sms_state,
                "marketingOptInLevel": "SINGLE_OPT_IN",
                "marketingUpdatedAt": None,
                "marketingCollectedFrom": "SHOPIFY",
            }
            if sms_state
            else None
        ),
    }
    node.update(extra)
    return node


def order_node(
    oid: int,
    *,
    customer: int | None,
    total: float,
    created: datetime,
    updated: datetime | None = None,
    lines: list[tuple[int, str, int, float]] = (),  # type: ignore[assignment]
    codes: tuple[str, ...] = (),
    refunds: list[tuple[int, datetime, float]] = (),  # type: ignore[assignment]
    test: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """`lines`: (product id, product type, quantity, unit price); `refunds`: (id, when, amount)."""
    node: dict[str, Any] = {
        "id": gid("Order", oid),
        "name": f"#{oid}",
        "createdAt": iso(created),
        "updatedAt": iso(updated or created),
        "processedAt": iso(created),
        "cancelledAt": None,
        "test": test,
        "displayFinancialStatus": "PAID",
        "currencyCode": "USD",
        "discountCodes": list(codes),
        "customer": {"id": gid("Customer", customer)} if customer is not None else None,
        "totalPriceSet": money(total),
        "subtotalPriceSet": money(total),
        "totalDiscountsSet": money(0),
        "refunds": [
            {"id": gid("Refund", rid), "createdAt": iso(when), "totalRefundedSet": money(amount)}
            for rid, when, amount in refunds
        ],
        "lineItems": {
            "nodes": [
                {
                    "id": gid("LineItem", oid * 100 + i),
                    "quantity": qty,
                    "sku": f"SKU-{pid}",
                    "title": f"Product {pid}",
                    "originalUnitPriceSet": money(price),
                    "discountedTotalSet": money(price * qty),
                    "variant": {"id": gid("ProductVariant", pid * 10)},
                    "product": {"id": gid("Product", pid), "productType": ptype},
                }
                for i, (pid, ptype, qty, price) in enumerate(lines)
            ],
            "pageInfo": {"hasNextPage": False},
        },
    }
    node.update(extra)
    return node


def product_node(pid: int, *, title: str, ptype: str, price: float, updated: datetime) -> dict[str, Any]:
    return {
        "id": gid("Product", pid),
        "title": title,
        "productType": ptype,
        "vendor": "Acme",
        "status": "ACTIVE",
        "createdAt": iso(updated - timedelta(days=30)),
        "updatedAt": iso(updated),
        "priceRangeV2": {"minVariantPrice": {"amount": f"{price:.2f}", "currencyCode": "USD"}},
    }


@dataclass
class BulkOp:
    op_id: str
    resource: str
    search: str
    created_at: datetime
    status: str = "RUNNING"
    error_code: str | None = None
    polls_left: int = 1


@dataclass
class FakeShopify:
    customers: dict[str, dict[str, Any]] = field(default_factory=dict)
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    products: dict[str, dict[str, Any]] = field(default_factory=dict)
    access_token: str = "shpat_live_1"
    refresh_token: str = "shprt_1"
    token_seq: int = 1
    revoked: bool = False
    app_api_key: str = CLIENT_ID
    page_size: int | None = None
    """Override every connection's `first` (to force paging with few records)."""
    available: float = 2000.0
    restore_rate: float = 100.0
    query_cost: float = 50.0
    failures: list[httpx.Response] = field(default_factory=list)
    bulk_ops: dict[str, BulkOp] = field(default_factory=dict)
    bulk_files: dict[str, bytes] = field(default_factory=dict)
    bulk_fail_next: str | None = None
    """Error code for the next `bulk_fail_count` bulk operations (they end FAILED)."""
    bulk_fail_count: int = 1
    bulk_busy: int = 0
    """How many starts to refuse with LIMIT_REACHED."""
    gone_downloads: int = 0
    """How many downloads to answer 403 (an expired signed link)."""
    denied_resources: set[str] = field(default_factory=set)
    subscriptions: dict[str, dict[str, str]] = field(default_factory=dict)
    discounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    tags: dict[str, set[str]] = field(default_factory=dict)
    lose_answer_to: set[str] = field(default_factory=set)
    """Operation names whose effect happens but whose answer is lost (a read timeout), once each."""
    calls: list[httpx.Request] = field(default_factory=list)
    operations: list[str] = field(default_factory=list)
    token_calls: list[dict[str, str]] = field(default_factory=list)
    ranges: list[str | None] = field(default_factory=list)
    clock_now: datetime = T0
    seq: int = 0

    # -- fixtures ---------------------------------------------------------------------------------

    def add_customer(self, cid: int, **kw: Any) -> dict[str, Any]:
        node = customer_node(cid, **kw)
        self.customers[str(cid)] = node
        return node

    def add_order(self, oid: int, **kw: Any) -> dict[str, Any]:
        node = order_node(oid, **kw)
        self.orders[str(oid)] = node
        return node

    def add_product(self, pid: int, **kw: Any) -> dict[str, Any]:
        node = product_node(pid, **kw)
        self.products[str(pid)] = node
        return node

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- routing ----------------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.host == STORAGE:
            return self._download(request)
        if request.url.host != SHOP:
            return httpx.Response(404)
        if request.url.path == "/admin/oauth/access_token":
            return self._token(request)
        if self.failures:
            return self.failures.pop(0)
        if request.url.path != GQL_PATH:
            return httpx.Response(404, json={"errors": "Not Found"})
        if self.revoked or request.headers.get("x-shopify-access-token") != self.access_token:
            return httpx.Response(401, json={"errors": "[API] Invalid API key or access token"})
        body = json.loads(request.content)
        query = str(body.get("query") or "")
        variables = body.get("variables") or {}
        name_match = re.search(r"(?:query|mutation)\s+(\w+)", query)
        name = name_match.group(1) if name_match else "anonymous"
        self.operations.append(name)
        handler = getattr(self, f"_op_{name}", None)
        if handler is None:
            return self._answer({}, errors=[{"message": "unknown", "extensions": {"code": "BAD_REQUEST"}}])
        result = handler(variables, query)
        if name in self.lose_answer_to:
            self.lose_answer_to.discard(name)
            raise httpx.ReadTimeout("the answer was lost", request=request)
        return result if isinstance(result, httpx.Response) else self._answer(result)

    def _answer(self, data: Any, *, errors: list[dict[str, Any]] | None = None) -> httpx.Response:
        body: dict[str, Any] = {
            "data": data,
            "extensions": {
                "cost": {
                    "requestedQueryCost": self.query_cost,
                    "actualQueryCost": self.query_cost / 2,
                    "throttleStatus": {
                        "maximumAvailable": 2000.0,
                        "currentlyAvailable": self.available,
                        "restoreRate": self.restore_rate,
                    },
                }
            },
        }
        if errors:
            body["errors"] = errors
        return httpx.Response(200, json=body)

    # -- OAuth --------------------------------------------------------------------------------------

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_calls.append(form)
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return httpx.Response(400, json={"error": "invalid_client"})
        if form.get("grant_type") == "authorization_code":
            if form.get("code") != "code_ok":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self._issue(expiring=form.get("expiring") == "1"))
        if form.get("grant_type") == "refresh_token":
            if self.revoked or form.get("refresh_token") != self.refresh_token:
                return httpx.Response(401, json={"error": "invalid_request"})
            return httpx.Response(200, json=self._issue(expiring=True))
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _issue(self, *, expiring: bool) -> dict[str, Any]:
        self.token_seq += 1
        self.access_token = f"shpua_{self.token_seq:08d}"
        body: dict[str, Any] = {
            "access_token": self.access_token,
            "scope": "write_customers,read_orders,read_all_orders,read_products,write_discounts",
            "a_new_field": True,
        }
        if expiring:
            self.refresh_token = f"shprt_{self.token_seq}"
            body.update(
                {"expires_in": 3599, "refresh_token": self.refresh_token, "refresh_token_expires_in": 7776000}
            )
        return body

    # -- reads ------------------------------------------------------------------------------------

    def _table(self, resource: str) -> dict[str, dict[str, Any]]:
        return {"customers": self.customers, "orders": self.orders, "products": self.products}[resource]

    def _select(self, resource: str, search: str | None) -> list[dict[str, Any]]:
        low = high = None
        for op, value in _FILTER.findall(search or ""):
            if op == ">=":
                low = parse_ts(value)
            else:
                high = parse_ts(value)
        rows = [
            r
            for r in self._table(resource).values()
            if (low is None or parse_ts(r["updatedAt"]) >= low)
            and (high is None or parse_ts(r["updatedAt"]) < high)
        ]
        return sorted(rows, key=lambda r: (r["updatedAt"], r["id"]))

    def _page(self, resource: str, variables: Mapping[str, Any]) -> dict[str, Any] | httpx.Response:
        if resource in self.denied_resources:
            return self._answer(
                None,
                errors=[
                    {"message": "Access denied for customers field.", "extensions": {"code": "ACCESS_DENIED"}}
                ],
            )
        rows = self._select(resource, variables.get("query"))
        first = self.page_size or int(variables["first"])
        after = variables.get("after")
        offset = int(after.split(":")[1]) if after else 0
        page = rows[offset : offset + first]
        more = offset + first < len(rows)
        return {
            resource: {
                "nodes": page,
                "pageInfo": {"hasNextPage": more, "endCursor": f"c:{offset + first}" if more else None},
            }
        }

    def _op_AutonomyCustomers(self, variables: Mapping[str, Any], query: str) -> Any:  # noqa: N802
        return self._page("customers", variables)

    def _op_AutonomyOrders(self, variables: Mapping[str, Any], query: str) -> Any:  # noqa: N802
        return self._page("orders", variables)

    def _op_AutonomyProducts(self, variables: Mapping[str, Any], query: str) -> Any:  # noqa: N802
        return self._page("products", variables)

    def _op_AutonomyShop(self, variables: Mapping[str, Any], query: str) -> Any:  # noqa: N802
        return {
            "shop": {
                "id": "gid://shopify/Shop/1",
                "name": "Acme Goods",
                "myshopifyDomain": SHOP,
                "currencyCode": "USD",
                "ianaTimezone": "America/New_York",
            },
            "currentAppInstallation": {"app": {"apiKey": self.app_api_key}},
        }

    # -- bulk operations --------------------------------------------------------------------------

    def _op_AutonomyBulkStart(self, variables: Mapping[str, Any], query: str) -> Any:  # noqa: N802
        if self.bulk_busy > 0:
            self.bulk_busy -= 1
            return {
                "bulkOperationRunQuery": {
                    "bulkOperation": None,
                    "userErrors": [{"field": None, "message": "limit reached", "code": "LIMIT_REACHED"}],
                }
            }
        text = str(variables["query"])
        root = _BULK_ROOT.search(text)
        assert root is not None, text
        self.seq += 1
        op = BulkOp(
            op_id=gid("BulkOperation", self.seq),
            resource=root.group(1),
            search=json.loads(root.group(2)),
            created_at=self.clock_now,
        )
        if self.bulk_fail_next and self.bulk_fail_count > 0:
            op.error_code = self.bulk_fail_next
            self.bulk_fail_count -= 1
        self.bulk_ops[op.op_id] = op
        return {
            "bulkOperationRunQuery": {
                "bulkOperation": {"id": op.op_id, "status": "CREATED", "createdAt": iso(op.created_at)},
                "userErrors": [],
            }
        }

    def _op_AutonomyBulkStatus(self, variables: Mapping[str, Any], query: str) -> Any:  # noqa: N802
        op = self.bulk_ops.get(str(variables["id"]))
        if op is None:
            return {"bulkOperation": None}
        if op.status == "RUNNING":
            if op.polls_left > 0:
                op.polls_left -= 1
            elif op.error_code:
                op.status = "FAILED"
            else:
                op.status = "COMPLETED"
                self.bulk_files[op.op_id] = self._jsonl(op)
        url = (
            f"https://{STORAGE}/bulk/{op.op_id.rsplit('/', 1)[1]}.jsonl?sig=abc"
            if op.status == "COMPLETED"
            else None
        )
        if op.status == "COMPLETED" and not self.bulk_files[op.op_id]:
            url = None
        return {
            "bulkOperation": {
                "id": op.op_id,
                "status": op.status,
                "errorCode": op.error_code if op.status == "FAILED" else None,
                "createdAt": iso(op.created_at),
                "completedAt": iso(self.clock_now) if op.status in ("COMPLETED", "FAILED") else None,
                "objectCount": "0",
                "fileSize": str(len(self.bulk_files.get(op.op_id, b""))),
                "url": url,
                "partialDataUrl": None,
            }
        }

    def _jsonl(self, op: BulkOp) -> bytes:
        lines: list[str] = []
        for row in self._select(op.resource, op.search):
            parent = {k: v for k, v in row.items() if k != "lineItems"}
            lines.append(json.dumps(parent))
            for child in (row.get("lineItems") or {}).get("nodes") or []:
                lines.append(json.dumps({**child, "__parentId": row["id"]}))
        return ("\n".join(lines) + "\n").encode() if lines else b""

    def _download(self, request: httpx.Request) -> httpx.Response:
        self.ranges.append(request.headers.get("range"))
        if self.gone_downloads > 0:
            self.gone_downloads -= 1
            return httpx.Response(403, text="<Error>ExpiredToken</Error>")
        op_id = gid("BulkOperation", request.url.path.rsplit("/", 1)[1].removesuffix(".jsonl"))
        data = self.bulk_files.get(op_id)
        if data is None:
            return httpx.Response(404)
        header = request.headers.get("range")
        if header:
            start = int(header.removeprefix("bytes=").rstrip("-"))
            if start >= len(data):
                return httpx.Response(416)
            return httpx.Response(206, content=data[start:])
        return httpx.Response(200, content=data)

    # -- webhooks, discounts, tags -----------------------------------------------------------------

    def _op_AutonomyWebhooks(self, variables: Mapping[str, Any], query: str) -> Any:  # noqa: N802
        nodes = [{"id": sid, **sub} for sid, sub in self.subscriptions.items()]
        return {
            "webhookSubscriptions": {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}
        }

    def _op_AutonomyWebhookCreate(self, variables: Mapping[str, Any], query: str) -> Any:  # noqa: N802
        self.seq += 1
        sid = gid("WebhookSubscription", self.seq)
        self.subscriptions[sid] = {"topic": str(variables["topic"]), "uri": str(variables["uri"])}
        return {"webhookSubscriptionCreate": {"webhookSubscription": {"id": sid}, "userErrors": []}}

    def _op_AutonomyWebhookUpdate(self, variables: Mapping[str, Any], query: str) -> Any:  # noqa: N802
        sub = self.subscriptions[str(variables["id"])]
        sub["uri"] = str(variables["uri"])
        return {
            "webhookSubscriptionUpdate": {"webhookSubscription": {"id": variables["id"]}, "userErrors": []}
        }

    def _op_AutonomyDiscountByCode(self, variables: Mapping[str, Any], query: str) -> Any:  # noqa: N802
        found = self.discounts.get(str(variables["code"]))
        return {"codeDiscountNodeByCode": {"id": found["id"], "codeDiscount": {}} if found else None}

    def _op_AutonomyDiscountCreate(self, variables: Mapping[str, Any], query: str) -> Any:  # noqa: N802
        spec = variables["input"]
        code = spec["code"]
        if code in self.discounts:
            errors = [
                {"field": ["basicCodeDiscount", "code"], "message": "Code must be unique.", "code": "TAKEN"}
            ]
            return {"discountCodeBasicCreate": {"codeDiscountNode": None, "userErrors": errors}}
        self.seq += 1
        self.discounts[code] = {"id": gid("DiscountCodeNode", self.seq), "input": spec}
        return {
            "discountCodeBasicCreate": {
                "codeDiscountNode": {"id": self.discounts[code]["id"]},
                "userErrors": [],
            }
        }

    def _op_AutonomyTagsAdd(self, variables: Mapping[str, Any], query: str) -> Any:  # noqa: N802
        self.tags.setdefault(str(variables["id"]), set()).update(variables["tags"])
        return {"tagsAdd": {"node": {"id": variables["id"]}, "userErrors": []}}

    def _op_AutonomyTagsRemove(self, variables: Mapping[str, Any], query: str) -> Any:  # noqa: N802
        self.tags.setdefault(str(variables["id"]), set()).difference_update(variables["tags"])
        return {"tagsRemove": {"node": {"id": variables["id"]}, "userErrors": []}}


def queued(rig: Rig, kind: str | None = None) -> list[Job]:
    """Every job waiting on the rig's queue (the SQL queue has no listing of its own)."""
    from sqlalchemy import select

    from autonomy.store.schema import job_table

    with rig.store.engine.connect() as conn:
        rows = conn.execute(select(job_table.c.body)).scalars().all()
    jobs = [Job.model_validate(b) for b in rows]
    return [j for j in jobs if kind is None or j.kind == kind]


# --- rig ---------------------------------------------------------------------------------------------


def make_shopify_rig(
    shop: FakeShopify | None = None, *, now: datetime = T0, connector: ShopifyConnector | None = None
) -> tuple[Rig, FakeShopify]:
    fake = shop or FakeShopify(clock_now=now)
    rig = make_rig(fake, now=now, connector=connector or ShopifyConnector())  # type: ignore[arg-type]
    rig.engine.apps["shopify"] = APP
    return rig, fake


def sign_query(params: Mapping[str, str], secret: str = CLIENT_SECRET) -> dict[str, str]:
    """`params` plus the `hmac` Shopify adds to a callback or an install link."""
    message = "&".join(
        sorted(
            f"{k.replace('%', '%25').replace('=', '%3D')}={v.replace('%', '%25')}".replace("&", "%26")
            for k, v in params.items()
        )
    )
    return {**params, "hmac": sign(secret, message.encode(), encoding="hex")}


def connect_oauth(rig: Rig, fake: FakeShopify, now: datetime = T0, *, seed: bool = True) -> Connection:
    url = rig.engine.begin_oauth(
        "shopify", business_id="biz_1", redirect_uri=CALLBACK, now=now, params={"shop": SHOP}
    )
    state = parse_qs(url.split("?", 1)[1])["state"][0]
    query = sign_query(
        {"code": "code_ok", "shop": SHOP, "state": state, "timestamp": str(int(now.timestamp()))}
    )
    return rig.engine.finish_oauth("shopify", query, now, rig.queue if seed else None)


def connect_token(rig: Rig, now: datetime = T0, token: str = "shpat_live_1") -> Connection:
    return rig.engine.connect_api_key(
        "shopify", business_id="biz_1", api_key=token, now=now, config={"shop": SHOP}
    )


def webhook(
    topic: str,
    payload: Mapping[str, Any],
    *,
    event_id: str,
    webhook_id: str | None = None,
    secret: str = CLIENT_SECRET,
    shop: str = SHOP,
    triggered: datetime = T0,
) -> tuple[dict[str, str], bytes]:
    raw = json.dumps(payload).encode()
    headers = {
        "X-Shopify-Topic": topic,
        "X-Shopify-Hmac-Sha256": sign(secret, raw),
        "X-Shopify-Shop-Domain": shop,
        "X-Shopify-API-Version": VERSION,
        "X-Shopify-Webhook-Id": webhook_id or f"wh-{event_id}",
        "X-Shopify-Event-Id": event_id,
        "X-Shopify-Triggered-At": iso(triggered),
        "Content-Type": "application/json",
    }
    return headers, raw


def rest_order(
    oid: int,
    *,
    customer: int | None,
    total: float,
    created: datetime,
    updated: datetime,
    refunds: list[tuple[int, datetime, float]] = (),  # type: ignore[assignment]
    presentment: str = "USD",
) -> dict[str, Any]:
    """An `orders/updated` body in the REST Order shape (abridged, as documented)."""
    return {
        "id": oid,
        "admin_graphql_api_id": gid("Order", oid),
        "name": f"#{oid}",
        "email": "jo@example.com",
        "created_at": iso(created),
        "updated_at": iso(updated),
        "processed_at": iso(created),
        "cancelled_at": None,
        "test": False,
        "financial_status": "paid",
        "currency": "USD",
        "presentment_currency": presentment,
        "total_price": f"{total:.2f}",
        "subtotal_price": f"{total:.2f}",
        "total_discounts": "0.00",
        "total_price_set": {
            "shop_money": {"amount": f"{total:.2f}", "currency_code": "USD"},
            "presentment_money": {"amount": f"{total:.2f}", "currency_code": presentment},
        },
        "discount_codes": [{"code": "WELCOME10", "amount": "2.00", "type": "percentage"}],
        "customer": {"id": customer, "email": "jo@example.com"} if customer is not None else None,
        "line_items": [
            {
                "id": oid * 100,
                "product_id": 501,
                "variant_id": 5010,
                "sku": "SKU-501",
                "title": "Serum",
                "quantity": 1,
                "price": f"{total:.2f}",
                "total_discount": "0.00",
                "vendor": "Acme",
            }
        ],
        "refunds": [
            {
                "id": rid,
                "created_at": iso(when),
                "transactions": [{"kind": "refund", "status": "success", "amount": f"{amount:.2f}"}],
                "refund_line_items": [],
            }
            for rid, when, amount in refunds
        ],
        "a_field_from_a_newer_version": {"x": 1},
    }


def rest_customer(
    cid: int, *, created: datetime, updated: datetime, email_state: str = "subscribed"
) -> dict[str, Any]:
    return {
        "id": cid,
        "email": "jo@example.com",
        "created_at": iso(created),
        "updated_at": iso(updated),
        "first_name": "Jo",
        "state": "enabled",
        "orders_count": 1,
        "total_spent": "20.00",
        "phone": None,
        "tags": "",
        "email_marketing_consent": {
            "state": email_state,
            "opt_in_level": "single_opt_in",
            "consent_updated_at": iso(updated),
        },
        "sms_marketing_consent": None,
        "admin_graphql_api_id": gid("Customer", cid),
    }
