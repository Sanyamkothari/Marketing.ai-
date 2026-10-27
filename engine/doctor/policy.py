"""A policy for every proposal kind (Plan O M233, DEC-1803; the design §5.4, §6.2, §6.6, §11).

Data Doctor's PROPOSE tools - a step, a setting, a person's edit, a join, a stack, a build, a split - are each
decided by a **rule set** in `configs/agentic/policy.yaml` (its `data:` section). The policy is Plan K's, applied to
data (`engine.agentic.policy`): rules are pure functions of `(DataProposal, DataPolicyContext)` that return a
Plan K `Decision` or nothing, the context is computed by the server, never taken from what a model wrote, and:

* **Default deny.** A proposal kind with no rule set is refused (`POLICY_NO_RULES`); a proposal not shaped as its
  kind says (`steps`, `setting` or `plan`) is refused (`POLICY_PAYLOAD_INVALID`); a rule set none of whose rules
  applies allows nothing (`POLICY_NO_RULES`). Otherwise it is allowed only when every rule that applies allows it.
* **What the rule sets hold** (the plan's §M233): the allowed step kinds, never the ID or the outcome, personal
  data hidden only, the levels, the never-ticked kinds, the setting allow-list and the APPROVE-class kinds - and
  the rules the wave-1 tools asked for: each tool's parameters (M240's Excel day numbers stay the rules', M241's
  `indian_scale_words`), the yes/no spellings (M241: both sides, from the lexicons, never `na`), decision O10's
  change-share guard, decision O3's reshape guards (M238), and the "new patterns, with permission" rule of the
  approval (an AI's fix cites a look at the column, is tried on the first 1,000 rows, and is never ticked).
* **The code keeps its own checks.** Plan G's hard-coded rules (`loop.CHAT_STEP_KINDS`, the personal-data
  refusal, `setting_allowed`, `advisor.NEVER_TICKED_STEPS`, `check_recipe`'s ID and outcome rule) stay where they
  are. Behind `data_doctor.unified` the callers (`engine.agent.loop`, `engine.agent.edits`, the advisor and the
  registry) also ask the policy, at the same place and with the same refusal code, so a refusal today is still
  one, with the same code; with the switch off nothing here is read.

Every rule is mutation-tested: removing it from :data:`DATA_RULES`, or from any rule set that names it, changes
the decision on at least one fixture (`tests/unit/doctor/test_policy_mutation.py`, after Plan K's
`engine.agentic.evals.mutation_test`).
"""

from __future__ import annotations

import functools
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from engine.access.roles import Principal, Role
from engine.agent.config import AgentLevel
from engine.agent.contracts import ROW_RESHAPE_STEP_KINDS, ProposalSource, RecipeStep, RecipeStepKind
from engine.agentic.errors import AgenticError
from engine.agentic.policy import Decision

if TYPE_CHECKING:
    from engine.agent.tools import AgentContext
    from engine.config import FieldSpec
    from engine.doctor.reshape_guard import ReshapeItem

__all__ = [
    "DATA_POLICY_CODES",
    "DATA_RULES",
    "DATA_RULE_NAMES",
    "FILES_KINDS",
    "HANDLER_CHECKED",
    "NOT_ALLOWED_CODES",
    "OUTPUT_READERS",
    "POLICY_KINDS",
    "ApprovalFacts",
    "ChangeShareSettings",
    "DataPolicyConfig",
    "DataPolicyContext",
    "DataProposal",
    "DataRule",
    "DataRuleSet",
    "ParamRule",
    "SettingChange",
    "StepEffect",
    "StepFacts",
    "agent_gate",
    "agent_policy_context",
    "allowed",
    "check_tool_output",
    "data_policy_on",
    "effect_of",
    "evaluate",
    "first_refusal",
    "keeps_its_values",
    "load_data_policy",
    "never_ticked_steps",
    "output_proposals",
    "policy_kind_for",
    "policy_or_deny",
    "refusal",
    "rule_mutants",
    "rule_set_mutants",
    "tool_policy_context",
    "warnings_of",
]

# ---------------------------------------------------------------------------
# Names and codes
# ---------------------------------------------------------------------------
STEP_KIND: Final[str] = "data.step_kind"
STEP_PARAMS: Final[str] = "data.step_params"
SPELLINGS: Final[str] = "data.spellings"
PROTECTED: Final[str] = "data.protected_columns"
PERSONAL: Final[str] = "data.personal_data"
LEVEL: Final[str] = "data.level"
GROUNDED: Final[str] = "data.grounded"
TRIED: Final[str] = "data.tried"
NEVER_TICKED: Final[str] = "data.never_ticked"
CHANGE_SHARE: Final[str] = "data.change_share"
SETTING: Final[str] = "data.setting"
APPROVE_CLASS: Final[str] = "data.approve_class"
RESHAPE_GUARD: Final[str] = "data.reshape_guard"

DATA_RULE_NAMES: Final[tuple[str, ...]] = (
    STEP_KIND,
    STEP_PARAMS,
    SPELLINGS,
    PROTECTED,
    PERSONAL,
    LEVEL,
    GROUNDED,
    TRIED,
    NEVER_TICKED,
    CHANGE_SHARE,
    SETTING,
    APPROVE_CLASS,
    RESHAPE_GUARD,
)
"""Every data rule this module implements. A rule set naming another is refused when the policy is read."""

DATA_POLICY_CODES: Final[frozenset[str]] = frozenset(
    {"POLICY_NEVER_TICKED", "POLICY_STEP_NOT_TRIED", "POLICY_BOOLEAN_SPELLING", "APPROVAL_SENTENCE_CHANGED"}
)
"""The codes M233 adds (for `PLAN_O_CODES` and `configs/pilot/help.yaml` at integration). Every other refusal
reuses the code the same refusal has today: Plan G's `AGENT_STEP_NOT_ALLOWED`, `AGENT_EDIT_NOT_ALLOWED`,
`AGENT_STEP_UNGROUNDED`, `AGENT_SETTING_NOT_ALLOWED` and `RECIPE_STEP_INVALID`, Plan K's `POLICY_NO_RULES`,
`POLICY_PAYLOAD_INVALID`, `POLICY_ROLE_SEPARATION` and `TOOL_ROLE_REQUIRED`, and M238's `AI_STEP_CHANGES_MANY`,
`AI_STEP_CHANGES_TOO_MANY` and `RESHAPE_GUARD_MISSING`."""

NOT_ALLOWED_CODES: Final[tuple[str, ...]] = ("AGENT_STEP_NOT_ALLOWED", "AGENT_EDIT_NOT_ALLOWED")
"""What a rule set may name as its refusal of a step kind or of a personal-data column: the chat's code, or the
person's edit's (`engine.agent.edits`), so the policy refuses with the code the caller refuses with today."""

Source = Literal["rules", "ai", "person"]

POLICY_KINDS: Final[Mapping[str, str]] = {
    "propose_setting": "data.setting",
    "propose_step": "data.recipe_step",
    "edit_by_hand": "data.person_edit",
    "find_excel_serial_dates": "data.excel_serial_dates",
}
"""The policy kind of each tool whose kind is not `data.<tool name>` (M232 named the chat's three; M240's
`find_excel_serial_dates` is a READ tool whose step is the Data Doctor's own fix of Excel day numbers)."""


def policy_kind_for(tool_name: str) -> str:
    """The rule set a tool's proposals are decided by: `data.<tool>` unless :data:`POLICY_KINDS` says otherwise."""
    return POLICY_KINDS.get(tool_name, f"data.{tool_name}")


