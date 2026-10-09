"""`configs/decide/catalogue.yaml`: the offer and channel catalogue (Plan J M99, DEC-1309).

Follows the `configs/privacy.yaml` pattern: validated by a frozen pydantic model and cached per config
root (and per file content, so an edited catalogue is read again).

**Absent by default.** The repository ships no catalogue: `configs/decide/catalogue.example.yaml` is a
labelled example, never read. With no `catalogue.yaml` nothing here runs - no action id is checked, no
cost is overridden, the treat list's `catalogue_sha256` is null - so every default run and config load
is what it was before M99 (DEC-1309 (h)).

**Region rule, data-driven.** `configs/regions/<region>.yaml` declares channel requirements (such as
`sms_requires: [dlt_template_id, message_category]`); catalogue validation applies the configured
region's list without naming any region in Python. A required field left as a placeholder in angle
brackets (`"<your DLT template id>"`, as the example writes it) counts as missing, so a copied example
is refused rather than sent.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from engine.config import ConfigError, config_root, load_yaml
from engine.contracts import Artefact
from engine.decide.spec import CHANNEL_NAME, CHANNEL_NAME_RULE
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from engine.config import UseCaseConfig
    from engine.storage import Storage

__all__ = [
    "CATALOGUE_FILENAME",
    "CATALOGUE_STAMP_FILENAME",
    "ActionCatalogue",
    "ActionItem",
    "CatalogueStamp",
    "StampedAction",
    "catalogue_or_none",
    "catalogue_sha256",
    "catalogue_stamp",
    "clear_catalogue_cache",
    "load_catalogue",
    "referenced_action_ids",
    "stamp_checked_catalogue",
    "stamped_at_creation",
    "validate_action_ids",
]

_LOGGER = get_logger(__name__)

CATALOGUE_FILENAME: Final[str] = "decide/catalogue.yaml"
REGIONS_DIRECTORY: Final[str] = "regions"

CATALOGUE_STAMP_FILENAME: Final[str] = "catalogue_stamp.json"
"""Run file: the catalogue a scoring run ran under (:class:`CatalogueStamp`)."""

_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"\s*<[^>]*>\s*")
"""A value an example leaves for the client to fill in, such as `<your DLT template id>`."""

_CACHE: dict[tuple[Path, str], ActionCatalogue] = {}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ActionItem(_Model):
    """One action (an offer sent on one or more channels) in the catalogue."""

    action_id: Annotated[
        str, Field(min_length=1, description="Machine-readable unique identifier of the action.")
    ]
    label: Annotated[str, Field(min_length=1, description="Human-readable business label.")]
    channels: Annotated[
        tuple[str, ...],
        Field(
            min_length=1,
            description=(
                "Channels the action can be sent on, in order of preference. A single `channel: sms` "
                "is accepted for a one-channel action."
            ),
        ),
    ]
    offer_cost: Annotated[float, Field(ge=0.0, description="Cost of the offer in rupees.")] = 0.0
    contact_cost: Annotated[
        float, Field(ge=0.0, description="Cost of one contact with this action, in rupees.")
    ] = 0.0
    eligibility: str | None = Field(default=None, description="Eligibility expression.")
    purpose: str | None = Field(default=None, description="Purpose id from privacy.yaml.")
    message_category: str | None = Field(
        default=None, description="Message category (promotional, service, transactional)."
    )
    dlt_template_id: str | None = Field(default=None, description="DLT template ID for SMS in India.")
    template_ref: str | None = Field(default=None, description="Template reference.")
    offer_id: str | None = Field(default=None, description="Offer identifier.")
    priority: int | None = Field(default=None, description="Priority score for arbitration.")

    @model_validator(mode="before")
    @classmethod
    def _coerce_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        mapped = dict(data)
        if "offer_cost_inr" in mapped and "offer_cost" not in mapped:
            mapped["offer_cost"] = mapped.pop("offer_cost_inr")
        if "contact_cost_inr" in mapped and "contact_cost" not in mapped:
            mapped["contact_cost"] = mapped.pop("contact_cost_inr")
        if "channel" in mapped:
            if "channels" in mapped:
                raise ValueError("give either `channel` (one channel) or `channels` (a list), not both")
            single = mapped.pop("channel")
            if isinstance(single, str) and "," in single:
                raise ValueError("`channel` names one channel; list several under `channels: [sms, email]`")
            mapped["channels"] = [single]
        return mapped

    @field_validator("channels", mode="before")
    @classmethod
    def _normalise_channels(cls, value: Any) -> Any:
        if isinstance(value, str):
            raise ValueError("`channels` is a list, such as `channels: [sms, email]`")
        if not isinstance(value, list | tuple):
            return value
        names = [str(item).strip().lower() for item in value]
        for name in names:
            if not CHANNEL_NAME.fullmatch(name):
                raise ValueError(f"{name!r} is not a channel name ({CHANNEL_NAME_RULE})")
        if len(set(names)) != len(names):
            raise ValueError("a channel is listed twice")
        return tuple(names)


class ActionCatalogue(_Model):
    """The whole of `configs/decide/catalogue.yaml`."""

    region: str | None = Field(
        default=None,
        description="Operating region (e.g. 'IN'); rules loaded from configs/regions/<region>.yaml.",
    )
    actions: tuple[ActionItem, ...] = Field(default=(), description="Actions declared in the catalogue.")

    @model_validator(mode="after")
    def _unique_action_ids(self) -> ActionCatalogue:
        seen: set[str] = set()
        duplicates: list[str] = []
        for action in self.actions:
            if action.action_id in seen:
                duplicates.append(action.action_id)
            seen.add(action.action_id)
        if duplicates:
            raise ValueError(f"Duplicate action_id in catalogue: {', '.join(sorted(set(duplicates)))}")
        return self

    @property
    def action_ids(self) -> tuple[str, ...]:
        """All declared action IDs."""
        return tuple(action.action_id for action in self.actions)

    @property
    def actions_by_id(self) -> dict[str, ActionItem]:
        """Map of action_id -> ActionItem."""
        return {action.action_id: action for action in self.actions}

    def get_action(self, action_id: str) -> ActionItem | None:
        """Find an action by action_id, or None when missing."""
        return self.actions_by_id.get(action_id)

    def channel_contact_costs(self) -> dict[str, float]:
        """Contact cost per channel: the first action (in file order) that lists the channel decides."""
        costs: dict[str, float] = {}
        for action in self.actions:
            for channel in action.channels:
                costs.setdefault(channel, action.contact_cost)
        return costs


def load_catalogue(root: Path | None = None) -> ActionCatalogue:
    """Load and validate `configs/decide/catalogue.yaml`, cached per root and file content.

    Raises `ConfigError` with:
    - `CONFIG_NOT_FOUND` when missing.
    - `CATALOGUE_INVALID` when the schema is invalid or a region's required field is missing.
    - `ACTION_DLT_TEMPLATE_MISSING` when the region requires a DLT template id an action lacks.
    """
    base = config_root(root)
    cat_path = base / CATALOGUE_FILENAME
    if not cat_path.is_file():
        raise ConfigError("CONFIG_NOT_FOUND", f"No catalogue file at {cat_path}.", path=str(cat_path))
    digest = hashlib.sha256(cat_path.read_bytes()).hexdigest()
    cached = _CACHE.get((base, digest))
    if cached is not None:
        return cached

    data = load_yaml(cat_path)
    try:
        loaded = ActionCatalogue.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        dotted = ".".join(str(part) for part in first["loc"]) or "catalogue"
        raise ConfigError("CATALOGUE_INVALID", f"{dotted}: {first['msg']}.", path=dotted) from exc

    if loaded.region:
        reg_file = base / REGIONS_DIRECTORY / f"{loaded.region.strip().lower()}.yaml"
        if reg_file.is_file():
            _apply_region_rules(loaded, load_yaml(reg_file))
        else:
            _LOGGER.warning(
                "catalogue region %r has no rules file; no channel requirement applied", loaded.region
            )

    _CACHE[(base, digest)] = loaded
    return loaded


def _missing(value: object) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and (not value.strip() or _PLACEHOLDER.fullmatch(value) is not None)


def _apply_region_rules(catalogue: ActionCatalogue, region_data: dict[str, Any]) -> None:
    """Validate catalogue actions against channel requirements from region config without naming any region in code."""
    for key, req_list in region_data.items():
        if not (key.endswith("_requires") and isinstance(req_list, list)):
            continue
        channel_name = key[: -len("_requires")].lower()
        for position, action in enumerate(catalogue.actions):
            if channel_name not in action.channels:
                continue
            for req in req_list:
                if not _missing(getattr(action, req, None)):
                    continue
                code = "ACTION_DLT_TEMPLATE_MISSING" if req == "dlt_template_id" else "CATALOGUE_INVALID"
                raise ConfigError(
                    code,
                    f"Action {action.action_id!r} is sent by {channel_name}, which region {catalogue.region!r} "
                    f"requires to have {req!r}; it is missing or still a placeholder.",
                    path=f"actions[{position}].{req}",
                )


def catalogue_or_none(root: Path | None = None) -> ActionCatalogue | None:
    """The action catalogue, or None when `configs/decide/catalogue.yaml` does not exist."""
    base = config_root(root)
    if not (base / CATALOGUE_FILENAME).is_file():
        return None
    return load_catalogue(root)


def catalogue_sha256(root: Path | None = None) -> str | None:
    """SHA-256 fingerprint of `configs/decide/catalogue.yaml`, or None when absent."""
    path = config_root(root) / CATALOGUE_FILENAME
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def referenced_action_ids(config: UseCaseConfig) -> tuple[str, ...]:
    """The catalogue action ids a use case names: each band's `action_id`, then the uplift treat action,
    then each offer's action (`uplift.policy.arm_action_ids`, Plan J M100 part B), in that order."""
    ids = [band.action_id for band in config.actions.bands if band.action_id is not None]
    uplift = config.uplift
    if uplift is not None and uplift.policy.treat_action_id is not None:
        ids.append(uplift.policy.treat_action_id)
    if uplift is not None:
        ids.extend(uplift.policy.arm_action_ids.values())
    return tuple(dict.fromkeys(ids))


