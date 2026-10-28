"""The typed menu of candidate policies, with typed settings in safe ranges.

The Scientist never writes code and never tunes freely. A set-up is one of four KINDS from this
menu plus a few settings, each checked against a range written down here:

* `BlanketPolicy(arm)`: send everyone the same arm (arm 0 is "send nothing"). Not fitted.
* `SegmentRule`: the best arm per segment of one to three customer features, fitted on the train part
  (`autonomy.proof.replay.fit_segment_policy`; the columns are chosen on a validation slice of that
  same train part).
* `UpliftPolicy`: the per-customer uplift model of `autonomy.uplift` (cross-fitted, calibrated out of
  sample), sending the arm whose expected uplift times the margin of a conversion most exceeds its
  cost, if any does.
* `BanditAgent`: the current learning agent (`autonomy.runtime.agent.AgentPolicy`). It is not a table
  that can be fitted here, so whoever runs the evaluation passes a `runner` that warm-starts an agent
  on the train part and returns its probabilities on the held-out part.

Settings (every one validated; a value outside its range, an unknown key, a non-number, a column the
campaign does not have: `MenuError`):

| setting | range | meaning |
|---|---|---|
| `exploration_floor` | 0 to 0.2 | share of customers given a uniformly random arm (also keeps the logs usable for proof) |
| `weekly_contact_budget` | 0 to 7 | most contacts per customer per week; 0 sends nothing |
| `lookback_days` | 7 to 365 | fit on the most recent days of the campaign only (needs timestamps) |
| `max_columns` | 1 to 3 | features a segment may use |
| `n_bins` | 2 to 8 | cut points of a numeric feature, plus one |
| `shrinkage` | 0 to 500 | pseudo-rows pulling a small segment towards the overall winner |

A candidate is a frozen dataclass: hashable, comparable, and `spec()` / `candidate_from_spec` turn it
into and out of plain JSON-able data, so a proposer (a person, later a model) only ever hands over
`{"kind": ..., "settings": {...}}`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol

import numpy as np
import numpy.typing as npt
import pandas as pd

from autonomy.contracts import Channel
from autonomy.proof.replay import LoggedBandit, SegmentPolicy, fit_segment_policy
from autonomy.proof.replay import evaluate as replay_evaluate
from autonomy.scientist.campaign import Campaign, attributes_of, unit_hash
from autonomy.uplift.campaign import CampaignTable
from autonomy.uplift.model import FittedUplift, fit_uplift

__all__ = [
    "KINDS",
    "SETTING_RANGES",
    "AgentRunner",
    "BanditAgent",
    "BlanketPolicy",
    "Candidate",
    "CandidateFailed",
    "FittedPolicy",
    "MenuError",
    "SegmentRule",
    "UpliftPolicy",
    "candidate_from_spec",
    "default_candidates",
]

FloatArray = npt.NDArray[np.float64]

Kind = Literal["blanket", "segment", "uplift", "bandit"]
KINDS: tuple[str, ...] = ("blanket", "segment", "uplift", "bandit")

SETTING_RANGES: dict[str, tuple[float, float]] = {
    "exploration_floor": (0.0, 0.2),
    "weekly_contact_budget": (0, 7),
    "lookback_days": (7, 365),
    "max_columns": (1, 3),
    "n_bins": (2, 8),
    "shrinkage": (0.0, 500.0),
}
"""The safe range of every typed setting (inclusive)."""

SEGMENT_SALT = "autonomy-scientist-v1:segment-columns"
SEGMENT_VALIDATION_SHARE = 0.3
MAX_SCREENED_COLUMNS = 60
MAX_LEVELS_FOR_SEGMENT = 30
MIN_ARM_ROWS_TO_FIT = 30
UPLIFT_CHUNK = 20_000

AgentRunner = Callable[[Campaign, Campaign], FloatArray]
"""Warm-start the learning agent on the first campaign (train part) and return its probability of
every arm on each customer of the second (held-out part): shape (rows, arms), rows summing to 1."""


class MenuError(ValueError):
    """A candidate or a setting that the menu does not allow."""


class CandidateFailed(RuntimeError):  # noqa: N818 (a result, not an error type)
    """A candidate that could not be fitted on this campaign (the reason is the message)."""


class FittedPolicy(Protocol):
    """A policy fitted on the train part: probabilities of every arm for any customers."""

    @property
    def label(self) -> str: ...

    def probabilities(self, features: pd.DataFrame, behaviour: FloatArray | None) -> FloatArray:
        """(rows, arms): non-negative, each row sums to 1."""
        ...

    def row_features(self, preferences: Mapping[str, str]) -> pd.DataFrame:
        """One customer's `CustomerState.preferences` (text) as a one-row feature frame with the
        columns and dtypes the policy was fitted on."""
        ...


class Candidate(Protocol):
    """One entry of the menu."""

    kind: ClassVar[str]

    def label(self, arm_names: Sequence[str]) -> str: ...

    def spec(self) -> dict[str, object]: ...

    def held_out_probabilities(self, fit: Campaign, held: Campaign) -> FloatArray:
        """Fit on `fit` only, then the probability of every arm for each row of `held`."""
        ...

    def fit(self, train: Campaign) -> FittedPolicy:
        """Fit on `train` (for running it on new customers); CandidateFailed when it cannot be."""
        ...


# --- validation ------------------------------------------------------------------------------------


def _number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MenuError(f"{name} must be a number, got {type(value).__name__}")
    v = float(value)
    if not math.isfinite(v):
        raise MenuError(f"{name} must be finite")
    return v


def _float_setting(name: str, value: object) -> float:
    lo, hi = SETTING_RANGES[name]
    v = _number(name, value)
    if not lo <= v <= hi:
        raise MenuError(f"{name} must be between {lo:g} and {hi:g}, got {v:g}")
    return v


def _int_setting(name: str, value: object) -> int:
    lo, hi = SETTING_RANGES[name]
    v = _number(name, value)
    if v != int(v):
        raise MenuError(f"{name} must be a whole number, got {v:g}")
    if not lo <= v <= hi:
        raise MenuError(f"{name} must be between {lo:g} and {hi:g}, got {int(v)}")
    return int(v)


def _check_common(floor: object, budget: object) -> None:
    _float_setting("exploration_floor", floor)
    _int_setting("weekly_contact_budget", budget)


def _shape(base: FloatArray, floor: float, budget: int) -> FloatArray:
    """The deployed probabilities: the policy's own, with the exploration floor mixed in.

    A budget of 0 contacts nobody (no floor either). A budget of 1 or more cannot bind inside one
    campaign (each customer got one decision); the runtime chooser enforces it per week."""
    k = base.shape[1]
    if budget == 0:
        out = np.zeros_like(base)
        out[:, 0] = 1.0
        return out
    shaped: FloatArray = (1.0 - floor) * base + floor / k
    return shaped


def _recent(train: Campaign, lookback_days: int | None) -> Campaign:
    """The train rows of the last `lookback_days` days (all rows without timestamps)."""
    if lookback_days is None or train.times is None or train.n == 0:
        return train
    keep = np.flatnonzero(train.times >= float(train.times.max()) - lookback_days * 86_400.0)
    return train if len(keep) == train.n else train.take(keep)


def _dtypes(features: pd.DataFrame) -> tuple[tuple[str, str], ...]:
    return tuple((str(c), str(features[c].dtype)) for c in features.columns)


def _np_dtype(name: str) -> np.dtype[Any]:
    try:
        return np.dtype(name)
    except TypeError:
        return np.dtype(object)


def frame_from_preferences(preferences: Mapping[str, str], dtypes: Sequence[tuple[str, str]]) -> pd.DataFrame:
    """A one-row feature frame from a customer's preferences (text), cast to the fitted dtypes."""
    row: dict[str, object] = {}
    cast: dict[str, str] = {}
    for col, dtype in dtypes:
        raw = preferences.get(col)
        if pd.api.types.is_numeric_dtype(_np_dtype(dtype)):
            try:
                value = float(raw) if raw is not None else math.nan
            except ValueError:
                value = math.nan
            row[col] = value
            if math.isfinite(value) and not dtype.startswith("float"):
                cast[col] = dtype
        else:
            row[col] = raw
    frame = pd.DataFrame([row])
    for col, dtype in cast.items():
        frame[col] = frame[col].astype(pd.api.types.pandas_dtype(dtype))
    return frame


