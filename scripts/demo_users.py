"""Create the demo's sign-in users, one per role, so a demo can show what each role may do (DEC-961).

    python -m scripts.demo_users [--data-dir data]

`make demo` runs with sign-in off: everyone acts as the local operator with every role, which is the
quickest way to look around. `make demo-signin` runs the same seeded demo with sign-in on and these
users, so a demo can show that a Viewer only reads, that an Analyst trains but cannot approve, and
that the person who trained a model can never approve it (separation of duties, Plan D M54).

The passwords are printed and documented in docs/QUICKSTART.md on purpose: these accounts exist only
in a local demo's data folder. The command refuses to run on a production deployment (`env=prod`),
and a user that already exists is left as it is, so running it twice is harmless.
"""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from engine.settings import SettingsError, load_settings
from scripts import create_user

COMMAND: Final[str] = "python -m scripts.demo_users"

DEMO_USERS: Final[tuple[tuple[str, str, tuple[str, ...]], ...]] = (
    ("demo-viewer", "Demo Viewer", ("viewer",)),
    ("demo-analyst", "Demo Analyst", ("analyst",)),
    ("demo-approver", "Demo Approver", ("approver",)),
    ("demo-admin", "Demo Admin", ("admin",)),
    ("demo-lead", "Demo Lead", ("analyst", "approver")),
)
"""Username, display name and roles. `demo-lead` trains and approves, which shows separation of duties."""


def demo_password(username: str) -> str:
    """`demo-viewer` -> `demo-viewer-2026`: long enough for the 12-character rule, easy to type."""
    return f"{username}-2026"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=COMMAND, description="Create the demo's sign-in users.")
    parser.add_argument("--data-dir", type=Path, default=None, help="artefact root (default: settings)")
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(f"{exc.message} ({exc.env_var})", file=sys.stderr)
        return create_user.EXIT_SETTINGS
    if settings.env == "prod":
        print("Refused: demo users are for a local demo, never a production deployment.", file=sys.stderr)
        return create_user.EXIT_REFUSED
    for username, display_name, roles in DEMO_USERS:
        argv_one = ["--username", username, "--display-name", display_name, "--password-stdin"]
        for role in roles:
            argv_one += ["--role", role]
        if args.data_dir is not None:
            argv_one += ["--data-dir", str(args.data_dir)]
        stdin = io.StringIO(demo_password(username) + "\n")
        err = io.StringIO()
        saved, sys.stderr = sys.stderr, err
        try:
            code = create_user.main(argv_one, stdin=stdin)
        finally:
            sys.stderr = saved
        if code not in (create_user.EXIT_OK, create_user.EXIT_REFUSED):
            print(err.getvalue(), file=sys.stderr, end="")
            return code
        if code == create_user.EXIT_REFUSED and "USERNAME_TAKEN" not in err.getvalue():
            print(err.getvalue(), file=sys.stderr, end="")
            return code
    print("\nDemo sign-in (local demo only):")
    for username, _, roles in DEMO_USERS:
        print(f"  {username:<14} password {demo_password(username):<20} {' + '.join(roles)}")
    return create_user.EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
