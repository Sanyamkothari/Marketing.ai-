"""The M50 documents name only commands that exist, and the checklist cannot hold an unsourced figure.

`docs/M50_CHECKLIST.md` is read on the one day an AWS account first exists, by somebody following it
line by line; `docs/AWS_DEPLOYMENT.md` is what it points into. The walk that produced both (M50)
found the guide's two worst defects were of exactly this kind: a workflow step invoking a module
that did not exist (`scripts.smoke_deployment`), and commands that could not do what the text said.
So three mechanical checks, in the spirit of `tests/unit/test_docs_honesty.py` (DEC-399), whose
helpers this module reuses rather than copies:

* every `make` target and every `MARKETING_AI_*` variable the checklist names is real - the guide
  is already held to that by the honesty test;
* every `python -m scripts.<name>` either document tells the reader to run is a module in
  `scripts/`. A module another workstream is still writing is listed in `PENDING_SCRIPTS`, and the
  list cleans itself: an entry whose module has landed fails the test until it is removed;
* every `--flag` a document passes to one of those modules is an option that module's parser
  declares. The modules the guide drives on a deployment (`create_user`, `run_retention`) are
  written by another workstream and their command lines can move under the guide; a renamed flag
  would otherwise surface as a usage error inside a one-off ECS task on the day of the deployment;
* a result recorded in the checklist's cost table carries a date, and a currency figure anywhere in
  the checklist is on a line that says where it came from.
"""

from __future__ import annotations

import importlib
import io
import re
from contextlib import redirect_stderr, redirect_stdout, suppress
from pathlib import Path

from engine.settings import ENV_VARS, NON_FIELD_ENV_VARS
from tests.unit.test_docs_honesty import (
    CITATION,
    CURRENCY,
    ENV_VAR,
    MAKE_TARGET,
    _cells,
    _code_fragments,
    _makefile_targets,
    _table_after,
)

CHECKLIST = "docs/M50_CHECKLIST.md"
DOCUMENTS: tuple[str, ...] = ("docs/AWS_DEPLOYMENT.md", CHECKLIST)

SCRIPT_MODULE = re.compile(r"python3? -m scripts\.([a-z_][a-z0-9_]*)")

PENDING_SCRIPTS: frozenset[str] = frozenset()
"""Modules the guide names that another workstream is still writing in parallel with M50.

Empty now: `fire_schedule` (the scheduled-job container's command, `infra/operations.py`
`FIRE_SCHEDULE_COMMAND`) was the one entry, and it has landed. Add an entry only while its module is
genuinely in flight; remove it when the module lands - the last test below insists."""

SCRIPT_INVOCATION = re.compile(r"(?=python3? -m scripts\.([a-z_][a-z0-9_]*)([^|'`\n]*))")
"""A module and what follows it on the line, up to a pipe, a closing quote or the end of the line.

A lookahead, so the matches overlap: `run_in_deployment ... -- python -m scripts.run_retention
--apply` yields both modules, each with the rest of the line from its own name on."""

FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)")

NOT_RUN = "not yet run"
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _text(repo_root: Path, relative: str) -> str:
    return (repo_root / relative).read_text(encoding="utf-8")


def test_every_make_target_the_checklist_names_exists(repo_root: Path) -> None:
    targets = _makefile_targets(repo_root)
    named = {
        target
        for fragment in _code_fragments(_text(repo_root, CHECKLIST))
        for target in MAKE_TARGET.findall(fragment)
    }
    missing = sorted(named - targets)
    assert not missing, f"{CHECKLIST} names make targets the Makefile lacks: {missing}"


def test_every_variable_the_checklist_names_is_real(repo_root: Path) -> None:
    known = set(ENV_VARS.values()) | set(NON_FIELD_ENV_VARS)
    unknown = sorted(set(ENV_VAR.findall(_text(repo_root, CHECKLIST))) - known)
    assert not unknown, f"{CHECKLIST} names {unknown}, which the application would silently ignore."


