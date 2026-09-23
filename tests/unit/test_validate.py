"""Unit tests for `engine.stages.validate` (plan sections 6.3 and 10, M2_DESIGN section 2).

Plan section 10 asks for *"every validation check (positive and negative case)"*. The matrix is
driven off `tests.fixtures.make_data.VARIANTS`, so a code with no case is a red test rather than an
oversight, and `test_every_code_has_a_positive_case` / `test_every_code_has_a_negative_case` close
the loop against `VALIDATION_CODES`.

The file also pins the architecture the design asks for: every check callable with nothing but a
DataFrame and `CheckParams()`, the leakage exemption that keeps a use case's own target from
flagging itself, acknowledgement that never mutates severity, and byte-identical reports across
two runs.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import logging
import re
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

import pandas as pd
import pytest

from engine.config import (
    ColumnRole,
    ColumnType,
    PiiHandling,
    ProblemType,
    RunMode,
    SplitType,
    UseCaseConfig,
    load_use_case,
    overridable_paths,
    resolve_config,
)
from engine.contracts import (
    VALIDATION_CODES,
    FeatureSchema,
    FeatureSchemaColumn,
    Severity,
    ValidationCheck,
    ValidationReport,
)
from engine.stages import validate as v
from tests.fixtures.make_data import (
    CLEAN,
    CONSTANT_COLUMN,
    HIGH_NULL_COLUMN,
    ID_LIKE_COLUMN,
    LEAKY_COLUMN,
    PII_EMAIL_COLUMN,
    SCORING,
    VARIANT_SPECS,
    VARIANTS,
    GenerationSpec,
    generate,
    predictive_use_case_ids,
    variants_for,
)

#: Small enough to keep the matrix quick, large enough that every variant still expresses itself
#: (`min_rows` is 1000, `min_positive` 200, and `too_few_positives` plants exactly 150).
ROWS: int = 2_000

#: The three variants that are scoring files and are therefore judged against a schema.
SCHEMA_VARIANTS: tuple[str, ...] = ("renamed_column", "missing_column", "type_changed_column")


def _reference_first(use_case_ids: tuple[str, ...]) -> tuple[str, ...]:
    """`use_case_ids` with the first one whose template has a consent and a time column moved first.

    The single-use-case checks below run on `USE_CASE_IDS[0]` and need both roles. Sorted order
    gave them that until the library's use cases, whose public files lack a consent column
    (DEC-407), moved into `configs/` and sorted ahead (DEC-098). Over the telecom use cases alone
    this is the identity, so every assertion below still runs on the use case it always did.
    """
    roles = (ColumnRole.CONSENT, ColumnRole.TIME)
    reference = next(
        use_case_id
        for use_case_id in use_case_ids
        if all(load_use_case(use_case_id).template.by_role(role) for role in roles)
    )
    return (reference, *(use_case_id for use_case_id in use_case_ids if use_case_id != reference))


USE_CASE_IDS: tuple[str, ...] = _reference_first(predictive_use_case_ids())
BROKEN_VARIANTS: tuple[str, ...] = tuple(name for name, code in VARIANTS.items() if code is not None)

#: Every (use case, broken variant) pair that means something, derived from the templates.
#: A variant that corrupts a column a template does not carry has nothing to corrupt there - the
#: public Telco Customer Churn file has no date column, so there is no `unparseable_time` file to
#: make - and the generator says so rather than the matrix assuming every template is the same
#: shape. `test_the_broken_matrix_still_covers_every_code` keeps that from hiding a real gap.
BROKEN_MATRIX: tuple[tuple[str, str], ...] = tuple(
    (use_case_id, variant)
    for use_case_id in USE_CASE_IDS
    for variant in variants_for(use_case_id)
    if VARIANTS[variant] is not None
)

UPLOAD_ID: str = "u_0123456789ab"
FIXED_NOW: datetime = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures owned by this module (M2_DESIGN section 6)
# ---------------------------------------------------------------------------
@cache
def config_for(use_case_id: str) -> UseCaseConfig:
    return load_use_case(use_case_id)


def config_with(use_case_id: str, overrides: dict[str, object]) -> UseCaseConfig:
    return resolve_config(use_case_id, overrides).config


def primary_key_of(config: UseCaseConfig) -> str:
    return config.template.by_role(ColumnRole.PRIMARY_KEY)[0].name


def target_of(config: UseCaseConfig) -> str:
    return config.template.by_role(ColumnRole.TARGET)[0].name


def time_column_of(config: UseCaseConfig) -> str:
    return config.template.by_role(ColumnRole.TIME)[0].name


@cache
def _cached_frame(use_case_id: str, variant: str, rows: int) -> pd.DataFrame:
    return generate(GenerationSpec(use_case_id, rows=rows, variant=variant))


def fixture_frame(use_case_id: str, variant: str = CLEAN, *, rows: int = ROWS) -> pd.DataFrame:
    """Just the frame, for calling a single check with nothing but a DataFrame."""
    return _cached_frame(use_case_id, variant, rows).copy()


def fixture_report(
    use_case_id: str,
    variant: str = CLEAN,
    *,
    overrides: dict[str, object] | None = None,
    primary_key: str | None = None,
    target: str | None = None,
    acknowledged: tuple[str, ...] = (),
    rows: int = ROWS,
    use_defaults: bool = True,
) -> ValidationReport:
    """Generate a variant and validate it for training, with the template's roles by default."""
    config = config_for(use_case_id) if overrides is None else config_with(use_case_id, overrides)
    if use_defaults:
        primary_key = primary_key_of(config) if primary_key is None else primary_key
        target = target_of(config) if target is None else target
    return v.validate_for_training(
        fixture_frame(use_case_id, variant, rows=rows),
        config,
        primary_key=primary_key,
        target=target,
        acknowledged=acknowledged,
        upload_id=UPLOAD_ID,
        now=FIXED_NOW,
    )


@cache
def schema_for(use_case_id: str) -> FeatureSchema:
    """A `FeatureSchema` taken from the clean frame, as `register` would save it at fit time."""
    config = config_for(use_case_id)
    frame = _cached_frame(use_case_id, CLEAN, ROWS)
    facts = v.derive_facts(frame)
    return FeatureSchema(
        use_case_id=use_case_id,
        model_version_id=f"m_{use_case_id}_1",
        primary_key=primary_key_of(config),
        target=target_of(config),
        problem_type=config.problem_type,
        columns=tuple(
            FeatureSchemaColumn(name=name, inferred_type=facts.types[name]) for name in facts.columns
        ),
        row_count_at_fit=len(frame),
        created_at=FIXED_NOW,
    )


def schema_report(
    use_case_id: str,
    variant: str = SCORING,
    *,
    with_config: bool = True,
    acknowledged: tuple[str, ...] = (),
) -> ValidationReport:
    config = config_for(use_case_id)
    return v.validate_against_schema(
        fixture_frame(use_case_id, variant),
        schema_for(use_case_id),
        primary_key=primary_key_of(config),
        config=config if with_config else None,
        acknowledged=acknowledged,
        upload_id=UPLOAD_ID,
        now=FIXED_NOW,
    )


def codes(report: ValidationReport) -> list[str]:
    return [check.code for check in report.checks]


def first(report: ValidationReport, code: str) -> ValidationCheck:
    found = [check for check in report.checks if check.code == code]
    assert found, f"{code} not in {codes(report)}"
    return found[0]


def params_for(use_case_id: str, **overrides: object) -> v.CheckParams:
    config = config_for(use_case_id)
    params = v.params_from_config(
        config,
        primary_key=primary_key_of(config),
        target=target_of(config),
        seed=7,
    )
    return dataclasses.replace(params, **overrides) if overrides else params


# ---------------------------------------------------------------------------
# The registry, the order and the modes (M2_DESIGN sections 2.1 - 2.3)
# ---------------------------------------------------------------------------
def test_check_order_is_the_plan_table() -> None:
    assert len(v.CHECK_ORDER) == 19
    assert frozenset(v.CHECK_ORDER) == VALIDATION_CODES
    assert len(set(v.CHECK_ORDER)) == len(v.CHECK_ORDER)
    assert v.CHECK_ORDER[:3] == ("PK_MISSING", "PK_NOT_UNIQUE", "PK_NULLS")
    assert v.CHECK_ORDER[-1] == "SUPPRESSION_COLUMN_MISSING"


def test_registry_covers_every_code() -> None:
    assert {spec.code for spec in v.CHECK_REGISTRY} == VALIDATION_CODES
    assert [spec.code for spec in v.CHECK_REGISTRY] == list(v.CHECK_ORDER)
    for spec in v.CHECK_REGISTRY:
        assert spec.order == v.CHECK_ORDER.index(spec.code)
        assert v.CHECKS_BY_CODE[spec.code] is spec


