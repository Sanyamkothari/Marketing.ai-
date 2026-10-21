"""The three AI roles of the scientist loop: Data Doctor, Proposer, Judge.

**The idea: the AI proposes and explains, held-out evidence decides.** A model that knows marketing
would have flagged "you are learning from clicks" before anyone built it. So a model reads the shop's
data profile and says what to optimise and what to watch for (Data Doctor), proposes at most three
set-ups from a fixed menu (Proposer), and a separate, sceptical model reads only the inputs and the
held-out evidence and explains the choice (Judge). **Code, not the model, enforces every limit:**

* `DataDoctorDiagnosis`, `Proposal` and `JudgeReport` are pydantic models; every choice is an enum, a
  bounded number or a name from a list. No model writes code, a number that is not in the evidence, or
  a setting outside its range.
* The Data Doctor may be *more cautious* than the profile allows and never less: `click_trust` cannot
  rank above what the control-group comparison supports, `enough_data` cannot be true when the
  thresholds in `enough_data_floor` fail, `feasible_goals` must be a subset of what the events can
  support, the outcome cannot be a sum across currencies when currencies are mixed, and it cannot claim
  a pitfall the profile shows is absent. Code adds the pitfalls it can detect itself.
* The Proposer's candidates come from `CandidateKind` (send-to-all per arm, segment rule, uplift model,
  the current learning bandit agent) with typed `Settings` (lookback, exploration floor, weekly contact
  budget) inside the ranges below and inside the mandate's contact limit. At most `MAX_CANDIDATES`.
  An arm or a column that is not in the profile, a duplicate, or a model the data cannot support is
  refused.
* **The pick is made by a rule written down beforehand** (`evidence_rule_pick`): the candidate with the
  best *lower bound after multiple-comparison correction* on its gain over the current set-up, if that
  bound is above zero and its evaluation sample is at least `MIN_EVAL_N`; otherwise `keep_current`
  (the safe default, a valid and common answer). The Judge may agree, or veto the pick by choosing
  `keep_current` with a concrete flag on it; it can never choose a different candidate. Code also flags
  a pick that is too good to be true (`code_flags`) and blocks it.
* The Judge sees only the data profile, each candidate's typed settings and the evidence: never the
  proposer's rationale or the diagnosis. Its explanation is at most three sentences and every number in
  it must be a number from the evidence (`autonomy.agents.claims.check_numbers`).

When a model's answer cannot be read or breaks a limit it is **rejected, not patched**, and the safe
default is used: a deterministic diagnosis (`rule_diagnosis`), a deterministic proposal
(`default_proposal`), or, for the Judge, the rule's own pick unless code flags it, in which case
`keep_current`. Every result says whether it came from the model or the fallback and lists the problems.

The roles call the model through `ask_json`. `AnswerPending` (see `llm_exchange`) is not caught here: a
`RecordingLLM` stops the run at the first unanswered prompt.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, Literal, TypeVar, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from autonomy.agents.base import AgentOutputError, ask_json, load_prompt
from autonomy.agents.claims import check_numbers
from autonomy.scientist.profile import DataProfile
from engine.llm import LLMClient, LLMError

T = TypeVar("T")

SAFE_DEFAULT_ID = "keep_current"
"""The pick that changes nothing: the current set-up stays (and is the comparator of every gain)."""

# --- the menu and its ranges -----------------------------------------------------------------------

MAX_CANDIDATES = 3
LOOKBACK_DAYS_RANGE = (7, 365)
EXPLORATION_FLOOR_RANGE = (0.01, 0.10)
WEEKLY_BUDGET_RANGE = (1, 7)
MAX_SEGMENT_COLUMNS = 3
DEFAULT_LOOKBACK_DAYS = 90
DEFAULT_EXPLORATION_FLOOR = 0.05
DEFAULT_WEEKLY_BUDGET = 2

# --- thresholds the code applies (written down before any evidence is seen) ------------------------

MIN_CUSTOMERS = 2000
MIN_SENT = 300
MIN_PURCHASES = 100
TRUST_MIN_EXTRA_PER_CLICK = 0.25
"""Click is trusted only when the lower end of its extra purchases per click is at least this."""
SRM_ALPHA = 0.001
MIN_EVAL_N = 500
MIN_OVERLAP_SHARE = 0.10
CALIBRATION_BAND = (0.5, 2.0)
MAX_SENTENCES = 3


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True)
class Limits:
    """What the signed mandate allows: no candidate may use a weekly contact budget above this."""

    max_contacts_per_week: int = 7

    def __post_init__(self) -> None:
        if self.max_contacts_per_week < WEEKLY_BUDGET_RANGE[0]:
            raise ValueError(f"max_contacts_per_week must be at least {WEEKLY_BUDGET_RANGE[0]}")


@dataclass(frozen=True)
class Staged(Generic[T]):
    """A role's result: the value used, where it came from, and why a model answer was rejected."""

    value: T
    source: Literal["model", "fallback"]
    problems: tuple[str, ...] = ()


