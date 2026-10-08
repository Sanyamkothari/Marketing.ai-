"""Per-channel contactability of a scoring run (Plan J M99, DEC-1309).

A customer may opt out of SMS and stay in for email. That is not a suppression reason - the customer
keeps their score, band and action in `scores.csv`, and the three Phase 1 reasons and their precedence
(DEC-A2) do not change - it only decides which channels the treat list may use. This module works it
out **during the run**, after the actions stage, because that is the last moment the inputs exist:
`scores.csv` and `scores.parquet` carry `scores_csv_columns()` and nothing else (DEC-083), and the
consent ledger is read as of the run.

**Only when configured.** It runs only when the use case sets `actions.suppression.channels`
(`{channel: {consent_column, contactable_column}}`). With the default configuration the stage table is
returned untouched: nothing is read, nothing is written, and the run is byte for byte what it was.

**What makes a customer contactable on a channel.** All of, for each configured channel:

* the channel's `consent_column` is truthy, read from the rows **as uploaded** (joined by the run's row
  key) with Phase 1's rule (:func:`truthy`, which is `engine.stages.actions`' own): a null is not a
  consent;
* its `contactable_column` is truthy, the same way;
* when the consent ledger gates the run (`engine.privacy.consent.consent_gate_for_run`, the same lookup
  the actions stage makes), the ledger gives the customer valid consent for the run's purpose **on
  that channel** (`ConsentLedger.classify(..., channel=ch)`): the latest of the customer's all-channel
  records (`channel` null, which every record from before M99 is) and their records for that channel.

A configured column the scoring file lacks is skipped with a warning and named in the summary, as a
Phase 1 suppression column is (DEC-030). With an entity key (a periodic file), a customer not
contactable on a channel at any snapshot is not contactable on it at all, as suppression is decided
per entity (DEC-083).

**What it writes.**

* `channel_contactability.parquet` - one row per scored row: the key column(s), as `scores.*` writes
  them, then one boolean `contactable_<channel>` per configured channel, in configuration order. A
  row-level artefact (`configs/privacy.yaml`, `engine/privacy/layout.py`, `ROW_LEVEL_ARTEFACTS`).
* `channel_contactability.json` (:class:`ChannelContactability`) - counts only, including
  `channel_counts`: per channel, the rows **eligible to be treated** (neither suppressed nor held out
  as control) that are not contactable on it.
* The same `channel_counts` in the frame's `attrs`, for `engine.stages.export._suppression_counts` to
  put on the `opted_out` entry, else the `consent_false` entry, of `scoring_summary.json` - never on an
  entry of its own.

The treat list (`engine.decide.treat_list`) joins the parquet file on every key column to choose each
treated customer's channel; a customer it does not cover is treated as before, with a null
`contactable_channels`.

**The catalogue the run ran under.** When `configs/decide/catalogue.yaml` exists, the same seam writes
`catalogue_stamp.json` (`engine.decide.catalogue.CatalogueStamp`: the file's sha256 and the channels of
each action the use case names), with or without channels configured. The treat list is built later, on
demand, and plans channels from that stamp, never from the file as it is then. With no catalogue and no
channels the seam returns the stage table untouched.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from pydantic import Field

from engine.contracts import Artefact
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import numpy as np
    import pandas as pd

    from engine.config import UseCaseConfig

__all__ = [
    "CHANNEL_CONTACTABILITY_FILENAME",
    "CHANNEL_CONTACTABILITY_SUMMARY_FILENAME",
    "CHANNEL_COUNTS_ATTR",
    "CONTACTABLE_PREFIX",
    "ChannelContactability",
    "ChannelSource",
    "contactability_masks",
    "contactable_column",
    "install_contactability",
    "truthy",
]

_LOGGER = get_logger(__name__)

CHANNEL_CONTACTABILITY_FILENAME: Final[str] = "channel_contactability.parquet"
CHANNEL_CONTACTABILITY_SUMMARY_FILENAME: Final[str] = "channel_contactability.json"
CONTACTABLE_PREFIX: Final[str] = "contactable_"
CHANNEL_COUNTS_ATTR: Final[str] = "plan_j_channel_counts"
"""`frame.attrs` key: `{channel: rows}` for `engine.stages.export._suppression_counts`."""
_INSTALLED: Final[str] = "_contactability_installed"

CONTACTABILITY_NOTE: Final[str] = (
    "A customer not contactable on a channel is not suppressed: they keep their score, band and action "
    "in scores.csv, and the treat list only never sends them anything on that channel. channel_counts "
    "counts, per channel, the customers neither suppressed nor held out as control who are not "
    "contactable on it."
)


class ChannelSource(Artefact):
    """What decided one channel's contactability in this run, and how many it left out."""

    channel: str = Field(description="The channel, as `actions.suppression.channels` names it.")
    consent_column: str | None = Field(
        description="The consent column read, or null when none was configured."
    )
    contactable_column: str | None = Field(
        description="The contactable column read, or null when none was configured."
    )
    columns_missing: tuple[str, ...] = Field(
        default=(), description="Configured columns the scoring file lacks; skipped with a warning."
    )
    ledger: bool = Field(description="Whether the consent ledger's records for this channel were applied.")
    contactable_rows: int = Field(description="Rows contactable on the channel.")
    not_contactable_eligible_rows: int = Field(
        description="Rows neither suppressed nor held out as control that are not contactable on the channel."
    )


