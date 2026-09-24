"""The plain-language rule, as a check a test can run (Plan E §3, DEC-904).

"Every client-facing sentence is reviewed for plain language: no model jargon without a one-line
explanation." A review by a person is still the rule; this module is the part of it a machine can
hold: a list of words a client analyst or marketing head should never meet unexplained, and a
function that finds them. The help catalogue, the data request and every report are run through
:func:`jargon_in` by the tests, so a sentence that says "AUC" or "SHAP" fails the build instead of
reaching a client.

Terms a report does need - lift, decile, control group, confidence interval - are not on the list:
they are explained, once each, by `configs/pilot/help.yaml`'s `terms`, and a report that uses one
prints its explanation (:func:`terms_used`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Final

__all__ = ["JARGON", "SCREEN_NAMES", "jargon_in", "terms_used"]

JARGON: Final[tuple[str, ...]] = (
    "auc",
    "roc",
    "shap",
    "psi",
    "f1",
    "precision",
    "recall",
    "logit",
    "stratif",
    "cardinality",
    "imputation",
    "hyperparameter",
    "feature",
    "target$",
    "entity",
    "schema",
    "holdout",
    "hold-out",
    "p-value",
    "p value",
    "bootstrap",
    "qini",
    "auuc",
    "uplift",
    "leakage",
    "overfit",
    "calibrat",
    "pii",
    "dataframe",
    "null",
    "censor",
    "snapshot",
    "subscriber",
)
"""Lower-case stems. A stem matches at the start of a word, so `feature` also catches `features`;
one ending in `$` matches the whole word only (or its plural), so `target$` leaves "targeted offer" alone."""

SCREEN_NAMES: Final[tuple[str, ...]] = ("features screen", "mapping screen", "setup screen")
"""Names of screens the client will see labelled that way; naming a screen is not jargon."""

_WORD_START: Final[str] = r"(?<![a-z0-9_])"


def jargon_in(text: str) -> tuple[str, ...]:
    """The jargon stems `text` uses, in list order; empty when the text is plain."""
    lowered = text.lower()
    for name in SCREEN_NAMES:
        lowered = lowered.replace(name, " ")
    found = []
    for stem in JARGON:
        whole = stem.endswith("$")
        pattern = _WORD_START + re.escape(stem.rstrip("$")) + (r"s?(?![a-z])" if whole else "")
        if re.search(pattern, lowered):
            found.append(stem.rstrip("$"))
    return tuple(found)


def terms_used(texts: Iterable[str], terms: Mapping[str, str]) -> tuple[str, ...]:
    """Which glossary terms appear in `texts`, in glossary order: the ones a report must explain."""
    joined = " ".join(texts).lower()
    return tuple(
        term
        for term in terms
        if re.search(_WORD_START + re.escape(term.lower()) + r"(?:s|es)?(?![a-z])", joined)
    )
