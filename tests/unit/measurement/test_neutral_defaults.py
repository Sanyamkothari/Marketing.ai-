"""Neutral defaults (Plan J M93): the pilot kit and the demo stop assuming telecom.

The product is sold to any B2C business, so the data request's default use case, its pre-flight line,
the pilot kit's README and the demo's name say nothing about telecom. `make pilot-check`
(`scripts.gen_data_request --check`) stays green: the committed kit is what the neutral configs make.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.config import load_use_case
from engine.pilot.data_request import build_data_request, load_wording, render_markdown
from engine.pilot.demo import DEMO_CLIENT_NAME, DemoManifest
from scripts import build_pilot_kit, gen_data_request

TELECOM_WORDS = ("telco", "telecom")


def test_make_pilot_check_is_green(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo_root)
    assert gen_data_request.main(["--check"]) == 0


def test_the_default_use_case_is_not_telecom(config_root: Path) -> None:
    wording = load_wording(config_root)
    assert wording.default_use_cases, "a data request needs a default"
    for use_case_id in wording.default_use_cases:
        assert not any(word in use_case_id for word in TELECOM_WORDS), use_case_id
        config = load_use_case(use_case_id, config_root)
        assert config.label is not None, "a default must be requestable from raw tables"
        assert not any(word in config.name.lower() for word in TELECOM_WORDS), config.name


def test_the_pre_flight_line_names_no_telecom_use_case(config_root: Path, repo_root: Path) -> None:
    wording = load_wording(config_root)
    preflight = " ".join(wording.preflight).lower()
    assert "--use-case" in preflight and not any(word in preflight for word in TELECOM_WORDS)
    assert not any(word in build_pilot_kit.README.lower() for word in TELECOM_WORDS)
    request = (repo_root / "docs" / "pilot" / "DATA_REQUEST.md").read_text(encoding="utf-8").lower()
    assert not any(word in request for word in TELECOM_WORDS)
    markdown = render_markdown(build_data_request(None, config_root), wording).lower()
    assert not any(word in markdown for word in TELECOM_WORDS)


def test_the_demo_is_demo_company() -> None:
    assert DEMO_CLIENT_NAME == "Demo Company"
    assert DemoManifest.model_fields["client_name"].default == "Demo Company"


def test_the_seeder_names_the_demo_by_the_constant(repo_root: Path) -> None:
    source = (repo_root / "scripts" / "seed_demo.py").read_text(encoding="utf-8")
    assert "Demo Telecom" not in source
    assert '"name": DEMO_CLIENT_NAME' in source