class ChannelContactability(Artefact):
    """`channel_contactability.json`: counts behind `channel_contactability.parquet` (Plan J M99)."""

    run_id: str = Field(description="Scoring run the figures belong to.")
    use_case_id: str = Field(description="Use case of the run.")
    channels: tuple[str, ...] = Field(description="Configured channels, in order of preference.")
    rows: int = Field(description="Rows scored.")
    eligible_rows: int = Field(description="Rows neither suppressed nor held out as control.")
    ledger_purpose: str | None = Field(
        description="The consent purpose whose ledger was read per channel, or null when no ledger gates the run."
    )
    channel_counts: dict[str, int] = Field(
        description="Per channel: eligible rows not contactable on it (not suppressed; only not treated on it)."
    )
    by_channel: tuple[ChannelSource, ...] = Field(description="What decided each channel, in order.")
    note: str = Field(default=CONTACTABILITY_NOTE, description="What the counts are, and what they are not.")
    created_at: datetime = Field(description="UTC time the file was written.")


def truthy(values: pd.Series) -> pd.Series:
    """Which values count as true for a consent or contactable column: Phase 1's own rule.

    `engine.stages.actions._truthy` read through one documented import, so a channel's column is read
    exactly as the suppression rules read a consent column (a null is not a consent), and Plan J edits
    nothing in that Phase 1 file to share it.
    """
    from engine.stages.actions import _truthy  # Phase 1's rule; private there, shared only from here

    return _truthy(values)


def contactable_column(channel: str) -> str:
    """The parquet column holding one channel's flag."""
    return f"{CONTACTABLE_PREFIX}{channel}"


def contactability_masks(
    banded: pd.DataFrame,
    uploaded: pd.DataFrame | None,
    config: UseCaseConfig,
    *,
    row_key: str,
    entity_key: str | None = None,
    ledger_valid: Mapping[str, np.ndarray] | None = None,
) -> tuple[dict[str, np.ndarray], tuple[tuple[str, tuple[str, ...]], ...]]:
    """Per configured channel, a boolean per row of `banded`: contactable on that channel.

    `uploaded` is the rows as uploaded, joined by `row_key` (a column the scored frame lost is read
    from there; a column it still has is read from the upload too, so a replayed feature's clipping
    never changes a consent). `ledger_valid` is the ledger's verdict per channel, aligned to `banded`.
    Returns the masks and, per channel, the configured columns that were missing.
    """
    import numpy as np

    from engine.keys import key_text

    n_rows = len(banded.index)
    masks: dict[str, np.ndarray] = {}
    missing: list[tuple[str, tuple[str, ...]]] = []
    for channel, columns in config.actions.suppression.channels.items():
        mask = np.ones(n_rows, dtype=bool)
        absent: list[str] = []
        for name in (columns.consent_column, columns.contactable_column):
            if name is None:
                continue
            values = _column(name, banded, uploaded, row_key)
            if values is None:
                absent.append(name)
                continue
            mask &= truthy(values).to_numpy(dtype=bool)
        if ledger_valid is not None and channel in ledger_valid:
            mask &= np.asarray(ledger_valid[channel], dtype=bool)
        if entity_key is not None and entity_key in banded.columns:
            import pandas as pd

            groups = key_text(banded[entity_key]).to_numpy()
            mask = pd.Series(mask).groupby(groups, sort=False).transform("min").to_numpy(dtype=bool)
        masks[channel] = mask
        missing.append((channel, tuple(absent)))
    return masks, tuple(missing)