# --- Data Doctor ---------------------------------------------------------------------------------


class OutcomeMetric(StrEnum):
    NET_PROFIT = "net_profit"
    PURCHASE_RATE = "purchase_rate"
    REVENUE = "revenue"


class ClickTrust(StrEnum):
    TRUST = "trust"
    DISCOUNT = "discount"
    IGNORE = "ignore"


_TRUST_RANK = {ClickTrust.IGNORE: 0, ClickTrust.DISCOUNT: 1, ClickTrust.TRUST: 2}

GoalName = Literal["win_back", "repeat_purchase", "cart_recovery", "cross_sell", "onboarding", "retention"]
GOAL_NAMES: tuple[GoalName, ...] = get_args(GoalName)


class PitfallCode(StrEnum):
    PROXY_METRIC = "proxy_metric"
    NO_CONTROL_GROUP = "no_control_group"
    LEAKAGE = "leakage"
    MIXED_CURRENCIES = "mixed_currencies"
    SRM = "srm"
    NOVELTY = "novelty"
    TINY_SAMPLE = "tiny_sample"
    MULTIPLE_COMPARISONS = "multiple_comparisons"


class Pitfall(_Frozen):
    code: PitfallCode
    evidence: str = Field(min_length=5, max_length=300, description="The numbers or facts that show it")


class DataDoctorDiagnosis(_Frozen):
    outcome_metric: OutcomeMetric
    click_trust: ClickTrust
    click_trust_reason: str = Field(min_length=5, max_length=300)
    enough_data: bool
    enough_data_reason: str = Field(min_length=5, max_length=300)
    feasible_goals: tuple[GoalName, ...]
    pitfalls: tuple[Pitfall, ...] = Field(max_length=len(PitfallCode))


def allowed_outcomes(profile: DataProfile) -> tuple[OutcomeMetric, ...]:
    """Outcomes the data can support: a purchase rate always; money only in one currency, with values."""
    money = (
        not profile.mixed_currencies
        and profile.outcome is not None
        and profile.outcome.mean_order_value is not None
    )
    if money:
        return (OutcomeMetric.NET_PROFIT, OutcomeMetric.REVENUE, OutcomeMetric.PURCHASE_RATE)
    return (OutcomeMetric.PURCHASE_RATE,)


def click_trust_ceiling(profile: DataProfile) -> tuple[ClickTrust, str]:
    """The most trust the control-group comparison supports, with the reason."""
    c, k = profile.control, profile.clicks
    if k is None or not k.clicks_logged:
        return ClickTrust.IGNORE, "no clicks are logged, so there is nothing to trust"
    if c is None or not c.exists:
        return ClickTrust.IGNORE, "no control group, so clicks cannot be checked against purchases"
    per = k.extra_purchases_per_click
    if per is None:
        return ClickTrust.IGNORE, "extra purchases per click cannot be computed"
    text = f"extra purchases per click {per.estimate:.3f} (95% interval {per.low:.3f} to {per.high:.3f})"
    if per.low >= TRUST_MIN_EXTRA_PER_CLICK:
        return ClickTrust.TRUST, text + f", lower end at least {TRUST_MIN_EXTRA_PER_CLICK}"
    if per.estimate > 0:
        return ClickTrust.DISCOUNT, text + f", lower end below {TRUST_MIN_EXTRA_PER_CLICK}"
    return ClickTrust.IGNORE, text + ", not above zero"


def enough_data_floor(profile: DataProfile) -> tuple[bool, str]:
    """True when the thresholds for learning from this data hold; else False with what is short."""
    short: list[str] = []
    if not profile.campaign_found or profile.outcome is None or profile.control is None:
        return False, "no closed campaign with sends was found"
    if profile.customers < MIN_CUSTOMERS:
        short.append(f"{profile.customers} customers (need {MIN_CUSTOMERS})")
    if profile.campaign_customers_sent < MIN_SENT:
        short.append(f"{profile.campaign_customers_sent} customers sent (need {MIN_SENT})")
    if profile.outcome.purchases < MIN_PURCHASES:
        short.append(f"{profile.outcome.purchases} purchases in the window (need {MIN_PURCHASES})")
    if not profile.control.design_probabilities_logged:
        short.append("send probabilities are not logged, so other policies cannot be scored fairly")
    if short:
        return False, "too little: " + "; ".join(short)
    return True, (
        f"{profile.customers} customers, {profile.campaign_customers_sent} sent, "
        f"{profile.outcome.purchases} purchases in the window, probabilities logged"
    )


def feasible_goal_ceiling(profile: DataProfile) -> tuple[GoalName, ...]:
    """Goals whose triggering events exist in the history (a goal needs data to define its audience)."""
    n = profile.events_by_type
    purchases = n.get("purchase", 0) > 0
    out: list[GoalName] = []
    for goal in GOAL_NAMES:
        ok = {
            "win_back": purchases and profile.history_days >= 30,
            "repeat_purchase": purchases,
            "cart_recovery": n.get("cart", 0) > 0,
            "cross_sell": purchases,
            "onboarding": n.get("signup", 0) > 0,
            "retention": purchases and profile.history_days >= 60,
        }[goal]
        if ok:
            out.append(goal)
    return tuple(out)


