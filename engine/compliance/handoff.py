"""The gate on a treat list: decide, write the record, sign it, and take the withheld contacts off the list.

Plan K M134 (DEC-1405). Called by `engine.decide.treat_list.build_treat_list` only when
`compliance.gate.enabled` is on, after the list has chosen every customer's treat flag, offer and channel and
before it writes anything. For the customers the list would contact:

1. **Rules.** The catalogue as it is now - used only when its sha256 is the one the run ran under
   (`catalogue_stamp.json`); otherwise every row with a catalogue action is `COMPLIANCE_CATALOGUE_CHANGED` and
   no message is checked. The region file the catalogue names gives the DLT channels (each `<channel>_requires`
   listing `dlt_template_id`) and the region block's digest; the URL whitelist comes from
   `configs/compliance/url_whitelist.yaml`, else the region file's `url_whitelist` block, else it is not applied.
   All of it, with the opt-in channels and the run's consent-basis rules digest, is one JSON document
   (`rules` in `compliance_decisions.json`); `rule_version` is its sha256 (`engine.agentic.canonical.digest`),
   so a reader can recompute it. A check that was not applied is written there as `null`.
2. **Per message** (once per action and channel): the DLT matcher on the DLT channels, when the action has both
   `dlt_template_text` and `message_text`; the whitelist on its channels, when the action has `message_text`.
   What was not checked, and why, is listed per action and channel in `messages`.
3. **Per customer:** `engine.compliance.gate.decide`, over the run's own `channel_contactability.parquet` and the
   opt-in grants (`engine.compliance.optin.channel_grants`, read for the customers who plan an opt-in channel).
4. **The list.** A customer is sent on the first planned channel the gate allows - the channel they had, or the
   next planned one - and is not treated when it allows none: never `treat = 1` with no channel. The outcome
   counts the customers withheld per reason (a customer counts under every reason that closed one of their
   channels) and those moved to another channel.
5. **The record.** `compliance_decisions.parquet` (row-level: `configs/privacy.yaml`, `engine/privacy/layout.py`,
   `ROW_LEVEL_ARTEFACTS`), then a **signed** `evidence.label` entry on the decision ledger carrying the file's
   sha256 and `rule_version` (`engine.agentic.ledger`), then `compliance_decisions.json` (counts, the rules, the
   evidence id and the ledger entry). A ledger that cannot be written stops the build: no list without its record.

**Message arms** (Plan K M145, DEC-1412). On a run whose M140 message arms are in force the treat list passes
`variants` (`engine.compliance.variants.VariantMessages`), and a customer of a variant arm is checked on the words
their arm sends them - per (arm, group, channel), against the variant's own registered catalogue action - instead
of their band's `message_text`; the control template's customers are checked as above. The catalogue the
messages are tied to is then the one the arms were drawn under (`variant_arms.json` `catalogue_sha256`, which
registers the variants' words and was checked when they were drawn), not the run's stamp: registering the variants
necessarily changed the catalogue after the run. Any other catalogue fails closed, as in 1. The rules gain a
`variant_arms` block (the batch, that catalogue, and every arm template's reference), the decision file a
`variant_arm` column and each variant row its variant's own `action_id`, and the summary `variant_messages` and
`withheld_by_arm`. Without `variants` none of this exists and every file is what it was.

The ledger is the deployment's platform database (SQLite beside a local store's files, else the settings'
database) and the signer the data folder's Ed25519 key (`engine.agentic.ledger.signer_for_data_dir`), as the
`/agentic` routes use them; a caller may pass its own.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from pydantic import Field

from engine.compliance.errors import ComplianceError
from engine.compliance.gate import (
    ACTION_COLUMN,
    OPT_IN_SEQ_COLUMN,
    REASON_ORDER,
    GateRules,
    MessageVerdict,
    decide,
    explode,
)
from engine.compliance.variants import VariantMessageCheck
from engine.contracts import Artefact
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    import numpy as np
    import pandas as pd
    import pyarrow as pa

    from engine.agentic.ledger import DecisionLedger
    from engine.compliance.variants import VariantMessages, VariantWords
    from engine.decide.catalogue import ActionCatalogue
    from engine.storage import Storage

__all__ = [
    "COMPLIANCE_DECISIONS_FILENAME",
    "COMPLIANCE_SUMMARY_FILENAME",
    "GATE_ACTOR",
    "GATE_TOOL",
    "ComplianceDecisionsSummary",
    "GateOutcome",
    "MessageCheck",
    "dlt_channels",
    "gate_treat_list",
    "message_checks",
]

_LOGGER = get_logger(__name__)

COMPLIANCE_DECISIONS_FILENAME: Final[str] = "compliance_decisions.parquet"
COMPLIANCE_SUMMARY_FILENAME: Final[str] = "compliance_decisions.json"
GATE_ACTOR: Final[str] = "system:compliance_gate"
GATE_TOOL: Final[str] = "compliance_gate"
EVIDENCE_LABEL_TEXT: Final[str] = "compliance_decisions"
DLT_FIELD: Final[str] = "dlt_template_id"

_NOTE: Final[str] = (
    "One row per customer the treat list would contact and per planned channel: allowed, or the reasons it "
    "is not. A customer is sent on the first planned channel the gate allows and is not treated when it "
    "allows none. withheld counts customers taken off the list, under every reason that closed one of their "
    "channels, so the counts can add up to more than withheld_rows."
)


class MessageCheck(Artefact):
    """What was checked of one action's message on one channel, and why not when it was not."""

    action_id: str
    channel: str
    template_checked: bool = Field(description="Whether the DLT matcher ran on this action and channel.")
    template_mismatch: bool | None = Field(description="Its verdict; null when it did not run.")
    urls_checked: bool = Field(description="Whether the links were checked against the whitelist.")
    urls_not_whitelisted: int | None = Field(description="Links not on the list; null when not checked.")
    notes: tuple[str, ...] = Field(default=(), description="Why a check did not run, in plain words.")


