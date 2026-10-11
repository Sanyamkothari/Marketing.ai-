"""One command for the facilitator of a usability session (Plan M M183, DEC-1613).

    python -m scripts.usability_session --root DIR --participant P1 [--port 8000]
    python -m scripts.usability_session --root DIR --participant P1 --reset

`docs/ux/USABILITY_TEST.md` (section 3.2) needs one untouched copy of the validated Hillstrom demo store for every
participant, served in the `campaigns` layout and without demo mode. This command does those steps in one go:

1. **A master store, seeded once.** The first run seeds `DIR/master` with the kit's documented command
   (`python -m scripts.seed_validated`, about three minutes on the full file). Later runs reuse it. `--reseed`
   replaces it; `--csv` and `--fetch` are passed to the seed command unchanged.
2. **A fresh copy per participant.** The participant's copy is `DIR/participants/<id>/store`, copied from the
   master, so Task 1 (which creates a campaign) never changes what the next person sees. A copy that already exists
   is refused (it may hold the last session's work); `--reset` wipes it, `--resume` serves it as it is (for a
   server that stopped in the middle of a session).
3. **The app, on a port you choose.** `MARKETING_AI_UI_LAYOUT=campaigns` and `MARKETING_AI_DATA_DIR=<the copy>`
   are set for the server and nothing else is added; `MARKETING_AI_DEMO_MODE` is removed from its environment, because
   on this store it would show a false "Sample data" notice (USABILITY_TEST.md section 3.2). The URL and the
   participant id are printed first, then the server runs until you press Ctrl+C. `--prepare-only` stops before the
   server and prints the command to run.

**It never touches your real data.** Everything goes under `--root`. A folder that already holds anything is
refused unless this command made it (it leaves a marker file), and a root that holds, or lies inside, the data
directory the environment names (or the checkout's `data/`) is refused. `--reset` removes only the folder `DIR/participants/<id>`.

If the master was made from the committed sample (no full file), the command says so every time: the numbers on
screen are then not the validated ones and the results must say so (USABILITY_TEST.md section 3.1).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "COMMAND",
    "LAYOUT",
    "SessionError",
    "SessionPaths",
    "main",
    "participant_paths",
    "prepare_root",
    "reset_participant",
    "server_command",
    "session_env",
    "valid_participant",
]

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
COMMAND: Final = "python -m scripts.usability_session"
LAYOUT: Final = "campaigns"
MARKER: Final = ".usability-session-root"
MASTER_NOTE: Final = "master.json"
PARTICIPANT_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,15}$")
ENV_LAYOUT: Final = "MARKETING_AI_UI_LAYOUT"
ENV_DATA_DIR: Final = "MARKETING_AI_DATA_DIR"
ENV_DEMO_MODE: Final = "MARKETING_AI_DEMO_MODE"


class SessionError(RuntimeError):
    """The session could not be set up; the message says what to do."""


@dataclass(frozen=True)
class SessionPaths:
    """Where everything for one participant lives under the root."""

    root: Path
    master: Path
    """The seed command's `--data-dir`; the store inside it is `master_store`."""
    master_store: Path
    folder: Path
    """`DIR/participants/<id>`: the one folder `--reset` removes."""
    store: Path
    """The copy the app serves (`MARKETING_AI_DATA_DIR`)."""


def valid_participant(participant: str) -> bool:
    """True for ids like `P1`, `p2` or `practice`: letters, digits, `-`, `_`, at most 16, starting with a letter or digit."""
    return PARTICIPANT_ID.fullmatch(participant) is not None


def participant_paths(root: Path, participant: str) -> SessionPaths:
    """The paths for `participant` under `root`; refuses an id that could leave the root."""
    if not valid_participant(participant):
        raise SessionError(
            f"{participant!r} is not a participant id: use letters, digits, - or _ (at most 16), e.g. P1 or practice"
        )
    master = root / "master"
    folder = root / "participants" / participant
    return SessionPaths(
        root=root,
        master=master,
        master_store=master / "hillstrom-email" / "journey" / "data",
        folder=folder,
        store=folder / "store",
    )


