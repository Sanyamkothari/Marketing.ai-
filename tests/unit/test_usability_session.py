"""The usability-session command (Plan M M183, DEC-1613): its arguments, the environment it sets, and what it will touch.

Nothing here seeds the real store or starts a server: the seed command and the server runner are replaced by fakes
that record what they were asked, so the tests check the command's own decisions (the layout and data directory it
sets, the demo mode it removes, a fresh copy per participant, a refusal to reuse or to touch real data).
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import seed_validated
from scripts import usability_session as session
from scripts.usability_session import SessionError

CLEAN_ENV = {"PATH": "/usr/bin", "MARKETING_AI_DEMO_MODE": "true", "MARKETING_AI_UI_LAYOUT": "classic"}


class FakeSeeder:
    """Stands in for `seed_validated.seed`: writes a small store where the real one would and records the call."""

    def __init__(self, *, sample: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.sample = sample

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        store = kwargs["data_dir"] / "hillstrom-email" / "journey" / "data"
        store.mkdir(parents=True, exist_ok=True)
        (store / "campaign.json").write_text("{}", encoding="utf-8")
        return SimpleNamespace(
            source_kind="sample" if self.sample else "full file",
            is_validated=not self.sample,
            is_sample=self.sample,
            rows=6401 if self.sample else 64000,
        )


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> None:
        self.calls.append((command, kwargs))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def run(
    root: Path,
    *argv: str,
    seeder: FakeSeeder | None = None,
    runner: FakeRunner | None = None,
) -> tuple[int, list[str], FakeSeeder, FakeRunner]:
    said: list[str] = []
    seeder = seeder or FakeSeeder()
    runner = runner or FakeRunner()
    code = session.main(
        ["--root", str(root), *argv],
        echo=said.append,
        runner=runner,
        seeder=seeder,
        environ=CLEAN_ENV,
    )
    return code, said, seeder, runner


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "session"


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
def test_root_and_participant_are_required() -> None:
    with pytest.raises(SystemExit):
        session.main(["--participant", "P1"], echo=lambda _line: None)
    with pytest.raises(SystemExit):
        session.main(["--root", "somewhere"], echo=lambda _line: None)


@pytest.mark.parametrize("bad", ["", "../escape", "a/b", "P 1", "-p1", "x" * 17, "p1;rm", ".hidden"])
def test_a_participant_id_that_could_leave_the_root_is_refused(bad: str, tmp_path: Path) -> None:
    assert not session.valid_participant(bad)
    with pytest.raises(SessionError, match="not a participant id"):
        session.participant_paths(tmp_path, bad)


@pytest.mark.parametrize("good", ["P1", "p5", "practice", "P-2", "p_3"])
def test_the_ids_the_kit_uses_are_accepted(good: str, tmp_path: Path) -> None:
    paths = session.participant_paths(tmp_path, good)
    assert paths.folder == tmp_path / "participants" / good and paths.store == paths.folder / "store"


def test_a_bad_participant_id_on_the_command_line_is_reported_and_nothing_is_made(root: Path) -> None:
    code, said, seeder, runner = run(root, "--participant", "../x")
    assert code == 1 and "not a participant id" in said[0] and not seeder.calls and not runner.calls
    assert not (root / "participants").exists()


def test_the_port_must_be_a_user_port(root: Path) -> None:
    code, said, seeder, runner = run(root, "--participant", "P1", "--port", "80")
    assert code == 2 and "--port" in said[0] and not seeder.calls and not runner.calls and not root.exists()


def test_reset_cannot_be_combined_with_resume_or_reseed(root: Path) -> None:
    for other in ("--resume", "--reseed"):
        code, said, _, _ = run(root, "--participant", "P1", "--reset", other)
        assert code == 2 and "--reset cannot be combined" in said[0]


# ---------------------------------------------------------------------------
# The environment and the server command
# ---------------------------------------------------------------------------
def test_the_environment_sets_the_campaigns_layout_and_the_copy_and_drops_demo_mode(tmp_path: Path) -> None:
    env = session.session_env(tmp_path / "store", CLEAN_ENV)
    assert env["MARKETING_AI_UI_LAYOUT"] == "campaigns" == session.LAYOUT
    assert env["MARKETING_AI_DATA_DIR"] == str(tmp_path / "store")
    assert "MARKETING_AI_DEMO_MODE" not in env
    assert env["PATH"] == "/usr/bin"
    assert CLEAN_ENV["MARKETING_AI_DEMO_MODE"] == "true"  # the caller's mapping is not changed


def test_the_server_command_names_the_app_and_the_port() -> None:
    command = session.server_command(8123)
    assert command[:3] == [sys.executable, "-m", "uvicorn"]
    assert command[3:] == ["api.main:app", "--port", "8123"]
    assert "--reload" not in command


def test_the_server_is_started_with_that_environment_and_the_url_and_participant_are_printed(
    root: Path,
) -> None:
    port = _free_port()
    code, said, _, runner = run(root, "--participant", "P2", "--port", str(port))
    assert code == 0 and len(runner.calls) == 1
    command, kwargs = runner.calls[0]
    store = root.resolve() / "participants" / "P2" / "store"
    assert kwargs["env"]["MARKETING_AI_DATA_DIR"] == str(store)
    assert kwargs["env"]["MARKETING_AI_UI_LAYOUT"] == "campaigns"
    assert "MARKETING_AI_DEMO_MODE" not in kwargs["env"]
    assert kwargs["cwd"] == session.REPO_ROOT
    assert command[-1] == str(port)
    text = "\n".join(said)
    assert f"http://localhost:{port}/ui" in text and "Participant   P2" in text


def test_prepare_only_makes_the_copy_and_does_not_start_a_server(root: Path) -> None:
    code, said, _, runner = run(root, "--participant", "P1", "--prepare-only")
    assert code == 0 and not runner.calls
    assert (root / "participants" / "P1" / "store" / "campaign.json").is_file()
    assert "uvicorn" in "\n".join(said)


def test_the_process_environment_is_not_changed(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKETING_AI_DEMO_MODE", "true")
    monkeypatch.delenv("MARKETING_AI_UI_LAYOUT", raising=False)
    run(root, "--participant", "P1", "--prepare-only")
    assert os.environ["MARKETING_AI_DEMO_MODE"] == "true" and "MARKETING_AI_UI_LAYOUT" not in os.environ


# ---------------------------------------------------------------------------
# A fresh copy per participant
# ---------------------------------------------------------------------------
def test_the_master_is_seeded_once_with_the_kits_command_and_each_participant_gets_a_copy(root: Path) -> None:
    seeder = FakeSeeder()
    run(root, "--participant", "P1", "--prepare-only", seeder=seeder)
    run(root, "--participant", "P2", "--prepare-only", seeder=seeder)
    assert len(seeder.calls) == 1
    assert seeder.calls[0]["data_dir"] == root.resolve() / "master" and seeder.calls[0]["force"] is True
    one = root / "participants" / "P1" / "store"
    two = root / "participants" / "P2" / "store"
    (one / "campaign.json").write_text('{"changed": true}', encoding="utf-8")
    assert (two / "campaign.json").read_text(encoding="utf-8") == "{}"
    master = root / "master" / "hillstrom-email" / "journey" / "data" / "campaign.json"
    assert master.read_text(encoding="utf-8") == "{}"


def test_reseed_seeds_again_and_csv_and_fetch_reach_the_seed_command(root: Path, tmp_path: Path) -> None:
    seeder = FakeSeeder()
    run(root, "--participant", "P1", "--prepare-only", seeder=seeder)
    csv = tmp_path / "prepared.csv"
    run(
        root, "--participant", "P2", "--prepare-only", "--reseed", "--csv", str(csv), "--fetch", seeder=seeder
    )
    assert len(seeder.calls) == 2
    assert seeder.calls[1]["csv"] == csv and seeder.calls[1]["fetch"] is True


def test_the_default_seeder_is_the_seed_validated_command(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSeeder()
    monkeypatch.setattr(seed_validated, "seed", fake)
    code = session.main(
        ["--root", str(root), "--participant", "P1", "--prepare-only"],
        echo=lambda _line: None,
        environ=CLEAN_ENV,
    )
    assert code == 0 and len(fake.calls) == 1


def test_a_seed_failure_is_reported_and_leaves_no_participant_copy(root: Path) -> None:
    def failing(**_kwargs: Any) -> None:
        raise seed_validated.SeedError("no file")

    said: list[str] = []
    code = session.main(
        ["--root", str(root), "--participant", "P1"],
        echo=said.append,
        seeder=failing,
        runner=FakeRunner(),
        environ=CLEAN_ENV,
    )
    assert code == 1 and "could not be seeded: no file" in said[-1]
    assert not (root / "participants" / "P1").exists()


def test_an_existing_copy_is_refused_until_reset_or_resumed(root: Path) -> None:
    run(root, "--participant", "P1", "--prepare-only")
    marker = root / "participants" / "P1" / "store" / "work.txt"
    marker.write_text("the last session", encoding="utf-8")
    code, said, _, runner = run(root, "--participant", "P1")
    assert code == 1 and "--reset" in said[0] and "--resume" in said[0] and not runner.calls
    assert marker.is_file()
    code, _, _, runner = run(root, "--participant", "P1", "--resume", "--port", str(_free_port()))
    assert code == 0 and marker.is_file() and len(runner.calls) == 1


def test_resume_without_a_copy_is_refused(root: Path) -> None:
    code, said, _, runner = run(root, "--participant", "P1", "--resume")
    assert code == 1 and "no copy" in said[0] and not runner.calls


def test_reset_wipes_only_that_participants_copy_and_the_next_run_is_fresh(root: Path) -> None:
    run(root, "--participant", "P1", "--prepare-only")
    run(root, "--participant", "P2", "--prepare-only")
    (root / "participants" / "P1" / "store" / "work.txt").write_text("x", encoding="utf-8")
    seeder = FakeSeeder()
    code, said, _, runner = run(root, "--participant", "P1", "--reset", seeder=seeder)
    assert code == 0 and not seeder.calls and not runner.calls and said[0].startswith("removed")
    assert not (root / "participants" / "P1").exists()
    assert (root / "participants" / "P2" / "store" / "campaign.json").is_file()
    assert (root / "master").is_dir()
    code, said, _, _ = run(root, "--participant", "P1", "--reset")
    assert code == 0 and said[0].startswith("nothing to remove")
    run(root, "--participant", "P1", "--prepare-only")
    assert not (root / "participants" / "P1" / "store" / "work.txt").exists()


def test_a_sample_master_is_announced_every_time(root: Path, tmp_path: Path) -> None:
    seeder = FakeSeeder(sample=True)
    _, said, _, _ = run(root, "--participant", "P1", "--prepare-only", seeder=seeder)
    assert any(line.startswith("SAMPLE:") for line in said)
    _, said, _, _ = run(root, "--participant", "P2", "--prepare-only", seeder=seeder)
    assert any(line.startswith("SAMPLE:") for line in said)  # read back from the master's note
    _, said, _, _ = run(tmp_path / "full", "--participant", "P1", "--prepare-only")
    assert not any(line.startswith("SAMPLE:") for line in said)


def test_a_port_in_use_is_refused_before_a_server_is_started(root: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        busy = held.getsockname()[1]
        code, said, _, runner = run(root, "--participant", "P1", "--port", str(busy))
    assert code == 1 and "in use" in said[0] and not runner.calls


# ---------------------------------------------------------------------------
# It never touches real data
# ---------------------------------------------------------------------------
def test_a_folder_that_holds_other_files_is_not_used_as_the_root(root: Path) -> None:
    root.mkdir()
    (root / "mine.csv").write_text("customer\n", encoding="utf-8")
    code, said, seeder, _ = run(root, "--participant", "P1", "--reset")
    assert code == 1 and "did not make" in said[0] and not seeder.calls
    assert (root / "mine.csv").is_file() and not (root / session.MARKER).exists()


def test_an_empty_folder_is_accepted_and_marked(root: Path) -> None:
    root.mkdir()
    code, _, _, _ = run(root, "--participant", "P1", "--prepare-only")
    assert code == 0 and (root / session.MARKER).is_file()


def test_the_root_may_not_hold_the_data_directory_the_environment_names(tmp_path: Path) -> None:
    real = tmp_path / "real-data"
    (real / "runs").mkdir(parents=True)
    with pytest.raises(SessionError, match="data directory"):
        session.prepare_root(tmp_path, environ={"MARKETING_AI_DATA_DIR": str(real)})
    with pytest.raises(SessionError, match="data directory"):
        session.prepare_root(real, environ={"MARKETING_AI_DATA_DIR": str(real)})
    assert (real / "runs").is_dir() and not (tmp_path / session.MARKER).exists()


def test_the_checkout_and_its_data_folder_are_not_roots() -> None:
    for unsafe in (session.REPO_ROOT, session.REPO_ROOT / "data", session.REPO_ROOT.parent, Path("/")):
        with pytest.raises(SessionError):
            session.prepare_root(unsafe, environ={})


def test_reset_refuses_a_root_this_command_did_not_make(tmp_path: Path) -> None:
    paths = session.participant_paths(tmp_path, "P1")
    paths.folder.mkdir(parents=True)
    with pytest.raises(SessionError, match="not made by this command"):
        session.reset_participant(paths)
    assert paths.folder.is_dir()


def test_reset_does_not_follow_a_link_out_of_the_root(tmp_path: Path) -> None:
    made = session.prepare_root(tmp_path / "session", environ={})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    paths = session.participant_paths(made, "P1")
    paths.folder.parent.mkdir(parents=True)
    paths.folder.symlink_to(outside, target_is_directory=True)
    assert session.reset_participant(paths) is True
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep" and not paths.folder.exists()