def _onehot(arms: npt.NDArray[np.int64], k: int) -> FloatArray:
    out = np.zeros((len(arms), k), dtype=np.float64)
    out[np.arange(len(arms)), arms] = 1.0
    return out


def _context_bandit(features: pd.DataFrame, arm_names: Sequence[str]) -> LoggedBandit:
    """Rows to score a fitted table on: only the features are read, the rest is placeholder."""
    n = len(features)
    return LoggedBandit(
        source="scoring",
        contexts=features.reset_index(drop=True),
        actions=np.zeros(n, dtype=np.int64),
        propensity=np.ones(n, dtype=np.float64),
        reward=np.zeros(n, dtype=np.float64),
        action_names=tuple(arm_names),
        reward_name="none",
    )


def _need_identified(train: Campaign) -> None:
    if not train.identified:
        raise CandidateFailed(
            "no propensities were logged for this campaign: it cannot be fitted or replayed"
        )
    counts = train.arm_counts()
    thin = [train.arm_names[a] for a in range(train.n_arms) if counts[a] < MIN_ARM_ROWS_TO_FIT]
    if thin:
        raise CandidateFailed(f"too few customers in the train part for: {', '.join(thin)}")


# --- fitted policies -------------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class _FittedBlanket:
    label: str
    arm: int
    n_arms: int
    floor: float
    budget: int
    dtypes: tuple[tuple[str, str], ...] = ()

    def row_features(self, preferences: Mapping[str, str]) -> pd.DataFrame:
        return frame_from_preferences(preferences, self.dtypes)

    def probabilities(self, features: pd.DataFrame, behaviour: FloatArray | None) -> FloatArray:
        del behaviour  # a blanket policy reads no feature
        base = _onehot(np.full(len(features), self.arm, dtype=np.int64), self.n_arms)
        return _shape(base, self.floor, self.budget)