def test_checks_for_modes() -> None:
    train = {spec.code for spec in v.checks_for(RunMode.TRAIN)}
    score = {spec.code for spec in v.checks_for(RunMode.SCORE)}
    assert "SCHEMA_MISMATCH" not in train
    assert train | score == VALIDATION_CODES
    assert score == {
        "PK_MISSING",
        "PK_NOT_UNIQUE",
        "PK_NULLS",
        "SCHEMA_MISMATCH",
        "PII_DETECTED",
        "CONSENT_COLUMN_MISSING",
        "SUPPRESSION_COLUMN_MISSING",
    }
    for mode in RunMode:
        ordered = [spec.order for spec in v.checks_for(mode)]
        assert ordered == sorted(ordered)


def test_acknowledgeable_is_leakage_and_every_warning() -> None:
    for spec in v.CHECK_REGISTRY:
        if spec.code == "LEAKAGE_SUSPECTED" or spec.severity is Severity.WARNING:
            assert spec.acknowledgeable, spec.code
        else:
            assert not spec.acknowledgeable, spec.code


# ---------------------------------------------------------------------------
# Purity: a check knows about data, not about a request (M2_DESIGN section 2.1)
# ---------------------------------------------------------------------------
_FLAT_ANNOTATION_ATOMS = {
    "str",
    "int",
    "float",
    "bool",
    "None",
    "tuple",
    "Mapping",
    "ColumnType",
    "ProblemType",
    "SplitType",
    "PiiHandling",
    "FeatureSchema",
    "FrameFacts",
    "ColumnStatsLike",
}


def test_check_params_is_flat() -> None:
    """A future attempt to smuggle a UseCaseConfig, a Storage or a profile into params fails here."""
    for field in dataclasses.fields(v.CheckParams):
        atoms = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(field.type)))
        unexpected = atoms - _FLAT_ANNOTATION_ATOMS
        assert not unexpected, f"{field.name}: {field.type!r} mentions {sorted(unexpected)}"


def test_check_params_is_fully_defaulted() -> None:
    for field in dataclasses.fields(v.CheckParams):
        has_default = field.default is not dataclasses.MISSING
        has_factory = field.default_factory is not dataclasses.MISSING
        assert has_default or has_factory, field.name
    assert v.CheckParams() == v.CheckParams()


def _referenced_names(function: object) -> set[str]:
    code = function.__code__  # type: ignore[attr-defined]
    names = set(code.co_names) | set(code.co_freevars)
    for const in code.co_consts:
        if hasattr(const, "co_names"):
            names |= set(const.co_names)
    return names


@pytest.mark.parametrize("spec", v.CHECK_REGISTRY, ids=lambda spec: spec.code)
def test_checks_are_pure(spec: v.CheckSpec) -> None:
    """No check may reach for a config, a store, a profile, a report or the clock."""
    forbidden = {
        "UseCaseConfig",
        "Storage",
        "DatasetProfile",
        "ValidationReport",
        "datetime",
        "utc_now",
        "load_use_case",
        "get_catalog",
    }
    assert not _referenced_names(spec.fn) & forbidden


@pytest.mark.parametrize("spec", v.CHECK_REGISTRY, ids=lambda spec: spec.code)
def test_every_check_runs_on_a_bare_frame(spec: v.CheckSpec) -> None:
    """`check(df, CheckParams())` is legal for all nineteen: no API, no config, no run context."""
    frame = pd.DataFrame({"a": [1, 2, 3, 4], "b": ["x", "y", "x", "y"]})
    result = spec.fn(frame, v.CheckParams())
    assert isinstance(result, v.CheckResult)
    assert result.code == spec.code
    if result.skipped:
        assert result.skip_reason
        assert result.findings == ()
    assert result.passed is (not result.findings)


@pytest.mark.parametrize("spec", v.CHECK_REGISTRY, ids=lambda spec: spec.code)
def test_every_check_runs_on_an_empty_frame(spec: v.CheckSpec) -> None:
    assert isinstance(spec.fn(pd.DataFrame(), v.CheckParams()), v.CheckResult)


def test_params_from_config_is_the_only_adapter() -> None:
    """`config.<attr>` appears in exactly one function body in the module."""
    source = Path(inspect.getfile(v)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    readers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Attribute)
                and isinstance(inner.value, ast.Name)
                and inner.value.id == "config"
            ):
                readers.add(node.name)
    assert readers == {"params_from_config"}


def test_validate_frame_needs_no_config() -> None:
    """The seam a later feature-engineering caller uses: a frame, a CheckParams and an id."""
    frame = pd.DataFrame(
        {
            "k": [f"K-{i}" for i in range(1200)],
            "f": list(range(1200)),
            "y": [i % 5 == 0 for i in range(1200)],
        }
    )
    params = v.CheckParams(primary_key="k", target="y", min_positive=50)
    report = v.validate_frame(
        frame, params, mode=RunMode.TRAIN, upload_id="sha256:v1:deadbeef", now=FIXED_NOW
    )
    assert isinstance(report, ValidationReport)
    assert report.passed is True
    assert report.upload_id == "sha256:v1:deadbeef"


def test_derive_facts_is_pure() -> None:
    frame = fixture_frame(USE_CASE_IDS[0])
    assert v.derive_facts(frame) == v.derive_facts(frame.copy())
    config = config_for(USE_CASE_IDS[0])
    params = params_for(USE_CASE_IDS[0])
    with_memo = v.run_checks(
        frame, dataclasses.replace(params, facts=v.derive_facts(frame)), mode=RunMode.TRAIN
    )
    without = v.run_checks(frame, params, mode=RunMode.TRAIN)
    assert with_memo == without
    assert v.facts_for(frame, params) == v.derive_facts(frame)
    assert config.id == USE_CASE_IDS[0]


# ---------------------------------------------------------------------------
# Positive cases: the fifteen broken variants, driven off VARIANTS
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("use_case_id", "variant"), BROKEN_MATRIX)
def test_variant_triggers_its_code(use_case_id: str, variant: str) -> None:
    """Each broken fixture produces the one code it was built for, for every use case it fits."""
    code = VARIANTS[variant]
    if variant in SCHEMA_VARIANTS:
        report = schema_report(use_case_id, variant)
    else:
        overrides: dict[str, object] | None = None
        if variant == "unparseable_time":
            # TIME_COLUMN_UNPARSEABLE is about the *configured* time column, and several use cases
            # carry a date column but split randomly, so they configure none.
            overrides = {"split.time_column": time_column_of(config_for(use_case_id))}
        report = fixture_report(use_case_id, variant, overrides=overrides)
    assert code in codes(report), f"{variant} -> {codes(report)}"


def test_the_broken_matrix_still_covers_every_code() -> None:
    """Deriving the matrix must not quietly drop a code or a use case from the sweep."""
    assert {use_case_id for use_case_id, _ in BROKEN_MATRIX} == set(USE_CASE_IDS)
    assert {VARIANTS[variant] for _, variant in BROKEN_MATRIX} == {
        VARIANTS[variant] for variant in BROKEN_VARIANTS
    }


@pytest.mark.parametrize("variant", BROKEN_VARIANTS)
def test_variant_triggers_nothing_else(variant: str) -> None:
    """Each variant is built to trigger exactly one code; anything more is a false positive."""
    use_case_id = USE_CASE_IDS[0]
    if variant in SCHEMA_VARIANTS:
        report = schema_report(use_case_id, variant)
    else:
        overrides = (
            {"split.time_column": time_column_of(config_for(use_case_id))}
            if variant == "unparseable_time"
            else None
        )
        report = fixture_report(use_case_id, variant, overrides=overrides)
    assert set(codes(report)) == {VARIANTS[variant]}


def test_variant_specs_and_variants_agree() -> None:
    assert len(VARIANTS) == 17
    assert {name: spec.code for name, spec in VARIANT_SPECS.items()} == dict(VARIANTS)


# ---------------------------------------------------------------------------
# The six codes no synthetic file can express, each from clean data
# ---------------------------------------------------------------------------
def consent_column_of(config: UseCaseConfig) -> str:
    return config.template.by_role(ColumnRole.CONSENT)[0].name


