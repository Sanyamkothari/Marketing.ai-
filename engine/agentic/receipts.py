"""Client-held checkpoints: the receipts a client keeps, and the check that they are still in the chain (Plan K
M141, DEC-1408 (d); design §3.2).

"You are marking your own homework" is answered by giving the client something we cannot rewrite afterwards.
Every approval receipt (`POST /agentic/proposals/{id}/approve` and `/reject`) and every MCP answer carries a
:class:`LedgerReceipt`:

* `ledger_chain` - the client workspace whose chain it is on (null: the deployment's own chain);
* `ledger_seq` and `ledger_checkpoint` - the `seq` and `entry_hash` of the entry the receipt is for (the decision,
  or the evidence label a tool's answer was recorded under). An `entry_hash` chains every entry before it, so a
  receipt is a checkpoint of the whole history up to it;
* `holdout_salt_fingerprint` - the fingerprint of the holdout salt the platform database records
  (`engine.holdout.salt`): the commitment to who is held out. Null, with `holdout_salt_note` saying why, when no
  persistent holdout has used a salt on this deployment (a per-run holdout uses none).

The client keeps the receipts it is given. ``python -m engine.agentic.ledger verify --against receipts.json``
walks the chain (every hash and signature) and then looks every receipt up in it: a receipt whose `entry_hash`
is not in the chain, or is at another `seq`, is **`RECEIPT_MISMATCH`**. Whoever holds the signing key could
rewrite the history and recompute every hash and signature - plain verification would then pass - but every
hash after the first edited entry changes, so the receipts the client already holds no longer match.

The file is a JSON list of receipts, or ``{"receipts": [...]}``. A receipt may carry other fields (a whole
`ProposalReceipt`, or the `marketing-ai/receipt` object of an MCP answer as it came); only the four above are
read. All receipts in one file must name one chain.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from engine.agentic.ledger import DecisionLedger

__all__ = [
    "NO_SALT_NOTE",
    "LedgerReceipt",
    "ReceiptCheck",
    "ReceiptError",
    "chain_of",
    "check_receipts",
    "holdout_salt_fingerprint",
    "load_receipts",
    "receipt_for",
]

NO_SALT_NOTE: Final[str] = (
    "No persistent holdout has used a salt on this deployment yet (a per-run holdout uses none), so there is no "
    "fingerprint to commit to."
)


class LedgerReceipt(BaseModel):
    """What a client keeps from one answer or one decision: a checkpoint of the chain, and the holdout's
    commitment. Extra fields are ignored when a kept receipt is read back (a whole answer may be kept)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    ledger_chain: str | None = Field(
        default=None, description="The client workspace whose chain holds it; null on the deployment's chain."
    )
    ledger_seq: int | None = Field(default=None, ge=1, description="The entry's seq on that chain.")
    ledger_checkpoint: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="The entry's entry_hash: a checkpoint of every entry before it.",
    )
    holdout_salt_fingerprint: str | None = Field(
        default=None,
        description="The holdout salt's fingerprint the platform records; null when there is none.",
    )
    holdout_salt_note: str | None = Field(default=None, description="Why there is no fingerprint, when null.")


class ReceiptError(ValueError):
    """A receipts file that cannot be read (`RECEIPT_MISMATCH`: nothing in it can be matched)."""

    code: Final[str] = "RECEIPT_MISMATCH"


class ReceiptCheck(BaseModel):
    """What `check_receipts` found."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    receipts: int = Field(description="Receipts checked (up to and including the first that does not match).")
    code: str | None = None
    bad_receipt: int | None = Field(
        default=None, description="Index (from 0) of the first receipt that failed."
    )
    reason: str | None = None


def holdout_salt_fingerprint(engine: Engine) -> str | None:
    """The holdout salt fingerprint recorded in this platform database, or None when none is recorded."""
    from engine.holdout.salt import HoldoutLedger

    return HoldoutLedger(engine).fingerprint()


def receipt_for(entry: Any, engine: Engine) -> LedgerReceipt:
    """The receipt of one ledger entry (`engine.agentic.ledger.LedgerEntry`), with the holdout commitment."""
    fingerprint = holdout_salt_fingerprint(engine)
    return LedgerReceipt(
        ledger_chain=entry.chain,
        ledger_seq=entry.seq,
        ledger_checkpoint=entry.entry_hash,
        holdout_salt_fingerprint=fingerprint,
        holdout_salt_note=None if fingerprint is not None else NO_SALT_NOTE,
    )


def load_receipts(path: Path) -> tuple[LedgerReceipt, ...]:
    """The receipts of a file: a JSON list, or an object with `receipts`."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ReceiptError(f"The receipts file could not be read: {exc}.") from exc
    items = document.get("receipts") if isinstance(document, dict) else document
    if not isinstance(items, list) or not items:
        raise ReceiptError('The receipts file holds no receipts (a JSON list, or {"receipts": [...]}).')
    try:
        return tuple(LedgerReceipt.model_validate(item) for item in items)
    except ValidationError as exc:
        raise ReceiptError(f"A receipt is not one this product hands out: {exc.errors()[0]['msg']}.") from exc


def chain_of(receipts: Sequence[LedgerReceipt]) -> str | None:
    """The one chain the receipts name; a file mixing chains is refused."""
    chains = {receipt.ledger_chain for receipt in receipts}
    if len(chains) > 1:
        raise ReceiptError(
            "The receipts name more than one chain; check each workspace's receipts on their own."
        )
    return next(iter(chains))


def check_receipts(ledger: DecisionLedger, receipts: Sequence[LedgerReceipt]) -> ReceiptCheck:
    """Whether every receipt is still in the ledger's chain: its `ledger_checkpoint` is an entry there, at its
    `ledger_seq`.

    Run it after the chain itself verified (`verify_ledger`); it reads only the indexed `entry_hash` column.
    """
    chain = ledger.chain
    for index, receipt in enumerate(receipts):
        if receipt.ledger_chain != chain:
            return ReceiptCheck(
                ok=False,
                receipts=index + 1,
                code="RECEIPT_MISMATCH",
                bad_receipt=index,
                reason=f"it is a receipt of another chain ({receipt.ledger_chain or 'the deployment'}).",
            )
        entry = ledger.find(receipt.ledger_checkpoint)
        if entry is None:
            return ReceiptCheck(
                ok=False,
                receipts=index + 1,
                code="RECEIPT_MISMATCH",
                bad_receipt=index,
                reason=(
                    f"its checkpoint {receipt.ledger_checkpoint[:16]}... is not in the chain: the history was "
                    "rewritten after this receipt was handed out."
                ),
            )
        if receipt.ledger_seq is not None and entry.seq != receipt.ledger_seq:
            return ReceiptCheck(
                ok=False,
                receipts=index + 1,
                code="RECEIPT_MISMATCH",
                bad_receipt=index,
                reason=f"its checkpoint is at seq {entry.seq} now, not {receipt.ledger_seq}.",
            )
    return ReceiptCheck(ok=True, receipts=len(receipts))