@dataclass(frozen=True, eq=False)
class _FittedSegment:
    label: str
    policy: SegmentPolicy
    arm_names: tuple[str, ...]
    floor: float
    budget: int
    dtypes: tuple[tuple[str, str], ...] = ()

    def row_features(self, preferences: Mapping[str, str]) -> pd.DataFrame:
        return frame_from_preferences(preferences, self.dtypes)

    @property
    def columns(self) -> tuple[str, ...]:
        return self.policy.columns

    def probabilities(self, features: pd.DataFrame, behaviour: FloatArray | None) -> FloatArray:
        del behaviour  # the table reads the features it was fitted on
        if len(features) == 0:
            return np.zeros((0, len(self.arm_names)), dtype=np.float64)
        arms = self.policy.actions_for(_context_bandit(features, self.arm_names))
        return _shape(_onehot(arms, len(self.arm_names)), self.floor, self.budget)


@dataclass(frozen=True, eq=False)
class _FittedUplift:
    label: str
    model: FittedUplift
    arm_cost: tuple[float, ...]
    arm_value: tuple[float, ...]
    """Margin of a conversion per arm (arm 0 unused)."""
    floor: float
    budget: int
    dtypes: tuple[tuple[str, str], ...] = ()

    def row_features(self, preferences: Mapping[str, str]) -> pd.DataFrame:
        return frame_from_preferences(preferences, self.dtypes)

    def uplift(self, features: pd.DataFrame, behaviour: FloatArray | None) -> FloatArray:
        """(rows, message arms): the calibrated rise in the chance of converting, as `FittedUplift.score`."""
        model = self.model
        k = len(model.keys)
        n = len(features)
        out = np.zeros((n, k), dtype=np.float64)
        beh_all = behaviour if behaviour is not None else np.zeros((n, 4), dtype=np.float64)
        attrs_all = attributes_of(features)
        for lo in range(0, n, UPLIFT_CHUNK):
            hi = min(lo + UPLIFT_CHUNK, n)
            x = model.encoder.matrix(attrs_all[lo:hi], beh_all[lo:hi])
            avg = np.zeros((hi - lo, k), dtype=np.float64)
            for m in model.models:
                mu0, mu = m.predict(x)
                avg += mu - mu0[:, None]
            avg /= len(model.models)
            for j in range(k):
                cal = model.calibration[j]
                c = np.clip(avg[:, j] - cal.centre, cal.low, cal.high)
                out[lo:hi, j] = np.clip(cal.ate + cal.slope * c, -1.0, 1.0)
        return out

    def probabilities(self, features: pd.DataFrame, behaviour: FloatArray | None) -> FloatArray:
        n = len(features)
        k = len(self.arm_cost)
        if n == 0:
            return np.zeros((0, k), dtype=np.float64)
        gain = np.zeros((n, k), dtype=np.float64)
        tau = self.uplift(features, behaviour)
        for a in range(1, k):
            gain[:, a] = tau[:, a - 1] * self.arm_value[a] - self.arm_cost[a]
        best = np.argmax(gain, axis=1)  # arm 0's gain is 0: a message must pay to be sent
        return _shape(_onehot(best.astype(np.int64), k), self.floor, self.budget)