# ---------------------------------------------------------------------------
# The configuration (`configs/agentic/policy.yaml`, section `data`)
# ---------------------------------------------------------------------------
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ParamRule(_Strict):
    """What a rule set lets a step kind carry: the parameters it may name, and values some must have."""

    allowed: tuple[str, ...] = Field(description="Every parameter the step may name; any other is refused.")
    equals: dict[str, Any] = Field(
        default_factory=dict, description="Parameters that must be present with exactly this value."
    )
    one_of: dict[str, tuple[Any, ...]] = Field(
        default_factory=dict, description="Parameters that, when present, must hold one of these values."
    )

    @model_validator(mode="after")
    def _within(self) -> ParamRule:
        named = set(self.equals) | set(self.one_of)
        if not named <= set(self.allowed):
            raise ValueError(
                f"equals and one_of name parameters not allowed: {sorted(named - set(self.allowed))}"
            )
        return self


class DataRuleSet(_Strict):
    """One PROPOSE kind's rules, evaluated in this order (every one, so a refusal lists every rule it breaks)."""

    rules: tuple[str, ...]
    proposes: Literal["steps", "setting", "plan"] = Field(
        description="What a proposal of this kind carries: recipe steps, one setting, or a plan (join, stack, "
        "build, split) whose steps are optional."
    )
    step_kinds: tuple[RecipeStepKind, ...] = ()
    params: dict[RecipeStepKind, ParamRule] = Field(default_factory=dict)
    levels: tuple[AgentLevel, ...] = (AgentLevel.CLEAN, AgentLevel.DERIVE, AgentLevel.RESHAPE)
    """The most this kind may use; the use case's own levels (with decision O3's reshape) narrow it further."""
    not_allowed_code: str = "AGENT_STEP_NOT_ALLOWED"
    tick_only_sure: bool = Field(
        default=False, description="A rules proposal of this kind may start ticked only when it is `sure`."
    )
    change_share: bool = Field(
        default=False,
        description="Decision O10 applies to an AI's proposal of this kind (`data.change_share`).",
    )
    approve: Literal["never", "always", "above_drop_share"] = "never"
    approve_role: Role = Role.ANALYST

    @field_validator("rules")
    @classmethod
    def _known(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(value) - set(DATA_RULE_NAMES))
        if unknown:
            raise ValueError(f"names unknown data rules: {', '.join(unknown)}")
        if len(set(value)) != len(value):
            raise ValueError("names a rule twice")
        if not value:
            raise ValueError("names no rule; leave the kind out instead (default deny)")
        return value

    @model_validator(mode="after")
    def _complete(self) -> DataRuleSet:
        if self.not_allowed_code not in NOT_ALLOWED_CODES:
            raise ValueError(f"not_allowed_code must be one of {NOT_ALLOWED_CODES}")
        if STEP_KIND in self.rules and not self.step_kinds:
            raise ValueError(f"{STEP_KIND} needs the step_kinds it allows")
        if STEP_PARAMS in self.rules and not self.params:
            raise ValueError(f"{STEP_PARAMS} needs the params it allows")
        if set(self.params) - set(self.step_kinds):
            raise ValueError("params names a step kind the rule set does not allow")
        if self.proposes == "setting" and (self.step_kinds or self.params):
            raise ValueError("a setting proposes no step")
        if CHANGE_SHARE in self.rules and not self.change_share:
            raise ValueError(f"{CHANGE_SHARE} is named, so change_share must be true")
        if self.approve != "never" and APPROVE_CLASS not in self.rules:
            raise ValueError(f"an APPROVE-class kind names {APPROVE_CLASS}")
        return self


class ChangeShareSettings(_Strict):
    """Decision O10 (warn above 30%, refuse above 60%); re-typing steps count the values they could not read."""

    warn_pct: float = Field(default=30.0, ge=0.0, le=100.0)
    refuse_pct: float = Field(default=60.0, ge=0.0, le=100.0)
    retyping_kinds: tuple[RecipeStepKind, ...] = (
        RecipeStepKind.PARSE_NUMBER,
        RecipeStepKind.PARSE_DATE,
        RecipeStepKind.MAP_BOOLEAN,
        RecipeStepKind.CONVERT_UNITS,
    )
    """Steps whose purpose is to rewrite every value in another type or unit (`haan` to 1, `2 GB` to 2048): for
    them the share is of the values they could not read, so `haan`/`nahi` to 1/0 is not refused for converting
    every value. Steps that rewrite none of their column's values (a rename, hiding a column, a step writing a
    new column) count nothing: :func:`effect_of`."""

    @model_validator(mode="after")
    def _ordered(self) -> ChangeShareSettings:
        if self.warn_pct > self.refuse_pct:
            raise ValueError("warn_pct must not exceed refuse_pct")
        return self


class BooleanSpellings(_Strict):
    never: tuple[str, ...] = ("na",)
    """Spellings never mapped to yes or no, by anyone (M241: `na` may mean no, or "not available")."""


class DataPolicyConfig(_Strict):
    """`configs/agentic/policy.yaml`'s `data:` section: one rule set per PROPOSE kind, and the shared limits."""

    rule_sets: dict[str, DataRuleSet] = Field(default_factory=dict)
    never_ticked_steps: tuple[RecipeStepKind, ...] = ()
    settings_never_suggested: tuple[str, ...] = ()
    change_share: ChangeShareSettings = Field(default_factory=ChangeShareSettings)
    approve_drop_share_pct: float = Field(default=2.0, ge=0.0, le=100.0)
    trial_rows: int = Field(default=1_000, ge=1)
    boolean_spellings: BooleanSpellings = Field(default_factory=BooleanSpellings)

    @field_validator("rule_sets")
    @classmethod
    def _kinds(cls, value: dict[str, DataRuleSet]) -> dict[str, DataRuleSet]:
        for kind in value:
            if not kind.startswith("data.") or not kind[5:].replace("_", "").isalnum():
                raise ValueError(f"rule set {kind!r}: data kinds are named data.<snake_case>")
        return value


def _policy_path(config_root: Path | None) -> Path:
    from engine.agentic.config import POLICY_FILE
    from engine.config import config_root as resolve_root

    return resolve_root(config_root) / POLICY_FILE


def load_data_policy(config_root: Path | None = None) -> DataPolicyConfig:
    """The `data:` section of `policy.yaml` under the root; `CONFIG_INVALID` when it cannot be read or is wrong.

    A file without the section reads as no rule sets at all: every data proposal is then refused (default deny).
    Read again whenever the file changes (cached on its path, size and modification time).
    """
    path = _policy_path(config_root)
    try:
        stat = path.stat()
    except OSError as exc:
        raise AgenticError(
            "CONFIG_INVALID", "agentic/policy.yaml is missing; data proposals need a policy.", status=500
        ) from exc
    return _load(str(path), stat.st_size, stat.st_mtime_ns)


@functools.lru_cache(maxsize=8)
def _load(path: str, size: int, mtime_ns: int) -> DataPolicyConfig:
    del size, mtime_ns  # part of the cache key only
    from engine.agentic.config import load_policy

    policy = load_policy(Path(path).parent.parent)
    try:
        return DataPolicyConfig.model_validate(policy.data)
    except ValidationError as exc:
        raise AgenticError(
            "CONFIG_INVALID", f"agentic/policy.yaml's data section is not valid: {exc}.", status=500
        ) from exc


def data_policy_on(config: Any) -> bool:
    """Whether the data policy is asked: `data_doctor.unified` (M230; default off), read on the registry module
    at call time so a test that switches it on there switches it on here too."""
    from engine.doctor import registry

    return registry.unified_on(config)


# ---------------------------------------------------------------------------
# What a rule reads: the proposal and the context
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StepFacts:
    """One step as the rules read it: kind, column, parameters and the new column (never a cell)."""

    kind: RecipeStepKind
    column: str
    params: Mapping[str, Any] = field(default_factory=dict)
    new_column: str | None = None

    @classmethod
    def of(cls, step: RecipeStep) -> StepFacts:
        return cls(kind=step.kind, column=step.column, params=dict(step.params), new_column=step.new_column)


@dataclass(frozen=True)
class StepEffect:
    """What a step did when it was tried: values (or rows) changed and not read, out of how many, on how many rows."""

    changed: int
    failed: int
    out_of: int
    rows_tried: int
    measured: bool = True
    """False for a step whose effect could not be measured (a query that makes a new table): never read as 0%."""


