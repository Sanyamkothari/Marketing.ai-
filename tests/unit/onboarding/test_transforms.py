"""M9 mapping transforms: every `TransformKind`, `cast_series`, `apply_dedupe` and the `derive`
expression whitelist.

Two things this suite has to prove that a plain example-based test cannot: that the `derive`
whitelist rejects every disallowed construct by a coded error rather than by crashing or, worse, by
quietly running it - and that a transform which should be idempotent (running it twice is the same
as running it once) actually is, over inputs nobody hand-picked. Those live in the hypothesis
sections at the bottom.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engine.onboarding.specs import ColumnTransform, StandardType, TransformKind
from engine.onboarding.transforms import (
    TransformError,
    TransformResult,
    apply_dedupe,
    apply_transform,
    cast_series,
    derive,
)


def _values(result: TransformResult) -> list[object]:
    """A plain Python list of a `TransformResult`'s values, `NaN`/`NaT` normalised to `None`."""
    return [None if pd.isna(v) else v for v in result.values]


# ---------------------------------------------------------------------------
# cast_series
# ---------------------------------------------------------------------------
def test_cast_numeric_counts_failures_over_non_null_inputs_only() -> None:
    series = pd.Series(["10", "20", "not-a-number", None, "40"])
    result = cast_series(series, StandardType.NUMERIC)
    assert _values(result) == [10.0, 20.0, None, None, 40.0]
    assert result.total == 4  # the pre-existing null is excluded
    assert result.failed == 1  # only "not-a-number" is a genuine cast failure


def test_cast_boolean_reads_y_n() -> None:
    series = pd.Series(["Y", "N", "y", "n", None])
    result = cast_series(series, StandardType.BOOLEAN)
    assert _values(result) == [True, False, True, False, None]
    assert result.failed == 0
    assert result.total == 4


def test_cast_boolean_unparseable_token_counts_as_failed() -> None:
    series = pd.Series(["Y", "maybe"])
    result = cast_series(series, StandardType.BOOLEAN)
    assert _values(result) == [True, None]
    assert result.failed == 1
    assert result.total == 2


def test_cast_date_ddmmyyyy_with_explicit_format() -> None:
    series = pd.Series(["25/12/2024", "01/02/2024"])
    result = cast_series(series, StandardType.DATE, date_format="%d/%m/%Y")
    parsed = list(result.values)
    assert parsed[0] == pd.Timestamp("2024-12-25")
    assert parsed[1] == pd.Timestamp("2024-02-01")  # day-first: 1 Feb, not 2 Jan
    assert result.failed == 0


def test_cast_date_mmddyyyy_with_explicit_format() -> None:
    series = pd.Series(["12/25/2024", "02/01/2024"])
    result = cast_series(series, StandardType.DATE, date_format="%m/%d/%Y")
    parsed = list(result.values)
    assert parsed[0] == pd.Timestamp("2024-12-25")
    assert parsed[1] == pd.Timestamp("2024-02-01")  # month-first: Feb 1, not Jan 2
    assert result.failed == 0


def test_cast_date_wrong_explicit_format_fails_rather_than_guessing() -> None:
    series = pd.Series(["25/12/2024"])
    result = cast_series(series, StandardType.DATE, date_format="%m/%d/%Y")
    assert result.failed == 1  # 25 is not a valid month; the engine never guesses (nothing fabricated)


def test_cast_categorical_and_text_stringify_without_losing_nulls() -> None:
    series = pd.Series([1, 2, None], dtype=object)
    result = cast_series(series, StandardType.CATEGORICAL)
    assert _values(result) == ["1", "2", None]
    result_text = cast_series(series, StandardType.TEXT)
    assert _values(result_text) == ["1", "2", None]


# ---------------------------------------------------------------------------
# apply_transform: cast, value_map, negate, scale, strip, lower, lstrip_zeros
# ---------------------------------------------------------------------------
def test_apply_transform_cast_routes_to_cast_series() -> None:
    transform = ColumnTransform(kind=TransformKind.CAST, to_type=StandardType.NUMERIC)
    result = apply_transform(pd.Series(["1", "2"]), transform, column="amount")
    assert _values(result) == [1.0, 2.0]


def test_apply_transform_negate_inverts_a_do_not_disturb_flag_into_opt_in() -> None:
    """Plan section 6.1's own example: DND_FLAG (Y means "do not market to me") becomes
    marketing_opt_in (True means "may market to me") in a single transform."""
    dnd_flag = pd.Series(["Y", "N", "Y"])
    transform = ColumnTransform(kind=TransformKind.NEGATE)
    result = apply_transform(dnd_flag, transform, column="DND_FLAG")
    assert _values(result) == [False, True, False]


