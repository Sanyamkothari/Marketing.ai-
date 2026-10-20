"""Readers that turn three public datasets into one shape, `LoggedBandit`, for off-policy replay.

Plan-v2 section 8 asks for a second check beside the Gym: run our kind of policy on real logged data
and estimate, off-policy, what it would have earned compared with the policy that produced the log.
That only works when the log records, for every row, the action taken *and the probability the
logging policy had of taking it*. Three public datasets carry that, each in its own layout:

* **Open Bandit Dataset** (ZOZOTOWN fashion e-commerce). One row per item shown in one of three
  slots, with `timestamp, item_id, position, click, propensity_score`, hashed `user_feature_0..3`
  and `user-item_affinity_*` columns. Two logging policies ran: `random` (uniform) and `bts`
  (Bernoulli Thompson sampling, propensities estimated by its authors). Campaigns are `all`, `men`
  and `women`. The action is the item; the slot is treated as context.
* **Hillstrom e-mail RCT** (MineThatData, 2008). Customers were split at random into three equal
  groups: a men's e-mail, a women's e-mail or no e-mail, so every action's propensity is 1/3. The
  reward is a visit, a conversion or spend over the next two weeks.
* **Criteo uplift v2.1**. Treatment was assigned at random at a known rate (85% treated), with
  anonymised features `f0..f11` and `visit` / `conversion` outcomes.

`LoggedBandit` keeps only what the estimators need: a context frame (features only, no outcomes),
the action index, the logging propensity of that action and the reward. Nothing here downloads
anything; `fetch.py` does that, and the tests read tiny fixtures shaped like each file.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import numpy as np
import numpy.typing as npt
import pandas as pd

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
HILLSTROM_SAMPLE: Final = REPO_ROOT / "library" / "hillstrom-email" / "sample.csv"
"""The committed 6,401-row Hillstrom sample. The full file comes from `fetch.py`."""

# --- Open Bandit Dataset -------------------------------------------------------------------------

OBD_BEHAVIOR_POLICIES: Final[tuple[str, ...]] = ("random", "bts")
OBD_CAMPAIGNS: Final[tuple[str, ...]] = ("all", "men", "women")
OBD_N_ITEMS: Final[dict[str, int]] = {"all": 80, "men": 34, "women": 46}
"""Items per campaign, as documented by the dataset's authors; the action space is 0..n-1."""
OBD_REQUIRED: Final[tuple[str, ...]] = ("timestamp", "item_id", "position", "click", "propensity_score")

# --- Hillstrom -----------------------------------------------------------------------------------

HILLSTROM_ACTIONS: Final[tuple[str, ...]] = ("No E-Mail", "Mens E-Mail", "Womens E-Mail")
"""Action 0 is "no e-mail", so a policy that does nothing is action 0 everywhere."""
HILLSTROM_FEATURES: Final[tuple[str, ...]] = (
    "recency",
    "history_segment",
    "history",
    "mens",
    "womens",
    "zip_code",
    "newbie",
    "channel",
)
HILLSTROM_REWARDS: Final[tuple[str, ...]] = ("visit", "conversion", "spend")

# --- Criteo uplift -------------------------------------------------------------------------------

CRITEO_FEATURES: Final[tuple[str, ...]] = tuple(f"f{i}" for i in range(12))
CRITEO_ACTIONS: Final[tuple[str, ...]] = ("control", "treatment")
CRITEO_REWARDS: Final[tuple[str, ...]] = ("visit", "conversion")
CRITEO_TREATMENT_RATE: Final[float] = 0.85
"""Share of users assigned to treatment in Criteo uplift v2.1, as documented by Criteo."""


