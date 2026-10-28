"""Fixtures for the data policy (Plan O M233): for every rule set, one allowed proposal and, for every rule it
names, one proposal that only that rule refuses.

The cases are built from the shipped rule sets (`configs/agentic/policy.yaml`), so a rule added to a set without
a way to break it - a rule that could never matter there - fails the mutation test instead of passing silently.
"""

from __future__ import annotations

import dataclasses
import functools
from typing import Any

from engine.access.roles import Principal, Role
from engine.agent.config import AgentLevel
from engine.agent.contracts import ROW_RESHAPE_STEP_KINDS, RecipeStepKind
from engine.agent.recommend import settings_fields
from engine.config import advanced_settings_schema, config_root, load_use_case
from engine.doctor import policy as pol
from engine.doctor.policy import (
    DataPolicyConfig,
    DataPolicyContext,
    DataProposal,
    DataRuleSet,
    SettingChange,
    StepEffect,
    StepFacts,
)

PK, TARGET, PII, PLAIN, WHEN = "customer_id", "churned", "email", "city", "joined"
COLUMNS = (PK, TARGET, PII, PLAIN, WHEN, "age", "spend")
ANALYST = Principal(user_id="u-asha", username="asha", roles=frozenset({Role.ANALYST}), kind="user")
APPROVER = Principal(user_id="u-ravi", username="ravi", roles=frozenset({Role.APPROVER}), kind="user")
VIEWER = Principal(user_id="u-vik", username="vik", roles=frozenset({Role.VIEWER}), kind="user")

VALID_PARAMS: dict[RecipeStepKind, dict[str, Any]] = {
    RecipeStepKind.PARSE_NUMBER: {"decimal": "."},
    RecipeStepKind.PARSE_DATE: {"dayfirst": True},
    RecipeStepKind.MAP_BOOLEAN: {"true_values": ["yes", "haan"], "false_values": ["no", "nahi"]},
    RecipeStepKind.NORMALISE_TEXT: {"strip": True},
    RecipeStepKind.SET_MISSING: {"values": [99]},
    RecipeStepKind.MAP_VALUES: {"mapping": {"Bombay": "Mumbai"}},
    RecipeStepKind.SET_MISSING_TEXT: {"values": ["unknown"]},
    RecipeStepKind.CONVERT_UNITS: {"units": {"GB": 1024, "MB": 1}},
    RecipeStepKind.SPLIT_COLUMN: {"separator": "-", "part": 0},
    RecipeStepKind.VALID_RANGE: {"minimum": 0},
    RecipeStepKind.FILL_MISSING: {"value": 0},
    RecipeStepKind.RENAME_COLUMN: {},
    RecipeStepKind.DROP_COLUMN: {},
    RecipeStepKind.CORRECT_CELLS: {"key_column": PK, "cells": {"C1": "x"}},
    RecipeStepKind.REMOVE_ROWS: {"operator": "equals", "values": ["test"]},
    RecipeStepKind.NORMALISE_KEY: {"zeros": True},
    RecipeStepKind.KEEP_LATEST_PER_ID: {"time_column": WHEN, "ties": "refuse"},
    RecipeStepKind.COMBINE_ROWS: {"time_column": WHEN},
    RecipeStepKind.UNPIVOT: {
        "id_columns": [PK],
        "value_columns": [["spend_jan", "spend", "jan"]],
        "year": 2025,
    },
}
_NOT_ALLOWED_CANDIDATES = (
    RecipeStepKind.DROP_COLUMN,
    RecipeStepKind.CORRECT_CELLS,
    RecipeStepKind.PARSE_NUMBER,
    RecipeStepKind.NORMALISE_TEXT,
)


@functools.cache
def shipped() -> DataPolicyConfig:
    return pol.load_data_policy(config_root(None))


@functools.cache
def fields() -> dict[str, Any]:
    config = load_use_case("targeted-advertisement", None)
    schema = advanced_settings_schema(config, columns=COLUMNS, primary_key=PK, target=TARGET)
    return settings_fields(schema)


def context(policy: DataPolicyConfig | None = None, **change: Any) -> DataPolicyContext:
    base = DataPolicyContext(
        policy=policy or shipped(),
        primary_key=PK,
        target=TARGET,
        personal_columns=frozenset({PII}),
        levels=(AgentLevel.CLEAN, AgentLevel.DERIVE, AgentLevel.RESHAPE),
        settings=fields(),
    )
    return dataclasses.replace(base, **change)


def params_for(kind: RecipeStepKind, rules: DataRuleSet) -> dict[str, Any]:
    """Valid parameters for `kind` under `rules`: the base ones the rule set allows, plus the values it requires."""
    params = dict(VALID_PARAMS[kind])
    rule = rules.params.get(kind)
    if rule is not None:
        params = {name: value for name, value in params.items() if name in rule.allowed}
        params.update(rule.equals)
    return params