def detected_pitfalls(profile: DataProfile) -> tuple[Pitfall, ...]:
    """The pitfalls code can see in the profile; the model may add the ones it cannot."""
    out: list[Pitfall] = []
    c = profile.control
    if not profile.campaign_found:
        out.append(
            Pitfall(code=PitfallCode.NO_CONTROL_GROUP, evidence="no closed campaign with sends was found")
        )
    elif c is not None and not c.exists:
        out.append(
            Pitfall(
                code=PitfallCode.NO_CONTROL_GROUP,
                evidence=f"only {c.unsent_customers} unsent customers (need at least 30)",
            )
        )
    if profile.mixed_currencies:
        out.append(
            Pitfall(
                code=PitfallCode.MIXED_CURRENCIES,
                evidence="purchases in " + ", ".join(f"{k} ({v})" for k, v in profile.currencies.items()),
            )
        )
    if c is not None and c.srm_p_value is not None and c.srm_p_value < SRM_ALPHA:
        out.append(
            Pitfall(
                code=PitfallCode.SRM,
                evidence=f"sent and unsent split differs from the design, p = {c.srm_p_value:.2g}",
            )
        )
    ok, why = enough_data_floor(profile)
    if not ok:
        out.append(Pitfall(code=PitfallCode.TINY_SAMPLE, evidence=why))
    if 0 < profile.history_days < 28:
        out.append(
            Pitfall(
                code=PitfallCode.NOVELTY,
                evidence=f"history covers {profile.history_days:.0f} days; early effects may fade",
            )
        )
    ev = profile.events_by_type
    if ev.get("click", 0) > 0 and ev.get("purchase", 0) == 0:
        out.append(Pitfall(code=PitfallCode.PROXY_METRIC, evidence="clicks are logged but no purchases are"))
    return tuple(out)


def _absent_pitfalls(profile: DataProfile) -> set[PitfallCode]:
    """Pitfalls the profile shows are NOT present; a model may not assert them."""
    absent: set[PitfallCode] = set()
    c = profile.control
    if c is not None and c.exists:
        absent.add(PitfallCode.NO_CONTROL_GROUP)
    if not profile.mixed_currencies:
        absent.add(PitfallCode.MIXED_CURRENCIES)
    if c is not None and c.srm_p_value is not None and c.srm_p_value >= SRM_ALPHA:
        absent.add(PitfallCode.SRM)
    return absent


def rule_diagnosis(profile: DataProfile) -> DataDoctorDiagnosis:
    """The safe default: every field from the profile by fixed rules, no model."""
    trust, trust_why = click_trust_ceiling(profile)
    ok, why = enough_data_floor(profile)
    allowed = allowed_outcomes(profile)
    return DataDoctorDiagnosis(
        outcome_metric=allowed[0],
        click_trust=trust,
        click_trust_reason=trust_why,
        enough_data=ok,
        enough_data_reason=why,
        feasible_goals=feasible_goal_ceiling(profile),
        pitfalls=detected_pitfalls(profile),
    )


def check_diagnosis(d: DataDoctorDiagnosis, profile: DataProfile) -> list[str]:
    """Why `d` breaks a limit the profile sets (empty when it does not)."""
    problems: list[str] = []
    if d.outcome_metric not in allowed_outcomes(profile):
        problems.append(
            f"outcome_metric {d.outcome_metric.value} is not supported here "
            f"(allowed: {', '.join(m.value for m in allowed_outcomes(profile))})"
        )
    ceiling, _ = click_trust_ceiling(profile)
    if _TRUST_RANK[d.click_trust] > _TRUST_RANK[ceiling]:
        problems.append(
            f"click_trust {d.click_trust.value} is above what the data supports ({ceiling.value})"
        )
    floor_ok, _ = enough_data_floor(profile)
    if d.enough_data and not floor_ok:
        problems.append("enough_data is true but the data thresholds fail")
    allowed_goals = set(feasible_goal_ceiling(profile))
    extra = [g for g in d.feasible_goals if g not in allowed_goals]
    if extra:
        problems.append(f"feasible_goals not supported by the events: {', '.join(extra)}")
    if len(set(d.feasible_goals)) != len(d.feasible_goals):
        problems.append("feasible_goals has duplicates")
    absent = _absent_pitfalls(profile)
    for p in d.pitfalls:
        if p.code in absent:
            problems.append(f"pitfall {p.code.value} is claimed but the profile shows it is absent")
    if len({p.code for p in d.pitfalls}) != len(d.pitfalls):
        problems.append("pitfalls has duplicate codes")
    return problems


def merge_pitfalls(model: Iterable[Pitfall], profile: DataProfile) -> tuple[Pitfall, ...]:
    """Code-detected pitfalls first (their evidence is computed), then the model's other ones."""
    seen: set[PitfallCode] = set()
    out: list[Pitfall] = []
    for p in (*detected_pitfalls(profile), *model):
        if p.code not in seen:
            seen.add(p.code)
            out.append(p)
    return tuple(out)


