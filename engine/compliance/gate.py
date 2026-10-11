"""`decide`: one compliance decision per customer, channel and purpose (Plan K M134, DEC-1405; design §8.2).

**Input.** `customers` - one row per customer a treat list would contact: the key columns (as text, the way the
treat list writes them) and `action_id` (the catalogue action, null for none). `planned` gives each action's
channels in order of preference (`None`: the channels of a contact with no catalogue action). Every customer is
exploded into one row per planned channel; a customer with no planned channel at all gets one row with a null
channel, which is never allowed (`COMPLIANCE_NO_CHANNEL`: a contact the gate could not clear on any channel).

**What decides, per row** (the reasons, in this order; `allowed` is true exactly when there is none):

1. `COMPLIANCE_NO_CHANNEL` - no channel.
2. `COMPLIANCE_CONSENT` - not contactable on the channel, read from M99/M131's `channel_contactability.parquet`
   (`contactability`) joined **on every key column**, exactly as the treat list reads it: the channel's
   `contactable_<channel>` flag; for a channel the use case does not configure, the file's
   `all_channel_consent` where it has one, else open; a customer the file does not cover is not restricted by
   it (and gets a null `ledger_ref`). The run worked these flags out from the uploaded consent columns and the
   consent ledger with the region's consent-basis rules (an inquiry consent past its validity, a legacy consent
   not registered): they are not computed again here.
3. `COMPLIANCE_WHATSAPP_NO_OPT_IN` - a channel of `rules.opt_in_channels` with no row in `opt_ins` (the
   customer's valid grant on that channel itself, from the consent ledger; `engine.compliance.handoff`).
4. `COMPLIANCE_CATALOGUE_CHANGED` - the row has a catalogue action and the catalogue is not the one the run ran
   under, so its message cannot be tied to the run.
5. `COMPLIANCE_TEMPLATE_MISMATCH` and 6. `COMPLIANCE_URL_NOT_WHITELISTED` - the per-message verdicts of the row's
   (action, channel) (`messages`, computed once per action and channel, never per customer).

**Output** (:func:`decision_schema`): the key columns, `channel`, `purpose`, `action_id`, `allowed`, `reasons`
(a list of codes), `ledger_ref` (the contactability file's reference where it covers the customer, and for an
opt-in channel `consent_record.seq=<n>` of the grant used), `catalogue_ref` (`<catalogue sha256>#<action_id>`,
null without an action) and `rule_version` (the sha256 of the rules applied, `engine.compliance.handoff`).

**Linear, no per-row Python.** Every step is a column operation: a hash join of the customers' key text against
the contactability file's (`pandas.Index.get_indexer`), `numpy.repeat` to explode, `pandas.factorize` for the
few distinct actions and channels, and one `numpy.nonzero` over the reason matrix to build the list column.
Python loops run only over the distinct actions and channels.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    import numpy as np
    import pyarrow as pa

__all__ = [
    "ACTION_COLUMN",
    "ALLOWED_COLUMN",
    "CHANNEL_COLUMN",
    "REASONS_COLUMN",
    "REASON_ORDER",
    "Exploded",
    "GateRules",
    "MessageVerdict",
    "decide",
    "decision_schema",
    "explode",
    "key_join_text",
]

ACTION_COLUMN: Final[str] = "action_id"
CHANNEL_COLUMN: Final[str] = "channel"
ALLOWED_COLUMN: Final[str] = "allowed"
REASONS_COLUMN: Final[str] = "reasons"
OPT_IN_SEQ_COLUMN: Final[str] = "consent_seq"
"""`opt_ins`: the key columns, `channel` and the `seq` of the consent record that is the opt-in."""

REASON_ORDER: Final[tuple[str, ...]] = (
    "COMPLIANCE_NO_CHANNEL",
    "COMPLIANCE_CONSENT",
    "COMPLIANCE_WHATSAPP_NO_OPT_IN",
    "COMPLIANCE_CATALOGUE_CHANGED",
    "COMPLIANCE_TEMPLATE_MISMATCH",
    "COMPLIANCE_URL_NOT_WHITELISTED",
)
"""Every reason a row can carry, in the order a row lists them."""

_PAIR_SEPARATOR: Final[str] = "\x1f"
_ALL_CHANNEL_CONSENT: Final[str] = "all_channel_consent"
_CONTACTABLE_PREFIX: Final[str] = "contactable_"


@dataclass(frozen=True)
class MessageVerdict:
    """The per-message checks of one (action, channel): computed once, applied to every customer."""

    template_mismatch: bool = False
    url_not_whitelisted: bool = False
    catalogue_ref: str | None = None


@dataclass(frozen=True)
class GateRules:
    """What applies to every row of one decision file."""

    rule_version: str
    purpose: str | None = None
    contactability_ref: str | None = None
    """`<run file>#sha256=<hex>` of the contactability file read; null when the run wrote none."""
    opt_in_channels: tuple[str, ...] = ()
    catalogue_changed: bool = False


def decision_schema(key_columns: Sequence[str]) -> pa.Schema:
    """The columns of `compliance_decisions.parquet`, in order."""
    import pyarrow as pa

    return pa.schema(
        [
            *(pa.field(name, pa.string()) for name in key_columns),
            pa.field(CHANNEL_COLUMN, pa.string()),
            pa.field("purpose", pa.string()),
            pa.field(ACTION_COLUMN, pa.string()),
            pa.field(ALLOWED_COLUMN, pa.bool_()),
            pa.field(REASONS_COLUMN, pa.list_(pa.string())),
            pa.field("ledger_ref", pa.string()),
            pa.field("catalogue_ref", pa.string()),
            pa.field("rule_version", pa.string()),
        ]
    )


def key_join_text(table: pa.Table, key_columns: Sequence[str]) -> np.ndarray:
    """One text per row naming the customer: each key column as `engine.keys.key_text` writes it, joined by
    `KEY_SEPARATOR` - the treat list's own join key, built column-wise."""
    import numpy as np

    from engine.keys import KEY_SEPARATOR, key_text

    missing = [name for name in key_columns if name not in table.column_names]
    if missing:
        raise KeyError(f"The table has no {', '.join(missing)} column, so its rows cannot be identified.")
    joined = None
    for name in key_columns:
        part = key_text(table[name].to_pandas()).astype("object")
        joined = part if joined is None else joined + KEY_SEPARATOR + part
    assert joined is not None
    return np.asarray(joined.to_numpy(dtype=object), dtype=object)


