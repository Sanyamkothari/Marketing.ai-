"""Scan the working tree and the git history for committed secrets (M52 security review).

Why this script exists
----------------------
`PARALLEL_WORK_PROTOCOL.md` rule 5 says "never commit keys, account ids, bucket names or ARNs", and
Phase 4a moved every secret behind `engine/settings.py` (Secrets Manager, SSM). Nothing *checked*
that rule. M52's security review asks for a secrets scan of the tree and the history, and a rule
that is only written down is a rule that is eventually broken by someone pasting a connection
string into a test.

Why a script and not gitleaks or trufflehog
-------------------------------------------
Both are better scanners, and a deployment's CI should run one of them. Neither is installed here,
neither is a Python package the project's venv can pin, and fetching a binary at test time would
make the fast suite depend on the network. This script is small enough to read in one sitting, has
no dependency beyond the standard library and git, and has a unit test that proves it flags each
kind of secret it claims to - which is the property a scanner most often silently loses. It is the
floor, not the ceiling: `docs/SECURITY_REVIEW.md` recommends gitleaks in CI on account day.

What counts as a finding
------------------------
Each `Rule` is one regex for one kind of credential: AWS access key ids and secret keys, PEM private
keys, GitHub / Slack / Google / Anthropic / OpenAI / Stripe tokens, JWTs, a URL that carries a
password (`scheme://user:password@host`), a quoted literal assigned to a name like `password`
or `api_key`, and an unquoted `.env` / shell line such as `DB_PASSWORD=...` - the shape a leaked
`.env` file has, which the quoted rule cannot see (whole line, upper-case name, no `$` reference).
Three things are **not** findings:

* a value that is visibly a placeholder - it contains `EXAMPLE` (AWS's own documented example keys
  do), or it is a template such as `${VAR}`, `<password>`, `{password}`, `***` or `changeme`;
* a line carrying the marker `secret-scan: allow`, for a deliberately fake credential a test needs
  (a local docker-compose password, a moto fixture). The marker is greppable, so every exception is
  one `git grep` away from review;
* a value whose fingerprint is in `REVIEWED_FAKES`: a fake credential that files this workstream
  does not own already carry (`u:p@h`, the local compose `marketing:marketing`, the tests'
  `hunter2-do-not-log`). They are listed by SHA-256 prefix, never by value, each with the reason it
  was judged fake, so the list documents the review and cannot itself leak anything. A new file
  should use the inline marker instead of growing this list.

A finding never prints the secret: `redact` keeps the first four characters and the length, which is
enough to find the line and not enough to use the key, so the scanner's own output (a CI log) does
not become the leak.

History
-------
`--history` scans every line ever *added* on every ref (`git log --all -p`), because deleting a key
in a later commit does not un-publish it: anyone with a clone has the old blob. A history finding
means rotate the credential, not just delete the line.

Run it with::

    .venv/bin/python -m scripts.scan_secrets              # the tree (tracked + untracked, not ignored)
    .venv/bin/python -m scripts.scan_secrets --history    # the tree and every commit on every ref
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

ALLOW_MARKER: Final[str] = "secret-scan: allow"
"""A line containing this is never reported; see the module docstring for when that is right."""

MAX_FILE_BYTES: Final[int] = 2 * 1024 * 1024
"""Larger files are skipped (and counted): generated data and lockfiles, not hand-written config."""

SKIPPED_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".parquet", ".pkl", ".zip", ".gz", ".whl", ".woff2"}
)

PLACEHOLDER: Final[re.Pattern[str]] = re.compile(
    r"EXAMPLE|^\$\{.*\}$|^\$[A-Z_]+$|^<[^>]*>$|^\{[^}]*\}$|^\*+$|^x+$|^\.\.\.$|^changeme$|^placeholder$"
    r"|^password$|^secret$|^redacted$|^REPLACE",
    re.IGNORECASE,
)
"""A captured value matching this is a placeholder in a doc or a template, not a credential."""


REVIEWED_FAKES: Final[dict[str, str]] = {
    "e2a530e251d36750": "`marketing`: the local docker-compose / CI service-container Postgres password",
    "148de9c5a7a44d19": "`p`: the one-letter password of `u:p@h` DSNs in settings and metadata tests",
    "30c952fab122c3f9": "`pw`: a two-letter DSN password in tests/unit/test_settings.py",
    "545c4652e05586b5": "`hunter2-do-not-log`: the value the redaction tests assert never reaches a log",
    "4f21cdb9cad54751": "`a-wrong-password`: the failed-login case of the Phase 4b auth route tests",
}
"""SHA-256 prefix (16 hex) of a value reviewed as fake -> why. Reviewed 2026-09-23 (M52)."""


def fingerprint(value: str) -> str:
    """The first 16 hex digits of the value's SHA-256: how a reviewed fake is named without its value."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class Rule:
    """One kind of secret. `group` is the capture group holding the value judged and redacted."""

    name: str
    pattern: re.Pattern[str]
    group: int = 0