class ComplianceDecisionsSummary(Artefact):
    """`compliance_decisions.json`: counts, the rules applied and where the signed record is. No row."""

    run_id: str
    use_case_id: str
    purpose: str | None = Field(description="The consent purpose decided for; null when none is mapped.")
    customers: int = Field(description="Customers the treat list would have contacted before the gate.")
    rows: int = Field(description="Decision rows: one per customer and planned channel.")
    allowed_rows: int = Field(description="Rows allowed.")
    withheld_rows: int = Field(
        description="Customers the gate took off the list (no planned channel allowed)."
    )
    rerouted_rows: int = Field(description="Customers sent on another planned channel than the list chose.")
    withheld: dict[str, int] = Field(description="Customers withheld, per reason (see note).")
    rows_without_consent_evidence: int = Field(
        description="Rows the run's channel_contactability.parquet does not cover (their ledger_ref is null)."
    )
    rule_version: str = Field(
        description="sha256 of `rules` (canonical JSON): what every row was decided by."
    )
    rules: dict[str, Any] = Field(description="The rules applied; a check not applied is null.")
    messages: tuple[MessageCheck, ...] = Field(
        description="Per action and planned channel: what was checked."
    )
    decisions_sha256: str = Field(description="sha256 of compliance_decisions.parquet as written.")
    evidence_id: str = Field(description="The evidence id of the signed ledger entry.")
    ledger_seq: int = Field(description="That entry's seq on the decision ledger.")
    ledger_entry_hash: str = Field(description="That entry's entry_hash (a checkpoint for verification).")
    note: str = Field(default=_NOTE)
    created_at: datetime
    # Plan K M145 (DEC-1412): a run whose message arms are in force. Absent otherwise.
    variant_messages: tuple[VariantMessageCheck, ...] | None = Field(
        default=None,
        description=(
            "Per variant arm, group and planned channel: what was checked of the words it sends. Absent unless "
            "the run's message arms are in force."
        ),
        exclude_if=lambda value: value is None,
    )
    withheld_by_arm: dict[str, int] | None = Field(
        default=None,
        description=(
            "Customers withheld per message arm drawn (the control template and each variant). Absent unless the "
            "run's message arms are in force."
        ),
        exclude_if=lambda value: value is None,
    )


@dataclass(frozen=True)
class GateOutcome:
    """What the gate did to the list, per scored row of the treat list."""

    treat: np.ndarray
    channel: np.ndarray
    summary: ComplianceDecisionsSummary