# --- Proposer ------------------------------------------------------------------------------------


class CandidateKind(StrEnum):
    SEND_TO_ALL = "send_to_all"
    SEGMENT_RULE = "segment_rule"
    UPLIFT_MODEL = "uplift_model"
    BANDIT_AGENT = "bandit_agent"


class Settings(_Frozen):
    lookback_days: int = Field(ge=LOOKBACK_DAYS_RANGE[0], le=LOOKBACK_DAYS_RANGE[1])
    exploration_floor: float = Field(ge=EXPLORATION_FLOOR_RANGE[0], le=EXPLORATION_FLOOR_RANGE[1])
    weekly_contact_budget: int = Field(ge=WEEKLY_BUDGET_RANGE[0], le=WEEKLY_BUDGET_RANGE[1])


class CandidateSpec(_Frozen):
    kind: CandidateKind
    arm: str | None = Field(default=None, description="send_to_all only: the message (arm) id to send")
    columns: tuple[str, ...] = Field(default=(), description="segment_rule only: 1 to 3 feature names")
    settings: Settings
    rationale: str = Field(min_length=5, max_length=200)

    @field_validator("rationale")
    @classmethod
    def _one_line(cls, v: str) -> str:
        if "\n" in v or "\r" in v:
            raise ValueError("rationale must be one line")
        return v

    @model_validator(mode="after")
    def _shape(self) -> CandidateSpec:
        if self.kind is CandidateKind.SEND_TO_ALL:
            if not self.arm or self.columns:
                raise ValueError("send_to_all needs an arm and no columns")
        elif self.kind is CandidateKind.SEGMENT_RULE:
            if self.arm is not None or not 1 <= len(self.columns) <= MAX_SEGMENT_COLUMNS:
                raise ValueError(f"segment_rule needs 1 to {MAX_SEGMENT_COLUMNS} columns and no arm")
            if len(set(self.columns)) != len(self.columns):
                raise ValueError("segment_rule columns repeat")
        elif self.arm is not None or self.columns:
            raise ValueError(f"{self.kind.value} takes no arm and no columns")
        return self

    def key(self) -> tuple[object, ...]:
        s = self.settings
        return (
            self.kind,
            self.arm,
            tuple(sorted(self.columns)),
            s.lookback_days,
            s.exploration_floor,
            s.weekly_contact_budget,
        )


class Proposal(_Frozen):
    candidates: tuple[CandidateSpec, ...] = Field(min_length=1, max_length=MAX_CANDIDATES)


def candidate_ids(proposal: Proposal) -> tuple[str, ...]:
    """Stable ids code gives the candidates: `c1_send_to_all`, `c2_bandit_agent`, ..."""
    return tuple(f"c{i + 1}_{c.kind.value}" for i, c in enumerate(proposal.candidates))


def check_proposal(p: Proposal, profile: DataProfile, limits: Limits) -> list[str]:
    """Why `p` breaks the menu or the data's limits (empty when it does not)."""
    problems: list[str] = []
    arms, feats = set(profile.arm_ids), set(profile.feature_names)
    logged = profile.control is not None and profile.control.design_probabilities_logged
    seen: set[tuple[object, ...]] = set()
    for i, c in enumerate(p.candidates, start=1):
        tag = f"candidate {i} ({c.kind.value})"
        if c.arm is not None and c.arm not in arms:
            problems.append(f"{tag}: arm {c.arm!r} is not an arm of the logged campaign")
        missing = [x for x in c.columns if x not in feats]
        if missing:
            problems.append(f"{tag}: columns not in the data: {', '.join(missing)}")
        if c.kind in (CandidateKind.SEGMENT_RULE, CandidateKind.UPLIFT_MODEL) and not feats:
            problems.append(f"{tag}: needs customer features and the profile lists none")
        if c.kind in (CandidateKind.SEGMENT_RULE, CandidateKind.UPLIFT_MODEL) and not logged:
            problems.append(f"{tag}: needs logged send probabilities to be scored fairly")
        if c.settings.weekly_contact_budget > limits.max_contacts_per_week:
            problems.append(
                f"{tag}: weekly_contact_budget {c.settings.weekly_contact_budget} is above the "
                f"mandate's {limits.max_contacts_per_week}"
            )
        if c.key() in seen:
            problems.append(f"{tag}: duplicates an earlier candidate")
        seen.add(c.key())
    return problems