@cache
def _config_driven_reports(use_case_id: str) -> dict[str, ValidationReport]:
    """The six codes that are about what the request or the config says, not about the bytes."""
    config = config_for(use_case_id)
    frame = fixture_frame(use_case_id)
    base = params_for(use_case_id)

    def report(params: v.CheckParams, on: pd.DataFrame = frame) -> ValidationReport:
        return v.validate_frame(on, params, mode=RunMode.TRAIN, upload_id=UPLOAD_ID, now=FIXED_NOW)

    # `_check_template` refuses a consent column the template does not carry, so the positive case
    # is the configured consent column on a file that does not have it.
    consent = consent_column_of(config)
    consentless = frame.drop(columns=[consent])
    with_consent = config_with(use_case_id, {"governance.consent_column": consent})

    return {
        "PK_MISSING": fixture_report(
            use_case_id, primary_key="", use_defaults=False, target=target_of(config)
        ),
        "TARGET_MISSING": fixture_report(
            use_case_id, primary_key=primary_key_of(config), target="", use_defaults=False
        ),
        "TIME_COLUMN_MISSING": fixture_report(
            use_case_id, overrides={"split.type": "time_based", "split.time_column": None}
        ),
        "CONSENT_COLUMN_MISSING": v.validate_for_training(
            consentless,
            with_consent,
            primary_key=primary_key_of(config),
            target=target_of(config),
            upload_id=UPLOAD_ID,
            now=FIXED_NOW,
        ),
        "SUPPRESSION_COLUMN_MISSING": report(dataclasses.replace(base, opt_out_column="do_not_contact")),
        "TARGET_IMBALANCE_SEVERE": v.validate_for_training(
            generate(GenerationSpec(use_case_id, rows=ROWS, positive_rate=0.004)),
            config,
            primary_key=primary_key_of(config),
            target=target_of(config),
            upload_id=UPLOAD_ID,
            now=FIXED_NOW,
        ),
    }


@pytest.mark.parametrize("code", sorted(_config_driven_reports(USE_CASE_IDS[0])))
def test_config_driven_code_has_a_positive_case(code: str) -> None:
    report = _config_driven_reports(USE_CASE_IDS[0])[code]
    assert code in codes(report)


def test_pk_missing_when_the_named_column_is_absent() -> None:
    report = fixture_report(
        USE_CASE_IDS[0],
        primary_key="not_a_column",
        target=target_of(config_for(USE_CASE_IDS[0])),
        use_defaults=False,
    )
    check = first(report, "PK_MISSING")
    assert "not_a_column" in check.suggestion
    assert check.details["present"] is False
    assert check.details["selected"] == "not_a_column"


def test_target_missing_when_the_named_column_is_absent() -> None:
    config = config_for(USE_CASE_IDS[0])
    report = fixture_report(
        USE_CASE_IDS[0], primary_key=primary_key_of(config), target="not_a_column", use_defaults=False
    )
    check = first(report, "TARGET_MISSING")
    assert "not_a_column" in check.message
    assert check.details["expected"] == config.target.column


def test_every_code_has_a_positive_case() -> None:
    """VARIANTS plus the config-driven cases must cover every code in `VALIDATION_CODES`."""
    covered = {code for code in VARIANTS.values() if code is not None}
    covered |= set(_config_driven_reports(USE_CASE_IDS[0]))
    assert covered == VALIDATION_CODES


# ---------------------------------------------------------------------------
# Negative cases: the clean (and scoring) reports are silent on every code
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code", sorted(VALIDATION_CODES))
def test_every_code_has_a_negative_case(code: str) -> None:
    """One assertion per code that a healthy file does not produce it."""
    use_case_id = USE_CASE_IDS[0]
    if code == "SCHEMA_MISMATCH":
        report = schema_report(use_case_id, SCORING)
    else:
        report = fixture_report(use_case_id, CLEAN)
    assert code not in codes(report)


def test_too_few_positives_is_the_negative_case_for_imbalance() -> None:
    """The fixture is built at 1.5 % so one variant is a positive and a negative at once."""
    report = fixture_report(USE_CASE_IDS[0], "too_few_positives")
    assert "TARGET_TOO_FEW_POSITIVES" in codes(report)
    assert "TARGET_IMBALANCE_SEVERE" not in codes(report)


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_clean_triggers_no_errors(use_case_id: str) -> None:
    """The milestone hangs on this: a correct upload starts a run, for every shipped use case."""
    report = fixture_report(use_case_id, CLEAN)
    assert report.passed is True
    assert report.error_count == 0
    assert [check.code for check in report.checks if check.severity is Severity.ERROR] == []


# ---------------------------------------------------------------------------
# Leakage: the exemption rule (M2_DESIGN section 2.26, DEC-062)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_clean_never_flags_the_target(use_case_id: str) -> None:
    """The regression test for DEC-062.

    Several shipped targets match the configured leakage name pattern - `converted_30d`,
    `churn_next_60d`, and the Telco file's own `Churn` - so without the exemption a perfectly
    clean upload for those use cases would be refused with a 409.
    """
    config = config_for(use_case_id)
    report = fixture_report(use_case_id, CLEAN)
    flagged = {check.column for check in report.checks if check.code == "LEAKAGE_SUSPECTED"}
    assert target_of(config) not in flagged
    assert primary_key_of(config) not in flagged
    assert not [
        check
        for check in report.checks
        if check.code == "LEAKAGE_SUSPECTED" and check.severity is Severity.ERROR
    ]


def targets_matching_the_leakage_pattern() -> tuple[str, ...]:
    """The shipped use cases whose own target name trips the configured leakage name pattern."""
    pattern = re.compile(config_for(USE_CASE_IDS[0]).catalog.column_name_patterns.leakage, re.IGNORECASE)
    return tuple(uid for uid in USE_CASE_IDS if pattern.search(target_of(config_for(uid))))


def test_the_leakage_pattern_really_does_match_shipped_targets() -> None:
    """Without this the exemption test above could pass for the wrong reason.

    Which targets match is read off the configs rather than counted here. When this was written
    two did; the Telco Customer Churn file's `Churn` is a third, and a use case added by YAML
    alone may add a fourth. What must keep holding is the shape of the claim: at least one shipped
    target trips the pattern, and every one that does really would be flagged if DEC-062's
    exemption were not there - which is what makes `test_clean_never_flags_the_target` a
    regression test and not a tautology.
    """
    matched = targets_matching_the_leakage_pattern()
    assert matched, "no shipped target trips the leakage pattern; the exemption test proves nothing"
    for use_case_id in matched:
        target = target_of(config_for(use_case_id))
        frame = fixture_frame(use_case_id)
        # Judge the file against some *other* target, so the real one is an ordinary candidate.
        frame["placeholder_target"] = [index % 4 == 0 for index in range(len(frame))]
        params = params_for(
            use_case_id,
            target="placeholder_target",
            target_aliases=(),
            leakage_baseline=False,
        )
        findings = v.check_leakage_suspected(frame, params).findings
        reasons = {check.column: check.details["reason"] for check in findings}
        assert reasons.get(target) == "name_pattern", f"{use_case_id}: {reasons}"


EXEMPT_ROLES: tuple[str, ...] = (
    "primary_key",
    "time_column",
    "group_column",
    "consent_column",
    "opt_out_column",
    "recently_contacted_column",
    "fairness_column",
)


@pytest.mark.parametrize("role", EXEMPT_ROLES)
def test_exempt_columns_are_never_flagged(role: str) -> None:
    """A copy of the target in a governed column is exempt; the same copy in a feature is not."""
    use_case_id = USE_CASE_IDS[0]
    frame = fixture_frame(use_case_id)
    target = target_of(config_for(use_case_id))
    frame["planted"] = frame[target]
    facts = v.derive_facts(frame)
    base = params_for(use_case_id, facts=facts)

    exempt = dataclasses.replace(base, **{role: "planted"})
    assert "planted" not in v.candidate_columns(frame, exempt, facts)
    assert not [
        check for check in v.check_leakage_suspected(frame, exempt).findings if check.column == "planted"
    ]

    assert "planted" in v.candidate_columns(frame, base, facts)
    flagged = [
        check for check in v.check_leakage_suspected(frame, base).findings if check.column == "planted"
    ]
    assert len(flagged) == 1
    assert flagged[0].severity is Severity.ERROR
    assert flagged[0].details["reason"] == "auc"


def test_leakage_exempt_names_covers_every_governed_column() -> None:
    params = v.CheckParams(
        primary_key="k",
        target="y",
        target_aliases=("y_yaml", "y_template"),
        time_column="t",
        group_column="g",
        consent_column="c",
        opt_out_column="o",
        recently_contacted_column="r",
        fairness_column="f",
        excluded_columns=("x1", "x2"),
    )
    assert v.leakage_exempt_names(params) == frozenset(
        {"k", "y", "y_yaml", "y_template", "t", "g", "c", "o", "r", "f", "x1", "x2"}
    )
    assert v.leakage_exempt_names(v.CheckParams()) == frozenset()


