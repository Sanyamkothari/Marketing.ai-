"""The Shopify connector: read customers, consent, orders and products; write discount codes and tags.

`ShopifyConnector` is what `autonomy.connectors.registry.BUILTIN` loads. The pieces:

* `api` - the pinned Admin API version (2026-10), scopes, OAuth endpoints and every GraphQL query;
* `normalize` - GraphQL nodes, bulk-export lines and REST webhook bodies into one record shape;
* `mapping` - those records as Autonomy events (purchase, refund, signup, consent) and products;
* `bulk` - history through bulk operations, paged from the JSONL file with bounded memory;
* `gql` - GraphQL calls that read Shopify's error codes (ACCESS_DENIED, THROTTLED);
* `writes` - single-use discount codes and customer tags, idempotent per decision;
* `privacy` - carrying out the mandatory data-request and redact webhooks on our copy;
* `connector` - `ShopifyConnector` itself (webhooks, OAuth checks, reads, writes).

See the "Shopify" section of `docs/v2/CONNECTORS.md`.
"""

from autonomy.connectors.shopify.api import API_VERSION, OAUTH, SCOPES, SHOP_PATTERN, normalize_shop
from autonomy.connectors.shopify.connector import (
    BULK_AFTER,
    CAPABILITIES,
    TOPIC_STREAMS,
    ProductSink,
    ShopifyConnector,
    privacy_subject,
)
from autonomy.connectors.shopify.mapping import SOURCE, map_customers, map_orders, products_from_records
from autonomy.connectors.shopify.privacy import (
    PRIVACY_JOB_KIND,
    PrivacyOutcome,
    ShopifyPrivacyDesk,
    create_privacy_tables,
    privacy_handlers,
    privacy_job,
)
from autonomy.connectors.shopify.writes import (
    CREATE_DISCOUNT,
    REMOVE_TAGS,
    TAG_CUSTOMER,
    discount_code,
)

__all__ = [
    "API_VERSION",
    "BULK_AFTER",
    "CAPABILITIES",
    "CREATE_DISCOUNT",
    "OAUTH",
    "PRIVACY_JOB_KIND",
    "REMOVE_TAGS",
    "SCOPES",
    "SHOP_PATTERN",
    "SOURCE",
    "TAG_CUSTOMER",
    "TOPIC_STREAMS",
    "PrivacyOutcome",
    "ProductSink",
    "ShopifyConnector",
    "ShopifyPrivacyDesk",
    "create_privacy_tables",
    "discount_code",
    "map_customers",
    "map_orders",
    "normalize_shop",
    "privacy_handlers",
    "privacy_job",
    "privacy_subject",
    "products_from_records",
]
