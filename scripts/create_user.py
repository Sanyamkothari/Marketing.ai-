"""Create a user from a shell - above all the first Admin, which nobody can create through the API.

With `auth_mode=local` every route but sign-in needs a session, and a session needs a user; so the
first Admin of a deployment has to come from somewhere that is not the API. This is it, and it is
also the recovery path when the last Admin has forgotten their password (the API refuses to leave
a deployment with no Admin, DEC-712, but it cannot stop one from being locked out).

**The password never touches `argv`** (DEC-722). A command-line argument is visible to every user
of the machine in `ps`, lands in shell history, and is logged by some process supervisors. So the
password is read from a prompt (twice, without echo) or, for automation, as the first line of
standard input with `--password-stdin` - the same convention as `docker login`.

The creation is recorded in the audit log as `users.create` by `system:bootstrap`, so even the
first Admin has an entry saying where it came from.

    python -m scripts.create_user --username asha --role admin
    printf '%s\\n' "$ADMIN_PASSWORD" | python -m scripts.create_user --username asha --password-stdin
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final, TextIO

from engine.access.roles import Principal, Role
from engine.access.users import PBKDF2_ITERATIONS, AccessError, SqlUserStore
from engine.audit.store import SqlAuditLog, record_event
from engine.platform_db import platform_engine
from engine.settings import SettingsError, load_settings
from engine.utils.logging import configure_logging

__all__ = ["BOOTSTRAP_PRINCIPAL", "main"]

COMMAND: Final[str] = "python -m scripts.create_user"

EXIT_OK: Final[int] = 0
EXIT_REFUSED: Final[int] = 2
"""The store refused the user (taken username, short password, ...): the message says why."""
EXIT_SETTINGS: Final[int] = 3
"""The deployment is described wrongly; nothing was written."""

BOOTSTRAP_PRINCIPAL: Final[Principal] = Principal(
    user_id="system:bootstrap",
    username="bootstrap",
    roles=frozenset({Role.ADMIN}),
    kind="system",
)
"""Who the audit log says created a user from the command line."""


def build_parser() -> argparse.ArgumentParser:
    """The command line. There is deliberately no `--password`."""
    parser = argparse.ArgumentParser(prog=COMMAND, description="Create a user in the built-in user store.")
    parser.add_argument("--username", required=True, help="What the person signs in with (case-insensitive).")
    parser.add_argument(
        "--display-name", default=None, help="How the person is shown; defaults to the username."
    )
    parser.add_argument(
        "--role",
        action="append",
        choices=[role.value for role in Role],
        help="A role to grant; repeat for several. Defaults to admin.",
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the password from the first line of standard input instead of prompting.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Use <data-dir>/platform.db instead of what the settings describe (a local deployment).",
    )
    return parser


def read_password(*, from_stdin: bool, stdin: TextIO | None = None) -> str | None:
    """The password from stdin's first line, or from two matching prompts; None when they differ."""
    if from_stdin:
        return (stdin or sys.stdin).readline().rstrip("\r\n")
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Password again: ")
    return first if first == second else None


def main(
    argv: Sequence[str] | None = None, *, stdin: TextIO | None = None, iterations: int | None = None
) -> int:
    """Create the user and record it; the exit code says what happened."""
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(f"{exc.message} ({exc.env_var})", file=sys.stderr)
        return EXIT_SETTINGS
    configure_logging(settings.log_level, log_format=settings.log_format)
    password = read_password(from_stdin=args.password_stdin, stdin=stdin)
    if password is None:
        print("The two passwords did not match; nothing was created.", file=sys.stderr)
        return EXIT_REFUSED
    engine = platform_engine(settings, data_dir=args.data_dir)
    store = SqlUserStore(
        engine,
        session_ttl_seconds=settings.auth_session_ttl_seconds,
        iterations=PBKDF2_ITERATIONS if iterations is None else iterations,
    )
    roles = [Role(value) for value in (args.role or [Role.ADMIN.value])]
    try:
        user = store.create_user(
            args.username,
            password,
            roles=roles,
            created_by=BOOTSTRAP_PRINCIPAL.user_id,
            display_name=args.display_name,
        )
    except AccessError as exc:
        print(f"{exc.message} ({exc.code})", file=sys.stderr)
        return EXIT_REFUSED
    record_event(
        SqlAuditLog(engine),
        BOOTSTRAP_PRINCIPAL,
        "users.create",
        object_type="user",
        object_id=user.user_id,
        details={
            "target_user_id": user.user_id,
            "roles": ",".join(sorted(role.value for role in user.roles)),
            "trigger": "create_user_script",
        },
    )
    print(f"created user {user.user_id} with roles {', '.join(sorted(role.value for role in user.roles))}")
    if settings.auth_mode != "local":
        print(
            "note: sign-in is off on this deployment (MARKETING_AI_AUTH_MODE); the user can sign in once it is local."
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
