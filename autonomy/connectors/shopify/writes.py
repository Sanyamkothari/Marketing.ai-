"""What Autonomy writes to a shop: single-use discount codes for approved offers, and customer tags.

Shopify cannot send a message, so these are the only writes; the message itself goes out through
the customer's email tool (Klaviyo), carrying the code.

**One decision, one code, whatever happens.** Shopify's discount mutations take no idempotency
key, so the key is turned into the code itself: `discount_code(key)` is a deterministic, readable
code (`AUTO-` and 10 base32 characters of the key's hash). Before creating, the code is looked up
(`codeDiscountNodeByCode`); a create that loses a race or is retried after a timeout fails with
`TAKEN` (codes are unique per shop) and is answered by the same lookup. So a retried job, two
workers or a replayed decision all end with the one code, and the caller can compute the code from
the decision without asking Shopify.

The code is single-use (`usageLimit: 1`, `appliesOncePerCustomer: true`), limited to the decision's
customer when one is given (`context.customers`), dated (`startsAt` .. `endsAt`, default 14 days)
and does not stack with other product or order discounts (free shipping may still combine).

**Tags** (`tagsAdd` / `tagsRemove`, e.g. `autonomy-holdout` so the merchant can see who is held
out) are idempotent by nature: adding a tag a customer already has changes nothing.

Bad input is answered with `WriteResult(accepted=False, detail=...)` before any call; Shopify's
`userErrors` the same way, so a refused write is visible without failing the job.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from autonomy.connectors.base import ConnectionContext, WriteResult
from autonomy.connectors.shopify import gql
from autonomy.connectors.shopify.api import DISCOUNT_BY_CODE, DISCOUNT_CREATE, TAGS_ADD, TAGS_REMOVE
from autonomy.connectors.shopify.normalize import gid, numeric_id, parse_time

__all__ = [
    "CREATE_DISCOUNT",
    "DEFAULT_PREFIX",
    "REMOVE_TAGS",
    "TAG_CUSTOMER",
    "create_discount_code",
    "discount_code",
    "set_tags",
]

CREATE_DISCOUNT: Final[str] = "create_discount_code"
TAG_CUSTOMER: Final[str] = "tag_customer"
REMOVE_TAGS: Final[str] = "remove_customer_tags"
DEFAULT_PREFIX: Final[str] = "AUTO-"
DEFAULT_VALID: Final[timedelta] = timedelta(days=14)
MAX_TAGS: Final[int] = 25
MAX_TAG_LENGTH: Final[int] = 255


def discount_code(idempotency_key: str, prefix: str = DEFAULT_PREFIX) -> str:
    """The code a decision's discount gets: the same key always gives the same code."""
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).digest()
    return prefix + base64.b32encode(digest).decode("ascii")[:10]


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number == number else None


def discount_input(payload: Mapping[str, Any], code: str, now: datetime) -> dict[str, Any] | str:
    """The `DiscountCodeBasicInput` for an offer, or a plain-words reason it cannot be made."""
    percentage = _number(payload.get("percentage"))
    amount = _number(payload.get("amount"))
    if (percentage is None) == (amount is None):
        return "give exactly one of percentage (0-1) and amount"
    if percentage is not None and not 0 < percentage <= 1:
        return "percentage must be above 0 and at most 1 (0.1 is 10%)"
    if amount is not None and amount <= 0:
        return "amount must be above 0"
    starts = parse_time(payload.get("starts_at")) or now
    ends = parse_time(payload.get("ends_at"))
    if ends is None:
        days = _number(payload.get("valid_days"))
        ends = starts + (timedelta(days=days) if days is not None and days > 0 else DEFAULT_VALID)
    if ends <= starts:
        return "ends_at must be after starts_at"
    customer = numeric_id(payload.get("customer_id"))
    value: dict[str, Any] = (
        {"percentage": percentage}
        if percentage is not None
        else {"discountAmount": {"amount": amount, "appliesOnEachItem": False}}
    )
    body: dict[str, Any] = {
        "title": str(payload.get("title") or f"Autonomy offer {code}")[:255],
        "code": code,
        "startsAt": _iso(starts),
        "endsAt": _iso(ends),
        "usageLimit": 1,
        "appliesOncePerCustomer": True,
        "context": {"customers": {"add": [gid("Customer", customer)]}} if customer else {"all": "ALL"},
        "customerGets": {"value": value, "items": {"all": True}},
        "combinesWith": {
            "orderDiscounts": False,
            "productDiscounts": False,
            "shippingDiscounts": bool(payload.get("combines_with_shipping", True)),
        },
    }
    minimum = _number(payload.get("minimum_subtotal"))
    if minimum is not None and minimum > 0:
        body["minimumRequirement"] = {"subtotal": {"greaterThanOrEqualToSubtotal": minimum}}
    return body