def _column(name: str, banded: pd.DataFrame, uploaded: pd.DataFrame | None, row_key: str) -> pd.Series | None:
    """`name` per row of `banded`, read from the upload by row key; else from `banded`; else None."""
    import pandas as pd

    if (
        uploaded is not None
        and name in uploaded.columns
        and row_key in uploaded.columns
        and row_key in banded
    ):
        keys = uploaded[row_key].astype(str)
        lookup = pd.Series(uploaded[name].to_numpy(), index=keys.to_numpy())
        lookup = lookup[~lookup.index.duplicated(keep="first")]
        aligned = lookup.reindex(banded[row_key].astype(str).to_numpy())
        return pd.Series(aligned.to_numpy(), index=banded.index)
    if name in banded.columns:
        return banded[name]
    return None


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------
def install_contactability(score_flow: type[Any]) -> None:
    """Wrap `score_flow._bodies` so a run writes its contactability and catalogue stamp after actions.

    Idempotent. Both the propensity and the uplift score flows use the stage table, so one rebinding
    covers both. The wrapper returns the table untouched when `actions.suppression.channels` is empty
    and there is no `configs/decide/catalogue.yaml` (the default).
    """
    if getattr(score_flow, _INSTALLED, False):
        return
    original: Callable[[Any], tuple[tuple[Any, Callable[[], Any]], ...]] = score_flow._bodies

    def _bodies(self: Any) -> tuple[tuple[Any, Callable[[], Any]], ...]:
        bodies = original(self)
        config = getattr(getattr(self, "_ctx", None), "config", None)
        actions = getattr(config, "actions", None)
        suppression = getattr(actions, "suppression", None)
        if not getattr(suppression, "channels", None) and not _catalogue_present():
            return bodies
        return tuple((key, _after_actions(self, key, body)) for key, body in bodies)

    score_flow._bodies = _bodies
    setattr(score_flow, _INSTALLED, True)


def _catalogue_present() -> bool:
    from engine.config import config_root
    from engine.decide.catalogue import CATALOGUE_FILENAME

    return (config_root() / CATALOGUE_FILENAME).is_file()


def _after_actions(flow: Any, key: Any, body: Callable[[], Any]) -> Callable[[], Any]:
    from functools import wraps

    from engine.contracts import StageKey

    if key is not StageKey.ACTIONS:
        return body

    @wraps(body)
    def acted() -> Any:
        outcome = body()
        if flow._ctx.config.actions.suppression.channels:
            _write_contactability(flow)
        _write_catalogue_stamp(flow)
        return outcome

    return acted


def _write_catalogue_stamp(flow: Any) -> None:
    """`catalogue_stamp.json`, when the run's config root has a catalogue (the run-time config root)."""
    from engine.decide.catalogue import CATALOGUE_STAMP_FILENAME, catalogue_stamp
    from engine.utils.time import utc_now

    ctx = flow._ctx
    stamp = catalogue_stamp(ctx.config, run_id=ctx.run_id, created_at=utc_now())
    if stamp is None:
        return
    flow._write(CATALOGUE_STAMP_FILENAME, stamp)
    _LOGGER.info(
        "catalogue stamp sha256=%s actions=%s",
        stamp.catalogue_sha256[:12],
        ",".join(stamp.planned_channels) or "-",
    )