def test_apply_transform_scale_converts_paise_to_rupees() -> None:
    transform = ColumnTransform(kind=TransformKind.SCALE, factor=0.01)
    result = apply_transform(pd.Series([10050, 500, None]), transform, column="amount_paise")
    assert _values(result) == [100.5, 5.0, None]


def test_apply_transform_strip_and_lower() -> None:
    series = pd.Series([" Delhi ", "MUMBAI ", None])
    stripped = apply_transform(series, ColumnTransform(kind=TransformKind.STRIP), column="city")
    assert _values(stripped) == ["Delhi", "MUMBAI", None]
    lowered = apply_transform(series, ColumnTransform(kind=TransformKind.LOWER), column="city")
    assert _values(lowered) == [" delhi ", "mumbai ", None]


def test_apply_transform_lstrip_zeros_normalises_a_leading_zero_key() -> None:
    series = pd.Series(["00042", "7", "000", None])
    result = apply_transform(series, ColumnTransform(kind=TransformKind.LSTRIP_ZEROS), column="account_no")
    assert _values(result) == ["42", "7", "0", None]


def test_apply_transform_value_map_keep_policy_passes_unmapped_values_through() -> None:
    transform = ColumnTransform(
        kind=TransformKind.VALUE_MAP, value_map={"M": "male", "F": "female"}, unmapped="keep"
    )
    result = apply_transform(pd.Series(["M", "F", "U", None]), transform, column="gender")
    assert _values(result) == ["male", "female", "U", None]
    assert result.unmapped == ("U",)
    assert result.failed == 0  # kept, not lost


def test_apply_transform_value_map_null_policy_drops_unmapped_values() -> None:
    transform = ColumnTransform(
        kind=TransformKind.VALUE_MAP, value_map={"M": "male", "F": "female"}, unmapped="null"
    )
    result = apply_transform(pd.Series(["M", "F", "U", None]), transform, column="gender")
    assert _values(result) == ["male", "female", None, None]
    assert result.unmapped == ("U",)
    assert result.failed == 1  # only the "null"-policy drop counts


def test_apply_transform_value_map_explicit_null_is_not_a_failure() -> None:
    """A value the client's map deliberately sends to null is a decision, not a cast failure."""
    transform = ColumnTransform(
        kind=TransformKind.VALUE_MAP, value_map={"UNKNOWN": None, "M": "male"}, unmapped="keep"
    )
    result = apply_transform(pd.Series(["M", "UNKNOWN"]), transform, column="gender")
    assert _values(result) == ["male", None]
    assert result.unmapped == ()
    assert result.failed == 0


def test_apply_transform_refuses_dedupe_and_derive() -> None:
    dedupe_transform = ColumnTransform(kind=TransformKind.DEDUPE, by="event_time")
    with pytest.raises(TransformError) as excinfo:
        apply_transform(pd.Series([1, 2]), dedupe_transform, column="entity_key")
    assert excinfo.value.code == "TRANSFORM_NOT_PER_COLUMN"

    derive_transform = ColumnTransform(kind=TransformKind.DERIVE, expression="1 + 1")
    with pytest.raises(TransformError) as excinfo:
        apply_transform(pd.Series([1, 2]), derive_transform, column="tenure_months")
    assert excinfo.value.code == "TRANSFORM_NOT_PER_COLUMN"


# ---------------------------------------------------------------------------
# apply_dedupe
# ---------------------------------------------------------------------------
def test_apply_dedupe_keeps_the_latest_row_per_entity() -> None:
    frame = pd.DataFrame(
        {
            "entity_key": ["A", "A", "B", "A"],
            "event_time": ["2024-01-01", "2024-03-01", "2024-02-01", "2024-02-15"],
            "amount": [1, 2, 3, 4],
        }
    )
    deduped, removed = apply_dedupe(frame, key="entity_key", by="event_time")
    assert removed == 2
    assert sorted(deduped["entity_key"]) == ["A", "B"]
    kept_a = deduped.loc[deduped["entity_key"] == "A", "amount"].iloc[0]
    assert kept_a == 2  # 2024-03-01 is the latest of A's three rows


