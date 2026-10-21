"""Shopify Admin GraphQL: the pinned version, scopes, OAuth endpoints and every query the connector sends.

Written against the shopify.dev docs as read on 2026-10-20 (through search extracts: this build had
no direct access to shopify.dev). Each constant names what it relies on, so the quarterly version
bump is a read of this one file.

* **Version.** `2026-10` became the stable Admin API version on 2026-10-01 and is supported until
  about 2026-10 + 12 months; `ApiVersion.review_by` in the connector asks for the bump before then.
  The version travels in the URL (`/admin/api/2026-10/graphql.json`), not in a header.
* **Auth header.** Every Admin API call carries `X-Shopify-Access-Token`, for OAuth tokens and
  custom-app tokens alike.
* **Customer consent** is read from `defaultEmailAddress` / `defaultPhoneNumber`
  (`marketingState`, `marketingOptInLevel`, `marketingUpdatedAt`): the Customer-level
  `emailMarketingConsent` / `smsMarketingConsent` are deprecated. The queries deliberately read no
  name, email, phone or address (Shopify's protected customer data level 2), so the connector works
  with level 1 access; identity linking to an email tool goes through the customer id.
* **Bulk operations.** `bulkOperationRunQuery(query:, groupObjects: true)` starts an export,
  `bulkOperation(id:)` (2026-01 and later) polls it. Nested connections come out as separate JSONL
  lines carrying `__parentId`; with `groupObjects: true` each parent's children follow it, which is
  what lets the connector page the file with bounded memory. Only the orders export has a nested
  connection (line items); customers and products have none.
* **Webhook subscriptions** use the `uri` field (`callbackUrl` is deprecated since 2025-10).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Final

from autonomy.connectors.base import ApiVersion
from autonomy.connectors.oauth import OAuthProvider

__all__ = [
    "API_VERSION",
    "AUTH_HEADER",
    "BULK_QUERIES",
    "CONTACT_CONFIG",
    "LIVE_QUERIES",
    "OAUTH",
    "SCOPES",
    "SHOP_PATTERN",
    "bulk_query",
    "live_query",
    "normalize_shop",
    "reads_contact",
    "shop_from",
]

VERSION: Final[str] = "2026-10"
API_VERSION: Final[ApiVersion] = ApiVersion(
    value=VERSION,
    header=None,
    doc_date=date(2026, 10, 20),
    docs_url="https://shopify.dev/docs/api/admin-graphql/2026-10",
    review_by=date(2027, 7, 1),
)
AUTH_HEADER: Final[str] = "X-Shopify-Access-Token"

SHOP_PATTERN: Final[str] = r"[a-z0-9][a-z0-9-]{0,98}\.myshopify\.com"
"""A shop's permanent domain. Never a custom domain: the token URL is built from it."""
_SHOP_RE: Final[re.Pattern[str]] = re.compile(SHOP_PATTERN)

SCOPES: Final[tuple[str, ...]] = (
    "read_customers",
    "write_customers",
    "read_orders",
    "read_all_orders",
    "read_products",
    "write_discounts",
)
"""Kept minimal. `write_customers` is only for tags (`tagsAdd`/`tagsRemove`); `write_discounts` for
single-use codes (it includes reading them back by code). `read_all_orders` lifts the default 60-day
order window so the 2-year backfill sees anything older; a Partner app must request it in the
Partner/Dev Dashboard before Shopify grants it. Without it the history lane still completes, with
60 days of orders."""

OAUTH: Final[OAuthProvider] = OAuthProvider(
    name="Shopify",
    authorize_url="https://{shop}/admin/oauth/authorize",
    token_url="https://{shop}/admin/oauth/access_token",
    scopes=SCOPES,
    scope_separator=",",
    use_pkce=False,
    client_auth="body",
    extra_token_params=(("expiring", "1"),),
    revoked_errors=frozenset({"invalid_grant", "invalid_request"}),
    refresh_unauthorized_is_revoked=True,
    refresh_margin=timedelta(minutes=5),
    keep_alive_every=timedelta(days=30),
    min_refresh_interval=timedelta(seconds=10),
    param_patterns=(("shop", SHOP_PATTERN),),
)
"""Offline access (no `grant_options[]=per-user`), expiring tokens (`expiring=1` in the code exchange:
1 h access, 90-day rotating refresh token; public apps must use them by 2027-01-01). A dead refresh
token is `401 {"error": "invalid_request"}`."""