# --- candidates ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BlanketPolicy:
    """Everyone gets the same arm (0 = nobody)."""

    kind: ClassVar[str] = "blanket"

    arm: int
    exploration_floor: float = 0.0
    weekly_contact_budget: int = 2

    def __post_init__(self) -> None:
        if isinstance(self.arm, bool) or not isinstance(self.arm, int) or self.arm < 0:
            raise MenuError("arm must be a whole number, 0 for 'send nothing'")
        _check_common(self.exploration_floor, self.weekly_contact_budget)

    def label(self, arm_names: Sequence[str]) -> str:
        if self.arm >= len(arm_names):
            raise MenuError(f"arm {self.arm} is not an arm of this campaign")
        return "send nothing" if self.arm == 0 else f"send to all: {arm_names[self.arm]}"

    def spec(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "settings": {
                "arm": self.arm,
                "exploration_floor": self.exploration_floor,
                "weekly_contact_budget": self.weekly_contact_budget,
            },
        }

    def fit(self, train: Campaign) -> FittedPolicy:
        if self.arm >= train.n_arms:
            raise MenuError(f"arm {self.arm} is not an arm of this campaign")
        return _FittedBlanket(
            self.label(train.arm_names),
            self.arm,
            train.n_arms,
            self.exploration_floor,
            self.weekly_contact_budget,
            _dtypes(train.features),
        )

    def held_out_probabilities(self, fit: Campaign, held: Campaign) -> FloatArray:
        return self.fit(fit).probabilities(held.features, held.behaviour)


def _screen_columns(train: Campaign, wanted: Sequence[str] | None) -> list[str]:
    """The columns a segment rule may look at: the asked-for ones, else every column that varies and
    is not an identifier (a text column with more than `MAX_LEVELS_FOR_SEGMENT` levels), at most
    `MAX_SCREENED_COLUMNS` of them in name order."""
    if wanted is not None:
        missing = [c for c in wanted if c not in train.features.columns]
        if missing:
            raise MenuError(f"columns not in the campaign: {', '.join(missing)}")
        return list(wanted)
    keep: list[str] = []
    for col in sorted(str(c) for c in train.features.columns):
        s = train.features[col]
        levels = s.nunique(dropna=True)
        if levels < 2:
            continue
        if not pd.api.types.is_numeric_dtype(s) and levels > MAX_LEVELS_FOR_SEGMENT:
            continue
        keep.append(col)
        if len(keep) >= MAX_SCREENED_COLUMNS:
            break
    return keep


def _validation_dr(policy: SegmentPolicy, log_val: LoggedBandit) -> float:
    return float(replay_evaluate(policy, log_val).dr.point)


