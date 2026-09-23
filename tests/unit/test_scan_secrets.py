"""The M52 secrets scan (`scripts/scan_secrets.py`): it finds what it claims to, and the tree is clean.

A scanner's worst failure is silent: a regex that no longer matches reports "clean" forever. So
every rule has a planted sample here that it must flag. The samples are assembled at run time from
fragments, so this file itself never contains a string any rule would match - otherwise the "tree
is clean" test below would have to exempt it, and an exemption for the scanner's own test is where
a real key would hide.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.scan_secrets import (
    ALLOW_MARKER,
    REPO_ROOT,
    RULES,
    fingerprint,
    parse_history,
    redact,
    scan_history,
    scan_text,
    scan_tree,
)

GIT = shutil.which("git")
NEEDS_GIT = pytest.mark.skipif(GIT is None, reason="needs the git binary (the slim image has none)")
NEEDS_CHECKOUT = pytest.mark.skipif(
    GIT is None or not (REPO_ROOT / ".git").exists(),
    reason="needs a git checkout; `.git` is not in the image's build context (.dockerignore)",
)

_A = "AKIA"
_UPPER = "QWERTYUIOPASDFGH"  # 16 characters of the id's alphabet
_SECRET = "wJalrXUtn" + "FEMIK7MDEN" + "GbPxRfiCYzz" + "Q" * 10  # 40 characters, no "EXAMPLE"
_PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"
_GH = "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
_SLACK = "xo" + "xb-" + "123456789012-abcdefghij"
_GOOGLE = "AI" + "za" + "Sy" + "A" * 33
_ANTHROPIC = "sk-" + "ant-" + "api03-" + "x1Y2z3" * 5
_OPENAI = "sk-" + "a" * 20 + "T3Blbk" + "FJ" + "b" * 20
_STRIPE = "sk_" + "live_" + "c" * 24
_JWT = (
    "ey"
    + "J"
    + "hbGciOiJIUzI1NiJ9"
    + ".ey"
    + "J"
    + "zdWIiOiIxMjM0NTY3ODkwIn0"
    + ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)
_DSN = "postgresql://app:" + "Tr0ub4dor-and-3" + "@db.internal:5432/marketing"
_ASSIGNED = "api_key = '" + "9f8e7d6c5b4a39281706" + "'"
_ENV_LINE = "export DB_PASS" + "WORD=" + "Tr0ub4dor-and-3-horses"

PLANTED: dict[str, str] = {
    "aws-access-key-id": f"key = {_A}{_UPPER}",
    "aws-secret-access-key": f"aws_secret_access_key = {_SECRET}",
    "private-key": _PEM,
    "github-token": f"token: {_GH}",
    "slack-token": _SLACK,
    "google-api-key": _GOOGLE,
    "anthropic-api-key": f"ANTHROPIC_API_KEY={_ANTHROPIC}",
    "openai-api-key": _OPENAI,
    "stripe-live-key": _STRIPE,
    "jwt": f"Authorization: Bearer {_JWT}",
    "url-with-password": f"MARKETING_AI_POSTGRES_DSN={_DSN}",
    "assigned-secret": _ASSIGNED,
    "env-assigned-secret": _ENV_LINE,
}


def test_every_rule_has_a_planted_sample() -> None:
    assert {rule.name for rule in RULES} == set(PLANTED)


@pytest.mark.parametrize(("rule", "sample"), sorted(PLANTED.items()))
def test_each_rule_flags_its_planted_sample(rule: str, sample: str) -> None:
    findings = scan_text(f"# config\n{sample}\n", "planted.txt")
    assert rule in {finding.rule for finding in findings}
    assert all(finding.line == 2 for finding in findings)


@pytest.mark.parametrize(
    "line",
    [
        "DB_PASSWORD = '" + "9f8e7d6c5b4a39281706" + "'",
        'config.password = "' + "9f8e7d6c5b4a39281706" + '"',
        '{"client_secret": "' + "9f8e7d6c5b4a39281706" + '"}',
    ],
)
def test_assigned_secrets_are_found_in_the_usual_spellings(line: str) -> None:
    assert [finding.rule for finding in scan_text(line, "x")] == ["assigned-secret"]


def test_a_finding_never_carries_the_secret() -> None:
    findings = scan_text(f"aws_secret_access_key = {_SECRET}\n", "planted.txt")
    rendered = " ".join(finding.render() for finding in findings)
    assert _SECRET not in rendered
    assert redact(_SECRET) in rendered


@pytest.mark.parametrize(
    "line",
    [
        f"key = {_A}IOSFODNN7EXAMPLE",  # AWS's documented example id
        "postgresql://user:${DB_PASSWORD}@host/db",
        "postgresql://user:<password>@host/db",
        "postgresql://user:***@host/db",
        f"key = {_A}{_UPPER}  # {ALLOW_MARKER} (moto fixture)",
        "postgresql+psycopg://u:p@h/db",  # a reviewed fake (`REVIEWED_FAKES`)
        'root.querySelector(state.username ? "#pb-password" : "#pb-username");',  # a selector, not a key
        "DB_PASS" + "WORD=${FROM_SECRETS_MANAGER}",  # an env line that references, not carries, it
        "DB_PASS" + 'WORD = os.environ["DB_PASSWORD"]',  # Python reading it, not a value
    ],
)
def test_placeholders_reviewed_fakes_and_marked_lines_are_not_findings(line: str) -> None:
    assert scan_text(line, "doc.md") == []


def test_reviewed_fakes_are_named_by_fingerprint_not_value() -> None:
    assert fingerprint("p") == "148de9c5a7a44d19"
    assert len(fingerprint("anything")) == 16


@NEEDS_CHECKOUT
def test_the_tree_is_clean() -> None:
    """Every tracked or addable file of this checkout. A failure names file, line and rule only."""
    scan = scan_tree(REPO_ROOT)
    assert scan.scanned > 100
    assert scan.findings == (), "\n".join(finding.render() for finding in scan.findings)


def test_a_planted_file_in_a_tree_is_found(tmp_path: Path) -> None:
    (tmp_path / "settings.env").write_text(f"MARKETING_AI_POSTGRES_DSN={_DSN}\n", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\0" + _PEM.encode())
    scan = scan_tree(tmp_path, files=sorted(tmp_path.iterdir()))
    assert [(f.path, f.rule) for f in scan.findings] == [("settings.env", "url-with-password")]
    assert scan.skipped == ("image.png",)


def test_parse_history_numbers_added_lines_per_file() -> None:
    patch = "\n".join(
        [
            "commit abc123",
            "diff --git a/x.py b/x.py",
            "--- a/x.py",
            "+++ b/x.py",
            "@@ -0,0 +10,2 @@",
            "+first",
            "+second",
            "commit def456",
            "+++ /dev/null",
            "+ignored: a deleted file has no b/ path",
        ]
    )
    assert list(parse_history(patch)) == [("abc123", "x.py", 10, "first"), ("abc123", "x.py", 11, "second")]


@NEEDS_GIT
def test_a_secret_deleted_in_a_later_commit_is_still_found_in_history(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "drill@example.invalid")
    git("config", "user.name", "drill")
    (tmp_path / "settings.env").write_text(f"MARKETING_AI_POSTGRES_DSN={_DSN}\n", encoding="utf-8")
    git("add", "settings.env")
    git("commit", "-q", "-m", "oops")
    (tmp_path / "settings.env").write_text(
        "MARKETING_AI_POSTGRES_DSN=${FROM_SECRETS_MANAGER}\n", encoding="utf-8"
    )
    git("commit", "-q", "-am", "remove it")

    assert scan_tree(tmp_path).findings == ()
    history = scan_history(tmp_path)
    assert [(f.rule, f.path, f.line) for f in history] == [("url-with-password", "settings.env", 1)]
    assert history[0].commit is not None