def normalize_shop(text: str) -> str | None:
    """What a person typed (`acme`, `Acme.myshopify.com`, `https://acme.myshopify.com/admin`) as the
    shop's permanent domain, or None when it cannot be one (a custom domain, a path trick, a port)."""
    value = text.strip().lower()
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    value = value.split("/", 1)[0]
    if value and "." not in value:
        value = f"{value}.myshopify.com"
    return value if _SHOP_RE.fullmatch(value) else None


def shop_from(value: str | None) -> str | None:
    """`value` when it is exactly a permanent shop domain, else None (no normalising: for URLs we build)."""
    if value is None:
        return None
    return value if _SHOP_RE.fullmatch(value) else None


# --- live (paged) queries --------------------------------------------------------------------------
# Every query must stay under the 1,000-point single-query limit (a heavier one is refused with
# MAX_COST_EXCEEDED before it runs). Shopify's requested cost: an object is 1, a scalar 0, and a
# connection (or a list taking `first`) is 2 + `first` x the cost of one node, multiplied through
# every level of nesting. So an order page costs about `first` x (refunds + line items x 7): 4
# orders with 20 line items and 10 refunds is about 690 points (`tests/v2/connectors/
# test_shopify_mapping.py` recomputes it for every live query). An order with more line items than
# that is marked `line_items_complete: False`; its total is always exact.

_MONEY = "shopMoney { amount currencyCode }"

CUSTOMER_FIELDS: Final[str] = """
      id
      createdAt
      updatedAt
      state
      numberOfOrders
      amountSpent { amount currencyCode }
      defaultEmailAddress { marketingState marketingOptInLevel marketingUpdatedAt }
      defaultPhoneNumber { marketingState marketingOptInLevel marketingUpdatedAt marketingCollectedFrom }
"""

ORDER_FIELDS: Final[str] = f"""
      id
      name
      createdAt
      updatedAt
      processedAt
      cancelledAt
      test
      displayFinancialStatus
      currencyCode
      discountCodes
      customer {{ id }}
      totalPriceSet {{ {_MONEY} }}
      subtotalPriceSet {{ {_MONEY} }}
      totalDiscountsSet {{ {_MONEY} }}
      refunds(first: 10) {{ id createdAt totalRefundedSet {{ {_MONEY} }} }}
"""

LINE_ITEM_FIELDS: Final[str] = f"""
          id
          quantity
          sku
          title
          originalUnitPriceSet {{ {_MONEY} }}
          discountedTotalSet {{ {_MONEY} }}
          variant {{ id }}
          product {{ id productType }}
"""

PRODUCT_FIELDS: Final[str] = """
      id
      title
      productType
      vendor
      status
      createdAt
      updatedAt
      priceRangeV2 { minVariantPrice { amount currencyCode } }
"""

CONTACT_CONFIG: Final[str] = "contact"
"""Connection setting: `read` also reads each customer's email address and phone number, so their
Klaviyo profiles can be linked to them (`autonomy.connectors.identity`). Shopify gives these fields
only to apps approved for protected customer data **level 2**; without that approval the query is
refused, so it is off unless the setting says `read`. The address is used only to link profiles and
is kept as a SHA-256 hash."""
CUSTOMER_CONTACT_FIELDS: Final[str] = CUSTOMER_FIELDS.replace(
    "defaultEmailAddress { ", "defaultEmailAddress { emailAddress "
).replace("defaultPhoneNumber { ", "defaultPhoneNumber { phoneNumber ")

MAX_QUERY_COST: Final[int] = 1000
LIVE_PAGE_SIZE: Final[dict[str, int]] = {"customers": 100, "orders": 4, "products": 50}
LINE_ITEMS_PER_ORDER: Final[int] = 20

LIVE_QUERIES: Final[dict[str, str]] = {
    "customers": f"""
query AutonomyCustomers($first: Int!, $after: String, $query: String) {{
  customers(first: $first, after: $after, query: $query, sortKey: UPDATED_AT) {{
    nodes {{{CUSTOMER_FIELDS}    }}
    pageInfo {{ hasNextPage endCursor }}
  }}
}}""",
    "orders": f"""
query AutonomyOrders($first: Int!, $after: String, $query: String) {{
  orders(first: $first, after: $after, query: $query, sortKey: UPDATED_AT) {{
    nodes {{{ORDER_FIELDS}
      lineItems(first: {LINE_ITEMS_PER_ORDER}) {{
        nodes {{{LINE_ITEM_FIELDS}        }}
        pageInfo {{ hasNextPage }}
      }}
    }}
    pageInfo {{ hasNextPage endCursor }}
  }}
}}""",
    "products": f"""
query AutonomyProducts($first: Int!, $after: String, $query: String) {{
  products(first: $first, after: $after, query: $query, sortKey: UPDATED_AT) {{
    nodes {{{PRODUCT_FIELDS}    }}
    pageInfo {{ hasNextPage endCursor }}
  }}
}}""",
}