def default_proposal(profile: DataProfile, limits: Limits) -> Proposal:
    """The safe default: the current learning agent, the best blanket arm, and one data-driven rule."""
    budget = max(WEEKLY_BUDGET_RANGE[0], min(DEFAULT_WEEKLY_BUDGET, limits.max_contacts_per_week))
    settings = Settings(
        lookback_days=DEFAULT_LOOKBACK_DAYS,
        exploration_floor=DEFAULT_EXPLORATION_FLOOR,
        weekly_contact_budget=budget,
    )
    out: list[CandidateSpec] = []
    if profile.arms:
        best = max(profile.arms, key=lambda a: (a.purchase_rate, a.customers))
        out.append(
            CandidateSpec(
                kind=CandidateKind.SEND_TO_ALL,
                arm=best.arm,
                settings=settings,
                rationale="Safe default: send the arm with the highest purchase rate to everyone.",
            )
        )
    logged = profile.control is not None and profile.control.design_probabilities_logged
    if profile.features and logged:
        control = profile.control is not None and profile.control.exists
        out.append(
            CandidateSpec(
                kind=CandidateKind.UPLIFT_MODEL if control else CandidateKind.SEGMENT_RULE,
                columns=() if control else tuple(profile.feature_names[:2]),
                settings=settings,
                rationale="Safe default: target by customer features, to be scored on held-out customers.",
            )
        )
    out.append(
        CandidateSpec(
            kind=CandidateKind.BANDIT_AGENT,
            settings=settings,
            rationale="Safe default: keep the current learning agent with standard settings.",
        )
    )
    return Proposal(candidates=tuple(out[:MAX_CANDIDATES]))


# --- Evidence and the Judge ----------------------------------------------------------------------


class CandidateEvidence(_Frozen):
    """What held-out data says about one candidate's gain over the current set-up (same unit as the table)."""

    candidate_id: str
    n_eval: int = Field(ge=0, description="Customers or logged decisions it was evaluated on, not fitted on")
    gain_estimate: float
    gain_low: float = Field(description="Lower bound after the multiple-comparison correction")
    gain_high: float
    adjusted_p: float | None = None
    effective_sample_share: float | None = Field(default=None, ge=0.0, le=1.0)
    forecast_vs_actual: float | None = Field(default=None, description="Forecast gain / measured gain")
    split: Literal["time", "customer"] = "customer"


class EvidenceTable(_Frozen):
    unit: str = Field(min_length=1, description="e.g. 'profit per customer'")
    correction: Literal["holm", "bonferroni"]
    comparisons: int = Field(ge=1)
    current_value: float | None = None
    candidates: tuple[CandidateEvidence, ...]

    @model_validator(mode="after")
    def _corrected_for_every_candidate(self) -> EvidenceTable:
        ids = [c.candidate_id for c in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("candidate ids repeat in the evidence")
        if self.comparisons < len(ids):
            raise ValueError(
                f"comparisons ({self.comparisons}) is below the number of candidates ({len(ids)}): "
                "the lower bounds cannot be corrected for all of them"
            )
        return self

    def numbers(self) -> dict[str, float]:
        out: dict[str, float] = {
            "comparisons": float(self.comparisons),
            "candidates": float(len(self.candidates)),
        }
        if self.current_value is not None:
            out["current_value"] = self.current_value
        for c in self.candidates:
            for name in (
                "n_eval",
                "gain_estimate",
                "gain_low",
                "gain_high",
                "adjusted_p",
                "effective_sample_share",
                "forecast_vs_actual",
            ):
                v = getattr(c, name)
                if v is not None:
                    out[f"{c.candidate_id}.{name}"] = float(v)
        return out


def evidence_rule_pick(table: EvidenceTable) -> str:
    """THE RULE, fixed in advance: the candidate with the best corrected lower bound on its gain over the
    current set-up, if that bound is above zero and it was evaluated on at least `MIN_EVAL_N`; else the
    safe default. Ties go to the candidate listed first. No model is involved."""
    eligible = [c for c in table.candidates if c.n_eval >= MIN_EVAL_N and c.gain_low > 0]
    if not eligible:
        return SAFE_DEFAULT_ID
    return max(eligible, key=lambda c: c.gain_low).candidate_id


class FlagCode(StrEnum):
    GAIN_IMPLAUSIBLE = "gain_implausible"
    CALIBRATION_OFF = "calibration_off"
    TINY_SAMPLE = "tiny_sample"
    LOW_OVERLAP = "low_overlap"
    INCONSISTENT_EVIDENCE = "inconsistent_evidence"
    LEAKAGE_SUSPECTED = "leakage_suspected"


class TooGoodFlag(_Frozen):
    candidate_id: str
    code: FlagCode
    evidence: str = Field(min_length=5, max_length=300)


def code_flags(table: EvidenceTable) -> tuple[TooGoodFlag, ...]:
    """Problems code can see in the evidence itself (the Judge may add ones it cannot)."""
    out: list[TooGoodFlag] = []
    for c in table.candidates:
        nums = (c.gain_estimate, c.gain_low, c.gain_high)
        if any(not math.isfinite(x) for x in nums) or not (c.gain_low <= c.gain_estimate <= c.gain_high):
            out.append(
                TooGoodFlag(
                    candidate_id=c.candidate_id,
                    code=FlagCode.INCONSISTENT_EVIDENCE,
                    evidence="the estimate is not between its own bounds or is not a number",
                )
            )
            continue
        if c.n_eval < MIN_EVAL_N:
            out.append(
                TooGoodFlag(
                    candidate_id=c.candidate_id,
                    code=FlagCode.TINY_SAMPLE,
                    evidence=f"evaluated on {c.n_eval} (need {MIN_EVAL_N})",
                )
            )
        if c.effective_sample_share is not None and c.effective_sample_share < MIN_OVERLAP_SHARE:
            out.append(
                TooGoodFlag(
                    candidate_id=c.candidate_id,
                    code=FlagCode.LOW_OVERLAP,
                    evidence=f"effective sample share {c.effective_sample_share:.3f} (need {MIN_OVERLAP_SHARE})",
                )
            )
        lo, hi = CALIBRATION_BAND
        if c.forecast_vs_actual is not None and not lo <= c.forecast_vs_actual <= hi:
            out.append(
                TooGoodFlag(
                    candidate_id=c.candidate_id,
                    code=FlagCode.CALIBRATION_OFF,
                    evidence=f"forecast over actual {c.forecast_vs_actual:.2f}, outside {lo} to {hi}",
                )
            )
        if (
            table.current_value is not None
            and table.current_value > 0
            and c.gain_estimate > table.current_value
        ):
            out.append(
                TooGoodFlag(
                    candidate_id=c.candidate_id,
                    code=FlagCode.GAIN_IMPLAUSIBLE,
                    evidence=(
                        f"gain {c.gain_estimate:.4g} is more than the current value {table.current_value:.4g}"
                    ),
                )
            )
    return tuple(out)


def _sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s])