def test_excluded_column_is_not_flagged() -> None:
    """Plan section 8's exclude-confirmation clears the 409 on re-submit."""
    use_case_id = USE_CASE_IDS[0]
    assert "LEAKAGE_SUSPECTED" in codes(fixture_report(use_case_id, "leaky_column"))
    cleared = fixture_report(
        use_case_id, "leaky_column", overrides={"prepare.exclude_columns": [LEAKY_COLUMN]}
    )
    assert "LEAKAGE_SUSPECTED" not in codes(cleared)
    assert cleared.passed is True


def test_exempt_columns_still_get_their_own_checks() -> None:
    """The exemption redirects a column to the right message; it never silences it."""
    use_case_id = USE_CASE_IDS[0]
    report = fixture_report(use_case_id, "duplicate_keys")
    assert first(report, "PK_NOT_UNIQUE").column == primary_key_of(config_for(use_case_id))


# ---------------------------------------------------------------------------
# Leakage: severity by branch (M2_DESIGN section 2.27, DEC-063)
# ---------------------------------------------------------------------------
def test_leaky_column_is_an_error_on_the_auc_branch() -> None:
    report = fixture_report(USE_CASE_IDS[0], "leaky_column")
    check = first(report, "LEAKAGE_SUSPECTED")
    assert check.column == LEAKY_COLUMN
    assert check.severity is Severity.ERROR
    assert check.details["reason"] == "auc"
    assert float(check.details["auc"]) > 0.98
    assert check.acknowledgeable is True
    assert report.passed is False
    assert check.message == (
        f"Column '{LEAKY_COLUMN}' almost perfectly predicts the target. "
        "It may contain the answer. Exclude it?"
    )


def test_name_branch_is_a_warning() -> None:
    """`win-back-campaign` carries `churn_reason`, a legitimate pre-outcome feature."""
    report = fixture_report("win-back-campaign", CLEAN)
    leakage = [check for check in report.checks if check.code == "LEAKAGE_SUSPECTED"]
    assert len(leakage) == 1
    assert leakage[0].column == "churn_reason"
    assert leakage[0].severity is Severity.WARNING
    assert leakage[0].details["reason"] == "name_pattern"
    assert report.passed is True
    assert report.error_count == 0


def test_after_target_branch() -> None:
    """Header position is the only signal such a file carries, so it is a warning, never an error."""
    use_case_id = USE_CASE_IDS[0]
    frame = fixture_frame(use_case_id)
    after = frame.copy()
    # Deliberately uninformative values: this branch is about the name and the position alone.
    after["conversion_date"] = [f"2026-0{1 + index % 9}-15" for index in range(len(after))]
    params = params_for(use_case_id, leakage_baseline=False)
    flagged = {
        check.column: check.details["reason"] for check in v.check_leakage_suspected(after, params).findings
    }
    assert flagged.get("conversion_date") == "after_target"

    before = after[["conversion_date", *[c for c in after.columns if c != "conversion_date"]]]
    reasons = {
        check.column: check.details["reason"] for check in v.check_leakage_suspected(before, params).findings
    }
    assert "conversion_date" not in reasons
    assert all(
        check.severity is Severity.WARNING for check in v.check_leakage_suspected(after, params).findings
    )


def test_after_target_branch_does_not_run_without_a_target() -> None:
    frame = pd.DataFrame({"a": [1, 2, 3], "b_date": ["2026-01-01", "2026-01-02", "2026-01-03"]})
    result = v.check_leakage_suspected(frame, v.CheckParams())
    assert result.skipped is True
    assert result.findings == ()


def test_name_branch_runs_on_an_empty_frame() -> None:
    """Plan section 8's 409 works even on an upload that was never fully read."""
    empty = pd.DataFrame({"k": [], "churn_flag": [], "y": []})
    facts = v.derive_facts(empty)
    params = v.CheckParams(primary_key="k", target="y", facts=facts)
    assert v.check_leakage_suspected(empty, params).skipped is True

    tiny = pd.DataFrame({"k": ["a", "b"], "churn_flag": [1, 0], "y": [1, 0]})
    params = v.CheckParams(primary_key="k", target="y")
    findings = v.check_leakage_suspected(tiny, params).findings
    assert [check.column for check in findings] == ["churn_flag"]
    assert findings[0].details["auc"] is None
    assert findings[0].severity is Severity.WARNING


def test_high_cardinality_strings_are_skipped_by_the_auc_branch() -> None:
    """A near-unique string column is HIGH_CARDINALITY_ID_LIKE's business, not leakage's."""
    rows = 1000
    frame = pd.DataFrame(
        {
            "k": [f"K-{i:05d}" for i in range(rows)],
            "chatter": [f"note-{i:05d}" for i in range(rows)],
            "y": [i % 4 == 0 for i in range(rows)],
        }
    )
    params = v.CheckParams(primary_key="k", target="y", leakage_baseline=False)
    assert v.check_leakage_suspected(frame, params).findings == ()


def test_leakage_check_switch_turns_off_all_three_branches() -> None:
    report = fixture_report(USE_CASE_IDS[0], "leaky_column", overrides={"validation.leakage_check": False})
    assert "LEAKAGE_SUSPECTED" not in codes(report)
    assert report.passed is True


def test_threshold_is_params_driven() -> None:
    use_case_id = USE_CASE_IDS[0]
    frame = fixture_frame(use_case_id, "leaky_column")
    strict = params_for(use_case_id, leakage_auc_threshold=0.6, leakage_baseline=False)
    lenient = params_for(use_case_id, leakage_auc_threshold=1.0, leakage_baseline=False)
    flagged = v.check_leakage_suspected(frame, strict).findings
    assert len(flagged) > 1
    assert LEAKY_COLUMN in {check.column for check in flagged}
    assert LEAKY_COLUMN not in {check.column for check in v.check_leakage_suspected(frame, lenient).findings}


# ---------------------------------------------------------------------------
# Leakage: the rank statistic and the sample
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_single_feature_auc_matches_sklearn(seed: int) -> None:
    import numpy as np
    from sklearn.metrics import roc_auc_score

    rng = np.random.default_rng(seed)
    y = pd.Series(rng.integers(0, 2, size=2000))
    x = pd.Series(rng.normal(size=2000) + y * 0.8)
    assert round(float(v.single_feature_auc(x, y) or 0.0), 4) == round(float(roc_auc_score(y, x)), 4)


def test_single_feature_auc_is_symmetric() -> None:
    import numpy as np

    rng = np.random.default_rng(3)
    y = pd.Series(rng.integers(0, 2, size=500))
    x = pd.Series(rng.normal(size=500) + y * 0.8)
    assert v.single_feature_auc(x, y) == pytest.approx(v.single_feature_auc(-x, y))


@pytest.mark.parametrize(
    ("x", "y"),
    [
        (list(range(200)), [1] * 200),
        (list(range(10)), [0, 1] * 5),
        ([7] * 200, [0, 1] * 100),
    ],
)
def test_single_feature_auc_returns_none_on_degenerate_input(x: list[int], y: list[int]) -> None:
    assert v.single_feature_auc(pd.Series(x), pd.Series(y)) is None


def test_leakage_sample_is_random_seeded_and_bounded() -> None:
    frame = pd.DataFrame({"a": range(1000)})
    params = v.CheckParams(sample_rows=100, seed=5)
    first_draw = v.leakage_sample(frame, params)
    assert len(first_draw) == 100
    assert first_draw.index.equals(v.leakage_sample(frame, params).index)
    assert not first_draw.index.equals(v.leakage_sample(frame, dataclasses.replace(params, seed=6)).index)
    assert v.leakage_sample(frame, v.CheckParams()) is frame


def test_sampling_is_deterministic_and_order_insensitive() -> None:
    """A frame sorted by the outcome gives the same verdict as its shuffled copy."""
    use_case_id = USE_CASE_IDS[0]
    target = target_of(config_for(use_case_id))
    frame = fixture_frame(use_case_id, "leaky_column")
    params = params_for(use_case_id, sample_rows=400, leakage_baseline=False)

    one = v.check_leakage_suspected(frame, params).findings[0].details["auc"]
    two = v.check_leakage_suspected(frame.copy(), params).findings[0].details["auc"]
    assert one == two

    ordered = frame.sort_values(target).reset_index(drop=True)
    shuffled = frame.sample(frac=1.0, random_state=99).reset_index(drop=True)
    assert v.check_leakage_suspected(ordered, params).findings[0].column == LEAKY_COLUMN
    assert v.check_leakage_suspected(shuffled, params).findings[0].column == LEAKY_COLUMN