def validate_action_ids(config: UseCaseConfig, *, root: Path | None = None) -> None:
    """Refuse a `Band.action_id`, `uplift.policy.treat_action_id` or an `uplift.policy.arm_action_ids`
    action (Plan J M100 part B) the catalogue lacks.

    Does nothing when there is no `catalogue.yaml` (the default). Called in place by
    `engine.config.load_use_case` and `resolve_config` (`CATALOGUE_ACTION_UNKNOWN`).
    """
    catalogue = catalogue_or_none(root)
    if catalogue is None:
        return
    known = set(catalogue.action_ids)
    listed = ", ".join(sorted(known)) or "none"
    for i, band in enumerate(config.actions.bands):
        if band.action_id is not None and band.action_id not in known:
            raise ConfigError(
                "CATALOGUE_ACTION_UNKNOWN",
                f"actions.bands[{i}] references unknown action_id {band.action_id!r}; "
                f"known actions in catalogue: {listed}.",
                path=f"actions.bands[{i}].action_id",
            )
    treat_action_id = None if config.uplift is None else config.uplift.policy.treat_action_id
    if treat_action_id is not None and treat_action_id not in known:
        raise ConfigError(
            "CATALOGUE_ACTION_UNKNOWN",
            f"uplift.policy.treat_action_id references unknown action_id {treat_action_id!r}; "
            f"known actions in catalogue: {listed}.",
            path="uplift.policy.treat_action_id",
        )
    arm_action_ids = {} if config.uplift is None else config.uplift.policy.arm_action_ids
    for level, action_id in arm_action_ids.items():
        if action_id not in known:
            raise ConfigError(
                "CATALOGUE_ACTION_UNKNOWN",
                f"uplift.policy.arm_action_ids names unknown action_id {action_id!r} for offer {level!r}; "
                f"known actions in catalogue: {listed}.",
                path=f"uplift.policy.arm_action_ids.{level}",
            )