@dataclass(frozen=True)
class SettingChange:
    path: str
    value: Any


@dataclass(frozen=True)
class DataProposal:
    """What the rules see of one proposal (the payload of Plan K's `ProposalSubject`, for data)."""

    kind: str
    """The policy kind: `data.recipe_step`, `data.setting`, `data.join_files`, ..."""
    source: Source
    proposed_by: str | None = None
    """The user id it was made for (the AI acts for the person in the chat), or None for the rules."""
    steps: tuple[StepFacts, ...] = ()
    setting: SettingChange | None = None
    ticked: bool = False
    """Whether it would be offered already ticked (accepted for the person)."""
    confidence: Literal["sure", "check"] = "check"
    looked_at: bool = False
    """For an AI's proposal: whether a look-tool result it cites is about its column (Plan L M152)."""
    effect: StepEffect | None = None
    """For an AI's proposal: what it did on the first rows it was tried on; None when it was never tried."""
    asked: bool = False
    """For a step that changes the rows: whether it is offered through one blocking question (decision O3)."""
    rows_in: int | None = None
    rows_out: int | None = None
    """For a join, stack, de-duplication or keep-latest: rows before and after (design §6.6's 2% rule)."""
    sentence: str | None = None
    """For an APPROVE-class item: the exact text the person is shown and approves."""


@dataclass(frozen=True)
class ApprovalFacts:
    """At approval: who approves, the sentence they read and the one the server would show now, the guards."""

    approver: Principal
    approved_sentence: str | None = None
    current_sentence: str | None = None
    approval_required: bool = False
    """The use case's `governance.approval_required`: separation of duties for APPROVE-class items (O17)."""
    reshape: tuple[ReshapeItem, ...] = ()


@dataclass(frozen=True)
class DataPolicyContext:
    """Everything the rules read, computed by the server from the session, the file and the use case."""

    policy: DataPolicyConfig
    primary_key: str | None = None
    target: str | None = None
    personal_columns: frozenset[str] = frozenset()
    levels: tuple[AgentLevel, ...] = (AgentLevel.CLEAN,)
    """The use case's levels, with decision O3's reshape when Data Doctor is on (`reshape_guard.doctor_levels`)."""
    settings: Mapping[str, FieldSpec] | None = None
    """The settings listed for this session (`recommend.settings_fields`), for `data.setting`."""
    approval: ApprovalFacts | None = None


DataRule = Callable[[DataProposal, DataPolicyContext, DataRuleSet], Decision | None]


def _allow(rule: str, **detail: Any) -> Decision:
    return Decision(allowed=True, rule=rule, code=None, detail=detail)


def _refuse(rule: str, code: str, reason: str, **detail: Any) -> Decision:
    return Decision(allowed=False, rule=rule, code=code, detail={"reason": reason, **detail})


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------
def step_kind(subject: DataProposal, ctx: DataPolicyContext, rules: DataRuleSet) -> Decision | None:
    """Only the step kinds the rule set lists (the chat's are `loop.CHAT_STEP_KINDS`, a person's `edits.PERSON_STEP_KINDS`)."""
    del ctx
    if not subject.steps:
        return None
    for step in subject.steps:
        if step.kind not in rules.step_kinds:
            allowed_kinds = ", ".join(sorted(kind.value for kind in rules.step_kinds))
            reason = (
                f"{step.kind.value} is not an edit made by hand."
                if rules.not_allowed_code == "AGENT_EDIT_NOT_ALLOWED"
                else f"{step.kind.value!r} is not a fix that may be suggested here. Allowed: {allowed_kinds}"
            )
            return _refuse(STEP_KIND, rules.not_allowed_code, reason, kind=step.kind.value)
    return _allow(STEP_KIND, kinds=sorted({step.kind.value for step in subject.steps}))


def step_params(subject: DataProposal, ctx: DataPolicyContext, rules: DataRuleSet) -> Decision | None:
    """Each listed kind names only the parameters the rule set allows, with the values it requires.

    M240: `parse_date`'s `excel_serial` and `date_1904` are the Data Doctor's own fix; the chat's rule set does not
    allow them. M241: `parse_indian_numbers` proposes `parse_number` with `indian_scale_words: true` only.
    """
    del ctx
    checked = [step for step in subject.steps if step.kind in rules.params]
    if not checked:
        return None
    for order, step in enumerate(subject.steps, start=1):
        rule = rules.params.get(step.kind)
        if rule is None:
            continue
        unknown = sorted(set(step.params) - set(rule.allowed))
        if unknown:
            return _refuse(
                STEP_PARAMS,
                "RECIPE_STEP_INVALID",
                f"Step {order} ({step.kind.value}): unknown parameter {unknown[0]!r}",
                parameter=unknown[0],
            )
        for name, wanted in rule.equals.items():
            if name not in step.params or step.params[name] != wanted:
                return _refuse(
                    STEP_PARAMS,
                    "RECIPE_STEP_INVALID",
                    f"Step {order} ({step.kind.value}): {name} must be {wanted!r}",
                    parameter=name,
                )
        for name, values in rule.one_of.items():
            if name in step.params and step.params[name] not in values:
                return _refuse(
                    STEP_PARAMS,
                    "RECIPE_STEP_INVALID",
                    f"Step {order} ({step.kind.value}): {name} must be one of {list(values)!r}",
                    parameter=name,
                )
    return _allow(STEP_PARAMS, steps=len(checked))


def _spelling(text: Any) -> str:
    """A spelling as the lexicons compare it (`engine.doctor.indian_values`): NFC, spaces collapsed, casefolded."""
    return " ".join(unicodedata.normalize("NFC", str(text)).split()).casefold()


@functools.cache
def _lexicon() -> tuple[frozenset[str], frozenset[str]]:
    """The English, Hinglish and Devanagari yes and no words (M241's lists and `formats.BOOLEAN_*`)."""
    from engine.agent.formats import BOOLEAN_FALSE, BOOLEAN_TRUE
    from engine.doctor.indian_values import DEVANAGARI_FALSE, DEVANAGARI_TRUE, HINGLISH_FALSE, HINGLISH_TRUE

    yes = frozenset(_spelling(word) for word in (*BOOLEAN_TRUE, *HINGLISH_TRUE, *DEVANAGARI_TRUE))
    no = frozenset(_spelling(word) for word in (*BOOLEAN_FALSE, *HINGLISH_FALSE, *DEVANAGARI_FALSE))
    return yes, no


def spellings(subject: DataProposal, ctx: DataPolicyContext, rules: DataRuleSet) -> Decision | None:
    """A yes/no step maps both sides, never one spelling to both, and never `na` (M241).

    A step that does not list both sides, or lists one spelling on both, is malformed: `check_recipe` refuses it
    today with `RECIPE_STEP_INVALID` and its words, so the policy refuses it with the same code and words. Only
    what the policy adds has its own code (`POLICY_BOOLEAN_SPELLING`): `na`, and a rules spelling outside the
    lexicons. An AI's spelling outside them is the approval's "new pattern, with permission": allowed here, and
    held by `data.grounded`, `data.tried`, `data.change_share` and `data.never_ticked` instead (it is never
    ticked; the person approves it).
    """
    del rules
    booleans = [step for step in subject.steps if step.kind is RecipeStepKind.MAP_BOOLEAN]
    if not booleans:
        return None
    never = {_spelling(word) for word in ctx.policy.boolean_spellings.never}
    yes_words, no_words = _lexicon()
    code = "POLICY_BOOLEAN_SPELLING"
    for order, step in enumerate(subject.steps, start=1):
        if step.kind is not RecipeStepKind.MAP_BOOLEAN:
            continue
        malformed = f"Step {order} ({step.kind.value}): "
        true_values = step.params.get("true_values")
        false_values = step.params.get("false_values")
        if not (
            isinstance(true_values, list | tuple)
            and true_values
            and isinstance(false_values, list | tuple)
            and false_values
        ):
            return _refuse(
                SPELLINGS, "RECIPE_STEP_INVALID", malformed + "list the spellings of yes and of no"
            )
        yes = {_spelling(word) for word in true_values}
        no = {_spelling(word) for word in false_values}
        if yes & no:
            return _refuse(
                SPELLINGS,
                "RECIPE_STEP_INVALID",
                malformed + "a spelling cannot mean both yes and no",
                both=len(yes & no),
            )
        if (yes | no) & never:
            return _refuse(
                SPELLINGS,
                code,
                "'NA' may mean no, or that the value is not available, so it is asked, never mapped.",
            )
        if subject.source == "rules" and not (yes <= yes_words and no <= no_words):
            return _refuse(
                SPELLINGS,
                code,
                "The rules map only the yes and no words they know; another spelling needs the person's approval.",
                unknown=len((yes - yes_words) | (no - no_words)),
            )
    return _allow(SPELLINGS, steps=len(booleans))