def _factorize(values: np.ndarray) -> tuple[np.ndarray, list[Any]]:
    """Integer codes (-1 for None) and the distinct values, in order of first appearance."""
    import numpy as np
    import pandas as pd

    codes, uniques = pd.factorize(pd.Series(values, dtype=object), use_na_sentinel=True)
    return np.asarray(codes, dtype=np.int64), list(uniques)


@dataclass(frozen=True)
class Exploded:
    """`customers` as contact rows: one per planned channel, in plan order (one null-channel row for none)."""

    parent: np.ndarray
    """Per contact row, the position of its customer in `customers`."""
    channel: np.ndarray
    """Per contact row, the channel (None for a customer with no planned channel)."""
    plan_of: np.ndarray
    """Per contact row, 0 for no catalogue action, else 1 + the position of its action in `actions`."""
    actions: list[Any]
    """The distinct actions, in order of first appearance."""


def explode(customers: pa.Table, planned: Mapping[str | None, tuple[str, ...]]) -> Exploded:
    """One row per (customer, planned channel), customers in order, channels in plan order."""
    import numpy as np

    n = customers.num_rows
    action_values = np.asarray(customers[ACTION_COLUMN].to_numpy(zero_copy_only=False), dtype=object)
    action_codes, actions = _factorize(action_values)
    plans = [tuple(planned.get(None, ()))] + [
        tuple(planned[action] if action in planned else planned.get(None, ())) for action in actions
    ]
    shifted = action_codes + 1  # 0: no action
    widths = np.asarray([max(len(plan), 1) for plan in plans], dtype=np.int64)
    per_row = widths[shifted]
    total = int(per_row.sum())
    parent = np.repeat(np.arange(n, dtype=np.int64), per_row)
    starts = np.cumsum(per_row) - per_row
    within = np.arange(total, dtype=np.int64) - np.repeat(starts, per_row)
    plan_of = shifted[parent]
    channel = np.full(total, None, dtype=object)
    for code, plan in enumerate(plans):
        if plan:
            rows = plan_of == code
            channel[rows] = np.asarray(plan, dtype=object)[within[rows]]
    return Exploded(parent=parent, channel=channel, plan_of=plan_of, actions=actions)


