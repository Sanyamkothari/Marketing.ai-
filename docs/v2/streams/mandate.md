# Stream: Mandate (the governance plane)

A mandate is the signed box autonomous marketing acts in: budget ceiling, daily spend, margin floor,
quiet hours, frequency cap, channels, templates, offers, excluded cohorts and a validity window. It
turns on only when the right people have signed it, every change is recorded in a hash-chained
ledger, and every action can be checked against it. Plain maths: no LLM, time is always passed in.
Full detail is in [docs/v2/MANDATE.md](../MANDATE.md).

## What was built

| Module | What it does |
|---|---|
| `autonomy/mandate/models.py` | `Mandate` (frozen, UTC times, validated limits), `QuietHours`, `FrequencyCap`, `MandateSignature`, and `SignaturePolicy`: a marketing lead always, finance above `finance_threshold`, a different person per role, at least two people. |
| `autonomy/mandate/signing.py` | Canonical JSON body hash, `HmacSigner` (stdlib) and `Ed25519Signer` (when `cryptography` imports), a `KeyRing` that binds each key to one person and its roles, `sign_mandate` / `verify_mandate` / `with_signature`, and the append-only hash-chained `MandateLedger`. |
| `autonomy/mandate/enforce.py` | `check_action` and `check_bid` (every violation, as `mandate_` codes), `mandate_to_envelope` (an `Envelope` never looser than the mandate or the brief) and the `TokenBucket` spend pacer (no rolling 24 hours above the daily cap). |
| `autonomy/mandate/catalogue.py` | The versioned Action Catalogue (offers, channel rates, approved provider templates), hash-chained and diffable, with in-memory and write-once JSON-directory stores, and `check_action_against_catalogue` (`catalogue_` codes). |
| `api/v2/engines/mandates.py` | `router` (`/mandates`): draft, sign, certify, get, ledger, check-action, revoke, and the catalogue routes. State is a `MandateRegistry` injected via `get_mandate_registry`. |

Tests: `tests/v2/mandate/` (56 tests, about a second).

## How other streams use it

Everything below is importable from `autonomy.mandate`.

```python
from autonomy.mandate import (
    Mandate, SignaturePolicy, KeyRing, HmacSigner, MandateLedger,
    sign_mandate, with_signature, verify_mandate,
    ActionCosts, SpendToDate, check_action, check_bid, mandate_to_envelope, TokenBucket,
    ActionCatalogue, CatalogueContent, check_action_against_catalogue, catalogue_costs,
)

# Is this mandate allowed to govern anything right now?
verify_mandate(mandate, policy, keys) -> list[str]          # [] = properly signed
mandate.effective_status(now) -> MandateStatus              # active / expired / revoked / ...

# Decide: after its own hard rules, add these codes to Decision.blocked_by
check_bid(mandate, bid, state, now, *, offers=brief.envelope.offers,
          spend=None, pending_touches=0) -> list[str]
mandate_to_envelope(mandate, offers=(), *, base=brief.envelope, spent=0.0) -> Envelope

# Dispatch: the exact check just before sending, then pace spend
check_action(mandate, action, costs: ActionCosts | Bid, state, now, *,
             spend: SpendToDate | None = None, pending_touches=0) -> list[str]
bucket = TokenBucket.for_mandate(mandate, now)               # None when no daily cap
allowed, bucket = bucket.try_spend(amount, now)
bucket.wait_seconds(amount, now) -> float | None

# Signing and the ledger (runtime / API)
sig = sign_mandate(mandate, signer, person_id=..., role=SignerRole.MARKETING_LEAD, now=now)
mandate = with_signature(mandate, sig, policy=policy, keys=keys)   # pending_signatures -> active
ledger.append("signed", mandate, actor=..., at=now) -> LedgerEntry
ledger.verify_chain(keys=None) -> ChainReport

# Catalogue
catalogue.publish(content, created_by=..., now=now) -> CatalogueVersion
catalogue.get(n | "latest"); catalogue.diff(a, b); catalogue.verify_history() -> list[str]
check_action_against_catalogue(version, action, *, currency=mandate.currency) -> list[str]
catalogue_costs(version, action, *, currency=mandate.currency) -> (offer_cost, channel_cost)
```

The app mounts `api.v2.engines.mandates.router`; tests override `get_mandate_registry`.

## Contract additions requested

None to `autonomy/contracts`. One in-stream follow-up: an optional `reason` on `LedgerEntry`, so a
revoke reason can be stored (a mandate's `note` is signed content, so writing the reason there
would break its signatures).

## Dependencies requested

None (pydantic, fastapi, stdlib). Optional: add `cryptography` to the light v2 CI install so the
Ed25519 test runs there; without it the code falls back to HMAC and an Ed25519 signature never
counts as verified.

## Known gaps

- mypy --strict passed locally on all six modules with Python 3.13 (`--python-version 3.13`);
  Python 3.11 is checked only in CI. No 3.12+ syntax is used.
- Nothing is wired into decide, dispatch or the runtime yet; that belongs to those streams.
- The ledger and the API registry are in memory. `JsonDirCatalogueStore` persists the catalogue,
  but the default registry uses the in-memory store.
- Amending a mandate (version + 1 with a `superseded` entry) is not wired in the API; re-posting an
  id returns 409.
- A mandate's `approved_template_ids` are catalogue template ids. If decide uses `ContentItem` ids
  as `Action.content_id`, the two id spaces need a mapping.
- check-action with an offer and no `costs` fails the margin floor (no revenue or margin known);
  callers must pass `revenue_if_converted` and `margin_if_converted`.
- The default finance threshold (10,000) ignores currency; production should configure an INR
  policy on the registry.
- Cohort exclusions reuse `autonomy.decide.audience.passes`, where an unknown field matches no
  one, so a mistyped exclusion silently excludes nobody. Checking fields at creation would fix it.
- A signature's `signed_at` is not checked against the validity window or `now`.
- `check_action` falls back to the mandate's quiet-hours zone; the envelope path (decide.rules)
  falls back to UTC. They differ only for customers with a blank or invalid zone.
- `api/v2/engines/__init__.py` does not exist (not this stream's file); the namespace package works.
- `black --check docs/v2/MANDATE.md` cannot work (black does not parse Markdown); the Python files
  are black- and ruff-clean.