_READS_ROWS: Final[frozenset[RecipeStepKind]] = ROW_RESHAPE_STEP_KINDS
_CHOOSING_PARAMS: Final[tuple[str, ...]] = ("time_column", "snapshot_column")


def protected_columns(subject: DataProposal, ctx: DataPolicyContext, rules: DataRuleSet) -> Decision | None:
    """Never a step on the ID or the outcome (Plan L §5.2.3, §5.2.6; `check_recipe`'s rule, read again here).

    A step that changes the rows names the ID as its column by design; it may not choose rows by the outcome. A
    new column may not be computed from the outcome.
    """
    del rules
    if not subject.steps:
        return None
    code = "RECIPE_STEP_INVALID"
    protected = {name for name in (ctx.primary_key, ctx.target) if name}
    for step in subject.steps:
        if step.kind in _READS_ROWS:
            if ctx.target and any(step.params.get(name) == ctx.target for name in _CHOOSING_PARAMS):
                return _refuse(
                    PROTECTED,
                    code,
                    f"The outcome column '{ctx.target}' is never used to choose which rows are kept.",
                )
            continue
        if step.kind is RecipeStepKind.DERIVE:
            from engine.agent.recipe import RecipeError, derive_names

            expression = step.params.get("expression")
            try:
                names = derive_names(str(expression)) if isinstance(expression, str) else frozenset()
            except RecipeError:
                names = frozenset()
            if ctx.target and ctx.target in names:
                return _refuse(
                    PROTECTED,
                    code,
                    f"A new column may not be computed from the outcome column '{ctx.target}'.",
                )
            continue
        leaves_rows_out_by_id = (
            step.kind is RecipeStepKind.REMOVE_ROWS and step.column == ctx.primary_key != ctx.target
        )  # no ID changes when the rows whose ID begins "999" go; only the outcome never chooses rows
        if not leaves_rows_out_by_id and (
            step.column in protected or (step.new_column is not None and step.new_column in protected)
        ):
            return _refuse(
                PROTECTED,
                code,
                f"'{step.column}' is the ID or the outcome column, which the recipe never changes.",
            )
    return _allow(PROTECTED)


def personal_data(subject: DataProposal, ctx: DataPolicyContext, rules: DataRuleSet) -> Decision | None:
    """A column holding personal data can only be hidden (Plan G; `loop._propose_step`, `edits.add_edit`)."""
    if not subject.steps:
        return None
    for step in subject.steps:
        if step.column in ctx.personal_columns and step.kind is not RecipeStepKind.DROP_COLUMN:
            return _refuse(
                PERSONAL, rules.not_allowed_code, "A column holding personal data can only be hidden."
            )
    return _allow(PERSONAL)


def _level_of(kind: RecipeStepKind) -> AgentLevel:
    from engine.agent.recipe import _LEVEL_OF

    return _LEVEL_OF.get(kind, AgentLevel.CLEAN)


def level(subject: DataProposal, ctx: DataPolicyContext, rules: DataRuleSet) -> Decision | None:
    """A step only at a level both the rule set and the use case allow (with O3's reshape when Data Doctor is on)."""
    if not subject.steps:
        return None
    for step in subject.steps:
        needed = _level_of(step.kind)
        if needed not in rules.levels or needed not in ctx.levels:
            return _refuse(
                LEVEL,
                "RECIPE_STEP_INVALID",
                f"This use case's helper may not use {step.kind.value} steps.",
                level=needed.value,
            )
    return _allow(LEVEL)


def grounded(subject: DataProposal, ctx: DataPolicyContext, rules: DataRuleSet) -> Decision | None:
    """An AI's fix cites a look at the column it changes (Plan L M152; the approval's new-patterns rule)."""
    del ctx, rules
    if subject.source != "ai":
        return None
    if not subject.looked_at:
        return _refuse(
            GROUNDED,
            "AGENT_STEP_UNGROUNDED",
            "Look at the column with a tool first, and cite that result in evidence_ids.",
        )
    return _allow(GROUNDED)


def tried(subject: DataProposal, ctx: DataPolicyContext, rules: DataRuleSet) -> Decision | None:
    """An AI's fix is tried on the file's first rows (1,000) before it is shown, so its effect is measured."""
    del rules
    if subject.source != "ai" or not subject.steps:
        return None
    effect = subject.effect
    if effect is None or effect.rows_tried <= 0:
        return _refuse(
            TRIED,
            "POLICY_STEP_NOT_TRIED",
            f"An AI suggestion is shown only after it is tried on the file's first {ctx.policy.trial_rows:,} rows.",
        )
    return _allow(TRIED, rows_tried=effect.rows_tried)


def never_ticked(subject: DataProposal, ctx: DataPolicyContext, rules: DataRuleSet) -> Decision | None:
    """Nothing the AI proposes, and no never-ticked kind, starts ticked; only a person's own edit is ticked by them.

    `advisor.NEVER_TICKED_STEPS` (placeholders, ranges, keep the latest row per ID), the AI's every proposal
    (design §6.2) and, where the rule set says so, a rules proposal that is not `sure` (M241).
    """
    if subject.source == "person":
        return None
    if not subject.ticked:
        return _allow(NEVER_TICKED)
    never = set(ctx.policy.never_ticked_steps)
    why: str | None = None
    if subject.source == "ai":
        why = "A suggestion from the AI is never ticked for you."
    elif any(step.kind in never for step in subject.steps):
        why = "Only you may tick this kind of change."
    elif rules.tick_only_sure and subject.confidence != "sure":
        why = "Only a suggestion the rules are sure of starts ticked."
    if why is not None:
        return _refuse(NEVER_TICKED, "POLICY_NEVER_TICKED", why)
    return _allow(NEVER_TICKED)


def _share(effect: StepEffect, kind: RecipeStepKind, settings: ChangeShareSettings) -> tuple[int, float]:
    counted = effect.failed if kind in settings.retyping_kinds else effect.changed
    return counted, (0.0 if effect.out_of <= 0 else counted / effect.out_of * 100.0)


def change_share(subject: DataProposal, ctx: DataPolicyContext, rules: DataRuleSet) -> Decision | None:
    """Decision O10: an AI step changing more than 60% of a column is refused, above 30% it carries a warning.

    The person may still make the same change as their own edit (a person's proposal is not the AI's). M238's
    `change_share_verdict` decides; the share is measured by running the step (`effect`), never estimated.
    """
    if subject.source != "ai" or not rules.change_share or subject.effect is None or not subject.steps:
        return None
    from engine.doctor.change_share import ChangeShareLimits, change_share_verdict

    settings = ctx.policy.change_share
    if not subject.effect.measured:
        from engine.doctor.change_share import unmeasured_verdict

        said = unmeasured_verdict(
            "This step makes a new table, so the share of values it changes was not measured."
        )
        return _allow(CHANGE_SHARE, warning="STEP_NOT_MEASURED", message=said.message)
    counted, _ = _share(subject.effect, subject.steps[0].kind, settings)
    verdict = change_share_verdict(
        changed=counted,
        out_of=subject.effect.out_of,
        source=ProposalSource.AI,
        limits=ChangeShareLimits(warn_pct=settings.warn_pct, refuse_pct=settings.refuse_pct),
    )
    detail = {"share_pct": round(verdict.share_pct, 4), "changed": counted, "out_of": subject.effect.out_of}
    if verdict.level == "refuse":
        return _refuse(CHANGE_SHARE, "AI_STEP_CHANGES_TOO_MANY", verdict.message, **detail)
    if verdict.level == "warn":
        return _allow(CHANGE_SHARE, warning="AI_STEP_CHANGES_MANY", message=verdict.message, **detail)
    return _allow(CHANGE_SHARE, **detail)