def session_env(store: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment the server runs with: the `campaigns` layout, this participant's copy, and no demo mode."""
    env = dict(os.environ if base is None else base)
    env.pop(ENV_DEMO_MODE, None)
    env[ENV_LAYOUT] = LAYOUT
    env[ENV_DATA_DIR] = str(store)
    return env


def server_command(port: int, *, python: str | None = None) -> list[str]:
    """The command that serves the app on `port` (the same app `make serve` runs, without `--reload`)."""
    return [python or sys.executable, "-m", "uvicorn", "api.main:app", "--port", str(port)]


def server_line(store: Path, port: int) -> str:
    """The shell command that serves `store`: quoted for a shell, and without demo mode even if the shell exports it."""
    return "env -u {demo} {rest}".format(
        demo=ENV_DEMO_MODE,
        rest=shlex.join([f"{ENV_LAYOUT}={LAYOUT}", f"{ENV_DATA_DIR}={store}", *server_command(port)]),
    )


def url(port: int) -> str:
    return f"http://localhost:{port}/ui"


def prepare_root(root: Path, *, environ: Mapping[str, str] | None = None) -> Path:
    """Create `root` (or accept one this command made), refusing anything that could be real data."""
    environ = os.environ if environ is None else environ
    root = root.expanduser().resolve()
    named = environ.get(ENV_DATA_DIR)
    for real in filter(None, [Path(named).expanduser().resolve() if named else None, REPO_ROOT / "data"]):
        if real == root or root in real.parents or real in root.parents:
            raise SessionError(
                f"{root} holds or lies inside {real}, a data directory this checkout or your environment uses; "
                "pick a new folder outside it for --root"
            )
    if root == REPO_ROOT or root in REPO_ROOT.parents or root == Path(root.anchor):
        raise SessionError(f"{root} is not a safe --root; pick a new folder for the session files")
    if root.exists():
        if not root.is_dir():
            raise SessionError(f"{root} is not a folder")
        if not (root / MARKER).is_file() and any(root.iterdir()):
            raise SessionError(
                f"{root} already holds files that this command did not make; pick an empty or new folder for --root"
            )
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text(
        "Made by scripts/usability_session.py. Everything under here can be deleted after the sessions.\n",
        encoding="utf-8",
    )
    return root


def reset_participant(paths: SessionPaths) -> bool:
    """Remove this participant's folder and nothing else. True when something was removed."""
    folder = paths.folder
    if not (paths.root / MARKER).is_file():
        raise SessionError(f"{paths.root} was not made by this command; nothing was removed")
    if folder.is_symlink():  # never follow a link out of the root
        folder.unlink()
        return True
    if not folder.exists():
        return False
    parent = (paths.root / "participants").resolve()
    if folder.resolve().parent != parent:
        raise SessionError(f"{folder} is outside {parent}; nothing was removed")
    shutil.rmtree(folder)
    return True


def _write_master_note(master: Path, **facts: object) -> None:
    (master / MASTER_NOTE).write_text(json.dumps(facts, indent=2), encoding="utf-8")


def _read_master_note(master: Path) -> dict[str, object]:
    try:
        loaded = json.loads((master / MASTER_NOTE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _remove_master(master: Path) -> None:
    """Remove a (half-built) master. It lies under the marked root, which `prepare_root` has already checked."""
    if master.is_symlink():
        master.unlink()
    else:
        shutil.rmtree(master, ignore_errors=True)


def ensure_master(
    paths: SessionPaths,
    *,
    reseed: bool,
    csv: Path | None,
    fetch: bool,
    echo: Callable[[str], None],
    seeder: Callable[..., object] | None = None,
) -> dict[str, object]:
    """Seed the master store once with the kit's command (`scripts.seed_validated`); later runs reuse it."""
    # The note is written last, so a master without it is a seed that failed or was interrupted: never reuse it.
    have = (
        (paths.master / MASTER_NOTE).is_file()
        and paths.master_store.is_dir()
        and any(paths.master_store.iterdir())
    )
    if have and not reseed:
        return _read_master_note(paths.master)
    from scripts import seed_validated

    seed = seeder or seed_validated.seed
    echo(
        f"seeding the master store in {paths.master} with {seed_validated.COMMAND} (about three minutes) ..."
    )
    _remove_master(paths.master)
    try:
        result = seed(data_dir=paths.master, csv=csv, fetch=fetch, force=True, echo=echo)
    except Exception as error:  # any failure leaves a half-built master that must not be reused
        _remove_master(paths.master)
        if isinstance(error, seed_validated.SeedError):
            raise SessionError(f"the demo store could not be seeded: {error}") from error
        raise SessionError(
            f"the demo store could not be seeded ({type(error).__name__}: {error}); nothing was kept, run again"
        ) from error
    facts: dict[str, object] = {
        "source_kind": getattr(result, "source_kind", "unknown"),
        "is_validated": bool(getattr(result, "is_validated", False)),
        "is_sample": bool(getattr(result, "is_sample", False)),
        "rows": getattr(result, "rows", None),
    }
    _write_master_note(paths.master, **facts)
    return facts


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def describe(participant: str, paths: SessionPaths, port: int, master: Mapping[str, object]) -> list[str]:
    """What the facilitator sees before the server starts."""
    lines = [
        "",
        f"Participant   {participant}",
        f"URL           {url(port)}",
        f"Store         {paths.store}",
        f"Server        {server_line(paths.store, port)}",
        "",
        "Before the participant arrives (USABILITY_TEST.md section 3.3): open the URL in a fresh browser profile, check",
        "that the sidebar shows five places and that there is no 'Sample data' notice, fill in the answer key from this",
        "store's Reports page, empty the downloads folder and start the recorder.",
    ]
    if master.get("is_sample"):
        lines += [
            "",
            "SAMPLE: this store was made from the committed sample (a tenth of the file). The numbers on screen are NOT the",
            "validated results; say so in the results sheet. For the validated numbers run library/hillstrom-email/fetch.py",
            f"and then {COMMAND} --root <same folder> --participant <id> --reseed.",
        ]
    elif not master or not master.get("is_validated", True):
        lines += [
            "",
            "UNVERIFIED: this store has no record that it was made from the validated file; do not quote its numbers.",
            f"Run {COMMAND} --root <same folder> --participant <id> --reseed to make it again.",
        ]
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=COMMAND, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--root", type=Path, required=True, help="the folder for all session files (made if absent)"
    )
    parser.add_argument("--participant", required=True, help="the participant id, e.g. P1 or practice")
    parser.add_argument("--port", type=int, default=8000, help="the port to serve on (default 8000)")
    parser.add_argument("--reset", action="store_true", help="wipe this participant's copy and stop")
    parser.add_argument(
        "--resume", action="store_true", help="serve an existing copy as it is (the server stopped)"
    )
    parser.add_argument(
        "--reseed", action="store_true", help="replace the master store (about three minutes)"
    )
    parser.add_argument(
        "--csv", type=Path, default=None, help="a prepared file to seed from (see seed_validated)"
    )
    parser.add_argument(
        "--fetch", action="store_true", help="fetch the full file when absent (see seed_validated)"
    )
    parser.add_argument(
        "--prepare-only", action="store_true", help="make the copy and print the command; do not serve"
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    echo: Callable[[str], None] = print,
    runner: Callable[..., object] | None = None,
    seeder: Callable[..., object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run the command. `runner` (default `subprocess.run`) and `seeder` are replaced by the tests."""
    args = build_parser().parse_args(argv)
    if not 1024 <= args.port <= 65535:
        echo("not started: --port must be between 1024 and 65535")
        return 2
    if args.reset and (args.resume or args.reseed):
        echo("not started: --reset cannot be combined with --resume or --reseed; reset, then run again")
        return 2
    try:
        participant_paths(args.root, args.participant)  # refuse a bad id before anything is made
        root = prepare_root(args.root, environ=environ)
        paths = participant_paths(root, args.participant)
        if args.reset:
            removed = reset_participant(paths)
            echo(f"{'removed' if removed else 'nothing to remove at'} {paths.folder}")
            return 0
        if paths.folder.exists() and not args.resume:
            raise SessionError(
                f"{paths.folder} already exists and may hold the last session's work; run with --reset first, "
                "or with --resume to serve it as it is"
            )
        if args.resume and not paths.store.is_dir():
            raise SessionError(f"there is no copy at {paths.store} to resume; run without --resume")
        if not args.prepare_only and not port_is_free(args.port):
            raise SessionError(f"port {args.port} is in use; close the last server or choose --port")
        master = ensure_master(
            paths, reseed=args.reseed, csv=args.csv, fetch=args.fetch, echo=echo, seeder=seeder
        )
        if not args.resume:
            paths.folder.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(paths.master_store, paths.store)
    except SessionError as error:
        echo(f"not started: {error}")
        return 1
    for line in describe(args.participant, paths, args.port, master):
        echo(line)
    if args.prepare_only:
        return 0
    echo("")
    echo("Serving now. Press Ctrl+C when the session ends, then run --reset for this participant.")
    run = runner or subprocess.run
    try:
        run(server_command(args.port), env=session_env(paths.store, environ), cwd=REPO_ROOT, check=False)
    except KeyboardInterrupt:
        echo("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
