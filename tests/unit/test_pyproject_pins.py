"""Owner A's tests for `pyproject.toml` (design §11).

Every runtime and dev pin must be the version the resolver actually produced. `requirements-freeze.txt`
is that resolver output, committed verbatim so this test has its input on a clean checkout (DEC-018).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

PIN_RE = re.compile(
    r"""^
    (?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)      # distribution name
    (?:\[(?P<extras>[^\]]*)\])?               # optional extras
    \s*==\s*
    (?P<version>[^\s;]+)                      # the pinned version
    $""",
    re.VERBOSE,
)


def normalise(name: str) -> str:
    """PEP 503 name normalisation: `autogluon.tabular` and `autogluon-tabular` are one name."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


@pytest.fixture(scope="session")
def pyproject_text(repo_root: Path) -> str:
    return (repo_root / "pyproject.toml").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def pyproject(pyproject_text: str) -> dict[str, Any]:
    return tomllib.loads(pyproject_text)


@pytest.fixture(scope="session")
def freeze(repo_root: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for raw in (repo_root / "requirements-freeze.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN_RE.match(line)
        assert match is not None, f"unparsable freeze line: {line!r}"
        versions[normalise(match.group("name"))] = match.group("version")
    return versions


def test_a_package_declared_twice_is_declared_at_one_version(pyproject: dict[str, Any]) -> None:
    """A name in two groups at two versions is a pin that pip resolves and nobody reviewed.

    `boto3` is the live case: Phase 3a needs it at runtime for Bedrock and Phase 4a declares it in
    the `aws` extra so a deployment that installs only that extra still pins it. `declared_pins`
    builds a dict, so the second declaration silently overwrites the first and the mismatch would
    never reach an assertion - which is exactly how it stayed invisible until this test existed.
    """
    project = pyproject["project"]
    groups = {"dependencies": project["dependencies"]}
    groups.update(project["optional-dependencies"])
    seen: dict[str, dict[str, str]] = {}
    for group, requirements in groups.items():
        for requirement in requirements:
            match = PIN_RE.match(requirement.strip())
            if match is None:
                continue
            seen.setdefault(normalise(match.group("name")), {})[group] = match.group("version")
    disagreeing = {name: where for name, where in seen.items() if len(set(where.values())) > 1}
    assert not disagreeing, f"declared at more than one version: {disagreeing}"


def declared_pins(pyproject: dict[str, Any]) -> dict[str, str]:
    """The `==` pins of `[project].dependencies` plus the `dev` extra, by normalised name."""
    project = pyproject["project"]
    requirements: list[str] = [
        *project["dependencies"],
        *project["optional-dependencies"]["dev"],
    ]
    pins: dict[str, str] = {}
    for requirement in requirements:
        match = PIN_RE.match(requirement.strip())
        assert match is not None, f"not an `==` pin: {requirement!r}"
        pins[normalise(match.group("name"))] = match.group("version")
    return pins


def test_the_freeze_file_parsed(freeze: dict[str, str]) -> None:
    assert len(freeze) > 50
    assert freeze["autogluon-tabular"] == "1.6.3"


def test_every_pin_matches_the_resolved_freeze(pyproject: dict[str, Any], freeze: dict[str, str]) -> None:
    pins = declared_pins(pyproject)
    assert pins, "no pins found in pyproject.toml"
    missing = sorted(name for name in pins if name not in freeze)
    assert not missing, f"pinned in pyproject.toml but absent from requirements-freeze.txt: {missing}"
    mismatched = {name: (version, freeze[name]) for name, version in pins.items() if freeze[name] != version}
    assert not mismatched, f"pyproject.toml disagrees with the freeze (pyproject, freeze): {mismatched}"


def test_runtime_dependencies_and_dev_extra_are_all_pinned(pyproject: dict[str, Any]) -> None:
    project = pyproject["project"]
    for requirement in [*project["dependencies"], *project["optional-dependencies"]["dev"]]:
        assert PIN_RE.match(requirement.strip()), f"unpinned requirement: {requirement!r}"


def test_xgboost_is_never_named_directly(pyproject_text: str) -> None:
    """On Linux the AutoGluon `xgboost` extra installs `xgboost-cpu`; depend on the extra (ENV_FACTS)."""
    assert "xgboost==" not in pyproject_text
    assert "xgboost-cpu" not in pyproject_text
    assert "autogluon.tabular[lightgbm,xgboost,catboost]==1.6.3" in pyproject_text


def test_pydantic_settings_is_not_a_dependency(pyproject: dict[str, Any]) -> None:
    """Plan §13.5: a few `os.environ` reads suffice."""
    project = pyproject["project"]
    groups: list[str] = [*project["dependencies"]]
    for extra in project["optional-dependencies"].values():
        groups.extend(extra)
    names = {
        normalise(requirement.split("[")[0].split("=")[0].split(">")[0].split("<")[0])
        for requirement in groups
    }
    assert "pydantic-settings" not in names
    assert "pydantic" in names


def test_hatchling_is_the_unpinned_build_backend(pyproject: dict[str, Any]) -> None:
    """DEC-018: build-time only, absent from the runtime freeze, so no invented pin."""
    build = pyproject["build-system"]
    assert build["requires"] == ["hatchling"]
    assert build["build-backend"] == "hatchling.build"


def test_mypy_is_globally_strict_over_engine_api_and_scripts(pyproject: dict[str, Any]) -> None:
    """DEC-013: one flag, no per-module strictness games."""
    mypy = pyproject["tool"]["mypy"]
    assert mypy["strict"] is True
    assert mypy["files"] == [
        "engine",
        "api",
        "scripts",
        "alembic",  # the migrations own the Postgres schema, so they are held to the same bar
        "tests/fixtures/make_data.py",
        "tests/fixtures/make_docs.py",
        "tests/fakes",  # FakeSageMaker stands in for a service; DEC-041 and DEC-206's precedent
    ]
    assert mypy["python_version"] == "3.11"


def test_no_mypy_override_re_enables_or_relaxes_strict(pyproject: dict[str, Any]) -> None:
    """`strict` is not a valid per-module option; an override that names it is a mistake."""
    overrides = pyproject["tool"]["mypy"].get("overrides", [])
    for override in overrides:
        assert "strict" not in override, f"override names `strict`: {override}"
    assert len(overrides) == 1
    assert set(overrides[0]["module"]) == {"autogluon.*", "shap.*", "sklearn.*", "imblearn.*"}
    assert overrides[0]["ignore_missing_imports"] is True


def test_project_identity_and_python_range(pyproject: dict[str, Any]) -> None:
    project = pyproject["project"]
    assert project["name"] == "marketing-ai"
    assert project["version"] == "0.1.0"
    assert project["requires-python"] == ">=3.11,<3.12"
    assert project["readme"] == "README.md"


def test_the_package_version_matches_the_engine_version(pyproject: dict[str, Any]) -> None:
    from engine import __version__

    assert pyproject["project"]["version"] == __version__


def test_wheel_packages_are_the_three_source_packages(pyproject: dict[str, Any]) -> None:
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "engine",
        "api",
        "scripts",
    ]


def test_line_length_is_110_for_both_formatters(pyproject: dict[str, Any]) -> None:
    assert pyproject["tool"]["ruff"]["line-length"] == 110
    assert pyproject["tool"]["black"]["line-length"] == 110


def test_pytest_finds_the_packages_without_an_editable_install(pyproject: dict[str, Any]) -> None:
    ini = pyproject["tool"]["pytest"]["ini_options"]
    assert ini["pythonpath"] == ["."]
    assert ini["testpaths"] == ["tests"]
    assert {marker.split(":")[0] for marker in ini["markers"]} == {
        "slow",
        "integration",
        "bedrock",
        "postgres",
        "docker",
        "aws",
    }
    assert "-rs" in ini["addopts"], "a skip must always print its reason (DEC-344)"


def test_the_nn_extra_is_torch_only(pyproject: dict[str, Any]) -> None:
    """DEC-011: the NeuralNet family is opt-in; torch is multi-GB and off by default."""
    assert pyproject["project"]["optional-dependencies"]["nn"] == ["torch>=2.10,<2.14"]


def test_the_phase_4a_extras_are_pinned_and_optional(pyproject: dict[str, Any]) -> None:
    """DEC-306: a laptop install stays exactly as heavy as it was; the AWS packages are opt-in."""
    extras = pyproject["project"]["optional-dependencies"]
    for name in ("aws", "deploy"):
        assert extras[name], f"the {name} extra is empty"
        for requirement in extras[name]:
            assert PIN_RE.match(requirement.strip()), f"unpinned {name} requirement: {requirement!r}"
    runtime = {
        normalise(requirement.split("[")[0].split("=")[0])
        for requirement in pyproject["project"]["dependencies"]
    }
    # boto3 and botocore are deliberately absent from this list. Phase 3a calls Bedrock from the
    # engine, so they became runtime dependencies when that branch merged; the `aws` extra declares
    # them too, which pins them for a deployment that installs only that extra. What must stay
    # optional is everything no engine code path imports: the Postgres driver, the migration tool,
    # and the whole CDK toolchain, which pulls jsii and a node bridge (DEC-306, DEC-364).
    for name in ("psycopg", "alembic", "aws-cdk-lib", "cdk-nag", "constructs"):
        assert name not in runtime, f"{name} must stay optional, not a runtime dependency"


def test_cdk_nag_is_pinned_below_3(pyproject: dict[str, Any]) -> None:
    """Measured (DEC-366): cdk-nag 3.0.2 against aws-cdk-lib 2.270.0 dies inside `cdk synth` with
    `TypeError: aspectApplication.aspect.visit is not a function`. 2.38.2 synthesises and reports."""
    deploy = pyproject["project"]["optional-dependencies"]["deploy"]
    assert "cdk-nag==2.38.2" in deploy
