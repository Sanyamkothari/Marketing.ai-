"""The runtime_pro stream's public surface holds together.

The behaviour is tested in `tests/v2/failure/`. These checks only make sure that what the stream
exports exists, that its pieces plug into each other (a `MandateBook` is a `MandateSource`, the
runtime's `send_gate` fits the worker's `SendGate`), and that the settings turn each new part on
and off as `docs/v2/streams/runtime_pro.md` says.
"""

from __future__ import annotations

import inspect
from types import ModuleType

import pytest

from autonomy.channels import breakers
from autonomy.runtime import service, settings, worker
from autonomy.runtime.service import AutonomyRuntime, MandateBook, MandateSource
from autonomy.runtime.settings import RuntimeSettings


@pytest.mark.parametrize("module", [service, settings, worker], ids=lambda m: m.__name__)
def test_every_exported_name_exists(module: ModuleType) -> None:
    missing = [name for name in module.__all__ if not hasattr(module, name)]
    assert missing == []


def test_the_new_breaker_names_are_importable() -> None:
    for name in ("LiftGuards", "DEFAULT_LIFT_GUARDS", "NEGATIVE_LIFT_CODE", "negative_lift"):
        assert hasattr(breakers, name), name
    assert breakers.NEGATIVE_LIFT_CODE == "proof.negative_lift"


def test_a_mandate_book_is_a_mandate_source() -> None:
    book: MandateSource = MandateBook(required_goals=["g1"])
    assert book.governs("g1") and not book.governs("g2")
    assert book.lookup("g1") is None


def test_the_runtime_send_gate_has_the_worker_gate_shape() -> None:
    params = list(inspect.signature(AutonomyRuntime.send_gate).parameters)
    assert params == ["self", "decision", "now"]


def test_the_runtime_takes_the_new_hooks_as_keywords() -> None:
    params = inspect.signature(AutonomyRuntime.__init__).parameters
    for name in ("state_enricher", "mandates", "gateway", "lift_guards"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert params[name].default is None, name


def test_settings_defaults_keep_the_gateway_off_and_the_lift_breaker_on() -> None:
    plain = RuntimeSettings()
    assert plain.gateway_enabled is False
    guards = plain.lift_guards()
    assert guards is not None
    assert (guards.min_group, guards.min_closed) == (plain.lift_min_group, plain.lift_min_closed)
    off = RuntimeSettings.from_env({"AUTONOMY_NEGATIVE_LIFT_BREAKER": "false"})
    assert off.lift_guards() is None


def test_gateway_settings_read_from_the_environment() -> None:
    s = RuntimeSettings.from_env(
        {
            "AUTONOMY_GATEWAY_ENABLED": "true",
            "AUTONOMY_COUNTRIES": "in, us, IN",
            "AUTONOMY_DND_REGISTRIES": "NCPR=/data/ncpr.txt",
            "AUTONOMY_CONSENT_REGISTRY": "none",
        }
    )
    assert s.gateway_enabled is True
    assert s.countries == ("IN", "US")
    assert s.dnd_registries == ("NCPR=/data/ncpr.txt",)
    assert s.consent_registry == "none"
    with pytest.raises(ValueError, match="alpha-2"):
        RuntimeSettings(countries=("IND",))