def _contactability(storage: Storage, run_id: str) -> tuple[pa.Table | None, str | None, Any]:
    """The run's contactability file (as written), its reference, and its summary (or None)."""
    import pyarrow.parquet as pq

    from engine.agentic.canonical import sha256_hex
    from engine.decide.contactability import (
        CHANNEL_CONTACTABILITY_FILENAME,
        CHANNEL_CONTACTABILITY_SUMMARY_FILENAME,
        ChannelContactability,
    )
    from engine.storage import StorageError, run_key

    key = run_key(run_id, CHANNEL_CONTACTABILITY_FILENAME)
    if not storage.exists(key):
        return None, None, None
    data = storage.read_bytes(key)
    table = pq.read_table(io.BytesIO(data))  # type: ignore[no-untyped-call]
    summary = None
    try:
        summary = storage.read_model(
            run_key(run_id, CHANNEL_CONTACTABILITY_SUMMARY_FILENAME), ChannelContactability
        )
    except (StorageError, ValueError):
        _LOGGER.warning("compliance gate %s: the contactability summary could not be read", run_id)
    return table, f"{CHANNEL_CONTACTABILITY_FILENAME}#sha256={sha256_hex(data)}", summary


def _region(root: Path, catalogue: ActionCatalogue | None) -> tuple[str | None, dict[str, Any] | None]:
    from engine.config import ConfigError, load_yaml
    from engine.decide.catalogue import REGIONS_DIRECTORY

    if catalogue is None or not catalogue.region:
        return None, None
    name = catalogue.region.strip().lower()
    path = root / REGIONS_DIRECTORY / f"{name}.yaml"
    if not path.is_file():
        return name, None
    try:
        return name, load_yaml(path)
    except ConfigError as exc:
        raise ComplianceError(exc.code, exc.message) from exc