class JudgeReport(_Frozen):
    pick: str = Field(min_length=1, description="A candidate id or keep_current")
    too_good_to_be_true: tuple[TooGoodFlag, ...] = ()
    explanation: str = Field(min_length=5, max_length=900)
    needs_human: str | None = Field(default=None, max_length=300)

    @field_validator("explanation")
    @classmethod
    def _short(cls, v: str) -> str:
        if _sentences(v) > MAX_SENTENCES:
            raise ValueError(f"explanation must be at most {MAX_SENTENCES} sentences")
        return v

    @field_validator("needs_human")
    @classmethod
    def _question(cls, v: str | None) -> str | None:
        if v is not None and not v.strip().endswith("?"):
            raise ValueError("needs_human must be a question, or null")
        return v


def check_judge(
    report: JudgeReport, table: EvidenceTable, rule_pick: str, *, flagged_by_code: Iterable[TooGoodFlag] = ()
) -> list[str]:
    """Why `report` breaks a limit (empty when it does not). The pick may differ from the rule's only
    by vetoing it: `keep_current`, with a concrete flag on the rule's pick."""
    ids = {c.candidate_id for c in table.candidates}
    problems: list[str] = []
    if report.pick != SAFE_DEFAULT_ID and report.pick not in ids:
        problems.append(f"pick {report.pick!r} is not a candidate or {SAFE_DEFAULT_ID}")
    for f in report.too_good_to_be_true:
        if f.candidate_id not in ids:
            problems.append(f"flag on unknown candidate {f.candidate_id!r}")
    if report.pick != rule_pick:
        vetoes = report.pick == SAFE_DEFAULT_ID and any(
            f.candidate_id == rule_pick for f in (*report.too_good_to_be_true, *flagged_by_code)
        )
        if not vetoes:
            problems.append(
                f"pick {report.pick!r} differs from the evidence rule's {rule_pick!r} without a concrete flag "
                f"on {rule_pick!r} and {SAFE_DEFAULT_ID}"
            )
    # candidate ids ("c2_uplift_model") carry a digit that is a label, not a claim: strip them first
    text = report.explanation
    for name in sorted({*ids, SAFE_DEFAULT_ID}, key=len, reverse=True):
        text = text.replace(name, " ")
    bad = check_numbers(text, table.numbers())
    if bad:
        problems.append("explanation has numbers that are not in the evidence: " + ", ".join(bad))
    return problems


@dataclass(frozen=True)
class JudgeDecision:
    """The final pick and why. `report` is the model's when it was accepted, else code's own."""

    final_pick: str
    rule_pick: str
    report: JudgeReport
    source: Literal["model", "fallback"]
    vetoed_by: Literal["model", "code"] | None
    code_flags: tuple[TooGoodFlag, ...]
    problems: tuple[str, ...] = ()


def _fallback_report(
    final: str, rule_pick: str, flags: tuple[TooGoodFlag, ...], table: EvidenceTable
) -> JudgeReport:
    if final == SAFE_DEFAULT_ID and rule_pick == SAFE_DEFAULT_ID:
        text = (
            "No candidate's corrected lower bound on its gain was above zero with enough evaluation data. "
            "The current set-up stays."
        )
        ask = None
    elif final == SAFE_DEFAULT_ID:
        codes = ", ".join(sorted({f.code.value for f in flags if f.candidate_id == rule_pick}))
        text = (
            f"{rule_pick} had the best corrected lower bound, but it looks too good to be true ({codes}). "
            "The current set-up stays until a person has looked."
        )
        ask = f"Should {rule_pick} be re-evaluated on newer data before anything changes?"
    else:
        row = next(c for c in table.candidates if c.candidate_id == final)
        text = (
            f"{final} has the best corrected lower bound on its gain, {row.gain_low:.4g} {table.unit}. "
            "It starts in Shadow and is monitored weekly."
        )
        ask = None
    return JudgeReport(
        pick=final,
        too_good_to_be_true=tuple(f for f in flags if f.candidate_id == rule_pick),
        explanation=text,
        needs_human=ask,
    )


