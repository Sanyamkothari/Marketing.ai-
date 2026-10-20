"""`ShopifyConnector`: customers (with consent), orders (lines, discounts, refunds) and products.

How each piece of the hands-off promise is met for Shopify (the base does the rest):

* **Connect.** A public app installs through OAuth (`api.OAUTH`: offline, expiring tokens; the
  callback's `hmac` is checked in `validate_callback`, the shop domain against `SHOP_PATTERN`). An
  install that starts in the Shopify admin first hits the app URL with a signed query:
  `validate_install` checks it and returns the shop for `engine.begin_oauth`. Pilots before app
  review can paste a custom-app Admin API token instead (`engine.connect_api_key(..., config={"shop":
  ...})`); it never expires.
* **Read.** Live windows (a few hours) page the Admin GraphQL API sorted by `UPDATED_AT` with an
  `updated_at` search filter; history windows (and any window longer than `BULK_AFTER`, such as the
  catch-up after an outage) go through bulk operations (`bulk.py`). Pacing follows Shopify's
  cost-based limit (`extensions.cost.throttleStatus`, through `pacing` / `retry_signal`).
* **Webhooks.** Verified with `X-Shopify-Hmac-Sha256` (base64 HMAC-SHA256 of the raw body with the
  app's client secret), deduped on the event id per topic (`X-Shopify-Event-Id`, falling back to
  `X-Shopify-Webhook-Id`), routed by `X-Shopify-Shop-Domain`. The data topics are subscribed per
  shop (`register_webhooks`, idempotent, renewed daily by the engine) to
  `.../webhooks/shopify/<connection id>`; the three mandatory compliance topics are declared once
  in the app's configuration (`shopify.app.toml` `compliance_topics`) and arrive at the app-level
  route. A custom-app connection is polled only: its webhooks would be signed with that app's own
  secret, which the base does not hold per connection.
* **Write.** Single-use discount codes and customer tags (`writes.py`), idempotent per decision.
* **Privacy.** `customers/data_request`, `customers/redact` and `shop/redact` become privacy
  requests (the base records them); `privacy.ShopifyPrivacyDesk` carries them out.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final

from autonomy.catalog.products import Product
from autonomy.connectors.base import (
    AuthMethod,
    ConfigField,
    Connection,
    ConnectionContext,
    ConnectorCapabilities,
    CursorExpired,
    Identity,
    MappedRecords,
    NotSupported,
    PrivacyKind,
    RateSpec,
    RecordKey,
    SaasConnector,
    SetupMode,
    Stream,
    StreamPage,
    StreamSpec,
    SyncWindow,
    WebhookDelivery,
    WebhookTopic,
    WriteActionSpec,
    WriteResult,
)
from autonomy.connectors.http import HttpResult, graphql_page, graphql_throttle_wait, graphql_throttled
from autonomy.connectors.oauth import AppCredentials, AuthorizationError
from autonomy.connectors.shopify import gql
from autonomy.connectors.shopify.api import (
    API_VERSION,
    AUTH_HEADER,
    LIVE_PAGE_SIZE,
    LIVE_QUERIES,
    OAUTH,
    SHOP_PATTERN,
    SHOP_QUERY,
    VERSION,
    WEBHOOK_CREATE,
    WEBHOOK_UPDATE,
    WEBHOOKS_QUERY,
    normalize_shop,
    shop_from,
)
from autonomy.connectors.shopify.bulk import BULK_PREFIX, PAGE_RECORDS, read_bulk, search_filter
from autonomy.connectors.shopify.mapping import map_customers, map_orders, products_from_records
from autonomy.connectors.shopify.normalize import (
    VIA_WEBHOOK,
    customer_from_graphql,
    customer_from_rest,
    numeric_id,
    order_from_graphql,
    order_from_rest,
    parse_time,
    product_from_graphql,
    product_from_rest,
)
from autonomy.connectors.shopify.writes import (
    CREATE_DISCOUNT,
    DEFAULT_PREFIX,
    REMOVE_TAGS,
    TAG_CUSTOMER,
    create_discount_code,
    set_tags,
)
from autonomy.connectors.tokens import TokenSet
from autonomy.connectors.webhooks import hmac_matches, query_hmac_matches

__all__ = [
    "BULK_AFTER",
    "CAPABILITIES",
    "TOPIC_STREAMS",
    "ProductSink",
    "ShopifyConnector",
    "privacy_subject",
]

ProductSink = Callable[[Connection, Sequence[Product]], None]
"""Receives the catalogue products of each stored products page (e.g. the catalogue's upsert)."""

PAGE_PREFIX: Final[str] = "page:"
BULK_AFTER: Final[timedelta] = timedelta(days=2)
"""A window longer than this is read with a bulk operation instead of paged queries."""
WEBHOOK_RANK: Final[timedelta] = timedelta(milliseconds=1)
"""A webhook copy ranks this far below the polled copy of the same version (see `normalize`)."""
INSTALL_MAX_AGE: Final[timedelta] = timedelta(hours=24)

TOPIC_STREAMS: Final[Mapping[str, Stream]] = {
    "orders/create": Stream.ORDERS,
    "orders/updated": Stream.ORDERS,
    "customers/create": Stream.CUSTOMERS,
    "customers/update": Stream.CUSTOMERS,
    "products/update": Stream.PRODUCTS,
}
PRIVACY_TOPICS: Final[Mapping[str, PrivacyKind]] = {
    "customers/data_request": PrivacyKind.DATA_REQUEST,
    "customers/redact": PrivacyKind.REDACT_CUSTOMER,
    "shop/redact": PrivacyKind.REDACT_SHOP,
}
UNINSTALL_TOPIC: Final[str] = "app/uninstalled"

_GRAPHQL: Final[Mapping[Stream, Callable[[Mapping[str, Any]], Mapping[str, Any]]]] = {
    Stream.CUSTOMERS: customer_from_graphql,
    Stream.ORDERS: order_from_graphql,
    Stream.PRODUCTS: product_from_graphql,
}
_REST: Final[Mapping[Stream, Callable[[Mapping[str, Any]], Mapping[str, Any]]]] = {
    Stream.CUSTOMERS: customer_from_rest,
    Stream.ORDERS: order_from_rest,
    Stream.PRODUCTS: product_from_rest,
}

CAPABILITIES: Final[ConnectorCapabilities] = ConnectorCapabilities(
    kind="shopify",
    label="Shopify",
    description=(
        "Reads customers and their email/SMS marketing consent, orders (lines, discount codes, "
        "refunds) and products; creates single-use discount codes and customer tags. Shopify does "
        "not send messages: they go out through your email tool."
    ),
    auth=AuthMethod.OAUTH2,
    setup=SetupMode.AUTOMATIC,
    setup_note=(
        "Install the Autonomy app from your Shopify admin and approve the access it asks for. Before "
        "the app is listed, a pilot can paste a custom-app Admin API token with the same scopes instead."
    ),
    streams=(
        StreamSpec(
            stream=Stream.ORDERS,
            backfill_days=730,
            backfill_chunk=timedelta(days=30),
            min_history_days=30,
            webhook_topics=("orders/create", "orders/updated"),
            required_fields=("id", "created_at", "updated_at", "total"),
        ),
        StreamSpec(
            stream=Stream.CUSTOMERS,
            # Consent rides on customers, so customers are critical: stale consent is never acted on.
            backfill_days=3650,
            backfill_chunk=timedelta(days=365),
            min_history_days=365,
            webhook_topics=("customers/create", "customers/update"),
            required_fields=("id", "created_at", "updated_at"),
        ),
        StreamSpec(
            stream=Stream.PRODUCTS,
            cadence=timedelta(hours=6),
            overlap=timedelta(hours=12),
            backfill_days=7300,
            backfill_chunk=timedelta(days=7300),
            min_history_days=0,
            webhook_topics=("products/update",),
            required_fields=("id", "updated_at", "title"),
            critical=False,
        ),
    ),
    write_actions=(
        WriteActionSpec(
            name=CREATE_DISCOUNT,
            description="Create a single-use discount code for an approved offer; one code per decision.",
        ),
        WriteActionSpec(name=TAG_CUSTOMER, description="Add tags to a customer (e.g. autonomy-holdout)."),
        WriteActionSpec(name=REMOVE_TAGS, description="Remove tags from a customer."),
    ),
    webhook_topics=(
        *(WebhookTopic(topic=t, stream=s) for t, s in TOPIC_STREAMS.items()),
        WebhookTopic(topic=UNINSTALL_TOPIC, uninstall=True),
        *(WebhookTopic(topic=t, privacy=k, mandatory=True) for t, k in PRIVACY_TOPICS.items()),
    ),
    api_version=API_VERSION,
    rate=RateSpec(requests=4, per=timedelta(seconds=1), burst=8),
    config_fields=(
        ConfigField(
            name="shop",
            label="Shop address",
            help="Your shop's permanent address, ending in .myshopify.com",
            pattern=SHOP_PATTERN,
        ),
    ),
    sends=False,
)


class ShopifyConnector(SaasConnector):
    """See the module docstring. Stateless: one instance serves every shop."""

    capabilities = CAPABILITIES
    oauth = OAUTH
    bulk_page_records: int = PAGE_RECORDS
    """Top-level objects per page of a bulk export (an order counts once, with its line items)."""

    def __init__(
        self, product_sink: ProductSink | None = None, *, discount_prefix: str = DEFAULT_PREFIX
    ) -> None:
        self.product_sink = product_sink
        self.discount_prefix = discount_prefix

    # -- talking to it ---------------------------------------------------------------------------

    def base_url(self, connection: Connection) -> str:
        shop = normalize_shop(connection.config.get("shop") or connection.external_id or "")
        if shop is None:
            raise NotSupported("the Shopify connection has no valid .myshopify.com shop address")
        return f"https://{shop}/admin/api/{VERSION}"

    def auth_headers(self, connection: Connection, tokens: TokenSet) -> dict[str, str]:  # noqa: ARG002
        return {AUTH_HEADER: tokens.access_token}

    def pacing(self, result: HttpResult) -> float | None:
        return graphql_throttle_wait(result)

    def retry_signal(self, result: HttpResult) -> float | None:
        return graphql_throttled(result)

    def validate_callback(self, query: Mapping[str, str], app: AppCredentials) -> None:
        if not query_hmac_matches(app.client_secret, query):
            raise AuthorizationError("the Shopify callback's signature does not match")
        shop = query.get("shop")
        if shop is not None and shop_from(shop) is None:
            raise AuthorizationError("the Shopify callback names an invalid shop")

    def validate_install(self, query: Mapping[str, str], app: AppCredentials, now: datetime) -> str:
        """The shop of an install or app-launch request from the Shopify admin (`?shop&timestamp&hmac`).

        Checked before `engine.begin_oauth`: the signature over the query, the shop's permanent
        domain, and a timestamp no older than a day (Shopify signs when the merchant clicks)."""
        if not query_hmac_matches(app.client_secret, query):
            raise AuthorizationError("the install request's signature does not match")
        shop = shop_from(query.get("shop"))
        if shop is None:
            raise AuthorizationError("the install request names an invalid shop")
        stamp = query.get("timestamp", "")
        if not stamp.isdigit():
            raise AuthorizationError("the install request has no timestamp")
        age = now.timestamp() - int(stamp)
        if age > INSTALL_MAX_AGE.total_seconds() or age < -300:
            raise AuthorizationError("the install link is too old; open the app from Shopify again")
        return shop

    def identify(self, ctx: ConnectionContext) -> Identity | None:
        shop = gql.query(ctx, SHOP_QUERY).get("shop")
        if not isinstance(shop, Mapping):
            return None
        domain = shop_from(str(shop.get("myshopifyDomain") or "").lower())
        if domain is None:
            return None
        return Identity(external_id=domain, label=str(shop.get("name") or domain))

    def check(self, ctx: ConnectionContext) -> None:
        gql.query(ctx, SHOP_QUERY)

    # -- reading ---------------------------------------------------------------------------------

    def read(
        self, ctx: ConnectionContext, stream: Stream, window: SyncWindow, cursor: str | None
    ) -> StreamPage:
        normalize = _GRAPHQL.get(stream)
        if normalize is None:
            raise NotSupported(f"Shopify has no {stream.value} stream")
        if (cursor is not None and cursor.startswith(BULK_PREFIX)) or (
            cursor is None and _wants_bulk(window)
        ):
            return read_bulk(
                ctx, stream.value, window, cursor, normalize, page_records=self.bulk_page_records
            )
        after = None
        if cursor is not None:
            if not cursor.startswith(PAGE_PREFIX):
                raise CursorExpired("the saved page position is unreadable")
            after = cursor[len(PAGE_PREFIX) :]
        variables = {"first": LIVE_PAGE_SIZE[stream.value], "after": after, "query": search_filter(window)}
        data = gql.query(ctx, LIVE_QUERIES[stream.value], variables)
        nodes, nxt = graphql_page(data, (stream.value,))
        records = tuple(normalize(node) for node in nodes)
        return StreamPage(records=records, next_cursor=PAGE_PREFIX + nxt if nxt else None)

    def record_key(self, stream: Stream, record: Mapping[str, Any]) -> RecordKey:  # noqa: ARG002
        record_id = record.get("id")
        if not record_id:
            raise KeyError("id")
        updated = parse_time(record.get("updated_at"))
        if updated is not None and record.get("_via") == VIA_WEBHOOK:
            updated -= WEBHOOK_RANK
        return RecordKey(str(record_id), updated)

    def map_records(
        self, stream: Stream, records: Sequence[Mapping[str, Any]], connection: Connection
    ) -> MappedRecords:
        account = connection.external_id or connection.connection_id
        if stream is Stream.ORDERS:
            return map_orders(records, account)
        if stream is Stream.CUSTOMERS:
            return map_customers(records, account)
        if stream is Stream.PRODUCTS and self.product_sink is not None:
            # Called before the engine records the versions, so a crash replays the products.
            self.product_sink(connection, products_from_records(records))
        return MappedRecords()

    # -- webhooks --------------------------------------------------------------------------------

    def webhook_account(self, headers: Mapping[str, str], body: bytes) -> str | None:  # noqa: ARG002
        shop = headers.get("x-shopify-shop-domain")
        return shop.strip().lower() if shop else None

    def verify_webhook(self, secret: str, headers: Mapping[str, str], body: bytes) -> bool:
        return hmac_matches(secret, body, headers.get("x-shopify-hmac-sha256"))

    def webhook_delivery_id(self, headers: Mapping[str, str], body: bytes) -> str | None:  # noqa: ARG002
        topic = headers.get("x-shopify-topic") or ""
        event = headers.get("x-shopify-event-id")
        if event:
            return f"{topic}:{event}"
        webhook = headers.get("x-shopify-webhook-id")
        return f"{topic}:w:{webhook}" if webhook else None

    def webhook_topic(self, headers: Mapping[str, str], body: bytes) -> str | None:  # noqa: ARG002
        return headers.get("x-shopify-topic")

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> WebhookDelivery:
        topic = headers.get("x-shopify-topic") or ""
        delivery_id = self.webhook_delivery_id(headers, body) or "sha256:" + hashlib.sha256(body).hexdigest()
        occurred = parse_time(headers.get("x-shopify-triggered-at"))
        data = json.loads(body) if body.strip() else {}
        if not isinstance(data, Mapping):
            raise ValueError("a Shopify webhook body is a JSON object")
        if topic == UNINSTALL_TOPIC:
            return WebhookDelivery(
                delivery_id=delivery_id, topic=topic, occurred_at=occurred, uninstalled=True
            )
        privacy = PRIVACY_TOPICS.get(topic)
        if privacy is not None:
            return WebhookDelivery(
                delivery_id=delivery_id,
                topic=topic,
                occurred_at=occurred,
                privacy=privacy,
                subject=privacy_subject(data),
            )
        stream = TOPIC_STREAMS.get(topic)
        if stream is None:
            return WebhookDelivery(delivery_id=delivery_id, topic=topic, occurred_at=occurred)
        record = _REST[stream](data)
        return WebhookDelivery(
            delivery_id=delivery_id, topic=topic, stream=stream, records=(record,), occurred_at=occurred
        )

    def register_webhooks(
        self, ctx: ConnectionContext, callback_url: str, topics: Sequence[str]
    ) -> tuple[str, ...]:
        """Subscribe every data topic and `app/uninstalled` for this shop to `callback_url`; idempotent.

        The mandatory compliance topics cannot be subscribed per shop: they are declared in the app's
        configuration, so they count as live. A connection made with a custom-app token (the
        installation is not our app) is polled only and has nothing to subscribe."""
        app = ctx.app
        installed = gql.query(ctx, SHOP_QUERY)
        api_key = _dig(installed, "currentAppInstallation", "app", "apiKey")
        if app is None or api_key != app.client_id:
            return tuple(topics)  # polled only: the hourly catch-up is the delivery path
        live = {t for t in topics if t in PRIVACY_TOPICS}
        existing = _subscriptions(ctx)
        suffix = "/" + ctx.connection.connection_id
        for topic in topics:
            if topic in PRIVACY_TOPICS:
                continue
            enum = topic.upper().replace("/", "_")
            same = [s for s in existing if s.get("topic") == enum]
            if any(s.get("uri") == callback_url for s in same):
                live.add(topic)
                continue
            moved = next((s for s in same if str(s.get("uri") or "").endswith(suffix)), None)
            key = f"shopify-webhook:{ctx.connection.connection_id}:{topic}:{callback_url}"
            if moved is not None:
                data = gql.mutate(
                    ctx, WEBHOOK_UPDATE, {"id": moved["id"], "uri": callback_url}, idempotency_key=key
                )
                payload = data.get("webhookSubscriptionUpdate")
            else:
                data = gql.mutate(
                    ctx, WEBHOOK_CREATE, {"topic": enum, "uri": callback_url}, idempotency_key=key
                )
                payload = data.get("webhookSubscriptionCreate")
            if (
                not gql.user_errors(payload)
                and isinstance(payload, Mapping)
                and payload.get("webhookSubscription")
            ):
                live.add(topic)
        return tuple(t for t in topics if t in live)

    # -- writing ---------------------------------------------------------------------------------

    def write(
        self,
        ctx: ConnectionContext,
        action: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> WriteResult:
        if action == CREATE_DISCOUNT:
            return create_discount_code(
                ctx, payload, idempotency_key=idempotency_key, prefix=self.discount_prefix
            )
        if action == TAG_CUSTOMER:
            return set_tags(ctx, payload, idempotency_key=idempotency_key)
        if action == REMOVE_TAGS:
            return set_tags(ctx, payload, idempotency_key=idempotency_key, remove=True)
        raise NotSupported(f"Shopify has no write action {action!r}")


def _wants_bulk(window: SyncWindow) -> bool:
    return window.start is None or window.end - window.start > BULK_AFTER


def _dig(value: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _subscriptions(ctx: ConnectionContext) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    after: str | None = None
    for _ in range(20):
        data = gql.query(ctx, WEBHOOKS_QUERY, {"after": after})
        nodes, after = graphql_page(data, ("webhookSubscriptions",))
        found.extend(nodes)
        if after is None:
            break
    return found


def _sha256(text: Any) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()


def privacy_subject(data: Mapping[str, Any]) -> dict[str, Any]:
    """Who a compliance webhook is about, kept to ids: an email or phone is stored only as a SHA-256.

    `customers/data_request` and `customers/redact` carry `customer {id, email, phone}` and the
    orders concerned (`orders_requested` / `orders_to_redact`); `shop/redact` only the shop."""
    customer = data.get("customer")
    customer_map: Mapping[str, Any] = customer if isinstance(customer, Mapping) else {}
    orders = data.get("orders_to_redact") or data.get("orders_requested") or []
    subject: dict[str, Any] = {
        "shop_domain": str(data.get("shop_domain") or "").lower() or None,
        "shop_id": numeric_id(data.get("shop_id")),
        "customer_id": numeric_id(customer_map.get("id")),
        "order_ids": [o for o in (numeric_id(x) for x in orders if not isinstance(x, Mapping)) if o],
        "email_sha256": _sha256(customer_map.get("email")),
        "phone_sha256": _sha256(customer_map.get("phone")),
        "data_request_id": numeric_id(_dig(data, "data_request", "id")),
    }
    return {k: v for k, v in subject.items() if v not in (None, [])}