@dataclass(frozen=True, eq=False)
class LoggedBandit:
    """One logged decision per row: what we knew, what was done, how likely it was, what happened.

    `contexts` holds features only (never outcomes), so a policy that reads it cannot peek at the
    reward. `propensity[i]` is the logging policy's probability of `actions[i]`, the action actually
    taken; it must be in (0, 1], or the row could not have been logged.
    """

    source: str
    contexts: pd.DataFrame
    actions: IntArray
    propensity: FloatArray
    reward: FloatArray
    action_names: tuple[str, ...]
    reward_name: str

    def __post_init__(self) -> None:
        n = len(self.contexts)
        for name, arr in (
            ("actions", self.actions),
            ("propensity", self.propensity),
            ("reward", self.reward),
        ):
            if arr.ndim != 1 or len(arr) != n:
                raise ValueError(f"{name} must be one value per context row ({n}), got shape {arr.shape}.")
        if not self.action_names:
            raise ValueError("A logged bandit needs at least one action.")
        if n and (self.actions.min() < 0 or self.actions.max() >= len(self.action_names)):
            raise ValueError(f"actions must index action_names (0..{len(self.action_names) - 1}).")
        if not np.all(np.isfinite(self.propensity)) or np.any(self.propensity <= 0.0):
            raise ValueError("Every logging propensity must be a finite number above 0.")
        if np.any(self.propensity > 1.0):
            raise ValueError("A logging propensity cannot exceed 1.")
        if not np.all(np.isfinite(self.reward)):
            raise ValueError("Every reward must be a finite number.")

    @property
    def n_rows(self) -> int:
        return len(self.contexts)

    @property
    def n_actions(self) -> int:
        return len(self.action_names)

    def take(self, rows: npt.NDArray[np.intp] | IntArray) -> LoggedBandit:
        """The same log restricted to `rows` (positions, in the order given)."""
        return LoggedBandit(
            source=self.source,
            contexts=self.contexts.iloc[rows].reset_index(drop=True),
            actions=self.actions[rows],
            propensity=self.propensity[rows],
            reward=self.reward[rows],
            action_names=self.action_names,
            reward_name=self.reward_name,
        )

    def split(self, test_share: float = 0.5, *, seed: int = 0) -> tuple[LoggedBandit, LoggedBandit]:
        """A random train/test split, the same for the same seed, so a policy never grades itself."""
        if not 0.0 < test_share < 1.0:
            raise ValueError("test_share must be strictly between 0 and 1.")
        order = np.random.default_rng(seed).permutation(self.n_rows)
        n_test = round(self.n_rows * test_share)
        if n_test < 1 or n_test >= self.n_rows:
            raise ValueError(f"{self.n_rows} rows cannot be split into a non-empty train and test.")
        return self.take(np.sort(order[n_test:])), self.take(np.sort(order[:n_test]))

    def to_frame(self) -> pd.DataFrame:
        """Contexts plus `action`, `action_name`, `propensity` and `reward`, for a quick look."""
        frame = self.contexts.copy()
        frame["action"] = self.actions
        frame["action_name"] = [self.action_names[a] for a in self.actions]
        frame["propensity"] = self.propensity
        frame["reward"] = self.reward
        return frame


def _require(frame: pd.DataFrame, columns: Sequence[str], *, dataset: str) -> None:
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise ValueError(f"{dataset}: missing column(s) {missing}; is this the right file?")


def _floats(series: pd.Series, *, name: str) -> FloatArray:
    values: FloatArray = np.asarray(
        pd.to_numeric(series, errors="coerce").to_numpy(dtype=np.float64, na_value=np.nan), dtype=np.float64
    )
    if not np.all(np.isfinite(values)):
        raise ValueError(f"{name} has missing or non-numeric values.")
    return values


def _ints(series: pd.Series, *, name: str) -> IntArray:
    values = _floats(series, name=name)
    if not np.all(values == np.round(values)):
        raise ValueError(f"{name} must hold whole numbers.")
    return values.astype(np.int64)


# --- Open Bandit Dataset -------------------------------------------------------------------------


def _infer_from_path(path: Path, options: tuple[str, ...]) -> list[str]:
    """Which of `options` the path names: a folder (`bts/men/`) first, else a `_` token of the stem."""
    parents = {part.lower() for part in path.parent.parts}
    found = [o for o in options if o in parents]
    if len(found) != 1:
        found = [o for o in options if o in path.stem.lower().split("_")]
    return found


