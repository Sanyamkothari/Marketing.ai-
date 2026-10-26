"""Settings for per-type copy (plan-v3 decision 6), and the weekly cost guard.

Everything here is opt-in: `CopyConfig.enabled` is False by default, and with it off the planner
writes nothing, asks no model and returns an empty plan, so the v2 agent is unchanged.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CopyConfig(BaseModel):
    """How the weekly copy run is sized. Frozen, so a run cannot change its own limits half way."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(default=False, description="Off: the planner does nothing and v2 copy is used")
    seed: int = Field(default=0, description="Makes the customer types and every tie-break repeat")

    # Customer types
    min_types: int = Field(
        default=50, ge=1, description="Fewest types when the customer base is large enough"
    )
    max_types: int = Field(default=300, ge=1, description="Most types; the cost guard scales with it")
    members_per_type: int = Field(default=20, ge=1, description="Aim for at least this many customers a type")
    min_members_floor: int = Field(
        default=5, ge=1, description="A small shop never gets types with fewer customers than this on average"
    )
    max_profile_features: int = Field(
        default=40, ge=0, description="Most `CustomerState.features` columns used"
    )
    fit_sample: int = Field(
        default=50_000, ge=100, description="Customers the types are fitted on; the rest are assigned"
    )
    kmeans_iterations: int = Field(default=25, ge=1)

    # Drafting
    drafts_per_type: int = Field(default=8, ge=5, le=10, description="Drafts written per type and goal")
    drafts_per_call: int = Field(default=2, ge=1, le=10, description="Variants asked for in one model call")
    max_writes_per_week: int | None = Field(
        default=None,
        ge=0,
        description="Cost guard: most drafts written in a week. None: max_types x drafts_per_type",
    )

    # Scoring model
    ridge: float = Field(
        default=5.0, gt=0, description="Strength of the penalty that keeps the scoring model small"
    )
    holdout_share: float = Field(
        default=0.25, gt=0, lt=0.5, description="Share of past drafts held out to test it"
    )
    min_train_purchases: int = Field(default=200, ge=1, description="Fewest purchases in the training part")
    min_test_purchases: int = Field(default=50, ge=1, description="Fewest purchases in the held-out part")
    min_auc_gain: float = Field(
        default=0.01, ge=0, description="The model's AUC must beat the theme-label AUC by this much"
    )
    auc_confidence_z: float = Field(
        default=2.0, ge=0, description="Standard errors the model's AUC, and its lead over themes, must clear"
    )
    bootstrap_draws: int = Field(
        default=200, ge=20, description="Resamples of held-out drafts for those errors"
    )

    # Live test
    top_k: int = Field(default=3, ge=2, description="Drafts per type that go live")
    min_sends_per_arm: int = Field(
        default=300, ge=1, description="No draft is cut before it has this many sends"
    )
    cut_z: float = Field(
        default=3.0, gt=0, description="Width of the confidence bands that must not overlap to cut"
    )

    @model_validator(mode="after")
    def _sane(self) -> CopyConfig:
        if self.min_types > self.max_types:
            raise ValueError("min_types is above max_types")
        if self.top_k > self.drafts_per_type:
            raise ValueError("top_k is above drafts_per_type: there would be nothing to choose from")
        return self

    @property
    def write_cap(self) -> int:
        """Most drafts written in one week."""
        if self.max_writes_per_week is not None:
            return self.max_writes_per_week
        return self.max_types * self.drafts_per_type


class CostGuard:
    """Counts the drafts written this week and refuses a request that would go over the cap."""

    def __init__(self, cap: int) -> None:
        if cap < 0:
            raise ValueError("cap must not be negative")
        self.cap = cap
        self.used = 0
        self.calls = 0

    @property
    def left(self) -> int:
        return self.cap - self.used

    def allow(self, writes: int) -> bool:
        """True when `writes` more drafts still fit under the cap."""
        return writes <= self.left

    def spend(self, writes: int) -> None:
        """Record one model call that was asked for `writes` drafts."""
        if not self.allow(writes):
            raise RuntimeError(f"cost guard: {writes} writes asked, {self.left} left of {self.cap}")
        self.used += writes
        self.calls += 1