# --- prompts -------------------------------------------------------------------------------------


def menu_text(limits: Limits) -> str:
    """The menu as the Proposer sees it, built from the same constants the code enforces."""
    return json.dumps(
        {
            "kinds": {
                "send_to_all": "send one arm to every customer; set arm to an arm id; columns empty",
                "segment_rule": f"send to customers in segments of 1-{MAX_SEGMENT_COLUMNS} feature columns; arm null",
                "uplift_model": "a per-customer uplift model on customer features; arm null, columns empty",
                "bandit_agent": "the current learning bandit agent; arm null, columns empty",
            },
            "settings": {
                "lookback_days": {
                    "min": LOOKBACK_DAYS_RANGE[0],
                    "max": LOOKBACK_DAYS_RANGE[1],
                    "integer": True,
                },
                "exploration_floor": {"min": EXPLORATION_FLOOR_RANGE[0], "max": EXPLORATION_FLOOR_RANGE[1]},
                "weekly_contact_budget": {
                    "min": WEEKLY_BUDGET_RANGE[0],
                    "max": min(WEEKLY_BUDGET_RANGE[1], limits.max_contacts_per_week),
                    "integer": True,
                },
            },
            "max_candidates": MAX_CANDIDATES,
        },
        separators=(",", ":"),
    )


def doctor_prompt(profile: DataProfile) -> str:
    return (
        "Diagnose this shop's data.\n\n"
        f"Data profile (numbers only, no customer rows):\n{profile.to_prompt_json()}\n\n"
        f"Allowed outcome_metric values here: {[m.value for m in allowed_outcomes(profile)]}\n"
        f"Goals the events can support: {list(feasible_goal_ceiling(profile))}\n"
        f"Pitfall codes: {[c.value for c in PitfallCode]}"
    )


def proposer_prompt(profile: DataProfile, diagnosis: DataDoctorDiagnosis, limits: Limits) -> str:
    return (
        "Propose one to three candidate set-ups to evaluate on held-out data.\n\n"
        f"Data profile:\n{profile.to_prompt_json()}\n\n"
        f"Diagnosis:\n{diagnosis.model_dump_json()}\n\n"
        f"Menu and ranges:\n{menu_text(limits)}\n\n"
        f"Arm ids: {list(profile.arm_ids)}\nFeature columns: {list(profile.feature_names)}"
    )


def judge_prompt(
    profile: DataProfile,
    proposal: Proposal,
    table: EvidenceTable,
    rule_pick: str,
    flags: tuple[TooGoodFlag, ...],
) -> str:
    """Inputs and evidence only: each candidate's typed settings, never its rationale; no diagnosis."""
    cands = {
        cid: c.model_dump(mode="json", exclude={"rationale"})
        for cid, c in zip(candidate_ids(proposal), proposal.candidates, strict=True)
    }
    return (
        "Judge these candidates from the evidence only.\n\n"
        f"Data profile:\n{profile.to_prompt_json()}\n\n"
        f"Candidates (settings only):\n{json.dumps(cands, separators=(',', ':'))}\n\n"
        f"Held-out evidence (gain over {SAFE_DEFAULT_ID}, correction applied):\n{table.model_dump_json()}\n\n"
        f"Evidence rule's pick: {rule_pick}\n"
        f"Safe default: {SAFE_DEFAULT_ID}\n"
        f"Problems code found: {json.dumps([f.model_dump(mode='json') for f in flags], separators=(',', ':'))}"
    )


# --- running a role ------------------------------------------------------------------------------


def run_data_doctor(
    llm: LLMClient, profile: DataProfile, *, model_id: str | None = None
) -> Staged[DataDoctorDiagnosis]:
    """Ask the Data Doctor; a rejected or unreadable answer is replaced by `rule_diagnosis`."""
    try:
        answer = ask_json(
            llm,
            DataDoctorDiagnosis,
            system=load_prompt("data_doctor"),
            prompt=doctor_prompt(profile),
            model_id=model_id,
            max_tokens=2000,
        )
    except (AgentOutputError, LLMError) as exc:
        return Staged(rule_diagnosis(profile), "fallback", (f"model answer unusable: {exc}",))
    problems = check_diagnosis(answer, profile)
    if problems:
        return Staged(rule_diagnosis(profile), "fallback", tuple(problems))
    merged = answer.model_copy(update={"pitfalls": merge_pitfalls(answer.pitfalls, profile)})
    return Staged(merged, "model")