def _obd_file(path: Path, behavior_policy: str | None, campaign: str | None) -> tuple[Path, str, str]:
    """Resolve a dataset root (`<root>/<policy>/<campaign>/<campaign>.csv`) or a CSV file.

    For a CSV, the policy and campaign are read from its path when not given (the documented layout
    names both); a campaign the path does not name defaults to 'all'.
    """
    if path.is_dir():
        if behavior_policy is None:
            raise ValueError("Pass behavior_policy ('random' or 'bts') when reading the dataset folder.")
        campaign = campaign or "all"
        return path / behavior_policy / campaign / f"{campaign}.csv", behavior_policy, campaign
    if behavior_policy is None:
        inferred = _infer_from_path(path, OBD_BEHAVIOR_POLICIES)
        if len(inferred) != 1:
            raise ValueError(f"Cannot tell the behaviour policy from {path}; pass behavior_policy.")
        behavior_policy = inferred[0]
    if campaign is None:
        named = _infer_from_path(path, OBD_CAMPAIGNS)
        campaign = named[0] if len(named) == 1 else "all"
    return path, behavior_policy, campaign


def read_open_bandit(
    path: str | Path,
    *,
    behavior_policy: Literal["random", "bts"] | None = None,
    campaign: Literal["all", "men", "women"] | None = None,
    n_items: int | None = None,
    nrows: int | None = None,
) -> LoggedBandit:
    """Read one Open Bandit Dataset log (one behaviour policy, one campaign).

    `path` is either the dataset folder (then `behavior_policy` picks `random/` or `bts/` and
    `campaign` defaults to 'all') or one CSV; for a CSV the policy and campaign are read from its
    path when not given, so `bts/men/men.csv` gets the men's 34-item action space. The action is
    `item_id`; the slot (`position`), the hashed user features and the user-item affinities are
    context. The reward is `click`. The logged `propensity_score` is the probability of that item
    in that slot.
    """
    if behavior_policy is not None and behavior_policy not in OBD_BEHAVIOR_POLICIES:
        raise ValueError(f"behavior_policy must be one of {OBD_BEHAVIOR_POLICIES}.")
    if campaign is not None and campaign not in OBD_CAMPAIGNS:
        raise ValueError(f"campaign must be one of {OBD_CAMPAIGNS}.")
    file, policy, camp = _obd_file(Path(path), behavior_policy, campaign)
    frame = pd.read_csv(file, nrows=nrows)
    # The published files carry the pandas index as an unnamed first column.
    frame = frame.loc[:, [c for c in frame.columns if not str(c).startswith("Unnamed")]]
    _require(frame, OBD_REQUIRED, dataset="Open Bandit Dataset")

    items = _ints(frame["item_id"], name="item_id")
    if len(items) and items.min() < 0:
        raise ValueError("item_id must be 0 or more.")
    observed = int(items.max()) + 1 if len(items) else 0
    size = n_items if n_items is not None else max(OBD_N_ITEMS[camp], observed)
    if observed > size:
        raise ValueError(f"item_id {observed - 1} is outside the {size} items given.")

    user_cols = [c for c in frame.columns if str(c).startswith("user_feature_")]
    affinity_cols = [c for c in frame.columns if str(c).startswith("user-item_affinity_")]
    contexts = pd.DataFrame({"position": _ints(frame["position"], name="position")})
    for col in user_cols:
        contexts[col] = frame[col].astype(str).to_numpy()
    for col in affinity_cols:
        contexts[col] = _floats(frame[col], name=col)
    return LoggedBandit(
        source=f"open_bandit/{policy}/{camp}",
        contexts=contexts,
        actions=items,
        propensity=_floats(frame["propensity_score"], name="propensity_score"),
        reward=_floats(frame["click"], name="click"),
        action_names=tuple(f"item_{i}" for i in range(size)),
        reward_name="click",
    )


