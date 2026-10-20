"""P2 Mandate & Catalogue Engine: the governance plane for autonomous marketing actions.

A `Mandate` is a versioned, signed statement of the hard limits no autonomous action may cross:
budget ceiling, daily spend, margin floor, quiet hours, frequency cap, channels, templates, offers,
excluded cohorts and a validity window. It becomes active only when the `SignaturePolicy` is met
(a marketing lead always, finance on a large budget, two different people at least), and every
version is recorded in a hash-chained `MandateLedger`.

* `models`: the mandate, its signatures and the signature policy (plain data).
* `signing`: canonical hashing, Ed25519 or HMAC signatures, the key ring and the ledger.
* `enforce`: `check_action` (every violation, as codes), `mandate_to_envelope` (so the existing
  decide rules keep working) and the `TokenBucket` spend pacer.
* `catalogue`: the versioned Action Catalogue (offers, channel rates, approved provider templates),
  hash-chained like the ledger, and `check_action_against_catalogue`.

The HTTP routes are in `api/v2/engines/mandates.py`.

See docs/v2/MANDATE.md.
"""

from autonomy.mandate.catalogue import (
    ActionCatalogue,
    CatalogueContent,
    CatalogueDiff,
    CatalogueError,
    CatalogueStore,
    CatalogueTemplate,
    CatalogueVersion,
    ChannelRate,
    InMemoryCatalogueStore,
    JsonDirCatalogueStore,
    catalogue_costs,
    check_action_against_catalogue,
)
from autonomy.mandate.enforce import (
    ALL_CODES,
    ActionCosts,
    SpendToDate,
    TokenBucket,
    check_action,
    check_action_against_mandate,
    check_bid,
    costs_from_bid,
    mandate_to_envelope,
)
from autonomy.mandate.models import (
    FrequencyCap,
    Mandate,
    MandateSignature,
    MandateStatus,
    QuietHours,
    SignatureAlgorithm,
    SignaturePolicy,
    SignerRole,
)
from autonomy.mandate.signing import (
    ChainReport,
    Ed25519Signer,
    HmacSigner,
    KeyRing,
    LedgerEntry,
    MandateLedger,
    Signer,
    body_hash,
    ed25519_available,
    sign_mandate,
    verify_entries,
    verify_mandate,
    verify_signature,
    with_signature,
)

__all__ = [
    "ALL_CODES",
    "ActionCatalogue",
    "ActionCosts",
    "CatalogueContent",
    "CatalogueDiff",
    "CatalogueError",
    "CatalogueStore",
    "CatalogueTemplate",
    "CatalogueVersion",
    "ChainReport",
    "ChannelRate",
    "Ed25519Signer",
    "FrequencyCap",
    "HmacSigner",
    "InMemoryCatalogueStore",
    "JsonDirCatalogueStore",
    "KeyRing",
    "LedgerEntry",
    "Mandate",
    "MandateLedger",
    "MandateSignature",
    "MandateStatus",
    "QuietHours",
    "SignatureAlgorithm",
    "SignaturePolicy",
    "Signer",
    "SignerRole",
    "SpendToDate",
    "TokenBucket",
    "body_hash",
    "catalogue_costs",
    "check_action",
    "check_action_against_catalogue",
    "check_action_against_mandate",
    "check_bid",
    "costs_from_bid",
    "ed25519_available",
    "mandate_to_envelope",
    "sign_mandate",
    "verify_entries",
    "verify_mandate",
    "verify_signature",
    "with_signature",
]