# ---------------------------------------------------------------------------
# Leakage: the baseline branch (M2_DESIGN section 2.25.1, DEC-066)
# ---------------------------------------------------------------------------
def _distributed_leakage_frame(rows: int = 3000) -> pd.DataFrame:
    """Neither `u` nor `vv` gives the answer away; `u + vv` reproduces it exactly."""
    import numpy as np

    rng = np.random.default_rng(17)
    u = rng.normal(size=rows)
    vv = rng.normal(size=rows)
    return pd.DataFrame(
        {
            "k": [f"K-{i:06d}" for i in range(rows)],
            "u": u,
            "vv": vv,
            "noise": rng.normal(size=rows),
            "y": (u + vv > 0).astype(int),
        }
    )


def test_baseline_fires_on_distributed_leakage() -> None:
    frame = _distributed_leakage_frame()
    params = v.CheckParams(primary_key="k", target="y", seed=1)
    findings = v.check_leakage_suspected(frame, params).findings
    assert len(findings) == 1
    check = findings[0]
    assert check.column is None
    assert check.severity is Severity.WARNING
    assert check.details["reason"] == "baseline"
    assert float(check.details["auc"]) > 0.98
    assert check.details["model"] == "logistic_regression"
    assert check.details["folds"] == v.BASELINE_FOLDS
    assert check.details["acknowledge"] == "LEAKAGE_SUSPECTED"
    assert {entry["column"] for entry in check.details["top_features"]} <= {"u", "vv", "noise"}
    assert check.details["features_used"] == 3


def test_baseline_is_suppressed_when_a_column_is_flagged() -> None:
    report = fixture_report(USE_CASE_IDS[0], "leaky_column")
    leakage = [check for check in report.checks if check.code == "LEAKAGE_SUSPECTED"]
    assert len(leakage) == 1
    assert leakage[0].details["reason"] == "auc"


def test_baseline_is_out_of_fold() -> None:
    """200 pure-noise columns against a random target must not fire; an in-sample fit would."""
    import numpy as np

    rng = np.random.default_rng(23)
    rows = 1500
    frame = pd.DataFrame({f"f{i:03d}": rng.normal(size=rows) for i in range(200)})
    frame["y"] = rng.integers(0, 2, size=rows)
    facts = v.derive_facts(frame)
    params = v.CheckParams(target="y", seed=2, facts=facts)
    auc, top = v.baseline_auc(frame, params, facts, v.candidate_columns(frame, params, facts))
    assert auc is not None
    assert auc < 0.70
    assert len(top) == 200
    assert v.check_leakage_suspected(frame, params).findings == ()


def test_baseline_never_sees_exempt_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, ...]] = []

    def spy(
        frame: pd.DataFrame,
        params: v.CheckParams,
        facts: v.FrameFacts,
        columns: list[str],
    ) -> tuple[float | None, tuple[tuple[str, float], ...]]:
        seen.append(tuple(columns))
        return (None, ())

    monkeypatch.setattr(v, "baseline_auc", spy)
    use_case_id = "rca"
    config = config_for(use_case_id)
    v.check_leakage_suspected(fixture_frame(use_case_id), params_for(use_case_id))
    assert seen, "the baseline branch did not run"
    columns = set(seen[0])
    assert target_of(config) not in columns
    assert primary_key_of(config) not in columns
    assert config.actions.suppression.opt_out_column not in columns
    assert config.actions.suppression.recently_contacted_column not in columns
    assert config.split.time_column not in columns


@pytest.mark.parametrize(
    "frame",
    [
        pd.DataFrame({"a": range(20), "y": [0, 1] * 10}),
        pd.DataFrame({"a": range(200), "y": [1] * 200}),
        pd.DataFrame({"a": range(200), "y": ([1] * 5) + ([0] * 195)}),
        pd.DataFrame({"a": [None] * 200, "y": [0, 1] * 100}),
    ],
    ids=["too-few-rows", "one-class", "tiny-minority", "no-usable-feature"],
)
def test_baseline_skips_small_or_degenerate_frames(frame: pd.DataFrame) -> None:
    facts = v.derive_facts(frame)
    params = v.CheckParams(target="y", facts=facts)
    auc, top = v.baseline_auc(frame, params, facts, v.candidate_columns(frame, params, facts))
    assert auc is None
    assert top == ()


def test_leakage_baseline_switch_disables_only_that_branch() -> None:
    frame = _distributed_leakage_frame()
    on = v.CheckParams(primary_key="k", target="y", seed=1)
    off = dataclasses.replace(on, leakage_baseline=False)
    assert len(v.check_leakage_suspected(frame, on).findings) == 1
    assert v.check_leakage_suspected(frame, off).findings == ()
    all_off = dataclasses.replace(on, leakage_check=False)
    assert v.check_leakage_suspected(frame, all_off).skipped is True


def test_baseline_is_deterministic() -> None:
    frame = _distributed_leakage_frame()
    params = v.CheckParams(primary_key="k", target="y", seed=1)
    one = v.check_leakage_suspected(frame, params).findings[0].details
    two = v.check_leakage_suspected(frame.copy(), params).findings[0].details
    assert one == two


# ---------------------------------------------------------------------------
# Acknowledgement (M2_DESIGN section 2.4, DEC-055)
# ---------------------------------------------------------------------------
def test_acknowledgement_downgrades_without_mutating_severity() -> None:
    use_case_id = USE_CASE_IDS[0]
    blocked = fixture_report(use_case_id, "leaky_column")
    assert blocked.passed is False
    assert blocked.error_count == 1

    cleared = fixture_report(use_case_id, "leaky_column", acknowledged=(f"LEAKAGE_SUSPECTED:{LEAKY_COLUMN}",))
    check = first(cleared, "LEAKAGE_SUSPECTED")
    assert check.severity is Severity.ERROR
    assert check.acknowledged is True
    assert cleared.error_count == 0
    assert cleared.passed is True
    assert [c.code for c in cleared.checks] == [c.code for c in blocked.checks]


def test_bare_code_acknowledges_every_column() -> None:
    cleared = fixture_report(USE_CASE_IDS[0], "leaky_column", acknowledged=("LEAKAGE_SUSPECTED",))
    assert first(cleared, "LEAKAGE_SUSPECTED").acknowledged is True
    assert cleared.passed is True


def test_acknowledging_a_different_column_does_nothing() -> None:
    still = fixture_report(
        USE_CASE_IDS[0], "leaky_column", acknowledged=("LEAKAGE_SUSPECTED:some_other_column",)
    )
    assert first(still, "LEAKAGE_SUSPECTED").acknowledged is False
    assert still.passed is False


def test_acknowledgement_cannot_clear_a_hard_error() -> None:
    report = fixture_report(USE_CASE_IDS[0], "duplicate_keys", acknowledged=("PK_NOT_UNIQUE",))
    check = first(report, "PK_NOT_UNIQUE")
    assert check.acknowledgeable is False
    assert check.acknowledged is False
    assert report.error_count == 1
    assert report.passed is False


def test_baseline_acknowledgement_takes_the_bare_code_only() -> None:
    frame = _distributed_leakage_frame()
    params = v.CheckParams(primary_key="k", target="y", seed=1)
    bare = v.validate_frame(
        frame, params, mode=RunMode.TRAIN, upload_id=UPLOAD_ID, acknowledged=("LEAKAGE_SUSPECTED",)
    )
    assert first(bare, "LEAKAGE_SUSPECTED").acknowledged is True
    named = v.validate_frame(
        frame, params, mode=RunMode.TRAIN, upload_id=UPLOAD_ID, acknowledged=("LEAKAGE_SUSPECTED:u",)
    )
    assert first(named, "LEAKAGE_SUSPECTED").acknowledged is False


def test_warning_counts_include_acknowledged_warnings() -> None:
    report = fixture_report(
        USE_CASE_IDS[0], "constant_column", acknowledged=(f"CONSTANT_COLUMN:{CONSTANT_COLUMN}",)
    )
    assert first(report, "CONSTANT_COLUMN").acknowledged is True
    assert report.warning_count == 1
    assert report.error_count == 0
    assert report.passed is True


# ---------------------------------------------------------------------------
# Messages and details (plan section 6.3, section 13.4)
# ---------------------------------------------------------------------------
def test_pk_not_unique_message_uses_the_configured_entity() -> None:
    customer = first(fixture_report("targeted-advertisement", "duplicate_keys"), "PK_NOT_UNIQUE")
    assert customer.message == (
        "This file has 1.0 rows per customer on average. The model needs one row per customer."
    )
    account = first(fixture_report("payment-propensity", "duplicate_keys"), "PK_NOT_UNIQUE")
    assert "per account on average" in account.message
    assert "customer" not in account.message