class StampedAction(_Model):
    """One catalogue action as a run read it: what the offer is called, where it goes, what it costs."""

    label: str = Field(description="The action's business label.")
    channels: tuple[str, ...] = Field(description="Its channels, in order of preference.")
    offer_cost: float = Field(description="Cost of the offer, in rupees, paid by a customer who takes it.")
    contact_cost: float = Field(description="Cost of one contact, in rupees.")


class CatalogueStamp(Artefact):
    """`catalogue_stamp.json`: the catalogue a scoring run ran under (Plan J M99, DEC-1309).

    Written during the run, after the actions stage, only when a `catalogue.yaml` exists. The treat list
    is built later, on demand, and plans each band's channels from this record, not from the file as it
    is then, so an edit between the run and the first treat-list request changes nothing it says.
    """

    run_id: str = Field(description="Scoring run the stamp belongs to.")
    catalogue_sha256: str = Field(description="SHA-256 of configs/decide/catalogue.yaml as the run read it.")
    planned_channels: dict[str, tuple[str, ...]] = Field(
        description=(
            "Per catalogue action id the use case names (bands, uplift treat action, offers) and the "
            "catalogue declares: its channels, in order of preference."
        )
    )
    created_at: datetime = Field(description="UTC time the stamp was written.")
    actions: dict[str, StampedAction] = Field(
        default_factory=dict,
        description=(
            "Plan J M100 part B: the same actions' labels and costs as the run read them, so a choice "
            "of offer is priced from the catalogue the use case was checked against."
        ),
    )