@dataclass(frozen=True)
class SegmentRule:
    """The best arm per segment of up to `max_columns` features, fitted on the train part.

    Columns (unless `columns` names them) are chosen greedily on a validation slice of the train
    part: the best single column by the DR value of the rule fitted on the rest, then the best
    second column added to it if that raises the validation value, and so on. The rule is then
    refitted on the whole train part."""

    kind: ClassVar[str] = "segment"

    columns: tuple[str, ...] | None = None
    max_columns: int = 2
    n_bins: int = 4
    shrinkage: float = 50.0
    lookback_days: int | None = None
    exploration_floor: float = 0.02
    weekly_contact_budget: int = 2

    def __post_init__(self) -> None:
        if self.columns is not None:
            if not isinstance(self.columns, tuple) or not self.columns:
                raise MenuError("columns must be a non-empty tuple of column names, or None")
            if any(not isinstance(c, str) for c in self.columns):
                raise MenuError("columns must be names (text)")
            if len(self.columns) > int(SETTING_RANGES["max_columns"][1]):
                raise MenuError(f"at most {int(SETTING_RANGES['max_columns'][1])} columns")
        _int_setting("max_columns", self.max_columns)
        _int_setting("n_bins", self.n_bins)
        _float_setting("shrinkage", self.shrinkage)
        if self.lookback_days is not None:
            _int_setting("lookback_days", self.lookback_days)
        _check_common(self.exploration_floor, self.weekly_contact_budget)

    def label(self, arm_names: Sequence[str]) -> str:  # noqa: ARG002
        return "segment rule" + (f" ({'+'.join(self.columns)})" if self.columns else "")

    def spec(self) -> dict[str, object]:
        settings: dict[str, object] = {
            "max_columns": self.max_columns,
            "n_bins": self.n_bins,
            "shrinkage": self.shrinkage,
            "exploration_floor": self.exploration_floor,
            "weekly_contact_budget": self.weekly_contact_budget,
        }
        if self.columns is not None:
            settings["columns"] = list(self.columns)
        if self.lookback_days is not None:
            settings["lookback_days"] = self.lookback_days
        return {"kind": self.kind, "settings": settings}

    def _fit_columns(self, log: LoggedBandit, ids: Sequence[str], pool: list[str]) -> tuple[str, ...]:
        h = unit_hash(ids, SEGMENT_SALT)
        val = h < SEGMENT_VALIDATION_SHARE
        log_fit, log_val = log.take(np.flatnonzero(~val)), log.take(np.flatnonzero(val))
        if log_val.n_rows < 2 or log_fit.n_rows < 2:
            raise CandidateFailed("too few customers to choose segment columns on a validation slice")

        def score(cols: Sequence[str]) -> float:
            pol = fit_segment_policy(
                log_fit, segment_columns=list(cols), n_bins=self.n_bins, shrinkage=self.shrinkage
            )
            return _validation_dr(pol, log_val)

        best: tuple[str, ...] = ()
        best_v = -math.inf
        for col in pool:
            v = score((col,))
            if v > best_v:
                best, best_v = (col,), v
        if not best:
            raise CandidateFailed("no feature varies, so there is nothing to segment on")
        while len(best) < self.max_columns:
            cand: tuple[str, ...] = ()
            cand_v = best_v
            for col in pool:
                if col in best:
                    continue
                v = score((*best, col))
                if v > cand_v:
                    cand, cand_v = (*best, col), v
            if not cand:
                break
            best, best_v = cand, cand_v
        return best

    def fit(self, train: Campaign) -> FittedPolicy:
        train = _recent(train, self.lookback_days)
        _need_identified(train)
        pool = _screen_columns(train, self.columns)
        if not pool:
            raise CandidateFailed("no usable feature columns")
        log = train.logged()
        cols = self.columns if self.columns is not None and len(self.columns) <= self.max_columns else None
        if cols is None:
            cols = self._fit_columns(log, train.customer_ids, pool)
        policy = fit_segment_policy(
            log, segment_columns=list(cols), n_bins=self.n_bins, shrinkage=self.shrinkage
        )
        label = "segment rule (" + "+".join(policy.columns) + ")"
        return _FittedSegment(
            label,
            policy,
            train.arm_names,
            self.exploration_floor,
            self.weekly_contact_budget,
            _dtypes(train.features),
        )

    def held_out_probabilities(self, fit: Campaign, held: Campaign) -> FloatArray:
        return self.fit(fit).probabilities(held.features, held.behaviour)