def test_target_too_few_positives_message_is_the_plan_pattern() -> None:
    check = first(fixture_report(USE_CASE_IDS[0], "too_few_positives"), "TARGET_TOO_FEW_POSITIVES")
    assert check.message == ("Only 150 positive examples. At least 200 are needed for a reliable model.")
    assert check.details["positive_count"] == 150
    assert check.details["min_positive"] == 200


def test_rows_too_few_message() -> None:
    check = first(fixture_report(USE_CASE_IDS[0], "too_few_rows"), "ROWS_TOO_FEW")
    assert check.message == (
        "This file has 900 rows. At least 1,000 are needed to train a model that generalises."
    )
    assert check.details == {"rows": 900, "min_rows": 1000}


def test_high_null_column_message_and_details() -> None:
    check = first(fixture_report(USE_CASE_IDS[0], "high_null_column"), "HIGH_NULL_COLUMN")
    assert check.column == HIGH_NULL_COLUMN
    assert check.message.startswith(f"'{HIGH_NULL_COLUMN}' is empty in 8")
    assert check.details["will_be_dropped"] is True
    assert check.details["threshold"] == 0.60
    assert 0.6 < float(check.details["null_rate"]) < 1.0


def test_high_cardinality_id_like_skips_the_chosen_key() -> None:
    report = fixture_report(USE_CASE_IDS[0], "id_like_column")
    flagged = [c.column for c in report.checks if c.code == "HIGH_CARDINALITY_ID_LIKE"]
    assert flagged == [ID_LIKE_COLUMN]
    assert primary_key_of(config_for(USE_CASE_IDS[0])) not in flagged


def test_pii_details_never_carry_a_value() -> None:
    report = fixture_report(USE_CASE_IDS[0], "pii_column")
    checks = [c for c in report.checks if c.code == "PII_DETECTED"]
    assert PII_EMAIL_COLUMN in {c.column for c in checks}
    frame = fixture_frame(USE_CASE_IDS[0], "pii_column")
    values = {str(value) for value in frame[PII_EMAIL_COLUMN].head(50)}
    for check in checks:
        assert set(check.details) == {"pii_kinds", "handling"}
        blob = f"{check.message} {check.suggestion} {check.details}"
        assert not any(value in blob for value in values)
    email = next(c for c in checks if c.column == PII_EMAIL_COLUMN)
    assert email.details["pii_kinds"] == ["email"]
    assert email.details["handling"] == PiiHandling.REDACT.value
    assert email.message == f"'{PII_EMAIL_COLUMN}' looks like it contains email addresses."


def test_target_not_binary_offers_regression_for_a_numeric_target() -> None:
    check = first(fixture_report(USE_CASE_IDS[0], "non_binary_target"), "TARGET_NOT_BINARY")
    assert check.details["numeric"] is True
    assert check.details["switch_to"] == "regression"
    assert check.details["override_path"] == "problem_type"
    assert "Regression" in check.suggestion
    assert check.message.startswith(f"'{target_of(config_for(USE_CASE_IDS[0]))}' has 3 different values.")


def test_target_constant_suppresses_the_other_target_checks() -> None:
    report = fixture_report(USE_CASE_IDS[0], "constant_target")
    assert codes(report) == ["TARGET_CONSTANT"]
    check = first(report, "TARGET_CONSTANT")
    assert check.details["distinct_count"] == 1
    assert check.details["value"] == "1"


def test_time_column_unparseable_reports_the_shape() -> None:
    use_case_id = USE_CASE_IDS[0]
    report = fixture_report(
        use_case_id,
        "unparseable_time",
        overrides={"split.time_column": time_column_of(config_for(use_case_id))},
    )
    check = first(report, "TIME_COLUMN_UNPARSEABLE")
    assert check.column == time_column_of(config_for(use_case_id))
    assert check.details["inferred_type"] == ColumnType.STRING.value
    assert float(check.details["parse_rate"]) < v.DATETIME_PARSE_RATE
    assert "could not be read as a date" in check.suggestion


def test_suppression_column_missing_names_the_role() -> None:
    frame = fixture_frame(USE_CASE_IDS[0])
    params = params_for(
        USE_CASE_IDS[0], opt_out_column="do_not_contact", recently_contacted_column="last_touch"
    )
    findings = v.check_suppression_column_missing(frame, params).findings
    assert [check.details["role"] for check in findings] == ["opt_out", "recently_contacted"]
    assert all(check.column is None for check in findings)
    assert all(check.acknowledgeable for check in findings)


def test_every_message_is_non_empty_and_every_code_is_known() -> None:
    reports = [
        fixture_report(uid, variant)
        for uid in USE_CASE_IDS[:2]
        for variant in BROKEN_VARIANTS
        if variant not in SCHEMA_VARIANTS
    ]
    reports += [schema_report(USE_CASE_IDS[0], variant) for variant in SCHEMA_VARIANTS]
    reports += list(_config_driven_reports(USE_CASE_IDS[0]).values())
    for report in reports:
        for check in report.checks:
            assert check.code in VALIDATION_CODES
            assert check.message.strip()
            assert check.suggestion.strip()


def test_every_suggested_override_path_is_overridable() -> None:
    """Following a suggestion must not then produce a 422 OVERRIDE_UNKNOWN_PATH."""
    use_case_id = USE_CASE_IDS[0]
    allowed = overridable_paths(config_for(use_case_id))
    reports = [
        fixture_report(use_case_id, variant) for variant in BROKEN_VARIANTS if variant not in SCHEMA_VARIANTS
    ]
    reports += [schema_report(use_case_id, variant) for variant in SCHEMA_VARIANTS]
    reports += list(_config_driven_reports(use_case_id).values())
    seen: set[str] = set()
    for report in reports:
        for check in report.checks:
            path = check.details.get("override_path")
            if path is None:
                continue
            assert path in allowed, f"{check.code} suggests {path!r}, which is not overridable"
            seen.add(str(path))
    assert seen >= {
        "problem_type",
        "validation.min_positive",
        "prepare.exclude_columns",
        "split.time_column",
        "governance.consent_column",
        "actions.suppression.suppress_opted_out",
    }


# ---------------------------------------------------------------------------
# validate_against_schema (M2_DESIGN section 2.28, DEC-057)
# ---------------------------------------------------------------------------
def _first_feature(use_case_id: str) -> str:
    return config_for(use_case_id).template.by_role(ColumnRole.FEATURE)[0].name


def test_scoring_file_matches_its_schema() -> None:
    report = schema_report(USE_CASE_IDS[0], SCORING)
    assert report.passed is True
    assert "SCHEMA_MISMATCH" not in codes(report)


def test_schema_target_is_never_reported_missing() -> None:
    """A scoring file has no outcome by definition."""
    report = schema_report(USE_CASE_IDS[0], SCORING)
    assert target_of(config_for(USE_CASE_IDS[0])) not in str(report.model_dump())


def test_missing_column_is_reported_by_name() -> None:
    use_case_id = USE_CASE_IDS[0]
    check = first(schema_report(use_case_id, "missing_column"), "SCHEMA_MISMATCH")
    assert check.severity is Severity.ERROR
    assert check.details["missing"] == [_first_feature(use_case_id)]
    assert check.details["extra"] == []
    assert check.details["type_changed"] == []
    assert f"Missing: {_first_feature(use_case_id)}." in check.message
    assert check.acknowledgeable is False


def test_renamed_column_is_both_missing_and_extra() -> None:
    use_case_id = USE_CASE_IDS[0]
    name = _first_feature(use_case_id)
    check = first(schema_report(use_case_id, "renamed_column"), "SCHEMA_MISMATCH")
    assert check.severity is Severity.ERROR
    assert check.details["missing"] == [name]
    assert check.details["extra"] == [f"{name}_v2"]


def test_type_changed_column_is_reported_by_name() -> None:
    use_case_id = USE_CASE_IDS[0]
    check = first(schema_report(use_case_id, "type_changed_column"), "SCHEMA_MISMATCH")
    assert check.severity is Severity.ERROR
    assert check.details["missing"] == []
    changed = check.details["type_changed"]
    assert len(changed) == 1
    assert changed[0]["actual"] == ColumnType.STRING.value
    assert "Changed type:" in check.message
    assert changed[0]["name"] in check.message