# --- bulk exports (history) -----------------------------------------------------------------------
# `{query}` is replaced with the window's search filter (already quoted for GraphQL). Bulk queries
# ignore pagination arguments; connections are written as edges { node } as in Shopify's examples.

BULK_QUERIES: Final[dict[str, str]] = {
    "customers": f"""
{{
  customers(query: {{query}}) {{
    edges {{ node {{{CUSTOMER_FIELDS}    }} }}
  }}
}}""",
    "orders": f"""
{{
  orders(query: {{query}}) {{
    edges {{ node {{{ORDER_FIELDS}
      lineItems {{ edges {{ node {{{LINE_ITEM_FIELDS}        }} }} }}
    }} }}
  }}
}}""",
    "products": f"""
{{
  products(query: {{query}}) {{
    edges {{ node {{{PRODUCT_FIELDS}    }} }}
  }}
}}""",
}

BULK_START: Final[str] = """
mutation AutonomyBulkStart($query: String!) {
  bulkOperationRunQuery(query: $query, groupObjects: true) {
    bulkOperation { id status createdAt }
    userErrors { field message code }
  }
}"""

BULK_STATUS: Final[str] = """
query AutonomyBulkStatus($id: ID!) {
  bulkOperation(id: $id) {
    id status errorCode createdAt completedAt objectCount fileSize url partialDataUrl
  }
}"""

# --- identity, webhooks, writes ---------------------------------------------------------------------

SHOP_QUERY: Final[str] = """
query AutonomyShop {
  shop { id name myshopifyDomain currencyCode ianaTimezone }
  currentAppInstallation { app { apiKey } }
}"""

WEBHOOKS_QUERY: Final[str] = """
query AutonomyWebhooks($after: String) {
  webhookSubscriptions(first: 100, after: $after) {
    nodes { id topic uri }
    pageInfo { hasNextPage endCursor }
  }
}"""

WEBHOOK_CREATE: Final[str] = """
mutation AutonomyWebhookCreate($topic: WebhookSubscriptionTopic!, $uri: String!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: { uri: $uri, format: JSON }) {
    webhookSubscription { id topic uri }
    userErrors { field message }
  }
}"""

WEBHOOK_UPDATE: Final[str] = """
mutation AutonomyWebhookUpdate($id: ID!, $uri: String!) {
  webhookSubscriptionUpdate(id: $id, webhookSubscription: { uri: $uri }) {
    webhookSubscription { id topic uri }
    userErrors { field message }
  }
}"""

DISCOUNT_BY_CODE: Final[str] = """
query AutonomyDiscountByCode($code: String!) {
  codeDiscountNodeByCode(code: $code) {
    id
    codeDiscount { __typename ... on DiscountCodeBasic { title status endsAt usageLimit } }
  }
}"""

DISCOUNT_CREATE: Final[str] = """
mutation AutonomyDiscountCreate($input: DiscountCodeBasicInput!) {
  discountCodeBasicCreate(basicCodeDiscount: $input) {
    codeDiscountNode { id }
    userErrors { field message code }
  }
}"""

TAGS_ADD: Final[str] = """
mutation AutonomyTagsAdd($id: ID!, $tags: [String!]!) {
  tagsAdd(id: $id, tags: $tags) { node { id } userErrors { field message } }
}"""

TAGS_REMOVE: Final[str] = """
mutation AutonomyTagsRemove($id: ID!, $tags: [String!]!) {
  tagsRemove(id: $id, tags: $tags) { node { id } userErrors { field message } }
}"""


def reads_contact(config: Mapping[str, str]) -> bool:
    """Whether a connection reads customers' email and phone (`CONTACT_CONFIG`)."""
    return (config.get(CONTACT_CONFIG) or "").strip().lower() == "read"


def live_query(stream: str, config: Mapping[str, str]) -> str:
    """The live query for a stream, with the contact fields when the connection reads them."""
    text = LIVE_QUERIES[stream]
    return (
        text.replace(CUSTOMER_FIELDS, CUSTOMER_CONTACT_FIELDS)
        if stream == "customers" and reads_contact(config)
        else text
    )


def bulk_query(stream: str, config: Mapping[str, str]) -> str:
    """The bulk export query for a stream (`{query}` still to fill), with contact fields when read."""
    text = BULK_QUERIES[stream]
    return (
        text.replace(CUSTOMER_FIELDS, CUSTOMER_CONTACT_FIELDS)
        if stream == "customers" and reads_contact(config)
        else text
    )
