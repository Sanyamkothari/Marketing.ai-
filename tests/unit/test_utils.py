"""Owner A's tests for `engine/utils/*` (design §11)."""

from __future__ import annotations

import ast
import inspect
import logging
import re
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from engine import __version__
from engine.utils.ids import new_model_id, new_run_id, new_upload_id, seed_from
from engine.utils.logging import configure_logging, get_logger, log_stage
from engine.utils.text import humanise_bytes, humanise_count, humanise_datetime
from engine.utils.time import parse_iso, to_iso, utc_now

RUN_ID_RE = re.compile(r"^r_\d{8}_[0-9a-f]{8}$")
UPLOAD_ID_RE = re.compile(r"^u_[0-9a-f]{12}$")


def test_version_is_the_m1_version() -> None:
    assert __version__ == "0.1.0"


# --------------------------------------------------------------------------------------------- ids


def test_new_run_id_has_the_documented_shape() -> None:
    run_id = new_run_id(datetime(2026, 9, 21, 11, 30, tzinfo=UTC))
    assert RUN_ID_RE.match(run_id), run_id
    assert run_id.startswith("r_20260921_")


def test_new_run_id_uses_the_clock_when_no_moment_is_given() -> None:
    run_id = new_run_id()
    assert RUN_ID_RE.match(run_id), run_id
    assert run_id[2:10] == utc_now().strftime("%Y%m%d")


def test_new_run_id_sorts_chronologically_as_a_plain_string() -> None:
    start = datetime(2026, 1, 30, tzinfo=UTC)
    moments = [start + timedelta(days=offset) for offset in (0, 1, 2, 40, 400)]
    ids = [new_run_id(moment) for moment in moments]
    assert ids == sorted(ids)


def test_new_run_id_is_unique() -> None:
    moment = datetime(2026, 9, 21, tzinfo=UTC)
    ids = {new_run_id(moment) for _ in range(2000)}
    assert len(ids) == 2000


def test_new_upload_id_has_the_documented_shape_and_is_unique() -> None:
    ids = {new_upload_id() for _ in range(1000)}
    assert len(ids) == 1000
    assert all(UPLOAD_ID_RE.match(upload_id) for upload_id in ids)


def test_new_model_id_joins_use_case_and_version() -> None:
    assert new_model_id("some-use-case", 1) == "m_some-use-case_1"
    assert new_model_id("some-use-case", 12) == "m_some-use-case_12"


def test_seed_from_is_deterministic_and_fits_in_32_bits() -> None:
    seed = seed_from("r_20260921_ab12cd34")
    assert seed == seed_from("r_20260921_ab12cd34")
    assert 0 <= seed < 2**32


def test_seed_from_differs_between_run_ids() -> None:
    seeds = {seed_from(f"r_20260921_{index:08x}") for index in range(500)}
    assert len(seeds) == 500


def test_seed_from_is_stable_across_processes(repo_root: Path) -> None:
    """`hash()` is salted per process; the seed must not be."""
    run_id = "r_20260921_ab12cd34"
    expected = seed_from(run_id)
    code = f"from engine.utils.ids import seed_from; print(seed_from({run_id!r}))"
    seen = {
        subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for _ in range(2)
    }
    assert seen == {str(expected)}


# -------------------------------------------------------------------------------------------- time


def test_utc_now_is_timezone_aware_and_utc() -> None:
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_to_iso_keeps_the_offset() -> None:
    moment = datetime(2026, 9, 14, 10, 20, 30, tzinfo=UTC)
    assert to_iso(moment) == "2026-09-14T10:20:30+00:00"


def test_to_iso_treats_a_naive_datetime_as_utc() -> None:
    assert to_iso(datetime(2026, 9, 14, 10, 20, 30)) == "2026-09-14T10:20:30+00:00"


def test_parse_iso_round_trips_to_iso() -> None:
    moment = datetime(2026, 9, 14, 10, 20, 30, 123456, tzinfo=UTC)
    assert parse_iso(to_iso(moment)) == moment


def test_parse_iso_accepts_z_and_other_offsets() -> None:
    assert parse_iso("2026-09-14T10:20:30Z") == datetime(2026, 9, 14, 10, 20, 30, tzinfo=UTC)
    ist = timezone(timedelta(hours=5, minutes=30))
    assert parse_iso("2026-09-14T15:50:30+05:30") == datetime(2026, 9, 14, 15, 50, 30, tzinfo=ist)


def test_parse_iso_treats_a_missing_offset_as_utc() -> None:
    parsed = parse_iso("2026-09-14T10:20:30")
    assert parsed.tzinfo is not None
    assert parsed == datetime(2026, 9, 14, 10, 20, 30, tzinfo=UTC)


