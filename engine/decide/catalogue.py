"""`configs/decide/catalogue.yaml`: the offer and channel catalogue (Plan J M99, DEC-1309).

Follows the `configs/privacy.yaml` pattern: validated by a frozen pydantic model, cached per config root,
and checked with data-driven region rules.

Region rule: `configs/regions/<region>.yaml` declares channel requirements (such as `sms_requires: [dlt_template_id, message_category]`);
catalogue validation applies the configured region's list without naming any region in Python.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from engine.config import ConfigError, config_root, load_yaml
from engine.utils.logging import get_logger

__all__ = [
    "CATALOGUE_FILENAME",
    "ActionCatalogue",
    "ActionItem",
    "catalogue_or_none",
    "catalogue_sha256",
    "clear_catalogue_cache",
    "load_catalogue",
]

_LOGGER = get_logger(__name__)

CATALOGUE_FILENAME: Final[str] = "decide/catalogue.yaml"

_CACHE: dict[Path, ActionCatalogue] = {}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ActionItem(_Model):
    """One action/offer item in the catalogue."""

    action_id: Annotated[
        str, Field(min_length=1, description="Machine-readable unique identifier of the action.")
    ]
    label: Annotated[str, Field(min_length=1, description="Human-readable business label.")]
    channel: Annotated[
        str, Field(min_length=1, description="Primary channel or comma-separated list of planned channels.")
    ]
    offer_cost: Annotated[float, Field(ge=0.0, description="Cost of the offer in rupees.")] = 0.0
    contact_cost: Annotated[
        float, Field(ge=0.0, description="Cost of contacting the customer on this channel in rupees.")
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
        if isinstance(data, dict):
            mapped = dict(data)
            if "offer_cost_inr" in mapped and "offer_cost" not in mapped:
                mapped["offer_cost"] = mapped.pop("offer_cost_inr")
            if "contact_cost_inr" in mapped and "contact_cost" not in mapped:
                mapped["contact_cost"] = mapped.pop("contact_cost_inr")
            return mapped
        return data

    @property
    def channels(self) -> tuple[str, ...]:
        """Parsed tuple of channels this action supports."""
        return tuple(c.strip().lower() for c in self.channel.split(",") if c.strip())


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


def load_catalogue(root: Path | None = None) -> ActionCatalogue:
    """Load and validate `configs/decide/catalogue.yaml`, cached per root.

    Raises `ConfigError` with:
    - `CONFIG_NOT_FOUND` when missing.
    - `CATALOGUE_INVALID` when schema is invalid or required region field is missing.
    - `ACTION_DLT_TEMPLATE_MISSING` when SMS in region IN is missing DLT template ID.
    """
    base = config_root(root)
    cached = _CACHE.get(base)
    if cached is not None:
        return cached

    cat_path = base / CATALOGUE_FILENAME
    if not cat_path.is_file():
        raise ConfigError("CONFIG_NOT_FOUND", f"No catalogue file at {cat_path}.", path=str(cat_path))

    data = load_yaml(cat_path)
    try:
        loaded = ActionCatalogue.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        dotted = ".".join(str(part) for part in first["loc"])
        raise ConfigError("CATALOGUE_INVALID", f"{dotted}: {first['msg']}.", path=dotted) from exc

    # Apply data-driven region rules
    if loaded.region:
        reg_file = base / "regions" / f"{loaded.region.strip().lower()}.yaml"
        if reg_file.is_file():
            reg_data = load_yaml(reg_file)
            _apply_region_rules(loaded, reg_data)

    _CACHE[base] = loaded
    return loaded


def _apply_region_rules(catalogue: ActionCatalogue, region_data: dict[str, Any]) -> None:
    """Validate catalogue actions against channel requirements from region config without naming any region in code."""
    for key, req_list in region_data.items():
        if key.endswith("_requires") and isinstance(req_list, list):
            channel_name = key[: -len("_requires")].lower()
            for action in catalogue.actions:
                if channel_name in action.channels:
                    for req in req_list:
                        val = getattr(action, req, None)
                        if val is None or (isinstance(val, str) and not val.strip()):
                            code = (
                                "ACTION_DLT_TEMPLATE_MISSING"
                                if req == "dlt_template_id"
                                else "CATALOGUE_INVALID"
                            )
                            raise ConfigError(
                                code,
                                f"Action {action.action_id!r} on channel {action.channel!r} is missing required field {req!r} "
                                f"for region {catalogue.region!r}.",
                                path=f"actions.{action.action_id}.{req}",
                            )


def catalogue_or_none(root: Path | None = None) -> ActionCatalogue | None:
    """The action catalogue, or None when `configs/decide/catalogue.yaml` does not exist."""
    base = config_root(root)
    if not (base / CATALOGUE_FILENAME).is_file():
        return None
    return load_catalogue(root)


def catalogue_sha256(root: Path | None = None) -> str | None:
    """SHA-256 fingerprint of `configs/decide/catalogue.yaml`, or None when absent."""
    base = config_root(root)
    path = base / CATALOGUE_FILENAME
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clear_catalogue_cache() -> None:
    """Clear internal catalogue cache (for testing edits)."""
    _CACHE.clear()