@dataclass(frozen=True)
class UpliftPolicy:
    """The per-customer uplift model of `autonomy.uplift`, turned into a send / no-send per arm."""

    kind: ClassVar[str] = "uplift"

    lookback_days: int | None = None
    exploration_floor: float = 0.02
    weekly_contact_budget: int = 2

    def __post_init__(self) -> None:
        if self.lookback_days is not None:
            _int_setting("lookback_days", self.lookback_days)
        _check_common(self.exploration_floor, self.weekly_contact_budget)

    def label(self, arm_names: Sequence[str]) -> str:  # noqa: ARG002
        return "uplift model"

    def spec(self) -> dict[str, object]:
        settings: dict[str, object] = {
            "exploration_floor": self.exploration_floor,
            "weekly_contact_budget": self.weekly_contact_budget,
        }
        if self.lookback_days is not None:
            settings["lookback_days"] = self.lookback_days
        return {"kind": self.kind, "settings": settings}

    def fit(self, train: Campaign) -> FittedPolicy:
        train = _recent(train, self.lookback_days)
        _need_identified(train)
        assert train.propensity is not None
        table = CampaignTable(
            customer_ids=train.customer_ids,
            arm=train.arm,
            arm_keys=tuple(train.arm_names[1:]),
            propensity=train.propensity,
            randomised=train.randomised,
            y=train.converted,
            revenue=train.margin,
            attributes=train.attributes,
            behaviour=train.behaviour_matrix,
            channel=Channel.EMAIL,
            started_at=train.started_at,
            horizon_days=7.0,
            explicit_control=False,
            notes=(),
        )
        model, notes = fit_uplift(table)
        if model is None:
            raise CandidateFailed("; ".join(notes[-2:]) or "the uplift model could not be fitted")
        pooled = train.margin_per_conversion()
        values = [0.0] + [train.margin_per_conversion(a) or pooled for a in range(1, train.n_arms)]
        return _FittedUplift(
            "uplift model",
            model,
            train.arm_cost,
            tuple(values),
            self.exploration_floor,
            self.weekly_contact_budget,
            _dtypes(train.features),
        )

    def held_out_probabilities(self, fit: Campaign, held: Campaign) -> FloatArray:
        return self.fit(fit).probabilities(held.features, held.behaviour)


@dataclass(frozen=True)
class BanditAgent:
    """The current learning agent. `runner` (not a setting) runs it: see `AgentRunner`.

    `exploration_floor` None keeps the agent's own (the signed envelope's)."""

    kind: ClassVar[str] = "bandit"

    exploration_floor: float | None = None
    weekly_contact_budget: int = 2
    runner: AgentRunner | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.exploration_floor is not None:
            _float_setting("exploration_floor", self.exploration_floor)
        _int_setting("weekly_contact_budget", self.weekly_contact_budget)

    def label(self, arm_names: Sequence[str]) -> str:  # noqa: ARG002
        return "learning agent"

    def spec(self) -> dict[str, object]:
        settings: dict[str, object] = {"weekly_contact_budget": self.weekly_contact_budget}
        if self.exploration_floor is not None:
            settings["exploration_floor"] = self.exploration_floor
        return {"kind": self.kind, "settings": settings}

    def fit(self, train: Campaign) -> FittedPolicy:  # noqa: ARG002
        raise CandidateFailed("the learning agent is not a fitted table: it runs as the agent itself")

    def held_out_probabilities(self, fit: Campaign, held: Campaign) -> FloatArray:
        if self.runner is None:
            raise CandidateFailed("no runner was given for the learning agent")
        pi = np.asarray(self.runner(fit, held), dtype=np.float64)
        if pi.shape != (held.n, held.n_arms):
            raise CandidateFailed(f"the agent runner returned {pi.shape}, expected {(held.n, held.n_arms)}")
        return pi


# --- from plain data ---------------------------------------------------------------------------------

_ALLOWED: dict[str, frozenset[str]] = {
    "blanket": frozenset({"arm", "exploration_floor", "weekly_contact_budget"}),
    "segment": frozenset(
        {
            "columns",
            "max_columns",
            "n_bins",
            "shrinkage",
            "lookback_days",
            "exploration_floor",
            "weekly_contact_budget",
        }
    ),
    "uplift": frozenset({"lookback_days", "exploration_floor", "weekly_contact_budget"}),
    "bandit": frozenset({"exploration_floor", "weekly_contact_budget"}),
}