def setting(subject: DataProposal, ctx: DataPolicyContext, rules: DataRuleSet) -> Decision | None:
    """The setting allow-list: a listed, live setting, never one the policy keeps from suggestions, within range."""
    del rules
    change = subject.setting
    if change is None:
        return None
    from engine.agent.recommend import setting_allowed

    fields = dict(ctx.settings or {})
    if (
        change.path in ctx.policy.settings_never_suggested
        or change.path not in fields
        or fields[change.path].advisory
        or not setting_allowed(change.path, change.value, fields)
    ):
        return _refuse(
            SETTING,
            "AGENT_SETTING_NOT_ALLOWED",
            f"{change.path[:80]} = {change.value!r:.80} is not a setting the helper may suggest.",
        )
    return _allow(SETTING, path=change.path)


def _drop_share(subject: DataProposal) -> float | None:
    if subject.rows_in is None or subject.rows_out is None:
        return None
    if subject.rows_in <= 0:
        return 0.0
    return max(0, subject.rows_in - subject.rows_out) / subject.rows_in * 100.0


def _approve_class(subject: DataProposal, ctx: DataPolicyContext, rules: DataRuleSet) -> bool:
    if rules.approve == "always":
        return True
    if rules.approve == "above_drop_share":
        share = _drop_share(subject)
        return (
            share is None or share > ctx.policy.approve_drop_share_pct
        )  # unknown counts as above: deny-safe
    return False


def approve_class(subject: DataProposal, ctx: DataPolicyContext, rules: DataRuleSet) -> Decision | None:
    """APPROVE-class items (design §6.6): their own yes, the exact text, the role, separation of duties.

    When proposed: the item carries the sentence the person will read. At approval: the approver holds the rule
    set's role (`TOOL_ROLE_REQUIRED`), approves the sentence the server shows now, word for word
    (`APPROVAL_SENTENCE_CHANGED`), and - when the use case sets `governance.approval_required` - is not the person
    who proposed it (`POLICY_ROLE_SEPARATION`, `engine.approvals.separation_refusal`).
    """
    if not _approve_class(subject, ctx, rules):
        return _allow(APPROVE_CLASS, approve_class=False, drop_share_pct=_drop_share(subject))
    role = rules.approve_role
    approval = ctx.approval
    if approval is None:
        if not subject.sentence:
            return _refuse(
                APPROVE_CLASS,
                "POLICY_PAYLOAD_INVALID",
                "An item that needs your explicit yes carries the exact sentence you approve.",
            )
        return _allow(APPROVE_CLASS, approve_class=True, role=role.value, drop_share_pct=_drop_share(subject))
    if not approval.approver.has(role):
        return _refuse(
            APPROVE_CLASS,
            "TOOL_ROLE_REQUIRED",
            f"Only someone with the {role.value} role can approve this.",
            role=role.value,
        )
    if not approval.approved_sentence or approval.approved_sentence != approval.current_sentence:
        return _refuse(
            APPROVE_CLASS,
            "APPROVAL_SENTENCE_CHANGED",
            "What would be approved has changed since you read it; read the new sentence and approve again.",
        )
    if approval.approval_required:
        from engine.approvals import separation_refusal

        if separation_refusal(approval.approver, subject.proposed_by) is not None:
            return _refuse(
                APPROVE_CLASS,
                "POLICY_ROLE_SEPARATION",
                "You proposed this, so another Approver has to decide on it.",
            )
    return _allow(APPROVE_CLASS, approve_class=True, role=role.value)


def reshape_guard(subject: DataProposal, ctx: DataPolicyContext, rules: DataRuleSet) -> Decision | None:
    """Decision O3's guards on a step that changes the rows (M238's `reshape_guard`).

    Offered: through one blocking question (never ticked is `data.never_ticked`'s). At approval: `check_reshape_guards` on each item - its
    question answered, never ticked when offered, the full (training) or narrow (scoring) future-data check set,
    its preview receipt shown (`RESHAPE_GUARD_MISSING`).
    """
    del rules
    reshaping = [step for step in subject.steps if step.kind in ROW_RESHAPE_STEP_KINDS]
    if not reshaping:
        return None
    code = "RESHAPE_GUARD_MISSING"
    if ctx.approval is None:
        if not subject.asked:  # never ticked is `data.never_ticked`'s (keep_latest_per_id is in its list)
            return _refuse(RESHAPE_GUARD, code, "A change to the rows is offered only through its question.")
        return _allow(RESHAPE_GUARD)
    from engine.doctor.reshape_guard import reshape_guard_failures

    items = {item.step.kind: item for item in ctx.approval.reshape}
    for step in reshaping:
        item = items.get(step.kind)
        if item is None:
            return _refuse(
                RESHAPE_GUARD, code, "This change to the rows has no record of how it was offered."
            )
        missing = reshape_guard_failures(item)
        if missing:
            return _refuse(
                RESHAPE_GUARD,
                code,
                "This change to the rows cannot be approved yet: " + "; ".join(missing) + ".",
                missing=list(missing),
            )
    return _allow(RESHAPE_GUARD)


DATA_RULES: Final[Mapping[str, DataRule]] = {
    STEP_KIND: step_kind,
    STEP_PARAMS: step_params,
    SPELLINGS: spellings,
    PROTECTED: protected_columns,
    PERSONAL: personal_data,
    LEVEL: level,
    GROUNDED: grounded,
    TRIED: tried,
    NEVER_TICKED: never_ticked,
    CHANGE_SHARE: change_share,
    SETTING: setting,
    APPROVE_CLASS: approve_class,
    RESHAPE_GUARD: reshape_guard,
}
"""Every data rule by name. The mutation test passes `evaluate` this map with one rule removed."""


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
def _shape_refusal(subject: DataProposal, rules: DataRuleSet) -> Decision | None:
    if rules.proposes == "steps" and (not subject.steps or subject.setting is not None):
        return _refuse(
            "default",
            "POLICY_PAYLOAD_INVALID",
            f"A {subject.kind} proposal carries its steps and nothing else.",
        )
    if rules.proposes == "setting" and (subject.setting is None or subject.steps):
        return _refuse(
            "default", "POLICY_PAYLOAD_INVALID", f"A {subject.kind} proposal carries one setting and no step."
        )
    if rules.proposes == "plan" and subject.setting is not None:
        return _refuse("default", "POLICY_PAYLOAD_INVALID", f"A {subject.kind} proposal carries no setting.")
    return None


def evaluate(
    subject: DataProposal,
    ctx: DataPolicyContext,
    *,
    rules: Mapping[str, DataRule] = DATA_RULES,
    names: Sequence[str] | None = None,
) -> tuple[Decision, ...]:
    """Every applicable rule's decision on `subject`; one `default` refusal when its kind has no rule set.

    `names` evaluates only those rules of the set (a caller that checks one rule where the code checks it).
    A rule named in the set but missing from `rules` decides nothing (that is how the mutation test disables
    one); a rule set with no rule that applies allows nothing - default deny holds.
    """
    rule_set = ctx.policy.rule_sets.get(subject.kind)
    if rule_set is None:
        return (
            _refuse("default", "POLICY_NO_RULES", f"No policy rules are set for a {subject.kind} proposal."),
        )
    shape = _shape_refusal(subject, rule_set)
    if shape is not None:
        return (shape,)
    wanted = rule_set.rules if names is None else tuple(name for name in rule_set.rules if name in names)
    decisions = [
        decision
        for name in wanted
        if (rule := rules.get(name)) is not None and (decision := rule(subject, ctx, rule_set)) is not None
    ]
    if not decisions and names is None:
        return (_refuse("default", "POLICY_NO_RULES", f"No rule applies to this {subject.kind} proposal."),)
    return tuple(decisions)