def _existing(ctx: ConnectionContext, code: str) -> str | None:
    node = gql.query(ctx, DISCOUNT_BY_CODE, {"code": code}).get("codeDiscountNodeByCode")
    if isinstance(node, Mapping) and isinstance(node.get("id"), str):
        return str(node["id"])
    return None


def create_discount_code(
    ctx: ConnectionContext, payload: Mapping[str, Any], *, idempotency_key: str, prefix: str = DEFAULT_PREFIX
) -> WriteResult:
    code = discount_code(idempotency_key, str(payload.get("code_prefix") or prefix))
    body = discount_input(payload, code, ctx.now)
    if isinstance(body, str):
        return WriteResult(accepted=False, detail=body)
    found = _existing(ctx, code)
    if found is not None:
        return WriteResult(accepted=True, provider_id=found, detail=code)
    data = gql.mutate(ctx, DISCOUNT_CREATE, {"input": body}, idempotency_key=idempotency_key)
    result = data.get("discountCodeBasicCreate")
    errors = gql.user_errors(result)
    if errors:
        if any(str(e.get("code") or "").upper() == "TAKEN" for e in errors):
            found = _existing(ctx, code)  # created by the attempt that timed out, or a racing worker
            if found is not None:
                return WriteResult(accepted=True, provider_id=found, detail=code)
        return WriteResult(accepted=False, detail="Shopify refused the discount: " + _describe(errors))
    node = result.get("codeDiscountNode") if isinstance(result, Mapping) else None
    provider = node.get("id") if isinstance(node, Mapping) else None
    return WriteResult(accepted=True, provider_id=str(provider) if provider else None, detail=code)


def _tags(value: Any) -> list[str] | str:
    raw: Sequence[Any] = (
        [value] if isinstance(value, str) else value if isinstance(value, list | tuple) else []
    )
    tags = [str(t).strip() for t in raw if str(t).strip()]
    if not tags:
        return "give at least one tag"
    if len(tags) > MAX_TAGS or any(len(t) > MAX_TAG_LENGTH or "," in t for t in tags):
        return f"at most {MAX_TAGS} tags, each at most {MAX_TAG_LENGTH} characters and without commas"
    return tags


def set_tags(
    ctx: ConnectionContext, payload: Mapping[str, Any], *, idempotency_key: str, remove: bool = False
) -> WriteResult:
    customer = numeric_id(payload.get("customer_id"))
    if customer is None:
        return WriteResult(accepted=False, detail="customer_id is required")
    tags = _tags(payload.get("tags"))
    if isinstance(tags, str):
        return WriteResult(accepted=False, detail=tags)
    data = gql.mutate(
        ctx,
        TAGS_REMOVE if remove else TAGS_ADD,
        {"id": gid("Customer", customer), "tags": tags},
        idempotency_key=idempotency_key,
    )
    result = data.get("tagsRemove" if remove else "tagsAdd")
    errors = gql.user_errors(result)
    if errors:
        return WriteResult(accepted=False, detail="Shopify refused the tags: " + _describe(errors))
    return WriteResult(accepted=True, provider_id=gid("Customer", customer), detail=",".join(tags))


def _describe(errors: Sequence[Mapping[str, Any]]) -> str:
    return "; ".join(str(e.get("message") or e.get("code") or "error")[:120] for e in errors[:3])