def step(kind: RecipeStepKind, rules: DataRuleSet, **change: Any) -> StepFacts:
    column = PK if kind in ROW_RESHAPE_STEP_KINDS else PLAIN
    new_column = "city_part" if kind is RecipeStepKind.SPLIT_COLUMN else None
    facts = StepFacts(kind=kind, column=column, params=params_for(kind, rules), new_column=new_column)
    return dataclasses.replace(facts, **change)


def base(kind: str, rules: DataRuleSet) -> DataProposal:
    """An allowed proposal of `kind`: the rules', unticked, with what an APPROVE-class item carries."""
    if rules.proposes == "setting":
        return DataProposal(kind=kind, source="ai", setting=SettingChange("split.test_fraction", 0.2))
    steps: tuple[StepFacts, ...] = ()
    if rules.proposes == "steps" or (rules.proposes == "plan" and rules.step_kinds):
        steps = (step(rules.step_kinds[0], rules),)
    return DataProposal(
        kind=kind,
        source="rules",
        proposed_by="u-asha",
        steps=steps,
        asked=True,
        rows_in=100,
        rows_out=100,
        sentence=f"Approve this {kind}.",
    )


def _effect(changed: int = 5, failed: int = 0) -> StepEffect:
    return StepEffect(changed=changed, failed=failed, out_of=100, rows_tried=100)


def violator(kind: str, rules: DataRuleSet, rule: str) -> tuple[DataProposal, DataPolicyContext]:
    """A proposal of `kind` that `rule` alone refuses (every other rule of the set allows it)."""
    ok = base(kind, rules)
    ctx = context()
    first = rules.step_kinds[0] if rules.step_kinds else None
    ai = dataclasses.replace(ok, source="ai", looked_at=True, effect=_effect())
    if rule == pol.STEP_KIND:
        other = next(k for k in _NOT_ALLOWED_CANDIDATES if k not in rules.step_kinds)
        return dataclasses.replace(ok, steps=(step(other, rules),)), ctx
    if rule == pol.STEP_PARAMS:
        assert first is not None
        broken = step(first, rules)
        return (
            dataclasses.replace(ok, steps=(dataclasses.replace(broken, params={**broken.params, "zzz": 1}),)),
            ctx,
        )
    if rule == pol.SPELLINGS:
        spelled = step(
            RecipeStepKind.MAP_BOOLEAN, rules, params={"true_values": ["yes", "na"], "false_values": ["no"]}
        )
        return dataclasses.replace(ok, steps=(spelled,)), ctx
    if rule == pol.PROTECTED:
        assert first is not None
        if first in ROW_RESHAPE_STEP_KINDS:
            moved = step(first, rules)
            moved = dataclasses.replace(moved, params={**moved.params, "time_column": TARGET})
        else:
            moved = step(first, rules, column=TARGET)
        return dataclasses.replace(ok, steps=(moved,)), ctx
    if rule == pol.PERSONAL:
        assert first is not None
        return dataclasses.replace(ok, steps=(step(first, rules, column=PII),)), ctx
    if rule == pol.LEVEL:
        from engine.agent.recipe import _LEVEL_OF

        high = next(k for k in rules.step_kinds if _LEVEL_OF.get(k, AgentLevel.CLEAN) is not AgentLevel.CLEAN)
        return dataclasses.replace(ok, steps=(step(high, rules),)), context(levels=(AgentLevel.CLEAN,))
    if rule == pol.GROUNDED:
        return dataclasses.replace(ai, looked_at=False), ctx
    if rule == pol.TRIED:
        return dataclasses.replace(ai, effect=None), ctx
    if rule == pol.NEVER_TICKED:
        return dataclasses.replace(ai, ticked=True), ctx
    if rule == pol.CHANGE_SHARE:
        assert first is not None
        retyping = first in shipped().change_share.retyping_kinds
        heavy = _effect(changed=70, failed=70) if retyping else _effect(changed=70)
        return dataclasses.replace(ai, effect=heavy), ctx
    if rule == pol.SETTING:
        return dataclasses.replace(ok, setting=SettingChange("prepare.exclude_columns", [PLAIN])), ctx
    if rule == pol.APPROVE_CLASS:
        return dataclasses.replace(ok, rows_out=50, sentence=None), ctx
    if rule == pol.RESHAPE_GUARD:
        return dataclasses.replace(ok, asked=False), ctx
    raise AssertionError(f"no violator for {rule}")


def fixtures(policy: DataPolicyConfig | None = None) -> list[tuple[str, DataProposal, DataPolicyContext]]:
    """Every rule set's allowed proposal and its one-rule violators, named `<kind>:<rule>`."""
    policy = policy or shipped()
    cases: list[tuple[str, DataProposal, DataPolicyContext]] = []
    for kind, rules in policy.rule_sets.items():
        cases.append((f"{kind}:allowed", base(kind, rules), context(policy)))
        for rule in rules.rules:
            subject, ctx = violator(kind, rules, rule)
            cases.append((f"{kind}:{rule}", subject, dataclasses.replace(ctx, policy=policy)))
    return cases