def test_every_script_module_named_exists(repo_root: Path) -> None:
    for relative in DOCUMENTS:
        named = set(SCRIPT_MODULE.findall(_text(repo_root, relative)))
        missing = sorted(
            name for name in named - PENDING_SCRIPTS if not (repo_root / "scripts" / f"{name}.py").is_file()
        )
        assert not missing, (
            f"{relative} tells the reader to run `python -m scripts.<name>` for {missing}, and there is no "
            "such module - the defect that failed every deploy-dev run before M50."
        )


def _declared_options(module_name: str) -> set[str]:
    """Every option the module's parser declares, read from its own `--help`, produced in-process."""
    module = importlib.import_module(f"scripts.{module_name}")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err), suppress(SystemExit):
        module.main(["--help"])
    return set(FLAG.findall(out.getvalue()))


def _documented_invocations(repo_root: Path) -> dict[str, set[str]]:
    """`{module: {flag, ...}}` for every `python -m scripts.<module> ...` in a code fragment of either document.

    A backslash-continued line is joined first. Flags after a bare ` -- ` belong to the command
    `run_in_deployment` starts, which is matched on its own as the next invocation on the line.
    """
    found: dict[str, set[str]] = {}
    for relative in DOCUMENTS:
        text = _text(repo_root, relative).replace("\\\n", " ")
        for fragment in _code_fragments(text):
            for module, rest in SCRIPT_INVOCATION.findall(fragment):
                own = re.split(r"\s--(?:\s|$)", rest, maxsplit=1)[0]
                found.setdefault(module, set()).update(FLAG.findall(own))
    return found


def test_every_flag_the_documents_pass_is_declared(repo_root: Path) -> None:
    invocations = _documented_invocations(repo_root)
    assert "create_user" in invocations and "run_retention" in invocations, "the scan found nothing"
    undeclared = {
        module: sorted(flag for flag in flags if flag not in _declared_options(module))
        for module, flags in sorted(invocations.items())
        if module not in PENDING_SCRIPTS
    }
    undeclared = {module: flags for module, flags in undeclared.items() if flags}
    assert not undeclared, (
        f"the documents pass options these modules do not declare: {undeclared}; a reader following "
        "them gets a usage error, on a deployment inside a one-off task."
    )


def test_the_pending_list_cleans_itself(repo_root: Path) -> None:
    landed = sorted(name for name in PENDING_SCRIPTS if (repo_root / "scripts" / f"{name}.py").is_file())
    message = f"{landed} now exist; remove them from PENDING_SCRIPTS so they are checked like the rest."
    assert not landed, message


def test_a_recorded_cost_carries_its_date(repo_root: Path) -> None:
    rows = _table_after(_text(repo_root, CHECKLIST), "### 9.5 Costs, before they move to the guide")
    assert rows, "the checklist's cost table moved or was renamed"
    undated = [row[0] for row in rows if row[1] != NOT_RUN and not DATE.search(row[2])]
    assert not undated, f"{CHECKLIST}: a cost was recorded without the date it was measured: {undated}"


def test_no_unsourced_currency_figure_in_the_checklist(repo_root: Path) -> None:
    lines = _text(repo_root, CHECKLIST).splitlines()
    unsourced = [line.strip() for line in lines if CURRENCY.search(line) and not CITATION.search(line)]
    assert not unsourced, f"{CHECKLIST} states a currency figure with no date or source: {unsourced}"


def test_no_result_cell_is_blank(repo_root: Path) -> None:
    """Every results table has rows, and each row is either not yet run or filled in - never blank."""
    text = _text(repo_root, CHECKLIST)
    section = text.split("## 9. Results", 1)[1]
    rows = [_cells(line) for line in section.splitlines() if _cells(line)]
    data = [row for row in rows if not all(cell and set(cell) <= {"-", ":"} for cell in row)]
    assert data, "the results tables are missing"
    blank = [row[0] for row in data if len(row) > 1 and not row[1]]
    assert not blank, f"{CHECKLIST}: a result cell is empty rather than '{NOT_RUN}' or a result: {blank}"