def test_apply_dedupe_missing_columns_raise_coded_errors() -> None:
    frame = pd.DataFrame({"entity_key": ["A"], "event_time": ["2024-01-01"]})
    with pytest.raises(TransformError) as excinfo:
        apply_dedupe(frame, key="missing_key", by="event_time")
    assert excinfo.value.code == "DEDUPE_KEY_MISSING"

    with pytest.raises(TransformError) as excinfo:
        apply_dedupe(frame, key="entity_key", by="missing_time")
    assert excinfo.value.code == "DEDUPE_ORDER_COLUMN_MISSING"


# ---------------------------------------------------------------------------
# derive
# ---------------------------------------------------------------------------
@pytest.fixture
def sample_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "entity_key": ["A", "B", "C"],
            "snapshot_at": pd.to_datetime(["2024-06-15", "2024-06-15", "2024-06-15"]),
            "signup_date": pd.to_datetime(["2023-01-10", "2024-05-01", None]),
            "revenue": [100.0, 250.0, None],
            "backup_revenue": [10.0, 20.0, 30.0],
            "name": ["Alice", "BOB", None],
        }
    )


def test_derive_months_between_uses_the_snapshot_column_alias(sample_frame: pd.DataFrame) -> None:
    """The expression names 'snapshot_date', which is bound to the actual 'snapshot_at' column -
    the whole point of the `snapshot_column` parameter is that the expression never has to know
    what the client's own snapshot column is called."""
    result = derive(sample_frame, "months_between(snapshot_date, signup_date)", snapshot_column="snapshot_at")
    assert result.iloc[0] == 17.0  # 2024-06 vs 2023-01: (2024-2023)*12 + (6-1)
    assert result.iloc[1] == 1.0  # 2024-06 vs 2024-05
    assert pd.isna(result.iloc[2])  # signup_date is null for C


def test_derive_days_between(sample_frame: pd.DataFrame) -> None:
    result = derive(sample_frame, "days_between(snapshot_date, signup_date)", snapshot_column="snapshot_at")
    expected_a = (pd.Timestamp("2024-06-15") - pd.Timestamp("2023-01-10")).days
    assert result.iloc[0] == float(expected_a)
    assert pd.isna(result.iloc[2])


def test_derive_year_and_month(sample_frame: pd.DataFrame) -> None:
    year_result = derive(sample_frame, "year(signup_date)", snapshot_column="snapshot_at")
    month_result = derive(sample_frame, "month(signup_date)", snapshot_column="snapshot_at")
    assert year_result.iloc[0] == 2023.0
    assert month_result.iloc[0] == 1.0


def test_derive_coalesce_falls_back_to_the_second_argument(sample_frame: pd.DataFrame) -> None:
    result = derive(sample_frame, "coalesce(revenue, backup_revenue)", snapshot_column="snapshot_at")
    assert result.tolist() == [100.0, 250.0, 30.0]


def test_derive_lower_and_abs(sample_frame: pd.DataFrame) -> None:
    lowered = derive(sample_frame, "lower(name)", snapshot_column="snapshot_at")
    assert lowered.iloc[0] == "alice"
    assert lowered.iloc[1] == "bob"
    assert pd.isna(lowered.iloc[2])

    absolute = derive(sample_frame, "abs(-5)", snapshot_column="snapshot_at")
    assert absolute.tolist() == [5, 5, 5]


def test_derive_arithmetic_on_columns_and_literals(sample_frame: pd.DataFrame) -> None:
    result = derive(sample_frame, "revenue * 0.01 + 1", snapshot_column="snapshot_at")
    assert result.iloc[0] == pytest.approx(2.0)
    assert result.iloc[1] == pytest.approx(3.5)
    assert pd.isna(result.iloc[2])


def test_derive_unknown_column_raises_a_coded_error(sample_frame: pd.DataFrame) -> None:
    with pytest.raises(TransformError) as excinfo:
        derive(sample_frame, "revenue + nonexistent_column", snapshot_column="snapshot_at")
    assert excinfo.value.code == "DERIVE_UNKNOWN_NAME"


def test_derive_wrong_arity_raises_a_coded_error(sample_frame: pd.DataFrame) -> None:
    with pytest.raises(TransformError) as excinfo:
        derive(sample_frame, "year(signup_date, snapshot_date)", snapshot_column="snapshot_at")
    assert excinfo.value.code == "DERIVE_WRONG_ARITY"


def test_derive_missing_snapshot_column_raises(sample_frame: pd.DataFrame) -> None:
    with pytest.raises(TransformError) as excinfo:
        derive(sample_frame, "1 + 1", snapshot_column="not_a_column")
    assert excinfo.value.code == "DERIVE_SNAPSHOT_COLUMN_MISSING"


