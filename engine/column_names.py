"""Safe internal column names, and the stored mapping that turns them back into the client's own.

Why this exists (DEC-093)
-------------------------
A CSV header can say anything. The public datasets the library runs say `default.payment.next.month`
and `emp.var.rate`; a client's export says `Customer ID`, `âge`, `limit, bal` or `pay:2{x}`. pandas,
the validation checks, prepare, the split, evaluate and export are indifferent to all of it - they
index a frame by whatever string the header held. Two things are not:

* **The model.** LightGBM refuses a feature name carrying a JSON special character (`[`, `]`, `{`,
  `}`, `:`, `,`, `"`), and XGBoost refuses `[`, `]` and `<`. AutoGluon does not fail the run when that
  happens - it logs "No model was trained during hyperparameter tuning ... Skipping this model" and
  carries on - so a file with one odd header silently loses two of the four model families, and
  the leaderboard never says why. Measured on the library's credit-default sample with five such
  headers: LightGBM and XGBoost both dropped out.
* **Anything that has to be an identifier**: a template column, a SQL identifier, a Python keyword
  argument. The use-case template refuses a dotted name outright, which is how the library found
  this (`docs/CROSS_BRANCH_REQUESTS.md`, "a template column name cannot contain a dot").

So ingest gives every column a safe internal name - letters, digits and underscores, not starting
with a digit, unique within the file - and records the mapping in :class:`ColumnNames`. A name that
is already safe keeps itself, so a file with ordinary headers has the identity mapping, every
existing artefact is byte-for-byte what it was, and nothing below changes for it.

Where the internal names are used
---------------------------------
**Inside the model boundary, and nowhere else.** The pipeline hands the train stage a recipe and
partitions in internal names, so AutoGluon, the baseline and `scorer.json` only ever see safe
names. The mapping is stored beside `scorer.json` (:func:`save_for_model`), and a scorer given it
(`with_column_names`) speaks the client's names to everything outside the boundary - evaluate, the
champion re-score, the score flow's predict - renaming a frame's feature columns on the way in.
The explain stage drives the predictor directly, so it runs on the internal side, and
:func:`restore_importance` and :func:`present_explanations` translate its output back before
anything is written. Every artefact
a person reads - the profile, the validation report, `prepare.json`, `schema.json`, the importance
chart, the reasons and `scores.csv` - therefore names columns exactly as the uploaded file did.

The mapping is reversible by construction (:meth:`ColumnNames.original` inverts
:meth:`ColumnNames.internal` for every column of the file it was built for), and
``tests/unit/test_column_names.py`` holds that as a Hypothesis property over arbitrary Unicode,
dotted and spaced names.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    import pandas as pd

    from engine.config import Recipe
    from engine.contracts import FeatureImportance, Reason, RowExplanation, ShapBeeswarm
    from engine.storage import Storage

__all__ = [
    "COLUMN_NAMES_FILENAME",
    "FALLBACK_BASE",
    "SAFE_NAME_PATTERN",
    "ColumnNames",
    "internal_importance",
    "is_safe_name",
    "load_for_model",
    "present_explanations",
    "restore_beeswarm",
    "restore_importance",
    "safe_base",
    "save_for_model",
]

SAFE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
"""An identifier: what a template column, a SQL identifier and every model family accept."""

COLUMN_NAMES_FILENAME: Final[str] = "column_names.json"
"""The stored mapping, written beside `scorer.json` in the predictor's directory when it is not empty."""

FALLBACK_BASE: Final[str] = "column"
"""The base of a name with nothing left once it is transliterated to ASCII (`婚姻` -> `column`)."""

_UNSAFE_RUN: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9_]+")


def is_safe_name(name: str) -> bool:
    """Whether `name` may be used as it is, everywhere the engine needs an identifier."""
    return SAFE_NAME_PATTERN.match(name) is not None