def _ledger_verdicts(
    flow: Any, banded: pd.DataFrame, channels: tuple[str, ...]
) -> tuple[dict[str, Any], str] | None:
    """Per channel, the ledger's valid-consent flag per row, when a ledger gates this run."""
    import pandas as pd

    from engine import keys
    from engine.privacy.consent import consent_gate_for_run, principal_key
    from engine.utils.time import utc_now

    ctx = flow._ctx
    gate = consent_gate_for_run(
        flow._storage, use_case_id=ctx.config.id, client_id=flow._run.record.client_id
    )
    if gate is None:
        return None
    principals = pd.Series(
        [principal_key(value) for value in banded[keys.entity_column(ctx.primary_key)].tolist()]
    )
    at = utc_now()
    verdicts: dict[str, Any] = {}
    for channel in channels:
        verdict = gate.ledger.classify(gate.client_id, gate.purpose, principals.tolist(), at, channel=channel)
        verdicts[channel] = principals.isin(verdict.valid).to_numpy(dtype=bool)
    return verdicts, gate.purpose


def _write_contactability(flow: Any) -> None:
    import io

    import numpy as np
    import pandas as pd

    from engine.stages.actions import CONTROL_GROUP_COLUMN, SUPPRESSED_REASON_COLUMN
    from engine.stages.export import _key_output
    from engine.storage import run_key
    from engine.utils.time import utc_now

    ctx = flow._ctx
    banded = flow._scored
    if banded is None:  # the actions body always sets it; a body that did not has failed on its own
        raise RuntimeError("the actions stage produced no scored rows")
    channels = tuple(ctx.config.actions.suppression.channels)
    ledger = _ledger_verdicts(flow, banded, channels)
    masks, missing = contactability_masks(
        banded,
        flow._frame,
        ctx.config,
        row_key=ctx.row_key,
        entity_key=ctx.entity_key,
        ledger_valid=None if ledger is None else ledger[0],
    )
    eligible = banded[SUPPRESSED_REASON_COLUMN].isna().to_numpy(dtype=bool) & ~banded[
        CONTROL_GROUP_COLUMN
    ].astype(bool).to_numpy(dtype=bool)
    counts = {channel: int((eligible & ~masks[channel]).sum()) for channel in channels}

    data: dict[str, pd.Series] = dict(_key_output(banded, ctx.key, key_source=flow._frame))
    for channel in channels:
        data[contactable_column(channel)] = pd.Series(masks[channel], index=banded.index, dtype=bool)
    table = pd.DataFrame(data).reset_index(drop=True)
    buffer = io.BytesIO()
    table.to_parquet(buffer, engine="pyarrow", index=False)
    parquet_key = run_key(ctx.run_id, CHANNEL_CONTACTABILITY_FILENAME)
    flow._storage.write_bytes(parquet_key, buffer.getvalue())
    flow._artefacts[CHANNEL_CONTACTABILITY_FILENAME] = parquet_key

    columns = ctx.config.actions.suppression.channels
    absent = dict(missing)
    for channel, names in missing:
        for name in names:
            _LOGGER.warning(
                "contactability: channel %s column %r is not in the scoring file; skipped", channel, name
            )
    flow._write(
        CHANNEL_CONTACTABILITY_SUMMARY_FILENAME,
        ChannelContactability(
            run_id=ctx.run_id,
            use_case_id=ctx.config.id,
            channels=channels,
            rows=len(banded.index),
            eligible_rows=int(eligible.sum()),
            ledger_purpose=None if ledger is None else ledger[1],
            channel_counts=counts,
            by_channel=tuple(
                ChannelSource(
                    channel=channel,
                    consent_column=columns[channel].consent_column,
                    contactable_column=columns[channel].contactable_column,
                    columns_missing=absent[channel],
                    ledger=ledger is not None,
                    contactable_rows=int(np.count_nonzero(masks[channel])),
                    not_contactable_eligible_rows=counts[channel],
                )
                for channel in channels
            ),
            created_at=utc_now(),
        ),
    )
    banded.attrs[CHANNEL_COUNTS_ATTR] = dict(counts)
    _LOGGER.info(
        "contactability channels=%s ledger=%s eligible=%d not_contactable=%s",
        ",".join(channels),
        ledger is not None,
        int(eligible.sum()),
        ",".join(f"{channel}:{count}" for channel, count in counts.items()),
    )