def catalogue_stamp(
    config: UseCaseConfig, *, run_id: str, created_at: datetime, root: Path | None = None
) -> CatalogueStamp | None:
    """The stamp for a run of `config`, or None when there is no catalogue (nothing is then written)."""
    path = config_root(root) / CATALOGUE_FILENAME
    if not path.is_file():
        return None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    actions = load_catalogue(root).actions_by_id
    named = [action_id for action_id in referenced_action_ids(config) if action_id in actions]
    return CatalogueStamp(
        run_id=run_id,
        catalogue_sha256=digest,
        planned_channels={action_id: actions[action_id].channels for action_id in named},
        created_at=created_at,
        actions={
            action_id: StampedAction(
                label=actions[action_id].label,
                channels=actions[action_id].channels,
                offer_cost=actions[action_id].offer_cost,
                contact_cost=actions[action_id].contact_cost,
            )
            for action_id in named
        },
    )


def stamp_checked_catalogue(
    storage: Storage, run_id: str, config: UseCaseConfig, *, root: Path | None, created_at: datetime
) -> CatalogueStamp | None:
    """Write `catalogue_stamp.json` for a run from the config root its use case was checked against.

    Called by everything that creates a run (the routes, beside `run_config.json`, and a scheduled
    firing; :func:`stamped_at_creation` says which runs), with the root the configuration was resolved
    from (`create_app(config_root=...)`, else `MARKETING_AI_CONFIG_DIR`, else `configs/`; a firing's
    `FiringServices.config_root`). The run keeps a stamp it finds instead of stamping again from its own root, so the
    catalogue a run is stamped and priced with is the one its action ids were checked against, even
    when the API was given a root the run's process does not know (the M99 gap, DEC-1309). Nothing is
    written when that root has no catalogue.
    """
    from engine.storage import run_key

    stamp = catalogue_stamp(config, run_id=run_id, created_at=created_at, root=root)
    if stamp is not None:
        storage.write_model(run_key(run_id, CATALOGUE_STAMP_FILENAME), stamp)
    return stamp


def stamped_at_creation(config: UseCaseConfig, *, scoring: bool) -> bool:
    """Whether whoever creates a run stamps its catalogue (:func:`stamp_checked_catalogue`) before it starts.

    Every scoring run (its actions stage plans channels and, with several offers, prices them from the
    stamp), and a training run whose offers are mapped to catalogue actions (`arm_policy_value.json`
    records their costs). The routes that create runs (`POST /runs`, `POST /uplift/runs`) and a
    scheduled firing (`engine.scheduling.firing.start_dataset_run`) ask the same question, so no creator
    of a run leaves the run to stamp from a root other than the one its use case was checked against.
    """
    if scoring:
        return True
    return config.uplift is not None and bool(config.uplift.policy.arm_action_ids)


def clear_catalogue_cache() -> None:
    """Clear internal catalogue cache (for testing edits)."""
    _CACHE.clear()