def decide(
    customers: pa.Table,
    *,
    key_columns: Sequence[str],
    planned: Mapping[str | None, tuple[str, ...]],
    rules: GateRules,
    contactability: pa.Table | None = None,
    opt_ins: pa.Table | None = None,
    messages: Mapping[tuple[str, str], MessageVerdict] | None = None,
) -> pa.Table:
    """The decision table for `customers` (see the module docstring)."""
    import numpy as np
    import pandas as pd
    import pyarrow as pa
    import pyarrow.compute as _pc

    pc: Any = _pc  # untyped: its stubs miss most kernels (as engine.decide.treat_list reads it)

    keys = tuple(key_columns)
    cust_text = key_join_text(customers, keys)

    # 1. Explode: one row per planned channel of the customer's action (one null-channel row for none).
    exploded = explode(customers, planned)
    parent, channel, plan_of, actions = exploded.parent, exploded.channel, exploded.plan_of, exploded.actions
    total = len(parent)
    channel_codes, channels = _factorize(channel)
    has_channel = channel_codes >= 0

    # 2. Consent: the run's contactability, joined on every key column (never by position).
    open_ = np.ones(total, dtype=bool)
    covered = np.zeros(total, dtype=bool)
    at = np.zeros(total, dtype=np.int64)
    if contactability is not None:
        index = pd.Index(key_join_text(contactability, keys))
        if index.is_unique:
            found = index.get_indexer(pd.Index(cust_text))
            covered = (found >= 0)[parent]
            at = np.where(found >= 0, found, 0)[parent]
        names = set(contactability.column_names)
        everyone = None
        if _ALL_CHANNEL_CONSENT in names:
            everyone = contactability[_ALL_CHANNEL_CONSENT].to_numpy(zero_copy_only=False).astype(bool)
        for code, name in enumerate(channels):
            column = _CONTACTABLE_PREFIX + str(name)
            flags = (
                contactability[column].to_numpy(zero_copy_only=False).astype(bool)
                if column in names
                else everyone
            )
            if flags is None or not len(flags):
                continue
            rows = channel_codes == code
            open_[rows] = np.where(covered[rows], flags[at[rows]], True)
    consent_fail = has_channel & ~open_

    # 3. Opt-in: a grant on the channel itself.
    opt_codes = [code for code, name in enumerate(channels) if name in set(rules.opt_in_channels)]
    needs_opt_in = np.isin(channel_codes, opt_codes) if opt_codes else np.zeros(total, dtype=bool)
    opted = np.zeros(total, dtype=bool)
    seq = np.zeros(total, dtype=np.int64)
    if opt_ins is not None and opt_ins.num_rows and bool(needs_opt_in.any()):
        grant_text = key_join_text(opt_ins, keys) + _PAIR_SEPARATOR
        grant_text = grant_text + np.asarray(
            opt_ins[CHANNEL_COLUMN].to_numpy(zero_copy_only=False), dtype=object
        )
        grants = pd.Index(grant_text)
        keep = ~grants.duplicated(keep="first")
        grants = grants[keep]
        grant_seq = np.asarray(opt_ins[OPT_IN_SEQ_COLUMN].to_numpy(zero_copy_only=False), dtype=np.int64)[
            keep
        ]
        wanted = np.flatnonzero(needs_opt_in)
        asked = cust_text[parent[wanted]] + _PAIR_SEPARATOR + channel[wanted]
        hit = grants.get_indexer(pd.Index(asked))
        opted[wanted] = hit >= 0
        seq[wanted] = np.where(hit >= 0, grant_seq[np.where(hit >= 0, hit, 0)], 0)
    opt_in_fail = needs_opt_in & ~opted

    # 4-6. Per message: once per (action, channel), then taken for every row.
    has_action = plan_of > 0
    changed_fail = has_channel & has_action & bool(rules.catalogue_changed)
    width = len(channels) + 1
    pair_codes, pairs = _factorize(plan_of * width + (channel_codes + 1))
    template_small = np.zeros(len(pairs), dtype=bool)
    url_small = np.zeros(len(pairs), dtype=bool)
    refs_small: list[str | None] = [None] * len(pairs)
    for position, pair in enumerate(pairs):
        action_code, channel_code = divmod(int(pair), width)
        if action_code == 0 or channel_code == 0:
            continue
        verdict = (messages or {}).get((str(actions[action_code - 1]), str(channels[channel_code - 1])))
        if verdict is None:
            continue
        template_small[position] = verdict.template_mismatch
        url_small[position] = verdict.url_not_whitelisted
        refs_small[position] = verdict.catalogue_ref
    message_ok = has_channel & ~changed_fail
    template_fail = message_ok & template_small[pair_codes]
    url_fail = message_ok & url_small[pair_codes]

    # The reasons as a list column: one nonzero() over the (rows x reasons) matrix, row-major.
    matrix = np.column_stack([~has_channel, consent_fail, opt_in_fail, changed_fail, template_fail, url_fail])
    rows_hit, which = np.nonzero(matrix)
    counts = np.bincount(rows_hit, minlength=total)
    offsets = np.zeros(total + 1, dtype=np.int64)
    np.cumsum(counts, out=offsets[1:])
    reasons = pa.LargeListArray.from_arrays(
        pa.array(offsets, pa.int64()), pa.array(REASON_ORDER, pa.string()).take(pa.array(which))
    ).cast(pa.list_(pa.string()))
    allowed = counts == 0

    # References.
    take = pa.array(parent)
    string_null = pa.scalar(None, pa.string())
    if rules.contactability_ref is None:
        base = pa.nulls(total, pa.string())
    else:
        base = pc.if_else(
            pa.array(covered & has_channel), pa.scalar(rules.contactability_ref, pa.string()), string_null
        )
    tagged = pc.binary_join_element_wise(
        pa.scalar("consent_record.seq=", pa.string()), pc.cast(pa.array(seq), pa.string()), ""
    )
    ledger_ref = pc.if_else(
        pa.array(opted), pc.binary_join_element_wise(base, tagged, ";", null_handling="skip"), base
    )
    catalogue_ref = pa.array(refs_small, pa.string()).take(pa.array(pair_codes))
    channel_array = pa.array([str(name) for name in channels], pa.string()).take(
        pa.array(channel_codes, mask=~has_channel)
    )
    purpose = (
        pa.nulls(total, pa.string())
        if rules.purpose is None
        else pa.repeat(pa.scalar(rules.purpose, pa.string()), total)
    )
    columns: list[Any] = [pc.cast(customers[name].take(take), pa.string()) for name in keys]
    columns += [
        channel_array,
        purpose,
        pc.cast(customers[ACTION_COLUMN].take(take), pa.string()),
        pa.array(allowed),
        reasons,
        ledger_ref,
        catalogue_ref,
        pa.repeat(pa.scalar(rules.rule_version, pa.string()), total),
    ]
    return pa.Table.from_arrays(columns, schema=decision_schema(keys))