# --- Hillstrom -----------------------------------------------------------------------------------


def read_hillstrom(
    path: str | Path = HILLSTROM_SAMPLE,
    *,
    reward: Literal["visit", "conversion", "spend"] = "conversion",
    email_cost: float = 0.0,
    nrows: int | None = None,
) -> LoggedBandit:
    """Read the Hillstrom e-mail trial: three randomised arms, so every propensity is exactly 1/3.

    `email_cost` is taken off the reward of every e-mailed customer, so `reward="spend"` with a cost
    becomes net spend: a policy then has to earn its sends rather than e-mail everyone for free.
    """
    if reward not in HILLSTROM_REWARDS:
        raise ValueError(f"reward must be one of {HILLSTROM_REWARDS}.")
    if email_cost < 0:
        raise ValueError("email_cost cannot be negative.")
    frame = pd.read_csv(path, nrows=nrows)
    _require(frame, (*HILLSTROM_FEATURES, "segment", reward), dataset="Hillstrom")

    index = {name: i for i, name in enumerate(HILLSTROM_ACTIONS)}
    labels = frame["segment"].astype(str).to_numpy()
    unknown = sorted({s for s in labels if s not in index})
    if unknown:
        raise ValueError(f"Hillstrom: unknown segment value(s) {unknown}; expected {HILLSTROM_ACTIONS}.")
    actions = np.array([index[s] for s in labels], dtype=np.int64)
    rewards = _floats(frame[reward], name=reward) - email_cost * (actions != 0)

    contexts = pd.DataFrame(index=range(len(frame)))
    for col in HILLSTROM_FEATURES:
        if col in ("history_segment", "zip_code", "channel"):
            contexts[col] = frame[col].astype(str).to_numpy()
        else:
            contexts[col] = _floats(frame[col], name=col)
    return LoggedBandit(
        source="hillstrom",
        contexts=contexts,
        actions=actions,
        propensity=np.full(len(frame), 1.0 / len(HILLSTROM_ACTIONS)),
        reward=rewards.astype(np.float64),
        action_names=HILLSTROM_ACTIONS,
        reward_name=reward if email_cost == 0 else f"{reward} - {email_cost:g} per e-mail",
    )


# --- Criteo uplift -------------------------------------------------------------------------------


def read_criteo_uplift(
    path: str | Path,
    *,
    reward: Literal["visit", "conversion"] = "conversion",
    treatment_rate: float | None = CRITEO_TREATMENT_RATE,
    nrows: int | None = None,
) -> LoggedBandit:
    """Read the Criteo uplift file (or a slice of its 13.9M rows with `nrows`).

    The propensity is the known treatment rate: `treatment_rate` for treated rows and one minus it
    for control rows. Pass `treatment_rate=None` to use the file's own treated share instead, which
    is right for a sample drawn with a different ratio (and for the test fixture).
    """
    if reward not in CRITEO_REWARDS:
        raise ValueError(f"reward must be one of {CRITEO_REWARDS}.")
    frame = pd.read_csv(path, nrows=nrows)
    _require(frame, (*CRITEO_FEATURES, "treatment", reward), dataset="Criteo uplift")

    actions = _ints(frame["treatment"], name="treatment")
    if not np.all((actions == 0) | (actions == 1)):
        raise ValueError("Criteo uplift: treatment must be 0 or 1.")
    rate = float(actions.mean()) if treatment_rate is None else treatment_rate
    if not 0.0 < rate < 1.0:
        raise ValueError("The treatment rate must be strictly between 0 and 1.")
    contexts = pd.DataFrame({col: _floats(frame[col], name=col) for col in CRITEO_FEATURES})
    return LoggedBandit(
        source="criteo_uplift",
        contexts=contexts,
        actions=actions,
        propensity=np.where(actions == 1, rate, 1.0 - rate).astype(np.float64),
        reward=_floats(frame[reward], name=reward),
        action_names=CRITEO_ACTIONS,
        reward_name=reward,
    )
