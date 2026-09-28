"""The Guided setup module (`ui/modules/agent/`, Plan G M75) against the API it talks to.

A static test, as `tests/unit/test_onboarding_ui.py` is for the onboarding panel: the module is plain
ES modules with no build step, so what can be checked from Python is that it agrees with the API it
calls - pinned to something outside the module, never to a transcript of it:

* every path `api.js` fetches is a route `api/routes/agent.py` (or the upload routes) declares;
* every request body it builds has exactly the fields its request model declares;
* every enum value it branches on is a real member of the contract's enum;
* no setting path or label from the settings schema is spelled in it (they come from the API);
* nothing is fabricated: no comma-grouped number, no bare number in the markup, no placeholder but
  the em dash;
* the entry point registers through the router's setup-mode seam, and `index.html` loads it in the
  PLAN-G block.

The behaviour itself - what is drawn, sent and filled in - is `test_guided_setup_ui.py`'s, in jsdom.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel

from api.main import create_app
from api.routes import agent as agent_routes
from engine.agent.contracts import (
    AgentConfidence,
    ChatRole,
    ProposalKind,
    ProposalState,
    RecipeStepKind,
    SessionStatus,
)

UI_DIR: Final[Path] = Path(__file__).resolve().parents[3] / "ui"
AGENT_DIR: Final[Path] = UI_DIR / "modules" / "agent"
MODULES: Final[tuple[str, ...]] = ("index.js", "api.js", "guided.js", "styles.js")

LINE_COMMENT: Final[re.Pattern[str]] = re.compile(r"//[^\n]*")
BLOCK_COMMENT: Final[re.Pattern[str]] = re.compile(r"/\*.*?\*/", re.DOTALL)
COMMA_GROUPED_NUMBER: Final[re.Pattern[str]] = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")
INTERPOLATED_NUMBER: Final[re.Pattern[str]] = re.compile(r"\$\{\s*-?\d[\d._]*\s*\}")
PLACEHOLDERS: Final[tuple[str, ...]] = ('"N/A"', "'N/A'", '"TBD"', '">--<"', '"n/a"')
SESSION_PATH: Final[re.Pattern[str]] = re.compile(r'session\(uploadId(?:, "(/[a-z]+)")?\)')
DEMO_ID: Final[str] = "targeted-advertisement"


def read(name: str) -> str:
    return (AGENT_DIR / name).read_text(encoding="utf-8")


def code_of(name: str) -> str:
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", read(name)))


def test_the_module_files_exist() -> None:
    for name in MODULES:
        assert (AGENT_DIR / name).is_file(), name


def test_the_entry_point_registers_a_setup_mode_and_index_html_loads_it() -> None:
    entry = code_of("index.js")
    assert 'from "../router.js"' in entry
    assert "registerSetupMode(" in entry
    router = (UI_DIR / "modules" / "router.js").read_text(encoding="utf-8")
    block = router[router.index("---- PLAN-G (agents)") : router.index("---- END PLAN-G ----")]
    assert "export function registerSetupMode(" in block and "export function setupModes(" in block
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    block = html[html.index("---- PLAN-G (agents)") : html.index("---- END PLAN-G ----")]
    assert '<script type="module" src="./modules/agent/index.js"></script>' in block


def test_relative_imports_resolve_to_a_real_file() -> None:
    for name in MODULES:
        for match in re.finditer(r"""from\s+["'](\.\.?/[A-Za-z0-9_./-]+)["']""", code_of(name)):
            assert (AGENT_DIR / match.group(1)).resolve().is_file(), f"{name} imports {match.group(1)}"


def test_every_session_path_is_a_route_the_agent_router_declares() -> None:
    declared = {
        (route.path, method)
        for route in agent_routes.router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    text = code_of("api.js")
    tails = [match.group(1) or "" for match in SESSION_PATH.finditer(text)]
    assert sorted(set(tails)) == ["", "/answers", "/apply", "/decisions", "/messages", "/preview"]
    for tail in tails:
        path = f"/uploads/{{upload_id}}/agent-session{tail}"
        assert (path, "POST") in declared, path
    assert ("/uploads/{upload_id}/agent-session", "GET") in declared


def _body_keys(text: str, marker: str) -> set[str]:
    """The keys of the object literal passed as `body:` in the call after `marker` (a shorthand
    `{ text }` is the key `text`)."""
    start = text.index(marker)
    body = text[text.index("body:", start) :]
    literal = body[body.index("{") + 1 : body.index("}")]
    return {entry.split(":")[0].strip() for entry in literal.split(",") if entry.strip()}


@pytest.mark.parametrize(
    ("marker", "model"),
    [
        ("export const startSession", agent_routes.SessionStartRequest),
        ("export const postDecisions", agent_routes.DecisionsRequest),
        ("export const postAnswer", agent_routes.AnswerRequest),
        ("export const postMessage", agent_routes.MessageRequest),
    ],
)
def test_every_request_body_has_exactly_its_models_fields(marker: str, model: type[BaseModel]) -> None:
    keys = _body_keys(code_of("api.js"), marker)
    assert keys == set(model.model_fields), (marker, keys)


def test_a_decision_the_screen_sends_is_a_decision_the_api_takes() -> None:
    text = code_of("guided.js")
    literal = re.search(r"\(\{ proposal_id: p\.proposal_id, state: [^}]+\}\)", text)
    assert literal, "the decision literal moved"
    assert {"proposal_id", "state"} <= set(agent_routes.Decision.model_fields)
    for state in re.findall(r'"(accepted|rejected)"', literal.group(0)):
        assert state in {s.value for s in ProposalState}


def test_every_enum_value_the_screen_branches_on_is_a_member() -> None:
    text = code_of("guided.js")
    values = {
        "confidence": {AgentConfidence(v) for v in re.findall(r'confidence === "([a-z_]+)"', text)},
        "state": {ProposalState(v) for v in re.findall(r'state === "([a-z_]+)"', text)},
        "status": {SessionStatus(v) for v in re.findall(r'status === "([a-z_]+)"', text)},
        "role": {ChatRole(v) for v in re.findall(r'role === "([a-z_]+)"', text)},
        "kind": {RecipeStepKind(v) for v in re.findall(r'kind === "([a-z_]+)"', text)},
    }
    assert all(values.values()), values
    kinds = re.search(r"const GROUPS = \[(.*?)\n\];", text, re.DOTALL)
    assert kinds
    grouped = set(re.findall(r'"(role|recipe_step|acknowledgement|setting)"', kinds.group(1)))
    assert grouped == {k.value for k in ProposalKind}, "every proposal kind has a group"


def test_the_module_names_no_advanced_setting() -> None:
    """Setting paths and labels come from the API (a proposal's `path`, `title`), never from here."""
    with TestClient(create_app()) as client:
        schema = client.get(f"/use-cases/{DEMO_ID}").json()["advanced_settings"]
    text = "\n".join(code_of(name) for name in MODULES)
    named = [
        value
        for stage in schema["stages"]
        for field in stage["fields"]
        for value in (field["path"], field["label"])
        if value and f'"{value}"' in text
    ]
    assert not named, f"the Guided setup module hard-codes {named}"


def test_nothing_is_fabricated() -> None:
    for name in MODULES:
        text = code_of(name)
        assert not COMMA_GROUPED_NUMBER.findall(text), name
        assert not INTERPOLATED_NUMBER.findall(text), name
        raw = read(name)
        for placeholder in PLACEHOLDERS:
            assert placeholder not in raw, f"{name} uses {placeholder} instead of the em dash"


def test_session_text_is_only_ever_drawn_escaped() -> None:
    """Every field of the session a person's file could have shaped reaches the page through `esc`."""
    text = code_of("guided.js")
    for field in ("title", "reason", "text", "label", "effect", "stop_reason", "agent_name"):
        for match in re.finditer(rf"\$\{{([^{{}}]*\.{field}\b[^{{}}]*)\}}", text):
            expression = match.group(1)
            assert expression.lstrip().startswith(("esc(", "dash(")) or "esc(" in expression, expression
    assert "innerHTML" in text
    assert text.count("innerHTML =") == 2, "the two elements are drawn in one place (`draw`)"
    for forbidden in ("marked(", "markdown", "insertAdjacentHTML", "outerHTML =", "import("):
        assert forbidden not in text, forbidden