def allowed(decisions: Iterable[Decision]) -> bool:
    """A proposal passes only when there is at least one decision and every one allows it."""
    found = tuple(decisions)
    return bool(found) and all(decision.allowed for decision in found)


def first_refusal(decisions: Iterable[Decision]) -> Decision | None:
    """The first refusal, in the rule set's order (what a caller raises), or None."""
    return next((decision for decision in decisions if not decision.allowed), None)


def refusal(subject: DataProposal, ctx: DataPolicyContext, *names: str) -> Decision | None:
    """The refusal of `subject` by the named rules of its rule set, or by default deny; None when they allow it.

    What the code's own checks call at the place they check the same thing, so the refusal has the same code.
    """
    if subject.kind not in ctx.policy.rule_sets:
        return evaluate(subject, ctx)[0]
    return first_refusal(evaluate(subject, ctx, names=names))


KEEPS_ITS_VALUES: Final[frozenset[RecipeStepKind]] = frozenset(
    {RecipeStepKind.RENAME_COLUMN, RecipeStepKind.DROP_COLUMN}
)
"""Steps that rewrite none of their column's values: a rename keeps every value, hiding a column keeps the file's
values as they are (the column is left out of what the model reads, which is its own decision, never O10's)."""


def keeps_its_values(step: RecipeStep) -> bool:
    """Whether `step` leaves every value of its own column as it is: a rename, hiding a column, or a step that
    writes its result into a new column (`split_column` with `new_column`; Plan G's receipt still counts the
    values it wrote there as changed)."""
    if step.kind in KEEPS_ITS_VALUES:
        return True
    return step.new_column is not None and step.new_column != step.column


def effect_of(step: RecipeStep, sample: Any, *, changed: int, failed: int) -> StepEffect:
    """A tried step's effect, counted as decision O10 counts it (M238's `change_share.measure_step`).

    Out of the column's non-empty values for a value step, of its rows for a fill, of the rows for a step that
    changes the rows. `changed` and `failed` are the trial's receipt on `sample` (the rows it was tried on).
    O10 is about the values of the column a step rewrites: a step that rewrites none of them
    (:func:`keeps_its_values`) changes none, whatever the receipt counts.
    """
    rows = len(sample)
    if step.kind is RecipeStepKind.SQL_QUERY:  # no single column to count in: needs a yes, never 0%
        return StepEffect(changed=0, failed=0, out_of=0, rows_tried=rows, measured=False)
    if keeps_its_values(step):
        changed, failed = 0, 0
    if step.kind in ROW_RESHAPE_STEP_KINDS or step.kind in (
        RecipeStepKind.REMOVE_ROWS,
        RecipeStepKind.FILL_MISSING,
    ):
        out_of = rows
    elif step.column in sample.columns:
        out_of = int(sample[step.column].notna().sum())
    else:
        out_of = 0
    return StepEffect(changed=changed, failed=failed, out_of=out_of, rows_tried=rows)


def policy_or_deny(config_root: Path | None) -> DataPolicyConfig:
    """The data policy, or - when it cannot be read - one with no rule sets, so every proposal is refused."""
    try:
        return load_data_policy(config_root)
    except AgenticError:
        return DataPolicyConfig()


def agent_policy_context(
    ctx: AgentContext,
    *,
    primary_key: str | None,
    target: str | None,
    settings: Mapping[str, FieldSpec] | None = None,
) -> DataPolicyContext:
    """The facts the rules read for one file of Plan G's runtime, computed from its context, never from a model.

    Personal columns as the chat and the edits read them (profiled personal data and `agent.always_hide_columns`);
    the use case's levels with decision O3's reshape (`reshape_guard.doctor_levels`, Data Doctor on).
    """
    from engine.doctor.reshape_guard import doctor_levels

    personal = {c.name for c in ctx.profile.columns if c.pii_kinds} | set(
        ctx.config.agent.always_hide_columns
    )
    return DataPolicyContext(
        policy=policy_or_deny(ctx.config_root),
        primary_key=primary_key,
        target=target,
        personal_columns=frozenset(personal),
        levels=doctor_levels(tuple(ctx.config.agent.levels), unified=True),
        settings=settings,
    )


Gate = Callable[..., tuple[Decision, ...]]
"""`gate(*rule_names, **proposal_fields)`: raises the caller's own error when the named rules refuse; otherwise
returns the warnings the named rules allowed it with (decision O10's `AI_STEP_CHANGES_MANY`, Plan O M246)."""


def warnings_of(decisions: Iterable[Decision]) -> tuple[Decision, ...]:
    """Plan O M246 (DEC-1816): the allowed decisions that carry a `warning` (O10's `AI_STEP_CHANGES_MANY` above
    30% of a column), so the caller shows it with the proposal instead of dropping it."""
    return tuple(d for d in decisions if d.allowed and d.detail.get("warning"))


def agent_gate(
    ctx: AgentContext,
    kind: str,
    *,
    source: Source,
    primary_key: str | None,
    target: str | None,
    raise_as: Callable[[str, str], Exception],
    settings: Mapping[str, FieldSpec] | None = None,
) -> Gate | None:
    """A check of `kind`'s rules for Plan G's handlers, behind `data_doctor.unified`; None when it is off.

    The handler calls it where its own check of the same thing is, after that check, so a proposal its own code
    refuses is refused exactly as today and the policy can only refuse more (default deny: a kind with no rule
    set, or a policy that cannot be read, refuses every proposal with `POLICY_NO_RULES`).
    """
    if not data_policy_on(ctx.config):
        return None
    policy_ctx = agent_policy_context(ctx, primary_key=primary_key, target=target, settings=settings)

    def gate(*names: str, **facts: Any) -> tuple[Decision, ...]:
        subject = DataProposal(kind=kind, source=source, **facts)
        found = refusal(subject, policy_ctx, *names)
        if found is not None:
            raise raise_as(found.code or "POLICY_NO_RULES", str(found.detail.get("reason", "Not allowed.")))
        return warnings_of(evaluate(subject, policy_ctx, names=names))

    return gate


def never_ticked_steps(config: Any, config_root: Path | None) -> frozenset[RecipeStepKind]:
    """The kinds only the person may tick: Plan G's own list, and - with Data Doctor on - the policy's too.

    The code's list (`advisor.NEVER_TICKED_STEPS`) always holds, so the policy can only add to it; a policy that
    cannot be read adds nothing here (proposals are refused by default deny where they are made).
    """
    from engine.agent.advisor import NEVER_TICKED_STEPS

    if not data_policy_on(config):
        return NEVER_TICKED_STEPS
    try:
        extra = load_data_policy(config_root).never_ticked_steps
    except AgenticError:
        return NEVER_TICKED_STEPS
    return NEVER_TICKED_STEPS | frozenset(extra)


# ---------------------------------------------------------------------------
# What the wave-1 tools return, read as proposals (the registry asks after each call)
# ---------------------------------------------------------------------------
HANDLER_CHECKED: Final[frozenset[str]] = frozenset({"data.setting", "data.recipe_step", "data.person_edit"})
"""Kinds whose handler asks the rules itself, where its own checks are (`loop._propose`, `loop._propose_step`,
`edits.add_edit`): the registry adds no second reading of their output."""

FILES_KINDS: Final[frozenset[str]] = frozenset(
    {
        "data.join_files",
        "data.stack_files",
        "data.build_dataset",
        "data.normalise_key",
        "data.propose_sql_step",
        "data.propose_features",
    }
)
"""Kinds over several files (M237): each file has its own ID column, which `normalise_key` writes by design, so
no single file's ID is protected for them; the outcome still is."""