def _opt_floor(s: dict[str, object], default: float | None) -> float | None:
    return (
        _float_setting("exploration_floor", s.pop("exploration_floor"))
        if "exploration_floor" in s
        else default
    )


def _budget(s: dict[str, object], default: int) -> int:
    return (
        _int_setting("weekly_contact_budget", s.pop("weekly_contact_budget"))
        if "weekly_contact_budget" in s
        else default
    )


def candidate_from_spec(
    spec: Mapping[str, object],
    *,
    arm_names: Sequence[str],
    columns: Sequence[str] = (),
    runner: AgentRunner | None = None,
) -> Candidate:
    """A candidate from `{"kind": ..., "settings": {...}}`, or `MenuError`.

    Nothing in `spec` is ever executed: `kind` must be one of `KINDS`, each setting key one that kind
    allows, each value a number (or, for `columns`, a list of names of `columns`), inside its range."""
    extra = set(spec) - {"kind", "settings"}
    if extra:
        raise MenuError(f"unknown keys in the spec: {', '.join(sorted(extra))}")
    kind = spec.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise MenuError(f"kind must be one of {', '.join(KINDS)}")
    raw = spec.get("settings", {})
    if not isinstance(raw, Mapping):
        raise MenuError("settings must be a mapping")
    unknown = set(raw) - _ALLOWED[kind]
    if unknown:
        raise MenuError(f"settings not allowed for {kind}: {', '.join(sorted(str(u) for u in unknown))}")
    s: dict[str, object] = {str(k): v for k, v in raw.items()}
    if kind == "blanket":
        arm = s.pop("arm", None)
        if isinstance(arm, bool) or not isinstance(arm, int) or not 0 <= arm < len(arm_names):
            raise MenuError(f"arm must be a whole number from 0 to {len(arm_names) - 1}")
        floor = _opt_floor(s, 0.0)
        assert floor is not None
        return BlanketPolicy(arm=arm, exploration_floor=floor, weekly_contact_budget=_budget(s, 2))
    if kind == "bandit":
        return BanditAgent(
            exploration_floor=_opt_floor(s, None), weekly_contact_budget=_budget(s, 2), runner=runner
        )
    lookback = s.pop("lookback_days", None)
    look = None if lookback is None else _int_setting("lookback_days", lookback)
    floor = _opt_floor(s, 0.02)
    assert floor is not None
    if kind == "uplift":
        return UpliftPolicy(lookback_days=look, exploration_floor=floor, weekly_contact_budget=_budget(s, 2))
    cols_raw = s.pop("columns", None)
    cols: tuple[str, ...] | None = None
    if cols_raw is not None:
        if (
            not isinstance(cols_raw, list | tuple)
            or not cols_raw
            or any(not isinstance(c, str) for c in cols_raw)
        ):
            raise MenuError("columns must be a non-empty list of column names")
        bad = [str(c) for c in cols_raw if c not in columns]
        if bad:
            raise MenuError(f"columns not in the campaign: {', '.join(bad)}")
        cols = tuple(str(c) for c in cols_raw)
    base = SegmentRule()
    return SegmentRule(
        columns=cols,
        max_columns=(
            _int_setting("max_columns", s.pop("max_columns")) if "max_columns" in s else base.max_columns
        ),
        n_bins=_int_setting("n_bins", s.pop("n_bins")) if "n_bins" in s else base.n_bins,
        shrinkage=_float_setting("shrinkage", s.pop("shrinkage")) if "shrinkage" in s else base.shrinkage,
        lookback_days=look,
        exploration_floor=floor,
        weekly_contact_budget=_budget(s, 2),
    )


def default_candidates(runner: AgentRunner | None = None) -> tuple[Candidate, ...]:
    """The fixed default list of a run with no proposer: a segment rule, the uplift model and (when a
    `runner` can run it) the learning agent, each with its default settings. Three at most, always."""
    base: tuple[Candidate, ...] = (SegmentRule(), UpliftPolicy())
    return (*base, BanditAgent(runner=runner)) if runner is not None else base