def dlt_channels(region_data: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The channels whose region rule requires a DLT template (`<channel>_requires: [dlt_template_id, ...]`)."""
    if region_data is None:
        return ()
    found = [
        key[: -len("_requires")].lower()
        for key, value in region_data.items()
        if key.endswith("_requires") and isinstance(value, list) and DLT_FIELD in value
    ]
    return tuple(sorted(found))


def message_checks(
    catalogue: ActionCatalogue | None,
    catalogue_sha256: str | None,
    planned: Mapping[str | None, tuple[str, ...]],
    *,
    dlt_channels: tuple[str, ...],
    whitelist: Any,
    var_max_chars: int | None,
) -> tuple[dict[tuple[str, str], MessageVerdict], tuple[MessageCheck, ...]]:
    """The per-message verdicts of every (action, planned channel), and what was checked of each."""
    from engine.compliance.dlt import TemplateMatcher, parse_template
    from engine.compliance.urls import find_links

    verdicts: dict[tuple[str, str], MessageVerdict] = {}
    checks: list[MessageCheck] = []
    if catalogue is None:
        return verdicts, ()
    for action_id in sorted(key for key in planned if key is not None):
        item = catalogue.get_action(action_id)
        if item is None:
            continue
        matcher = None
        if item.dlt_template_text is not None:
            matcher = TemplateMatcher(parse_template(item.dlt_template_text), var_max_chars=var_max_chars)
        for channel in planned[action_id]:
            notes: list[str] = []
            mismatch: bool | None = None
            if channel not in dlt_channels:
                notes.append(f"The region does not require a DLT template on {channel}: no template check.")
            elif matcher is None:
                notes.append("The action has no dlt_template_text: the template was not checked.")
            elif item.message_text is None:
                notes.append("The action has no message_text: the template was not checked.")
            else:
                mismatch = not matcher.matches(item.message_text)
            bad: int | None = None
            if whitelist is None:
                notes.append("No URL whitelist is configured: links were not checked.")
            elif not whitelist.applies_to(channel):
                notes.append(f"The URL whitelist does not cover {channel}: links were not checked.")
            elif item.message_text is None:
                notes.append("The action has no message_text: links were not checked.")
            else:
                bad = sum(1 for url in find_links(item.message_text) if not whitelist.allows(url))
            verdicts[(action_id, channel)] = MessageVerdict(
                template_mismatch=bool(mismatch),
                url_not_whitelisted=bool(bad),
                catalogue_ref=f"{catalogue_sha256}#{action_id}",
            )
            checks.append(
                MessageCheck(
                    action_id=action_id,
                    channel=channel,
                    template_checked=mismatch is not None,
                    template_mismatch=mismatch,
                    urls_checked=bad is not None,
                    urls_not_whitelisted=bad,
                    notes=tuple(notes),
                )
            )
    return verdicts, tuple(checks)


def _opt_ins(
    storage: Storage,
    *,
    customers: pa.Table,
    key_columns: Sequence[str],
    planned: Mapping[str | None, tuple[str, ...]],
    opt_in_channels: tuple[str, ...],
    use_case_id: str,
    client_id: str | None,
    config_root: Path,
    at: datetime,
) -> pa.Table | None:
    """The opt-in grants of the customers who plan an opt-in channel; None when no ledger gates the run."""
    import numpy as np
    import pandas as pd
    import pyarrow as pa

    from engine.compliance.optin import channel_grants
    from engine.privacy.consent import consent_gate_for_run, principal_key

    needed = [
        channel for channel in opt_in_channels if any(channel in channels for channels in planned.values())
    ]
    if not needed or customers.num_rows == 0:
        return None
    gate = consent_gate_for_run(
        storage, use_case_id=use_case_id, client_id=client_id, config_root=config_root
    )
    if gate is None:
        return None
    entity = np.asarray(customers[key_columns[0]].to_numpy(zero_copy_only=False), dtype=object)
    principals = np.asarray([principal_key(value) for value in entity], dtype=object)
    distinct = sorted(set(principals.tolist()))
    pieces: list[pa.Table] = []
    for channel in needed:
        grants = channel_grants(
            gate.ledger,
            client_id=gate.client_id,
            purpose=gate.purpose,
            principals=distinct,
            channel=channel,
            at=at,
        )
        if not grants:
            continue
        seq = pd.Series(grants, dtype="float64").reindex(principals).to_numpy(dtype=np.float64)
        rows = np.flatnonzero(~np.isnan(seq))
        picked = customers.select(list(key_columns)).take(pa.array(rows))
        picked = picked.append_column("channel", pa.repeat(pa.scalar(channel, pa.string()), len(rows)))
        pieces.append(picked.append_column(OPT_IN_SEQ_COLUMN, pa.array(seq[rows].astype(np.int64))))
    if not pieces:
        return (
            customers.select(list(key_columns))
            .slice(0, 0)
            .append_column("channel", pa.array([], pa.string()))
            .append_column(OPT_IN_SEQ_COLUMN, pa.array([], pa.int64()))
        )
    return pa.concat_tables(pieces)


def _default_ledger(storage: Storage, config_root: Path) -> DecisionLedger:
    """The deployment's decision ledger and its development signer (see the module docstring)."""
    from engine.agentic.config import policy_version
    from engine.agentic.errors import AgenticError
    from engine.agentic.ledger import DecisionLedger, signer_for_data_dir
    from engine.platform_db import PLATFORM_DB_FILENAME, platform_engine, sqlite_engine
    from engine.settings import load_settings
    from engine.storage import LocalStorage

    settings = load_settings()
    data_dir = storage.root if isinstance(storage, LocalStorage) else settings.data_dir
    if settings.metadata_backend == "sqlite":
        engine = sqlite_engine(data_dir / PLATFORM_DB_FILENAME)
    else:
        engine = platform_engine(settings)
    try:
        return DecisionLedger(
            engine, signer=signer_for_data_dir(data_dir), policy_version=policy_version(config_root)
        )
    except AgenticError as exc:
        raise ComplianceError(exc.code, exc.message) from exc


def _withheld(decisions: pa.Table, parent: np.ndarray, withheld: np.ndarray) -> dict[str, int]:
    """Customers withheld, per reason: each customer once per reason among their rows."""
    import numpy as np
    import pyarrow.compute as _pc

    pc: Any = _pc  # untyped: its stubs miss most kernels (as engine.decide.treat_list reads it)

    reasons = decisions["reasons"].combine_chunks()
    values = np.asarray(reasons.flatten().to_numpy(zero_copy_only=False), dtype=object)
    rows = np.asarray(pc.list_parent_indices(reasons).to_numpy(), dtype=np.int64)
    customer = parent[rows]
    keep = withheld[customer]
    counts: dict[str, int] = {}
    for code in REASON_ORDER:
        hit = keep & (values == code)
        if bool(hit.any()):
            counts[code] = int(np.unique(customer[hit]).size)
    return counts


def gate_treat_list(
    storage: Storage,
    *,
    run_id: str,
    use_case_id: str,
    client_id: str | None,
    key_frame: pd.DataFrame,
    treat: np.ndarray,
    channel: np.ndarray,
    actions: np.ndarray,
    planned_by_action: Mapping[str, tuple[str, ...]],
    configured: tuple[str, ...],
    stamp_sha256: str | None,
    config_root: Path | None,
    ledger: DecisionLedger | None = None,
    at: datetime | None = None,
    variants: VariantMessages | None = None,
) -> GateOutcome:
    """Gate the treat list of `run_id` (see the module docstring).

    `key_frame` holds the key columns as the treat list writes them (text), one row per scored row;
    `treat`, `channel` and `actions` (each row's catalogue action, None for none) are aligned to it, and so are
    `variants.arm` and `variants.unit` when the run's message arms are in force (Plan K M145).
    """
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    from engine.agentic.canonical import digest, sha256_hex
    from engine.agentic.errors import AgenticError
    from engine.agentic.ledger import EVIDENCE_LABEL
    from engine.agentic.registry import evidence_id_for
    from engine.compliance.config import load_compliance_config, load_url_whitelist
    from engine.compliance.dlt import DLT_TAGS
    from engine.config import ConfigError
    from engine.config import config_root as resolve_root
    from engine.decide.catalogue import catalogue_or_none, catalogue_sha256
    from engine.privacy.config import privacy_config_or_none
    from engine.storage import run_key
    from engine.utils.time import utc_now

    root = resolve_root(config_root)
    moment = at or utc_now()
    settings = load_compliance_config(root).gate
    key_columns = tuple(str(name) for name in key_frame.columns)

    # 1. The rules. Plan K M145: with message arms in force, tied to the catalogue the arms were drawn under.
    now_sha = catalogue_sha256(root)
    reference_sha = stamp_sha256 if variants is None else variants.catalogue_sha256
    changed = now_sha != reference_sha
    try:
        catalogue = None if changed else catalogue_or_none(root)
    except ConfigError as exc:
        raise ComplianceError(exc.code, exc.message) from exc
    region, region_data = _region(root, catalogue)
    dlt_on = dlt_channels(region_data)
    whitelist, whitelist_source = load_url_whitelist(root, region_data)
    contactability, contactability_ref, contact_summary = _contactability(storage, run_id)
    purpose = None if contact_summary is None else contact_summary.ledger_purpose
    if purpose is None:
        privacy = privacy_config_or_none(root)
        purpose = None if privacy is None else privacy.purpose_for(use_case_id)
    rules: dict[str, Any] = {
        "gate": "compliance_gate/1",
        "region": region,
        "region_rules_sha256": None if region_data is None else digest(region_data),
        "dlt": {
            "channels": list(dlt_on),
            "tags": list(DLT_TAGS),
            "var_max_chars": settings.dlt_var_max_chars,
        },
        "url_whitelist": (
            None
            if whitelist is None
            else {
                "source": whitelist_source,
                "channels": None if whitelist.channels is None else list(whitelist.channels),
                "urls": list(whitelist.urls),
            }
        ),
        "opt_in_channels": list(settings.opt_in_channels),
        "consent_basis_rules_sha256": (
            None if contact_summary is None else contact_summary.consent_basis_rules_sha256
        ),
    }
    if variants is not None:
        rules["variant_arms"] = variants.rules()
    rule_version = digest(rules)

    # 2. The customers the list would contact, and their planned channels. Plan K M145: a variant customer's
    # message is their arm's words, under the gate's own key for (arm, group, band action).
    treated = np.flatnonzero(np.asarray(treat, dtype=bool))
    band_actions = np.asarray(actions, dtype=object)[treated]
    plan = None if variants is None else _variant_plan(variants, treated, band_actions)
    row_actions = band_actions if plan is None else plan.row_actions
    customers = pa.Table.from_arrays(
        [
            *(pa.array(key_frame[name].to_numpy(dtype=object)[treated], pa.string()) for name in key_columns),
            pa.array(row_actions, pa.string()),
        ],
        names=[*key_columns, ACTION_COLUMN],
    )
    present = sorted({str(action) for action in row_actions if action is not None})
    if plan is not None:
        present = [action for action in present if action not in plan.keys]
    planned: dict[str | None, tuple[str, ...]] = {None: tuple(configured)}
    for action in present:
        planned[action] = tuple(planned_by_action.get(action, configured))

    verdicts, checks = message_checks(
        catalogue,
        now_sha,
        planned,
        dlt_channels=dlt_on,
        whitelist=whitelist,
        var_max_chars=settings.dlt_var_max_chars,
    )
    variant_checks = None
    if plan is not None:
        variant_checks = _variant_verdicts(
            plan,
            planned,
            verdicts,
            catalogue=catalogue,
            catalogue_sha=now_sha,
            planned_by_action=planned_by_action,
            configured=tuple(configured),
            dlt_channels=dlt_on,
            whitelist=whitelist,
            var_max_chars=settings.dlt_var_max_chars,
        )
    opt_ins = _opt_ins(
        storage,
        customers=customers,
        key_columns=key_columns,
        planned=planned,
        opt_in_channels=settings.opt_in_channels,
        use_case_id=use_case_id,
        client_id=client_id,
        config_root=root,
        at=moment,
    )
    decisions = decide(
        customers,
        key_columns=key_columns,
        planned=planned,
        rules=GateRules(
            rule_version=rule_version,
            purpose=purpose,
            contactability_ref=contactability_ref,
            opt_in_channels=settings.opt_in_channels,
            catalogue_changed=changed,
        ),
        contactability=contactability,
        opt_ins=opt_ins,
        messages=verdicts,
    )

    # 3. The list: the first planned channel allowed, else not treated.
    exploded = explode(customers, planned)
    if plan is not None:
        decisions = _variant_columns(decisions, plan, exploded)
    allowed = np.asarray(decisions["allowed"].to_numpy(zero_copy_only=False), dtype=bool)
    open_rows = np.flatnonzero(allowed)
    sent, first = np.unique(exploded.parent[open_rows], return_index=True)
    kept = np.zeros(len(treated), dtype=bool)
    kept[sent] = True
    new_channel = np.full(len(treated), None, dtype=object)
    new_channel[sent] = exploded.channel[open_rows[first]]
    old_channel = np.asarray(channel, dtype=object)[treated]
    treat_out = np.asarray(treat, dtype=bool).copy()
    treat_out[treated[~kept]] = False
    channel_out = np.asarray(channel, dtype=object).copy()
    channel_out[treated] = new_channel
    rerouted = int((kept & (new_channel != old_channel)).sum())
    withheld = _withheld(decisions, exploded.parent, ~kept)
    no_evidence = decisions["ledger_ref"].is_null().to_numpy(zero_copy_only=False) & ~decisions[
        "channel"
    ].is_null().to_numpy(zero_copy_only=False)
    withheld_by_arm = None
    if plan is not None and variants is not None:
        withheld_by_arm = _withheld_by_arm(plan, variants.levels, ~kept)

    # 4. The record: the file, the signed ledger entry, the summary.
    buffer = io.BytesIO()
    pq.write_table(decisions, buffer, compression="snappy")  # type: ignore[no-untyped-call]
    data = buffer.getvalue()
    file_sha = sha256_hex(data)
    file_key = run_key(run_id, COMPLIANCE_DECISIONS_FILENAME)
    storage.write_bytes(file_key, data)
    counts = {
        "customers": len(treated),
        "rows": decisions.num_rows,
        "allowed_rows": int(allowed.sum()),
        "withheld_rows": int((~kept).sum()),
        "rerouted_rows": rerouted,
        "withheld": withheld,
    }
    given = {"run_id": run_id, "rule_version": rule_version, "catalogue_sha256": now_sha}
    output = {"artefact_sha256": file_sha, **counts}
    evidence_id = evidence_id_for(GATE_TOOL, given, output)
    try:
        book = ledger if ledger is not None else _default_ledger(storage, root)
        entry = book.append(
            EVIDENCE_LABEL,
            GATE_ACTOR,
            {
                "evidence_id": evidence_id,
                "tool": GATE_TOOL,
                "label": EVIDENCE_LABEL_TEXT,
                "input_digest": digest(given),
                "output_digest": digest(output),
                "artefact_refs": [file_key],
                "artefact_sha256": file_sha,
                "rule_version": rule_version,
                "run_id": run_id,
                "use_case_id": use_case_id,
                "codes": sorted(withheld),
            },
            at=moment,
        )
    except AgenticError as exc:
        raise ComplianceError(exc.code, exc.message) from exc
    summary = ComplianceDecisionsSummary(
        run_id=run_id,
        use_case_id=use_case_id,
        purpose=purpose,
        customers=len(treated),
        rows=decisions.num_rows,
        allowed_rows=counts["allowed_rows"],
        withheld_rows=counts["withheld_rows"],
        rerouted_rows=rerouted,
        withheld=withheld,
        rows_without_consent_evidence=int(no_evidence.sum()),
        rule_version=rule_version,
        rules=rules,
        messages=checks,
        decisions_sha256=file_sha,
        evidence_id=evidence_id,
        ledger_seq=entry.seq,
        ledger_entry_hash=entry.entry_hash,
        created_at=moment,
        variant_messages=variant_checks,
        withheld_by_arm=withheld_by_arm,
    )
    storage.write_model(run_key(run_id, COMPLIANCE_SUMMARY_FILENAME), summary)
    _LOGGER.info(
        "compliance gate %s: customers=%d rows=%d allowed=%d withheld=%d rerouted=%d rule_version=%s ledger_seq=%d",
        run_id,
        len(treated),
        decisions.num_rows,
        counts["allowed_rows"],
        counts["withheld_rows"],
        rerouted,
        rule_version[:12],
        entry.seq,
    )
    return GateOutcome(treat=treat_out, channel=channel_out, summary=summary)


# ---------------------------------------------------------------------------
# Plan K M145 (DEC-1412): message arms in force - each customer checked on the words they will receive
# ---------------------------------------------------------------------------
VARIANT_ARM_COLUMN: Final[str] = "variant_arm"
"""`compliance_decisions.parquet`, on a run whose message arms are in force: the arm drawn for the customer."""
_VARIANT_KEY: Final[str] = "\x1fvariant\x1f"
"""Starts the gate's own key for a variant customer's message (arm, group, band action). A catalogue action id is
never spelt with a control character, so the key cannot be taken for one; it never reaches a file."""


@dataclass(frozen=True)
class _VariantPlan:
    """The contacted customers' messages on a run with message arms, aligned to the gate's `customers`.

    Everything per customer is an integer code into a short list, so the work over the customers is column work.
    """

    row_actions: np.ndarray
    """Per contacted customer: their band's action, or for a variant customer the gate's variant key."""
    arm_code: np.ndarray
    """Per contacted customer: the position of the arm drawn in `arm_names`, -1 outside the arms."""
    arm_names: list[str]
    band_code: np.ndarray
    """Per contacted customer: the position of their band's action in `band_names`, -1 for none."""
    band_names: list[str | None]
    key_code: np.ndarray
    """Per contacted customer: the position of their variant key in `key_values`, -1 for no variant."""
    key_values: list[tuple[str, str, str | None]]
    """Per variant key: (arm, group, band action)."""
    keys: dict[str, tuple[str, str, str | None]]
    """Variant key -> (arm, group, band action)."""
    words: Mapping[tuple[str, str, str], VariantWords]


def _codes(values: np.ndarray) -> tuple[np.ndarray, list[Any]]:
    import numpy as np
    import pandas as pd

    codes, uniques = pd.factorize(pd.Series(values, dtype=object), use_na_sentinel=True)
    return np.asarray(codes, dtype=np.int64), list(uniques)


def _variant_plan(variants: VariantMessages, treated: np.ndarray, band_actions: np.ndarray) -> _VariantPlan:
    """Each contacted customer's message: a variant customer's is keyed by (arm, group, band action), each key
    once (see `_VariantPlan`)."""
    import numpy as np

    from engine.compliance.variants import is_variant_level

    arm_code, arm_names = _codes(np.asarray(variants.arm, dtype=object)[treated])
    unit_code, unit_names = _codes(np.asarray(variants.unit, dtype=object)[treated])
    band_code, band_names = _codes(np.asarray(band_actions, dtype=object))
    variant_arm = np.asarray([is_variant_level(name) for name in arm_names] + [False], dtype=bool)
    rows = np.flatnonzero(variant_arm[arm_code])  # -1 reads the trailing False
    combined = (arm_code[rows] * (len(unit_names) + 1) + unit_code[rows] + 1) * (len(band_names) + 1)
    combined = combined + band_code[rows] + 1
    distinct, inverse = np.unique(combined, return_inverse=True)
    key_values: list[tuple[str, str, str | None]] = []
    for value in distinct.tolist():
        rest, band = divmod(int(value), len(band_names) + 1)
        arm, unit = divmod(rest, len(unit_names) + 1)
        action = None if band == 0 else band_names[band - 1]
        key_values.append((str(arm_names[arm]), str(unit_names[unit - 1]) if unit else "", action))
    names = [_VARIANT_KEY + "\x1f".join((arm, unit, action or "")) for arm, unit, action in key_values]
    key_code = np.full(len(arm_code), -1, dtype=np.int64)
    key_code[rows] = np.asarray(inverse, dtype=np.int64).reshape(-1)
    row_actions = np.asarray(band_actions, dtype=object).copy()
    if len(rows):
        row_actions[rows] = np.asarray(names, dtype=object)[key_code[rows]]
    return _VariantPlan(
        row_actions=row_actions,
        arm_code=arm_code,
        arm_names=[str(name) for name in arm_names],
        band_code=band_code,
        band_names=[None if name is None else str(name) for name in band_names],
        key_code=key_code,
        key_values=key_values,
        keys=dict(zip(names, key_values, strict=True)),
        words=variants.words,
    )


def _withheld_by_arm(plan: _VariantPlan, levels: Sequence[str], withheld: np.ndarray) -> dict[str, int]:
    """Customers withheld per arm drawn, every arm named (0 when none of its customers was withheld)."""
    import numpy as np

    hit = plan.arm_code[withheld & (plan.arm_code >= 0)]
    counts = np.bincount(hit, minlength=len(plan.arm_names))
    by_name = {name: int(counts[position]) for position, name in enumerate(plan.arm_names)}
    return {level: by_name.get(level, 0) for level in levels}


def _variant_verdicts(
    plan: _VariantPlan,
    planned: dict[str | None, tuple[str, ...]],
    verdicts: dict[tuple[str, str], MessageVerdict],
    *,
    catalogue: ActionCatalogue | None,
    catalogue_sha: str | None,
    planned_by_action: Mapping[str, tuple[str, ...]],
    configured: tuple[str, ...],
    dlt_channels: tuple[str, ...],
    whitelist: Any,
    var_max_chars: int | None,
) -> tuple[VariantMessageCheck, ...]:
    """Plan every variant key on its band action's channels (as the band's own customers are planned), add its
    verdicts - checked once per arm, group and channel - to `verdicts`, and return what was checked."""
    from engine.compliance.variants import variant_message_checks

    wanted: list[tuple[str, str, str]] = []
    for name, (level, group, action) in plan.keys.items():
        channels = configured if action is None else tuple(planned_by_action.get(action, configured))
        planned[name] = tuple(channels)
        wanted.extend((level, group, channel) for channel in channels)
    found, checks = variant_message_checks(
        catalogue,
        catalogue_sha,
        wanted,
        plan.words,
        dlt_channels=dlt_channels,
        whitelist=whitelist,
        var_max_chars=var_max_chars,
    )
    for name, (level, group, _action) in plan.keys.items():
        for channel in planned[name]:
            verdict = found.get((level, group, channel))
            if verdict is not None:
                verdicts[(name, channel)] = verdict
    return checks


def _variant_columns(decisions: pa.Table, plan: _VariantPlan, exploded: Any) -> pa.Table:
    """The decision file of a run with message arms: a variant row names its variant's own catalogue action on
    the row's channel (null where the variant has no words there), and every row the arm drawn for its customer.

    Built by codes: each row's action is one of the bands' actions or one per (variant key, channel), so the
    column is a `take` from a short list of names.
    """
    import numpy as np
    import pyarrow as pa

    parent = np.asarray(exploded.parent, dtype=np.int64)
    key = plan.key_code[parent]
    channel_code, channel_names = _codes(np.asarray(exploded.channel, dtype=object))
    width = len(channel_names) + 1
    variant_rows = key >= 0
    pairs, inverse = np.unique(
        key[variant_rows] * width + channel_code[variant_rows] + 1, return_inverse=True
    )
    named: list[str | None] = []
    for value in pairs.tolist():
        position, channel = divmod(int(value), width)
        level, group, _action = plan.key_values[position]
        words = None if channel == 0 else plan.words.get((level, group, str(channel_names[channel - 1])))
        named.append(None if words is None else words.catalogue_action_id)
    names = [*plan.band_names, *named]
    code = plan.band_code[parent]
    code[variant_rows] = len(plan.band_names) + np.asarray(inverse, dtype=np.int64).reshape(-1)
    actions = pa.array(names, pa.string()).take(pa.array(code, mask=code < 0))
    out = decisions.set_column(decisions.schema.get_field_index(ACTION_COLUMN), ACTION_COLUMN, actions)
    arm = plan.arm_code[parent]
    arms = pa.array(plan.arm_names, pa.string()).take(pa.array(arm, mask=arm < 0))
    return out.append_column(VARIANT_ARM_COLUMN, arms)