def safe_base(name: str) -> str:
    """The readable identifier `name` sanitises to, before it is made unique within its file.

    Accents are transliterated (`âge` -> `age`), every run of anything else becomes one underscore
    (`default.payment.next.month` -> `default_payment_next_month`, `bill[amt]1` -> `bill_amt_1`),
    the ends are trimmed, and a leading digit is prefixed (`2nd_line` -> `c_2nd_line`). The library
    renamed its dotted columns by exactly this rule by hand, so its configurations keep working.
    """
    ascii_only = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    base = _UNSAFE_RUN.sub("_", ascii_only).strip("_")
    if not base:
        return FALLBACK_BASE
    if base[0].isdigit():
        return f"c_{base}"
    return base


class ColumnNames(BaseModel):
    """The mapping for one file: original header -> internal name, for the columns that changed.

    Columns whose header is already safe are absent, so the identity mapping is `renamed == {}` and
    serialises to nothing worth storing. Built by :meth:`for_columns`, never by hand.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    renamed: dict[str, str] = Field(
        default_factory=dict,
        description="Original header -> safe internal name, for every column whose header is not safe.",
    )

    @classmethod
    def for_columns(cls, columns: Iterable[object]) -> ColumnNames:
        """The mapping for a file with these headers.

        Every safe header keeps its name and claims it first, so a sanitised name can never take a
        name the file already uses (`a.b` beside `a_b` becomes `a_b_2`). An unsafe header takes its
        :func:`safe_base`, suffixed `_2`, `_3`... until it is unique. A scoring file is never given
        a mapping of its own: the model's stored one is applied to it, so a header reaches the model
        under the name it was trained with whatever else the new file carries.
        """
        originals = [str(column) for column in columns]
        taken = {name for name in originals if is_safe_name(name)}
        renamed: dict[str, str] = {}
        for name in originals:
            if is_safe_name(name) or name in renamed:
                continue
            base = safe_base(name)
            wanted, suffix = base, 2
            while wanted in taken:
                wanted, suffix = f"{base}_{suffix}", suffix + 1
            taken.add(wanted)
            renamed[name] = wanted
        return cls(renamed=renamed)

    @property
    def is_identity(self) -> bool:
        """True when every header was already safe - the case for every file before M36."""
        return not self.renamed

    def internal(self, name: str) -> str:
        """The name the model knows `name` by."""
        return self.renamed.get(name, name)

    def original(self, name: str) -> str:
        """The header the client's file used for the internal name `name`."""
        for original, internal in self.renamed.items():
            if internal == name:
                return original
        return name

    def internals(self, names: Sequence[str]) -> tuple[str, ...]:
        return tuple(self.internal(name) for name in names)

    def originals(self, names: Sequence[str]) -> tuple[str, ...]:
        return tuple(self.original(name) for name in names)

    def to_internal(self, frame: pd.DataFrame) -> pd.DataFrame:
        """`frame` with its columns renamed to internal names; the same object when nothing changes."""
        if self.is_identity:
            return frame
        return frame.rename(columns={name: self.renamed[name] for name in self.renamed if name in frame})

    def to_original(self, frame: pd.DataFrame) -> pd.DataFrame:
        """`frame` with internal names turned back into the file's own headers."""
        if self.is_identity:
            return frame
        back = {internal: original for original, internal in self.renamed.items()}
        return frame.rename(columns={name: back[name] for name in frame.columns if name in back})

    def recipe_for_model(self, recipe: Recipe) -> Recipe:
        """`recipe` with every column it names in internal names: what the train stage is handed.

        The run's own recipe - the one in the manifest and in `run_config.json` - keeps the client's
        names; only the copy that crosses into the model boundary is translated.
        """
        if self.is_identity:
            return recipe
        from engine.config import key_columns

        keys = self.internals(key_columns(recipe.primary_key))
        return recipe.model_copy(
            update={
                "target": self.internal(recipe.target),
                "primary_key": keys[0] if isinstance(recipe.primary_key, str) else list(keys),
                "feature_columns": self.internals(recipe.feature_columns),
            }
        )


