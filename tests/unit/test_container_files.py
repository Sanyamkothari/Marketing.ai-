"""The Dockerfile, the compose file and the workflows, checked without building or running anything.

None of these files can be executed by the fast suite: a build needs a daemon and a registry, and a
workflow needs GitHub. What *can* be checked offline is every property that, if it silently changed,
would break a deployment in a way no other test would notice - the source layout the UI depends on,
the system library lightgbm needs, the digest-not-tag rule, and the fact that the image runs as a
non-root user. Each of those is a real mistake someone has made in a Dockerfile before.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="module")
def dockerfile(repo_root: Path) -> str:
    return (repo_root / "Dockerfile").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose(repo_root: Path) -> dict[str, object]:
    loaded = yaml.safe_load((repo_root / "docker-compose.yml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture(scope="module")
def workflows(repo_root: Path) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    for path in sorted((repo_root / ".github" / "workflows").glob("*.yml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict), f"{path} is not a mapping"
        out[path.stem] = loaded
    return out


# ---------------------------------------------------------------------------
# The image
# ---------------------------------------------------------------------------
def test_the_app_is_copied_as_source_and_not_installed(dockerfile: str) -> None:
    """`api/main.py` resolves `ui/` relative to its own file, so an installed package serves a 404."""
    assert "PYTHONPATH=/app" in dockerfile
    for directory in ("engine", "api", "ui", "configs", "templates", "scripts"):
        assert re.search(rf"^COPY\s+{directory}\s+/app/{directory}$", dockerfile, re.MULTILINE), directory
    assert not re.search(
        r"pip install\s+(-e\s+)?\.\s*$", dockerfile, re.MULTILINE
    ), "installing the project itself would move ui/ and configs/ into site-packages"


def test_libgomp_is_installed_because_lightgbm_cannot_load_without_it(dockerfile: str) -> None:
    """Measured: `import lightgbm` in an image without it dies with `libgomp.so.1: cannot open`."""
    assert 'RUNTIME_APT_PACKAGES="libgomp1"' in dockerfile
    assert "apt-get install -y --no-install-recommends ${RUNTIME_APT_PACKAGES}" in dockerfile


def test_the_pins_come_from_the_freeze_rather_than_a_second_resolution(dockerfile: str) -> None:
    assert "-c requirements-freeze.txt" in dockerfile
    assert "--only-binary=:all:" in dockerfile


def test_the_image_runs_as_a_non_root_user(dockerfile: str) -> None:
    stages = dockerfile.split("\nFROM ")
    for stage in stages[1:]:
        name = stage.splitlines()[0]
        if " AS builder" in name or " AS base" in name:
            continue
        assert re.search(r"^USER 10001$", stage, re.MULTILINE), f"stage {name!r} does not drop root"


def test_the_two_sagemaker_launch_conventions_are_both_handled(repo_root: Path) -> None:
    """A Training job appends the bare word `train`; a Processing job replaces the entrypoint."""
    entrypoint = repo_root / "scripts" / "entrypoint.sh"
    body = entrypoint.read_text(encoding="utf-8")
    assert "train|job)" in body
    assert 'exec python -m scripts.run_job_entrypoint "$@"' in body
    assert 'exec "$@"' in body, "an unrecognised argument must still be runnable"
    assert entrypoint.stat().st_mode & stat.S_IXUSR, "entrypoint.sh must be executable"


def test_the_health_check_does_not_assume_curl(dockerfile: str) -> None:
    """A slim image has no curl; installing one to serve a health check buys a dependency for nothing."""
    instructions = [line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")]
    body = "\n".join(instructions)
    assert "HEALTHCHECK" in body
    assert "curl" not in body
    assert "/healthz" in body


def test_the_build_context_excludes_the_artefact_directory(repo_root: Path) -> None:
    """A stray local `data/` would otherwise be COPYed into the image as somebody's real artefacts."""
    ignored = (repo_root / ".dockerignore").read_text(encoding="utf-8").split()
    for entry in ("data", ".git", ".venv"):
        assert entry in ignored


# ---------------------------------------------------------------------------
# The local stack
# ---------------------------------------------------------------------------
def test_compose_publishes_both_postgres_servers_on_the_ports_the_fixtures_use(
    compose: dict[str, object],
) -> None:
    services = compose["services"]
    assert isinstance(services, dict)
    assert services["postgres"]["ports"] == ["55432:5432"]
    assert services["postgres-tz"]["ports"] == ["55433:5432"]


def test_the_second_server_is_not_utc_so_the_timezone_handling_is_really_tested(
    compose: dict[str, object],
) -> None:
    """On a UTC server a naive datetime and an aware one are indistinguishable."""
    services = compose["services"]
    assert isinstance(services, dict)
    assert services["postgres-tz"]["command"] == ["postgres", "-c", "timezone=Asia/Kolkata"]


def test_the_test_databases_are_disposable(compose: dict[str, object]) -> None:
    services = compose["services"]
    assert isinstance(services, dict)
    for name in ("postgres", "postgres-tz"):
        assert "/var/lib/postgresql/data" in services[name]["tmpfs"]


# ---------------------------------------------------------------------------
# The pipelines
# ---------------------------------------------------------------------------
def test_ci_runs_the_suite_inside_the_image(workflows: dict[str, dict[str, object]]) -> None:
    """Running it on the runner cannot catch a missing system library; the runner has everything."""
    jobs = workflows["ci"]["jobs"]
    assert isinstance(jobs, dict)
    steps = jobs["image"]["steps"]
    assert any("make image-test" in str(step.get("run", "")) for step in steps)


def test_ci_makes_the_postgres_tests_fail_rather_than_skip(workflows: dict[str, dict[str, object]]) -> None:
    """A test that silently stopped running is worse than one that fails."""
    jobs = workflows["ci"]["jobs"]
    assert isinstance(jobs, dict)
    steps = jobs["lint-and-test"]["steps"]
    assert any(step.get("env", {}).get("MARKETING_AI_REQUIRE_POSTGRES") == "1" for step in steps)


def test_ci_synthesises_the_infrastructure_for_real(workflows: dict[str, dict[str, object]]) -> None:
    jobs = workflows["ci"]["jobs"]
    assert isinstance(jobs, dict)
    runs = " ".join(str(step.get("run", "")) for step in jobs["infra"]["steps"])
    assert "make infra-synth" in runs
    assert "make infra-nag" in runs


def test_deploying_is_manual_only(workflows: dict[str, dict[str, object]]) -> None:
    """One deployment per customer account: a push to a branch has no business changing one."""
    triggers = workflows["deploy-dev"][True]  # PyYAML reads the key `on` as the boolean True
    assert set(triggers) == {"workflow_dispatch"}


def test_deploying_uses_oidc_and_carries_no_long_lived_credentials(
    workflows: dict[str, dict[str, object]], repo_root: Path
) -> None:
    deploy = workflows["deploy-dev"]
    assert deploy["permissions"]["id-token"] == "write"
    body = (repo_root / ".github" / "workflows" / "deploy-dev.yml").read_text(encoding="utf-8")
    for forbidden in ("AWS_SECRET_ACCESS_KEY", "aws-access-key-id", "aws-secret-access-key"):
        assert forbidden not in body


def test_deploying_names_a_digest_rather_than_a_tag(repo_root: Path) -> None:
    """A tag can be moved after it is tested; a digest cannot."""
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert "image_digest" in makefile
    push = (repo_root / "scripts" / "build_push_image.sh").read_text(encoding="utf-8")
    assert "Manifest.Digest" in push
    assert "sha256:" in push