def test_extra_columns_alone_are_a_warning() -> None:
    use_case_id = USE_CASE_IDS[0]
    config = config_for(use_case_id)
    frame = fixture_frame(use_case_id, SCORING)
    frame["bonus_metric"] = 1.5
    report = v.validate_against_schema(
        frame,
        schema_for(use_case_id),
        primary_key=primary_key_of(config),
        config=config,
        upload_id=UPLOAD_ID,
        now=FIXED_NOW,
    )
    check = first(report, "SCHEMA_MISMATCH")
    assert check.severity is Severity.WARNING
    assert check.acknowledgeable is True
    assert check.details["extra"] == ["bonus_metric"]
    assert check.message == "This file has 1 column(s) the model was not trained on: bonus_metric."
    assert report.passed is True


@pytest.mark.parametrize(
    ("expected", "actual", "compatible"),
    [
        (ColumnType.INTEGER, ColumnType.FLOAT, True),
        (ColumnType.FLOAT, ColumnType.BOOLEAN, True),
        (ColumnType.BOOLEAN, ColumnType.INTEGER, True),
        (ColumnType.DATE, ColumnType.DATETIME, True),
        (ColumnType.STRING, ColumnType.TEXT, True),
        (ColumnType.STRING, ColumnType.STRING, True),
        (ColumnType.STRING, ColumnType.INTEGER, False),
        (ColumnType.DATE, ColumnType.STRING, False),
        (ColumnType.INTEGER, ColumnType.DATETIME, False),
    ],
)
def test_types_compatible(expected: ColumnType, actual: ColumnType, compatible: bool) -> None:
    assert v.types_compatible(expected, actual) is compatible


def test_score_mode_runs_the_pk_checks_but_no_training_checks() -> None:
    use_case_id = USE_CASE_IDS[0]
    config = config_for(use_case_id)
    frame = fixture_frame(use_case_id, SCORING).head(12).copy()
    frame.loc[frame.index[:3], primary_key_of(config)] = frame.at[frame.index[0], primary_key_of(config)]
    report = v.validate_against_schema(
        frame,
        schema_for(use_case_id),
        primary_key=primary_key_of(config),
        config=config,
        upload_id=UPLOAD_ID,
        now=FIXED_NOW,
    )
    assert "PK_NOT_UNIQUE" in codes(report)
    assert "ROWS_TOO_FEW" not in codes(report)
    assert not [code for code in codes(report) if code.startswith("TARGET_")]
    assert "LEAKAGE_SUSPECTED" not in codes(report)


def test_score_mode_without_a_config_skips_the_governed_checks() -> None:
    use_case_id = USE_CASE_IDS[0]
    frame = fixture_frame(use_case_id, SCORING)
    frame = frame.drop(columns=[config_for(use_case_id).actions.suppression.opt_out_column])
    schema = schema_for(use_case_id)
    without = v.validate_against_schema(
        frame,
        schema,
        primary_key=primary_key_of(config_for(use_case_id)),
        config=None,
        upload_id=UPLOAD_ID,
        now=FIXED_NOW,
    )
    assert "SUPPRESSION_COLUMN_MISSING" not in codes(without)
    with_config = v.validate_against_schema(
        frame,
        schema,
        primary_key=primary_key_of(config_for(use_case_id)),
        config=config_for(use_case_id),
        upload_id=UPLOAD_ID,
        now=FIXED_NOW,
    )
    assert "SUPPRESSION_COLUMN_MISSING" in codes(with_config)


# ---------------------------------------------------------------------------
# Ordering, arithmetic and robustness (M2_DESIGN sections 2.3, 0.4)
# ---------------------------------------------------------------------------
def _multi_problem_frame(use_case_id: str) -> tuple[pd.DataFrame, v.CheckParams]:
    """A frame that trips several codes at once."""
    config = config_for(use_case_id)
    frame = fixture_frame(use_case_id, "leaky_column").head(500).copy()
    key = primary_key_of(config)
    frame.loc[frame.index[:5], key] = frame.at[frame.index[0], key]
    frame.loc[frame.index[5:10], key] = None
    frame["zzz_constant"] = "one"
    frame["aaa_constant"] = "one"
    frame["mostly_null"] = [None] * 480 + [1] * 20
    params = params_for(use_case_id, row_count=500)
    return frame, params


def test_errors_then_warnings_then_table_order_then_column_position() -> None:
    frame, params = _multi_problem_frame(USE_CASE_IDS[0])
    checks = v.run_checks(frame, params, mode=RunMode.TRAIN)
    assert len(checks) >= 5
    positions = {name: index for index, name in enumerate(frame.columns)}
    keys = [
        (
            v.SEVERITY_RANK[check.severity],
            v.CHECK_ORDER.index(check.code),
            -1 if check.column is None else positions.get(check.column, -1),
            check.column or "",
        )
        for check in checks
    ]
    assert keys == sorted(keys)
    severities = [v.SEVERITY_RANK[check.severity] for check in checks]
    assert severities == sorted(severities)


def test_all_checks_run_and_none_stops_another() -> None:
    frame, params = _multi_problem_frame(USE_CASE_IDS[0])
    emitted = {check.code for check in v.run_checks(frame, params, mode=RunMode.TRAIN)}
    assert {
        "PK_NOT_UNIQUE",
        "PK_NULLS",
        "ROWS_TOO_FEW",
        "LEAKAGE_SUSPECTED",
        "HIGH_NULL_COLUMN",
        "CONSTANT_COLUMN",
    } <= emitted


def test_report_arithmetic() -> None:
    frame, params = _multi_problem_frame(USE_CASE_IDS[0])
    report = v.validate_frame(frame, params, mode=RunMode.TRAIN, upload_id=UPLOAD_ID, now=FIXED_NOW)
    errors = [c for c in report.checks if c.severity is Severity.ERROR]
    warnings = [c for c in report.checks if c.severity is Severity.WARNING]
    assert report.error_count == len([c for c in errors if not c.acknowledged])
    assert report.warning_count == len(warnings)
    assert report.passed is (report.error_count == 0)
    assert report.mode is RunMode.TRAIN
    assert report.run_id is None
    assert report.validated_at == FIXED_NOW


def test_run_id_is_carried_when_given() -> None:
    report = fixture_report(USE_CASE_IDS[0])
    assert report.run_id is None
    with_run = v.validate_for_training(
        fixture_frame(USE_CASE_IDS[0]),
        config_for(USE_CASE_IDS[0]),
        primary_key=primary_key_of(config_for(USE_CASE_IDS[0])),
        target=target_of(config_for(USE_CASE_IDS[0])),
        upload_id=UPLOAD_ID,
        run_id="r_20260921_abcdef12",
        now=FIXED_NOW,
    )
    assert with_run.run_id == "r_20260921_abcdef12"