_Reader = Callable[[str, Mapping[str, Any]], list[DataProposal]]


def _facts(raw: Any) -> StepFacts:
    """A step as a tool returns it (a `RecipeStep`, a `StepProposal`, or `{kind, column, params}`)."""
    if not isinstance(raw, Mapping):
        raise TypeError("a step is an object")
    params = raw.get("params") or {}
    if not isinstance(params, Mapping):
        raise TypeError("a step's params are an object")
    new_column = raw.get("new_column")
    return StepFacts(
        kind=RecipeStepKind(str(raw["kind"])),
        column=str(raw["column"]),
        params=dict(params),
        new_column=str(new_column) if new_column else None,
    )


def _source(raw: Mapping[str, Any], default: Source = "rules") -> Source:
    value = str(raw.get("source", default))
    if value not in ("rules", "ai", "person"):
        raise ValueError(f"unknown source {value!r}")
    return value  # type: ignore[return-value]


def _step_proposal(kind: str, raw: Any, **extra: Any) -> DataProposal:
    """M238's `StepProposal`: who proposed it, never ticked, measured on the file (its O10 effect for an AI's)."""
    if not isinstance(raw, Mapping):
        raise TypeError("a proposal is an object")
    source = _source(raw)
    facts: dict[str, Any] = {
        "ticked": bool(raw.get("ticked", False)),
        "confidence": "sure" if raw.get("confidence") == "sure" else "check",
    }
    if source == "ai":
        # The tool itself read the column it proposes for, and measured the step on the whole file.
        out_of = int(raw["out_of"])
        facts["looked_at"] = True
        facts["effect"] = StepEffect(changed=int(raw["changed"]), failed=0, out_of=out_of, rows_tried=out_of)
    return DataProposal(kind=kind, source=source, steps=(_facts(raw),), **{**facts, **extra})


def _present(*values: Any) -> list[Any]:
    return [value for value in values if value is not None]


def _read_synonyms(kind: str, output: Mapping[str, Any]) -> list[DataProposal]:
    steps = _present(output["report"].get("step"), output.get("ai_step"))
    return [_step_proposal(kind, step) for step in steps]


def _read_impossible(kind: str, output: Mapping[str, Any]) -> list[DataProposal]:
    return [_step_proposal(kind, f["step"]) for f in output["findings"] if f.get("step") is not None]


def _read_units(kind: str, output: Mapping[str, Any]) -> list[DataProposal]:
    return [_step_proposal(kind, step) for step in _present(output.get("step"))]


def _read_keep_latest(kind: str, output: Mapping[str, Any]) -> list[DataProposal]:
    return [
        _step_proposal(
            kind,
            output["step"],
            asked=output.get("question") is not None,
            rows_in=int(output["rows_in"]),
            rows_out=int(output["rows_out"]),
            sentence=str(output.get("sentence") or "") or None,
        )
    ]


def _read_excel_serial(kind: str, output: Mapping[str, Any]) -> list[DataProposal]:
    found: list[DataProposal] = []
    for column in output["columns"]:
        params: dict[str, Any] = {"excel_serial": bool(column.get("excel_serial", True))}
        if column.get("date_1904"):
            params["date_1904"] = True
        step = StepFacts(RecipeStepKind.PARSE_DATE, str(column["column"]), params)
        found.append(DataProposal(kind=kind, source="rules", steps=(step,)))
    return found


def _read_locale(kind: str, output: Mapping[str, Any]) -> list[DataProposal]:
    """M241: a proposal the rules are `sure` of is offered ticked; any other is pending."""
    found: list[DataProposal] = []
    for proposal in output["proposals"]:
        if proposal.get("step") is None:
            continue
        sure = proposal.get("confidence") == "sure"
        found.append(
            DataProposal(
                kind=kind,
                source="rules",
                steps=(_facts(proposal["step"]),),
                ticked=sure,
                confidence="sure" if sure else "check",
            )
        )
    return found