def test_derive_invalid_syntax_raises_a_coded_error(sample_frame: pd.DataFrame) -> None:
    with pytest.raises(TransformError) as excinfo:
        derive(sample_frame, "revenue +", snapshot_column="snapshot_at")
    assert excinfo.value.code == "DERIVE_INVALID_EXPRESSION"


REJECTED_DERIVE_EXPRESSIONS: tuple[str, ...] = (
    "__import__('os')",
    "os.system('ls')",
    "lambda x: x",
    "[x for x in range(3)]",
    "{x: x for x in range(3)}",
    "(x for x in range(3))",
    "revenue[0]",
    "sum(revenue)",  # a real builtin, but not on the whitelist
    "revenue.values",
    "open('/etc/passwd')",
)


@pytest.mark.parametrize("expression", REJECTED_DERIVE_EXPRESSIONS)
def test_derive_rejects_every_disallowed_construct(sample_frame: pd.DataFrame, expression: str) -> None:
    with pytest.raises(TransformError) as excinfo:
        derive(sample_frame, expression, snapshot_column="snapshot_at")
    assert excinfo.value.code.startswith("DERIVE_")


def test_derive_source_never_uses_eval_or_exec() -> None:
    """Belt and suspenders: the whitelist walk in `_eval_node` must be the only way an expression
    is evaluated - never Python's own dynamic-evaluation builtins over user text."""
    import engine.onboarding.transforms as transforms_module

    source = Path(transforms_module.__file__).read_text(encoding="utf-8")
    assert "eval(" not in source
    assert "exec(" not in source


# ---------------------------------------------------------------------------
# Property tests: apply-twice-equals-apply-once, where that should hold
# ---------------------------------------------------------------------------
_TEXTY = st.one_of(
    st.none(),
    st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=12),
    st.integers(min_value=-1000, max_value=1000),
)


def _series_from(values: list[object]) -> pd.Series:
    return pd.Series(values, dtype=object)


@settings(deadline=None, max_examples=50)
@given(values=st.lists(_TEXTY, min_size=0, max_size=15))
def test_strip_is_idempotent(values: list[object]) -> None:
    series = _series_from(values)
    once = apply_transform(series, ColumnTransform(kind=TransformKind.STRIP), column="c")
    twice = apply_transform(once.values, ColumnTransform(kind=TransformKind.STRIP), column="c")
    assert _values(once) == _values(twice)


@settings(deadline=None, max_examples=50)
@given(values=st.lists(_TEXTY, min_size=0, max_size=15))
def test_lower_is_idempotent(values: list[object]) -> None:
    series = _series_from(values)
    once = apply_transform(series, ColumnTransform(kind=TransformKind.LOWER), column="c")
    twice = apply_transform(once.values, ColumnTransform(kind=TransformKind.LOWER), column="c")
    assert _values(once) == _values(twice)


@settings(deadline=None, max_examples=50)
@given(values=st.lists(_TEXTY, min_size=0, max_size=15))
def test_lstrip_zeros_is_idempotent(values: list[object]) -> None:
    series = _series_from(values)
    transform = ColumnTransform(kind=TransformKind.LSTRIP_ZEROS)
    once = apply_transform(series, transform, column="c")
    twice = apply_transform(once.values, transform, column="c")
    assert _values(once) == _values(twice)


@settings(deadline=None, max_examples=50)
@given(values=st.lists(_TEXTY, min_size=0, max_size=15))
def test_cast_to_text_is_idempotent(values: list[object]) -> None:
    series = _series_from(values)
    once = cast_series(series, StandardType.TEXT)
    twice = cast_series(once.values, StandardType.TEXT)
    assert _values(once) == _values(twice)


@settings(deadline=None, max_examples=50)
@given(values=st.lists(st.sampled_from(["Y", "N", "y", "n", "yes", "no", "1", "0", None]), max_size=15))
def test_cast_to_boolean_is_idempotent_once_parsed(values: list[object]) -> None:
    """A boolean already cast is a Python bool or null; casting it again must be a no-op, which is
    what lets the mapping screen re-run a saved mapping's casts without drifting a second time."""
    series = _series_from(values)
    once = cast_series(series, StandardType.BOOLEAN)
    twice = cast_series(once.values, StandardType.BOOLEAN)
    assert _values(once) == _values(twice)