RULES: Final[tuple[Rule, ...]] = (
    Rule("aws-access-key-id", re.compile(r"\b((?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16})\b"), 1),
    Rule(
        "aws-secret-access-key",
        re.compile(
            r"(?i)aws_?secret_?(?:access_?)?key[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9/+]{40})(?![A-Za-z0-9/+])"
        ),
        1,
    ),
    Rule("private-key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY")),
    Rule(
        "github-token",
        re.compile(r"\b((?:gh[pousr]_[A-Za-z0-9]{36,})|(?:github_pat_[A-Za-z0-9_]{50,}))\b"),
        1,
    ),
    Rule("slack-token", re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})\b"), 1),
    Rule("google-api-key", re.compile(r"\b(AIza[0-9A-Za-z_-]{35})\b"), 1),
    Rule("anthropic-api-key", re.compile(r"\b(sk-ant-[A-Za-z0-9_-]{20,})"), 1),
    Rule("openai-api-key", re.compile(r"\b(sk-(?:proj-)?[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20})\b"), 1),
    Rule("stripe-live-key", re.compile(r"\b((?:sk|rk)_live_[A-Za-z0-9]{20,})\b"), 1),
    Rule("jwt", re.compile(r"\b(eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"), 1),
    Rule(
        "url-with-password",
        re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@\"'`]+:([^\s@/\"'`]+)@[^\s/\"'`]+", re.IGNORECASE),
        1,
    ),
    Rule(
        "assigned-secret",
        re.compile(
            r"(?i)(?<![#-])(?:\b|(?<=_))"
            r"(?:password|passwd|pwd|secret|api_?key|access_?token|auth_?token|client_?secret)\b"
            r"[\"']?\s*[:=]\s*[\"']([^\"'\s]{12,})[\"']"
        ),
        1,
    ),
    Rule(
        "env-assigned-secret",
        re.compile(
            r"^\s*(?:export\s+)?[A-Z][A-Z0-9_]*"
            r"(?:PASSWORD|PASSWD|SECRET|API_KEY|ACCESS_TOKEN|AUTH_TOKEN|PRIVATE_KEY)[A-Z0-9_]*"
            r"\s*=\s*([^\s\"'`$#]{12,})\s*(?:#.*)?$"
        ),
        1,
    ),
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One suspected secret: where it is and which rule matched. The value itself is never kept."""

    rule: str
    path: str
    line: int
    preview: str
    commit: str | None = None

    def render(self) -> str:
        """`path:line: rule preview`, prefixed with the short commit for a history finding."""
        where = f"{self.commit[:12]} " if self.commit else ""
        return f"{where}{self.path}:{self.line}: {self.rule} {self.preview}"


def redact(value: str) -> str:
    """The first four characters and the length: enough to find it, not enough to use it."""
    return f"{value[:4]}…({len(value)} chars)"


def scan_text(text: str, path: str, *, commit: str | None = None, first_line: int = 1) -> list[Finding]:
    """Every finding in `text`, attributed to `path` and numbered from `first_line`."""
    findings: list[Finding] = []
    for offset, line in enumerate(text.splitlines()):
        if ALLOW_MARKER in line:
            continue
        for rule in RULES:
            for match in rule.pattern.finditer(line):
                value = match.group(rule.group)
                if PLACEHOLDER.search(value) or fingerprint(value) in REVIEWED_FAKES:
                    continue
                findings.append(Finding(rule.name, path, first_line + offset, redact(value), commit))
    return findings


def _git(root: Path, *args: str) -> str:
    # A fixed argv and no shell: git is the tool being driven, from PATH as every developer runs it.
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return completed.stdout


def tree_files(root: Path) -> list[Path]:
    """Tracked files plus untracked ones git does not ignore: what the next commit could contain."""
    listed = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted({root / name for name in listed.split("\0") if name})


@dataclass(frozen=True, slots=True)
class TreeScan:
    """The findings of a tree scan and what was skipped, so a clean result can be read as clean."""

    findings: tuple[Finding, ...]
    scanned: int
    skipped: tuple[str, ...]


def scan_tree(root: Path = REPO_ROOT, files: Iterable[Path] | None = None) -> TreeScan:
    """Scan every text file of the tree (or `files`); binaries, big files and media are skipped."""
    findings: list[Finding] = []
    skipped: list[str] = []
    scanned = 0
    for path in files if files is not None else tree_files(root):
        relative = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
        if not path.is_file():
            continue  # deleted in the working tree but still in the index
        if path.suffix.lower() in SKIPPED_SUFFIXES or path.stat().st_size > MAX_FILE_BYTES:
            skipped.append(relative)
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            skipped.append(relative)
            continue
        scanned += 1
        findings.extend(scan_text(data.decode("utf-8", errors="replace"), relative))
    return TreeScan(tuple(findings), scanned, tuple(skipped))


_HUNK: Final[re.Pattern[str]] = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_history(patch: str) -> Iterator[tuple[str, str, int, str]]:
    """`(commit, path, line, text)` for every added line of `git log -p --unified=0` output."""
    commit = path = ""
    line_no = 0
    for raw in patch.splitlines():
        if raw.startswith("commit "):
            commit, path = raw.split()[1], ""
        elif raw.startswith("+++ "):
            target = raw[4:]
            path = target[2:] if target.startswith("b/") else ""
        elif raw.startswith("@@"):
            hunk = _HUNK.match(raw)
            line_no = int(hunk.group(1)) if hunk else 0
        elif raw.startswith("+") and path:
            yield commit, path, line_no, raw[1:]
            line_no += 1


def scan_history(root: Path = REPO_ROOT) -> list[Finding]:
    """Every finding in any line ever added on any ref. Deduplicated per (rule, path, preview)."""
    patch = _git(
        root, "log", "--all", "-p", "--unified=0", "--no-color", "--no-ext-diff", "--format=commit %H"
    )
    seen: set[tuple[str, str, str]] = set()
    findings: list[Finding] = []
    for commit, path, line_no, text in parse_history(patch):
        if Path(path).suffix.lower() in SKIPPED_SUFFIXES:
            continue
        for finding in scan_text(text, path, commit=commit, first_line=line_no):
            key = (finding.rule, finding.path, finding.preview)
            if key not in seen:
                seen.add(key)
                findings.append(finding)
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    """Print every finding; 1 when there is any, 0 when the scan is clean."""
    parser = argparse.ArgumentParser(
        prog="python -m scripts.scan_secrets", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--history", action="store_true", help="also scan every commit on every ref")
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    tree = scan_tree(args.root)
    findings = list(tree.findings)
    print(f"tree: {tree.scanned} files scanned, {len(tree.skipped)} skipped (binary or large)")
    if args.history:
        history = scan_history(args.root)
        print(f"history: {len(history)} distinct findings in added lines")
        findings.extend(history)
    for finding in findings:
        print(finding.render())
    print("clean" if not findings else f"{len(findings)} finding(s): rotate, then remove or mark")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