def _sentence(output: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        text = output.get(name)
        if isinstance(text, str) and text.strip():
            return text
    return None


def _read_split(kind: str, output: Mapping[str, Any]) -> list[DataProposal]:
    receipt = output["receipt"]
    rows_out = int(receipt["rows_training"]) + int(receipt["rows_scoring"])
    return [
        DataProposal(
            kind=kind,
            source="rules",
            rows_in=int(receipt["rows_in"]),
            rows_out=rows_out,
            sentence=_sentence(output, "sentence"),
        )
    ]


def _read_convert(kind: str, output: Mapping[str, Any]) -> list[DataProposal]:
    """M242's `convert_currency` (wave-2 integration): nothing proposed when it refused (`refused`, a coded
    `CURRENCY_RATE_INVALID` and the like); otherwise one APPROVE-class item with the exact sentence."""
    if output.get("refused") is not None or not output["steps"]:
        return []
    return [DataProposal(kind=kind, source="rules", sentence=_sentence(output, "sentence"))]


def _read_join(kind: str, output: Mapping[str, Any]) -> list[DataProposal]:
    """The file that loses the largest share of its rows decides (M237: over 2% of any file is APPROVE-class)."""
    receipt = output.get("receipt")
    rows_in: int | None = None
    rows_out: int | None = None
    if receipt is not None:
        parts = list(receipt["parts"])
        if parts:
            worst = max(parts, key=lambda part: (int(part["rows_dropped"]) / max(int(part["rows"]), 1)))
            rows_in, rows_out = int(worst["rows"]), int(worst["rows"]) - int(worst["rows_dropped"])
    return [
        DataProposal(
            kind=kind,
            source="rules",
            rows_in=rows_in,
            rows_out=rows_out,
            sentence=_sentence(output, "summary"),
        )
    ]


def _read_sql(kind: str, output: Mapping[str, Any]) -> list[DataProposal]:
    """A query's table: who wrote it, never ticked, the first file's rows before and the new table's after."""
    return [
        DataProposal(
            kind=kind,
            source=_source(output),
            ticked=bool(output["ticked"]),
            confidence="sure" if output["confidence"] == "sure" else "check",
            rows_in=int(output["rows_in"]),
            rows_out=int(output["rows_out"]),
            sentence=_sentence(output, "sentence"),
        )
    ]


def _read_stack(kind: str, output: Mapping[str, Any]) -> list[DataProposal]:
    receipt = output.get("receipt")
    rows_in = None if receipt is None else int(receipt["rows_in"])
    rows_out = None if receipt is None else int(receipt["rows_out"])
    return [
        DataProposal(
            kind=kind,
            source="rules",
            rows_in=rows_in,
            rows_out=rows_out,
            sentence=_sentence(output, "summary"),
        )
    ]


def _read_build(kind: str, output: Mapping[str, Any]) -> list[DataProposal]:
    """The plan's key rules as `normalise_key` steps; its stacks' worst row loss; its approval sentence."""
    from engine.agent.key_steps import KeyRule

    steps: list[StepFacts] = []
    for source in (output["entity"], *output.get("events", ())):
        rule = KeyRule.model_validate(source.get("key_rule") or {})
        if not rule.changes_nothing:
            steps.append(StepFacts(RecipeStepKind.NORMALISE_KEY, str(source["key_column"]), rule.params()))
    rows_in: int | None = None
    rows_out: int | None = None
    receipts = list(output.get("stack_receipts", ()))
    if receipts:
        worst = max(
            receipts, key=lambda r: (int(r["rows_in"]) - int(r["rows_out"])) / max(int(r["rows_in"]), 1)
        )
        rows_in, rows_out = int(worst["rows_in"]), int(worst["rows_out"])
    return [
        DataProposal(
            kind=kind,
            source="rules",
            steps=tuple(steps),
            rows_in=rows_in,
            rows_out=rows_out,
            sentence=_sentence(output, "approval_sentence"),
        )
    ]


def _read_normalise_key(kind: str, output: Mapping[str, Any]) -> list[DataProposal]:
    steps = tuple(_facts(step) for step in output["steps"])
    return [DataProposal(kind=kind, source="rules", steps=steps)] if steps else []


def _read_pending(kind: str, output: Mapping[str, Any]) -> list[DataProposal]:
    """M236's wrappers (DEC-1806): one pending item, no step - who proposed it, ticked or not, how sure."""
    return [
        DataProposal(
            kind=kind,
            source=_source(output),
            ticked=bool(output["ticked"]),
            confidence="sure" if output["confidence"] == "sure" else "check",
        )
    ]


def _read_outcome(kind: str, output: Mapping[str, Any]) -> list[DataProposal]:
    """M239 (DEC-1809): an outcome definition, with the exact sentence it is approved with; nothing to decide while
    a meaning-bearing question is open (no definition is proposed then)."""
    if output["definition"] is None:
        return []
    return [
        DataProposal(
            kind=kind,
            source=_source(output),
            ticked=bool(output["ticked"]),
            confidence="sure" if output["confidence"] == "sure" else "check",
            sentence=_sentence(output, "sentence"),
        )
    ]


OUTPUT_READERS: Final[Mapping[str, _Reader]] = {
    "data.propose_role": _read_pending,
    "data.propose_key": _read_pending,
    "data.propose_mapping": _read_pending,
    "data.propose_intake": _read_pending,
    "data.propose_import": _read_pending,
    "data.second_opinion": _read_pending,
    "data.synonym_clusters": _read_synonyms,
    "data.impossible_values": _read_impossible,
    "data.detect_units": _read_units,
    "data.keep_latest_per_id": _read_keep_latest,
    "data.split_file": _read_split,
    "data.excel_serial_dates": _read_excel_serial,
    "data.parse_indian_numbers": _read_locale,
    "data.indian_booleans": _read_locale,
    "data.join_files": _read_join,
    "data.stack_files": _read_stack,
    "data.propose_sql_step": _read_sql,
    "data.propose_features": _read_sql,
    "data.build_dataset": _read_build,
    "data.normalise_key": _read_normalise_key,
    "data.convert_currency": _read_convert,
    "data.define_outcome": _read_outcome,
    "data.map_outcome_values": _read_outcome,
    # Plan O M244 (DEC-1814): the training budget and the data time settings - one pending item each, no step.
    "data.propose_training_budget": _read_pending,
    "data.propose_time_settings": _read_pending,
}
"""How each wave-1 kind's output reads as proposals: the steps with who proposed them, whether they would be
offered ticked and how sure the rules are, the question a reshape is offered through, rows before and after, and
the exact sentence an APPROVE-class item is approved with."""


def output_proposals(kind: str, output: Mapping[str, Any]) -> list[DataProposal]:
    """The proposals in one tool's output (`model_dump(mode="json")`); `ValueError` when it cannot be read."""
    reader = OUTPUT_READERS.get(kind)
    if reader is None:
        raise ValueError(f"the policy cannot read what a {kind} proposal carries")
    try:
        return reader(kind, output)
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ValueError(f"a {kind} proposal is not shaped as its tool's output: {exc}") from exc


def check_tool_output(kind: str, output: Mapping[str, Any], ctx: DataPolicyContext) -> Decision | None:
    """The first refusal of what a tool proposed, by its kind's rule set; None when every proposal is allowed.

    Default deny: a kind with no rule set (`POLICY_NO_RULES`), or whose output the policy cannot read
    (`POLICY_PAYLOAD_INVALID`), is refused. The chat's and the person's kinds (:data:`HANDLER_CHECKED`) are
    decided in their handlers. An output that proposes nothing (no step found) has nothing to decide.
    """
    if kind in HANDLER_CHECKED:
        return None
    if kind not in ctx.policy.rule_sets:
        return _refuse("default", "POLICY_NO_RULES", f"No policy rules are set for a {kind} proposal.")
    try:
        proposals = output_proposals(kind, output)
    except ValueError as exc:
        return _refuse("default", "POLICY_PAYLOAD_INVALID", str(exc)[:300])
    for subject in proposals:
        found = first_refusal(evaluate(subject, ctx))
        if found is not None:
            return found
    return None


def tool_policy_context(ctx: Any, kind: str) -> DataPolicyContext:
    """The facts the rules read for one registry call (`DataToolContext`), computed by the server.

    The file's ID (the context's `primary_key` once M232's integration gives it one, else the session's), its
    outcome, its personal columns and the use case's levels with decision O3's reshape; for several files
    (:data:`FILES_KINDS`) no single ID is protected. A call with no file reads only the policy.
    """
    from engine.agent.proposals import roles_of
    from engine.doctor.reshape_guard import doctor_levels

    policy = policy_or_deny(ctx.root())
    session = getattr(ctx, "session", None)
    key, target = roles_of(session) if session is not None else (None, None)
    key = getattr(ctx, "primary_key", None) or key
    agent = getattr(ctx, "agent", None)
    if agent is None:
        return DataPolicyContext(policy=policy, target=target)
    personal = {c.name for c in agent.profile.columns if c.pii_kinds} | set(
        agent.config.agent.always_hide_columns
    )
    return DataPolicyContext(
        policy=policy,
        primary_key=None if kind in FILES_KINDS else key,
        target=getattr(agent, "target", None) or target,
        personal_columns=frozenset(personal),
        levels=doctor_levels(tuple(agent.config.agent.levels), unified=True),
    )


# ---------------------------------------------------------------------------
# Mutation testing (after `engine.agentic.evals.mutation_test`)
# ---------------------------------------------------------------------------
Fixture = tuple[str, DataProposal, DataPolicyContext]
"""(name, proposal, context): one case the mutation test decides with and without each rule."""


def _outcome(decisions: tuple[Decision, ...]) -> tuple[bool, tuple[str, ...]]:
    return allowed(decisions), tuple(
        sorted({d.code for d in decisions if d.code is not None and not d.allowed})
    )


def rule_mutants(fixtures: Sequence[Fixture]) -> dict[str, tuple[str, ...]]:
    """Per data rule: the fixtures whose decision changes once the rule is removed. A rule with none is untested."""
    caught: dict[str, tuple[str, ...]] = {}
    for name in DATA_RULES:
        mutant = {rule: function for rule, function in DATA_RULES.items() if rule != name}
        caught[name] = tuple(
            label
            for label, subject, ctx in fixtures
            if _outcome(evaluate(subject, ctx, rules=mutant)) != _outcome(evaluate(subject, ctx))
        )
    return caught


def rule_set_mutants(
    fixtures: Sequence[Fixture], policy: DataPolicyConfig
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Per (rule set, rule it names): the fixtures whose decision changes once that rule is left out of that set."""
    caught: dict[tuple[str, str], tuple[str, ...]] = {}
    for kind, rule_set in policy.rule_sets.items():
        for name in rule_set.rules:
            kept = tuple(rule for rule in rule_set.rules if rule != name) or (name,)
            mutated_set = rule_set.model_copy(update={"rules": kept})
            mutated = policy.model_copy(update={"rule_sets": {**policy.rule_sets, kind: mutated_set}})
            labels: list[str] = []
            for label, subject, ctx in fixtures:
                if subject.kind != kind:
                    continue
                before = _outcome(evaluate(subject, ctx))
                if kept == (
                    name,
                ):  # the only rule: without it the set names nothing, and default deny refuses
                    after: tuple[bool, tuple[str, ...]] = (False, ("POLICY_NO_RULES",))
                else:
                    after = _outcome(evaluate(subject, _with_policy(ctx, mutated)))
                if after != before:
                    labels.append(label)
            caught[(kind, name)] = tuple(labels)
    return caught


def _with_policy(ctx: DataPolicyContext, policy: DataPolicyConfig) -> DataPolicyContext:
    from dataclasses import replace

    return replace(ctx, policy=policy)