# -------------------------------------------------------------------------------------------- text


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "0"),
        (1, "1"),
        (999, "999"),
        (1_000, "1K"),
        (1_500, "2K"),  # Math.round(1.5) = 2, not Python's round-half-even 2
        (2_500, "3K"),  # Math.round(2.5) = 3, not Python's round-half-even 2
        (184_000, "184K"),
        (999_000, "999K"),
        (999_500, "1000K"),  # fmtN rounds before it changes unit, as the prototype does
        (1_000_000, "1.0M"),
        (1_250_000, "1.3M"),  # (1.25).toFixed(1) = "1.3": an exact half rounds up
        (2_400_000, "2.4M"),
        (1_500_000_000, "1.5B"),  # beyond the prototype (DEC-040)
        (-184_000, "-184K"),
    ],
)
def test_humanise_count_matches_the_prototype_format(count: int, expected: str) -> None:
    """The prototype's `fmtN`, JavaScript rounding included (DEC-040)."""
    assert humanise_count(count) == expected


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KB"),  # fmtSize: (b/1024).toFixed(1) KB
        (1536, "1.5 KB"),
        (2560, "2.5 KB"),
        (1024 * 1024, "1.0 MB"),
        (1_310_720, "1.3 MB"),  # exactly 1.25 MB: toFixed(1) rounds the half up
        (2_400_000, "2.3 MB"),
        (1024 * 1024 * 1024, "1.0 GB"),  # beyond the prototype (DEC-040)
        (2 * 1024 * 1024 * 1024, "2.0 GB"),
    ],
)
def test_humanise_bytes_matches_the_prototype_format(size: int, expected: str) -> None:
    """The prototype's `fmtSize`, JavaScript rounding included (DEC-040)."""
    assert humanise_bytes(size) == expected


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 9, 14, 10, 20, tzinfo=UTC), "14 Sep 2026, 10:20"),
        (datetime(2026, 1, 5, 9, 5, tzinfo=UTC), "05 Jan 2026, 09:05"),
        (datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC), "31 Dec 2026, 23:59"),
    ],
)
def test_humanise_datetime_matches_the_prototype_format(moment: datetime, expected: str) -> None:
    assert humanise_datetime(moment) == expected


def test_humanise_datetime_does_not_convert_the_timezone() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    assert humanise_datetime(datetime(2026, 9, 14, 15, 50, tzinfo=ist)) == "14 Sep 2026, 15:50"


def test_humanise_datetime_is_not_locale_dependent() -> None:
    """The month abbreviations are spelled out, not taken from strftime."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(humanise_datetime)))
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    body = function.body[1:] if ast.get_docstring(function) else function.body
    code = "\n".join(ast.unparse(node) for node in body)
    assert "%b" not in code
    assert "strftime" not in code


# ----------------------------------------------------------------------------------------- logging


def test_get_logger_returns_the_named_logger() -> None:
    assert get_logger("engine.stages.train").name == "engine.stages.train"


def test_configure_logging_is_idempotent() -> None:
    root = logging.getLogger()
    configure_logging()
    before = len(root.handlers)
    configure_logging("DEBUG")
    assert len(root.handlers) == before
    assert root.level == logging.DEBUG
    configure_logging("INFO")
    assert root.level == logging.INFO


def test_log_stage_accepts_nothing_but_a_stage_a_row_count_and_a_duration() -> None:
    """Plan §13.7: there must be no parameter through which a data value could be logged."""
    signature = inspect.signature(log_stage)
    assert list(signature.parameters) == ["logger", "stage", "rows", "seconds"]
    assert signature.parameters["rows"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["seconds"].kind is inspect.Parameter.KEYWORD_ONLY
    annotations = {name: parameter.annotation for name, parameter in signature.parameters.items()}
    assert annotations == {
        "logger": "logging.Logger",
        "stage": "str",
        "rows": "int | None",
        "seconds": "float",
    }
    assert "**" not in str(signature)


def test_log_stage_emits_only_the_stage_rows_and_seconds(caplog: pytest.LogCaptureFixture) -> None:
    logger = get_logger("engine.tests.log_stage")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_stage(logger, "train", rows=1234, seconds=2.5)
    record = caplog.records[-1]
    assert record.levelno == logging.INFO
    assert record.getMessage() == "stage=train rows=1234 seconds=2.500"


def test_log_stage_renders_a_missing_row_count_as_a_dash(caplog: pytest.LogCaptureFixture) -> None:
    logger = get_logger("engine.tests.log_stage")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_stage(logger, "register", rows=None, seconds=0.125)
    assert caplog.records[-1].getMessage() == "stage=register rows=- seconds=0.125"
