"""The pilot deployment kit (`deploy/pilot/`), checked without Docker, Caddy or a network.

The kit cannot be run by the fast suite. What can be checked offline is what would break a pilot
silently: a variable in `.env.example` the settings no longer know, a required secret that stopped being
required, a published API port, a script that lost its safety flags, a `.env` that is not gitignored.
"""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from engine.settings import ENV_VARS


@pytest.fixture(scope="module")
def kit(repo_root: Path) -> Path:
    return repo_root / "deploy" / "pilot"


@pytest.fixture(scope="module")
def compose(kit: Path) -> dict[str, Any]:
    loaded = yaml.safe_load((kit / "docker-compose.yml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _env_example(kit: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (kit / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.partition("=")
            values[name] = value
    return values


def test_every_marketing_ai_variable_in_env_example_is_a_real_setting(kit: Path) -> None:
    known = set(ENV_VARS.values())
    unknown = [name for name in _env_example(kit) if name.startswith("MARKETING_AI_") and name not in known]
    assert not unknown, f"{unknown} are not read by engine/settings.py"


def test_env_example_sets_prod_with_local_sign_in_and_one_proxy_hop(kit: Path) -> None:
    values = _env_example(kit)
    assert values["MARKETING_AI_ENV"] == "prod"
    assert values["MARKETING_AI_AUTH_MODE"] == "local"
    assert values["MARKETING_AI_TRUSTED_PROXY_HOPS"] == "1"
    assert values["MARKETING_AI_CORS_ORIGINS"].startswith("https://")
    assert "*" not in values["MARKETING_AI_CORS_ORIGINS"]


def test_env_example_carries_no_secret_values(kit: Path) -> None:
    values = _env_example(kit)
    assert values["MARKETING_AI_PRIVACY_SALT"] == ""
    assert values["MARKETING_AI_CONNECTIONS_KEY"] == ""
    assert values["BACKUP_TARGET"] == ""


def test_compose_refuses_to_start_without_the_secrets_and_the_client(compose: dict[str, Any]) -> None:
    api = compose["services"]["api"]
    for name in (
        "MARKETING_AI_PRIVACY_SALT",
        "MARKETING_AI_CONNECTIONS_KEY",
        "MARKETING_AI_CLIENT_ID",
        "MARKETING_AI_CORS_ORIGINS",
    ):
        assert api["environment"][name].startswith("${" + name + ":?"), name


def test_the_api_is_not_published_and_caddy_is_the_only_way_in(compose: dict[str, Any]) -> None:
    services = compose["services"]
    assert "ports" not in services["api"]
    assert services["api"]["restart"] == "unless-stopped"
    assert services["api"]["env_file"] == ".env"
    assert "healthcheck" in services["api"]
    assert services["api"]["volumes"] == ["data:/var/lib/marketing-ai"]
    assert services["caddy"]["ports"][:2] == ["80:80", "443:443"]
    assert services["caddy"]["depends_on"]["api"]["condition"] == "service_healthy"


def test_the_caddyfile_proxies_to_the_api_and_allows_the_largest_upload(kit: Path) -> None:
    caddyfile = (kit / "Caddyfile").read_text(encoding="utf-8")
    assert "{$DOMAIN}" in caddyfile
    assert "reverse_proxy api:8000" in caddyfile
    match = re.search(r"max_size\s+(\d+)MiB", caddyfile)
    assert match is not None and int(match.group(1)) > 2048, "the API accepts a 2048 MB file plus overhead"


@pytest.mark.parametrize("script", ["backup.sh", "restore.sh", "smoke_test.sh"])
def test_scripts_are_executable_and_fail_on_error(kit: Path, script: str) -> None:
    path = kit / script
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert re.search(r"^set -[a-z]*u", text, re.MULTILINE), "unset variables must fail"
    assert path.stat().st_mode & stat.S_IXUSR, f"{script} is not executable"


def test_backup_and_restore_refuse_to_leave_the_api_stopped_silently(kit: Path) -> None:
    backup = (kit / "backup.sh").read_text(encoding="utf-8")
    assert "trap cleanup EXIT" in backup and "docker compose start api" in backup
    assert "BACKUP_TARGET is not set" in backup
    restore = (kit / "restore.sh").read_text(encoding="utf-8")
    assert "integrity_check" in (kit / "lib.sh").read_text(encoding="utf-8")
    assert "pre-restore" in restore and "sha256sum -c" in restore


def test_the_readme_names_only_scripts_and_make_targets_that_exist(repo_root: Path, kit: Path) -> None:
    readme = (kit / "README.md").read_text(encoding="utf-8")
    for script in set(re.findall(r"\./(\w+\.sh)", readme)):
        assert (kit / script).is_file(), script
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    for target in set(re.findall(r"`make ([a-z][a-z-]*)", readme)):
        assert re.search(rf"^{re.escape(target)}:", makefile, re.MULTILINE), target
    for module in set(re.findall(r"python -m (scripts\.\w+)", readme)):
        assert (repo_root / (module.replace(".", "/") + ".py")).is_file(), module


def test_the_secrets_file_and_the_backups_are_never_committed_or_built_into_an_image(repo_root: Path) -> None:
    for ignore_file in (".gitignore", ".dockerignore"):
        lines = (repo_root / ignore_file).read_text(encoding="utf-8").splitlines()
        assert "deploy/pilot/.env" in lines, ignore_file
        assert any(line.startswith("deploy/pilot/backups") for line in lines), ignore_file


def _has_database(kit: Path, listing: str) -> int:
    """Exit status of lib.sh's has_database under the same `set -euo pipefail` the scripts use."""
    script = f'set -euo pipefail; . "{kit}/lib.sh"; listing=$(cat); has_database "$listing" || exit 3'
    return subprocess.run(["bash", "-c", script], input=listing, text=True, cwd=kit, check=False).returncode


def test_the_database_check_survives_a_huge_listing_under_pipefail(kit: Path) -> None:
    # A run folder is thousands of files; a `tar -t | grep -q` pipe dies of SIGPIPE there and the script
    # then refuses a good backup. The check must give the right answer however long the listing is.
    files = "\n".join(f"./runs/r{i}/models/part_{i}.pkl" for i in range(20000))
    assert _has_database(kit, f"./registry.db\n{files}\n") == 0
    assert _has_database(kit, f"{files}\n./runs/x/last.db\n") == 0
    assert _has_database(kit, f"{files}\n") == 3
    for script in ("backup.sh", "restore.sh"):
        text = (kit / script).read_text(encoding="utf-8")
        assert "| grep -q" not in text, f"{script}: grep -q at the end of a pipe"