def run_proposer(
    llm: LLMClient,
    profile: DataProfile,
    diagnosis: DataDoctorDiagnosis,
    limits: Limits,
    *,
    model_id: str | None = None,
) -> Staged[Proposal]:
    """Ask the Proposer; a rejected or unreadable answer is replaced by `default_proposal`."""
    try:
        answer = ask_json(
            llm,
            Proposal,
            system=load_prompt("proposer"),
            prompt=proposer_prompt(profile, diagnosis, limits),
            model_id=model_id,
            max_tokens=2000,
        )
    except (AgentOutputError, LLMError) as exc:
        return Staged(default_proposal(profile, limits), "fallback", (f"model answer unusable: {exc}",))
    problems = check_proposal(answer, profile, limits)
    if problems:
        return Staged(default_proposal(profile, limits), "fallback", tuple(problems))
    return Staged(answer, "model")


def run_judge(
    llm: LLMClient,
    profile: DataProfile,
    proposal: Proposal,
    table: EvidenceTable,
    *,
    model_id: str | None = None,
) -> JudgeDecision:
    """The decision: the evidence rule's pick, which the Judge may only veto and code may block."""
    ids = set(candidate_ids(proposal))
    have = {c.candidate_id for c in table.candidates}
    if have != ids:
        raise ValueError(f"evidence candidates {sorted(have)} do not match the proposal's {sorted(ids)}")
    rule_pick = evidence_rule_pick(table)
    flags = code_flags(table)
    blocked = rule_pick != SAFE_DEFAULT_ID and any(f.candidate_id == rule_pick for f in flags)
    try:
        report = ask_json(
            llm,
            JudgeReport,
            system=load_prompt("judge"),
            prompt=judge_prompt(profile, proposal, table, rule_pick, flags),
            model_id=model_id,
            max_tokens=1500,
        )
    except (AgentOutputError, LLMError) as exc:
        return _fallback_decision(table, rule_pick, flags, blocked, (f"model answer unusable: {exc}",))
    problems = check_judge(report, table, rule_pick, flagged_by_code=flags)
    if problems:
        return _fallback_decision(table, rule_pick, flags, blocked, tuple(problems))
    if report.pick == SAFE_DEFAULT_ID and rule_pick != SAFE_DEFAULT_ID:
        return JudgeDecision(SAFE_DEFAULT_ID, rule_pick, report, "model", "model", flags)
    if blocked:
        text = report.model_copy(
            update={
                "needs_human": report.needs_human
                or f"Should {rule_pick} be re-evaluated on newer data before anything changes?"
            }
        )
        return JudgeDecision(SAFE_DEFAULT_ID, rule_pick, text, "model", "code", flags)
    return JudgeDecision(rule_pick, rule_pick, report, "model", None, flags)


def _fallback_decision(
    table: EvidenceTable,
    rule_pick: str,
    flags: tuple[TooGoodFlag, ...],
    blocked: bool,
    problems: tuple[str, ...],
) -> JudgeDecision:
    final = SAFE_DEFAULT_ID if blocked else rule_pick
    return JudgeDecision(
        final,
        rule_pick,
        _fallback_report(final, rule_pick, flags, table),
        "fallback",
        "code" if blocked else None,
        flags,
        problems,
    )


def table_from_mapping(data: Mapping[str, object]) -> EvidenceTable:
    """An `EvidenceTable` from parsed JSON (the CLI's `--evidence` file)."""
    return EvidenceTable.model_validate(data)


__all__ = [
    "CALIBRATION_BAND",
    "DEFAULT_EXPLORATION_FLOOR",
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_WEEKLY_BUDGET",
    "EXPLORATION_FLOOR_RANGE",
    "GOAL_NAMES",
    "LOOKBACK_DAYS_RANGE",
    "MAX_CANDIDATES",
    "MIN_EVAL_N",
    "SAFE_DEFAULT_ID",
    "WEEKLY_BUDGET_RANGE",
    "CandidateEvidence",
    "CandidateKind",
    "CandidateSpec",
    "ClickTrust",
    "DataDoctorDiagnosis",
    "EvidenceTable",
    "FlagCode",
    "JudgeDecision",
    "JudgeReport",
    "Limits",
    "OutcomeMetric",
    "Pitfall",
    "PitfallCode",
    "Proposal",
    "Settings",
    "Staged",
    "TooGoodFlag",
    "allowed_outcomes",
    "candidate_ids",
    "check_diagnosis",
    "check_judge",
    "check_proposal",
    "click_trust_ceiling",
    "code_flags",
    "default_proposal",
    "detected_pitfalls",
    "doctor_prompt",
    "enough_data_floor",
    "evidence_rule_pick",
    "feasible_goal_ceiling",
    "judge_prompt",
    "menu_text",
    "merge_pitfalls",
    "proposer_prompt",
    "rule_diagnosis",
    "run_data_doctor",
    "run_judge",
    "run_proposer",
    "table_from_mapping",
]