def test_a_check_that_raises_is_swallowed_and_logged_without_its_message(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(frame: pd.DataFrame, params: v.CheckParams) -> v.CheckResult:
        raise ZeroDivisionError("a secret cell value: C-10482")

    patched = tuple(
        dataclasses.replace(spec, fn=boom) if spec.code == "PK_NULLS" else spec for spec in v.CHECK_REGISTRY
    )
    monkeypatch.setattr(v, "CHECK_REGISTRY", patched)
    frame, params = _multi_problem_frame(USE_CASE_IDS[0])
    with caplog.at_level(logging.WARNING, logger=v.logger.name):
        checks = v.run_checks(frame, params, mode=RunMode.TRAIN)
    assert "PK_NULLS" not in {check.code for check in checks}
    assert "PK_NOT_UNIQUE" in {check.code for check in checks}
    failures = [record for record in caplog.records if "check_failed" in record.getMessage()]
    assert len(failures) == 1
    assert "ZeroDivisionError" in failures[0].getMessage()
    assert "C-10482" not in failures[0].getMessage()


def test_nothing_is_logged_but_counts(caplog: pytest.LogCaptureFixture) -> None:
    use_case_id = USE_CASE_IDS[0]
    frame = fixture_frame(use_case_id, "pii_column")
    values = [str(value) for value in frame[PII_EMAIL_COLUMN].head(20)]
    with caplog.at_level(logging.DEBUG):
        fixture_report(use_case_id, "pii_column")
    blob = " ".join(record.getMessage() for record in caplog.records)
    assert not any(value in blob for value in values)


@pytest.mark.parametrize("use_case_id", USE_CASE_IDS)
def test_reports_are_deterministic(use_case_id: str) -> None:
    """Two runs over the same frame produce byte-identical JSON."""
    one = fixture_report(use_case_id, "leaky_column").model_dump_json()
    two = fixture_report(use_case_id, "leaky_column").model_dump_json()
    assert one == two


def test_determinism_does_not_depend_on_the_frame_object() -> None:
    frame = fixture_frame(USE_CASE_IDS[0], "leaky_column")
    config = config_for(USE_CASE_IDS[0])
    kwargs = {
        "primary_key": primary_key_of(config),
        "target": target_of(config),
        "upload_id": UPLOAD_ID,
        "now": FIXED_NOW,
    }
    one = v.validate_for_training(frame, config, **kwargs)
    two = v.validate_for_training(frame.copy(deep=True), config, **kwargs)
    assert one.model_dump_json() == two.model_dump_json()


def test_seed_is_derived_from_the_upload_id() -> None:
    """The same upload gives the same verdict in every process."""
    from engine.utils.ids import seed_from

    config = config_for(USE_CASE_IDS[0])
    params = v.params_from_config(config, seed=seed_from(UPLOAD_ID))
    assert params.seed == seed_from(UPLOAD_ID)
    assert seed_from(UPLOAD_ID) == seed_from(UPLOAD_ID)


# ---------------------------------------------------------------------------
# params_from_config and the stage detail line
# ---------------------------------------------------------------------------
def test_params_from_config_copies_the_column_roles() -> None:
    config = config_for("rca")
    params = v.params_from_config(config, primary_key="customer_id", target="churn_next_60d")
    assert params.entity == config.entity
    assert params.time_column == config.split.time_column
    assert params.opt_out_column == config.actions.suppression.opt_out_column
    assert params.recently_contacted_column == config.actions.suppression.recently_contacted_column
    assert params.consent_column == config.governance.consent_column
    assert params.fairness_column == config.evaluation.fairness_column
    assert params.min_rows == config.validation.min_rows
    assert params.leakage_auc_threshold == config.validation.leakage_auc_threshold
    assert params.leakage_pattern == config.catalog.column_name_patterns.leakage
    assert params.time_like_pattern == config.catalog.column_name_patterns.time_like
    assert params.problem_type is config.problem_type
    assert params.split_type is config.split.type
    assert params.pii_handling is config.prepare.pii_handling


def test_params_from_config_collects_every_target_spelling() -> None:
    config = config_for("targeted-advertisement")
    params = v.params_from_config(config, target="some_other_column")
    assert params.target == "some_other_column"
    assert config.target.column in params.target_aliases
    assert params.target_definition == config.target.definition
    exempt = v.leakage_exempt_names(params)
    assert {"some_other_column", config.target.column} <= exempt


def test_check_params_defaults_are_usable_without_a_catalog() -> None:
    params = v.CheckParams()
    assert params.leakage_pattern == v.LEAKAGE_PATTERN
    assert params.time_like_pattern == v.TIME_LIKE_PATTERN
    assert params.id_like_pattern == v.ID_LIKE_PATTERN
    assert params.problem_type is ProblemType.BINARY_CLASSIFICATION
    assert params.split_type is SplitType.RANDOM_STRATIFIED
    assert params.sample_rows == v.LEAKAGE_SAMPLE_ROWS


@pytest.mark.parametrize(
    ("errors", "warnings", "expected"),
    [
        (0, 0, "No problems found"),
        (0, 2, "2 warnings"),
        (1, 2, "1 error · 2 warnings"),
        (3, 0, "3 errors"),
        (1, 1, "1 error · 1 warning"),
    ],
)
def test_validation_detail(errors: int, warnings: int, expected: str) -> None:
    report = ValidationReport(
        upload_id=UPLOAD_ID,
        mode=RunMode.TRAIN,
        checks=(),
        error_count=errors,
        warning_count=warnings,
        passed=errors == 0,
        validated_at=FIXED_NOW,
    )
    assert v.validation_detail(report) == expected


# ---------------------------------------------------------------------------
# The shared helpers (M2_DESIGN section 2.5)
# ---------------------------------------------------------------------------
def test_column_stats_prefers_the_exact_pass() -> None:
    @dataclasses.dataclass(frozen=True)
    class Stats:
        row_count: int
        null_count: int
        distinct_count: int
        is_unique: bool
        value_counts: dict[str, int]

    frame = pd.DataFrame({"k": ["a", "a", "b"]})
    sampled = v.CheckParams()
    assert v.column_stats(frame, sampled, "k") == (3, 0, 2, False)
    exact = v.CheckParams(exact={"k": Stats(9, 1, 8, True, {"a": 5, "b": 4})})
    assert v.column_stats(frame, exact, "k") == (9, 1, 8, True)
    assert v.value_counts_for(frame, exact, "k") == {"a": 5, "b": 4}


def test_value_counts_for_returns_none_above_the_cardinality_gate() -> None:
    frame = pd.DataFrame({"many": [f"v{i}" for i in range(40)]})
    assert v.value_counts_for(frame, v.CheckParams(), "many") is None
    assert v.value_counts_for(frame, v.CheckParams(), "absent") is None


def test_sample_values_redacts_a_pii_column() -> None:
    frame = pd.DataFrame({"contact_email": [f"person{i}@example.invalid" for i in range(8)]})
    facts = v.derive_facts(frame)
    assert facts.pii_kinds["contact_email"] == ("email",)
    assert v.sample_values(frame, "contact_email", facts) == (v.REDACTED,) * 5


def test_looks_like_id_needs_rows_uniqueness_and_a_type() -> None:
    rows = 50
    frame = pd.DataFrame(
        {
            "ref": [f"R-{i:04d}" for i in range(rows)],
            "flag": [i % 2 for i in range(rows)],
            "ratio": [i / 7 for i in range(rows)],
        }
    )
    facts = v.derive_facts(frame)
    assert v.looks_like_id(facts, "ref") is True
    assert v.looks_like_id(facts, "flag") is False
    assert v.looks_like_id(facts, "ratio") is False
    small = v.derive_facts(frame.head(10))
    assert v.looks_like_id(small, "ref") is False


def test_key_candidates_prefer_id_like_names_then_file_position() -> None:
    rows = 40
    frame = pd.DataFrame(
        {
            "serial": [f"S-{i}" for i in range(rows)],
            "account_id": [f"A-{i}" for i in range(rows)],
            "amount": [float(i) for i in range(rows)],
        }
    )
    facts = v.derive_facts(frame)
    assert v.key_candidates(facts, v.CheckParams())[0] == "account_id"


def test_time_candidates_need_a_name_and_a_parsing_type() -> None:
    frame = pd.DataFrame(
        {
            "snapshot_date": ["2026-01-01", "2026-01-02", "2026-01-03"],
            "update_day": ["n/a", "n/a", "n/a"],
            "amount": [1.0, 2.0, 3.0],
        }
    )
    facts = v.derive_facts(frame)
    assert v.time_candidates(facts, v.CheckParams()) == ("snapshot_date",)


def test_target_candidate_in_is_case_insensitive() -> None:
    frame = pd.DataFrame({"Converted_30d": [0, 1]})
    facts = v.derive_facts(frame)
    params = v.CheckParams(target_aliases=("converted_30d",))
    assert v.target_candidate_in(facts, params) == "Converted_30d"
    assert v.target_candidate_in(facts, v.CheckParams()) is None


@pytest.mark.parametrize(
    ("values", "positive_label", "expected"),
    [
        (["yes", "no", "no", "no"], None, "yes"),
        (["1", "0", "0", "0"], None, "1"),
        (["active", "churned", "active", "active"], None, "churned"),
        (["a", "b", "a", "b"], None, "b"),
        (["a", "a", "a", "b"], None, "b"),
        (["a", "b", "b", "b"], None, "a"),
        (["red", "blue", "blue", "blue"], "red", "red"),
    ],
)
def test_resolve_positive_label(values: list[str], positive_label: str | None, expected: str) -> None:
    frame = pd.DataFrame({"y": values})
    params = v.CheckParams(target="y", positive_label=positive_label)
    label, positives, negatives = v.resolve_positive_label(frame, params)
    assert label == expected
    assert positives + negatives == len(values)
    assert positives == values.count(expected)


@pytest.mark.parametrize(
    "values",
    [["a", "a", "a"], ["a", "b", "c"], [None, None, None]],
    ids=["constant", "three-valued", "all-null"],
)
def test_resolve_positive_label_gives_up_on_a_non_binary_target(values: list[str | None]) -> None:
    frame = pd.DataFrame({"y": values})
    assert v.resolve_positive_label(frame, v.CheckParams(target="y")) == (None, 0, 0)
    assert v.resolve_positive_label(frame, v.CheckParams()) == (None, 0, 0)


def test_positive_tokens_are_lower_case_and_unique() -> None:
    assert list(v.POSITIVE_TOKENS) == [token.lower() for token in v.POSITIVE_TOKENS]
    assert len(set(v.POSITIVE_TOKENS)) == len(v.POSITIVE_TOKENS)