def save_for_model(storage: Storage, predictor_key: str, names: ColumnNames) -> str | None:
    """Store the mapping with the model it belongs to; nothing is written for the identity mapping.

    It lives in the predictor's own directory, beside `scorer.json`, because it is part of what the
    model needs to be scored again: the registry copies that directory whole, and a scoring run
    months later must rename a new file's headers exactly the way the training file's were.
    """
    if names.is_identity:
        return None
    key = f"{predictor_key}/{COLUMN_NAMES_FILENAME}"
    storage.write_text(key, names.model_dump_json(indent=2))
    return key


def load_for_model(storage: Storage, predictor_key: str) -> ColumnNames:
    """The mapping a stored model was trained with; the identity mapping when none was stored."""
    key = f"{predictor_key}/{COLUMN_NAMES_FILENAME}"
    if not storage.exists(key):
        return ColumnNames()
    return ColumnNames.model_validate_json(storage.read_text(key))


def restore_importance(importance: FeatureImportance, names: ColumnNames) -> FeatureImportance:
    """The importance chart with every feature under the name the client's file gave it."""
    if names.is_identity:
        return importance
    items = tuple(
        item.model_copy(update={"feature": names.original(item.feature)}) for item in importance.items
    )
    return importance.model_copy(update={"items": items})


def restore_beeswarm(beeswarm: ShapBeeswarm, names: ColumnNames) -> ShapBeeswarm:
    """The beeswarm with every feature under the name the client's file gave it."""
    if names.is_identity:
        return beeswarm
    features = tuple(
        item.model_copy(update={"feature": names.original(item.feature)}) for item in beeswarm.features
    )
    return beeswarm.model_copy(update={"features": features})


def internal_importance(importance: FeatureImportance | None, names: ColumnNames) -> FeatureImportance | None:
    """A stored (client-named) importance chart translated for the explain stage, which ranks by it."""
    if importance is None or names.is_identity:
        return importance
    items = tuple(
        item.model_copy(update={"feature": names.internal(item.feature)}) for item in importance.items
    )
    return importance.model_copy(update={"items": items})


def _present_reason(reason: Reason, names: ColumnNames, masked: frozenset[str]) -> Reason:
    """One reason under the client's feature name, its value masked when the feature is free text.

    `Reason.text` always begins with the feature name (`engine.stages.explain.reason_text`), so the
    rest of the sentence - the arrow, the equals sign, the value - is kept exactly as the explain
    stage wrote it, and only the name in front of it changes.
    """
    from engine import pii

    original = names.original(reason.feature)
    mask = original in masked
    if original == reason.feature and not mask:
        return reason
    value = pii.redact_text(reason.value)[0] if mask else reason.value
    if reason.text.startswith(reason.feature):
        rest = reason.text[len(reason.feature) :]
        text = original + (pii.redact_text(rest)[0] if mask else rest)
    else:
        text = pii.redact_text(reason.text)[0] if mask else reason.text
    return reason.model_copy(update={"feature": original, "value": value, "text": text})


def present_explanations(
    explanations: Sequence[RowExplanation],
    names: ColumnNames,
    *,
    masked_features: Iterable[str] = (),
) -> tuple[RowExplanation, ...]:
    """Per-row reasons as a person reads them: the client's column names, free-text PII masked.

    `masked_features` are the columns the profile found personal data inside (DEC-095). A reason's
    value is a sample of the row's own cell, so a complaint quoted as a reason is shown with each
    contact replaced by its marker, exactly as the Setup preview shows it.
    """
    masked = frozenset(masked_features)
    if names.is_identity and not masked:
        return tuple(explanations)
    return tuple(
        explanation.model_copy(
            update={
                "reasons": tuple(_present_reason(reason, names, masked) for reason in explanation.reasons)
            }
        )
        for explanation in explanations
    )
