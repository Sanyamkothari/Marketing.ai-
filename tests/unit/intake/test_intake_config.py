"""`configs/intake.yaml` and `IntakeConfig`'s defaults say the same thing (Plan L M150, DEC-1500)."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.intake.config import IntakeConfig, load_intake_config


def test_the_shipped_file_equals_the_defaults(config_root: Path) -> None:
    assert load_intake_config(config_root) == IntakeConfig()


def test_a_directory_without_the_file_reads_the_defaults(tmp_path: Path) -> None:
    assert load_intake_config(tmp_path) == IntakeConfig()


def test_a_malformed_file_is_an_error_not_a_fallback(tmp_path: Path) -> None:
    (tmp_path / "intake.yaml").write_text("max_footer_rows: lots\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"intake\.yaml is not valid"):
        load_intake_config(tmp_path)


def test_words_are_trimmed_lower_cased_and_unique() -> None:
    config = IntakeConfig(missing_words=(" N/A ", "n/a", "Unknown"), total_words=("Grand Total",))
    assert config.missing_words == ("n/a", "unknown")
    assert config.total_words == ("grand total",)


def test_an_empty_word_list_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one word"):
        IntakeConfig(missing_words=())
