"""No module reaches a language-model provider without going through the consent gate (`engine.ai_consent`).

The gate is :class:`engine.ai_consent.ConsentGatedClient`, handed out by :func:`engine.ai_service.resolve_client`
with a required scope (a client's data, or an explicit "no client data"). Four rules keep it the only way in:

1. only the provider modules (and the gate, and the `Meter`) call ``complete``/``embed``/``count_tokens`` on a client;
2. only the provider modules build or name a provider client (`build_client`, `BedrockLLMClient`, ...);
3. every call of `resolve_client` / `resolve_slot` passes `consent=`;
4. a `Meter` / `AiGuard` is built over a resolved client (``ai.client``), never a client made on the spot.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCES = ("engine", "api")
PROVIDER_MODULES = {"engine/llm.py", "engine/llm_http.py", "engine/ai_service.py", "engine/ai_consent.py"}
METER_MODULES = PROVIDER_MODULES | {"engine/generative/budget.py"}
CLIENT_CALLS = {"complete", "embed", "count_tokens"}
PROVIDER_NAMES = {
    "build_client",
    "build_service_client",
    "BedrockLLMClient",
    "AnthropicClient",
    "OpenAICompatibleClient",
    "LocalEmbeddingClient",
    "FakeLLMClient",
}
RESOLVERS = {"resolve_client", "resolve_slot"}
METERS = {"Meter", "AiGuard"}


def _receiver(node: ast.expr) -> str:
    """The last name of the object a method is called on: ``self.meter`` and ``meter`` are both ``meter``."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def violations(path: str, source: str) -> list[str]:
    found: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        where = f"{path}:{getattr(node, 'lineno', 0)}"
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in CLIENT_CALLS
                and path not in METER_MODULES
                and not _receiver(func.value).endswith("meter")
            ):
                found.append(f"{where}: .{func.attr}() on a client that did not come through a Meter")
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else ""
            )
            if (
                name in RESOLVERS
                and path != "engine/ai_service.py"
                and not any(keyword.arg == "consent" for keyword in node.keywords)
            ):
                found.append(f"{where}: {name}() without consent=")
            if name in METERS and node.args:
                first = node.args[0]
                if not (isinstance(first, ast.Attribute) and first.attr == "client"):
                    found.append(f"{where}: {name}() over a client that is not a resolved one (ai.client)")
        if path not in PROVIDER_MODULES:
            named = (
                node.id
                if isinstance(node, ast.Name)
                else (
                    node.attr
                    if isinstance(node, ast.Attribute)
                    else node.name if isinstance(node, ast.alias) else ""
                )
            )
            if named in PROVIDER_NAMES:
                found.append(
                    f"{where}: {named} is for the provider modules; use engine.ai_service.resolve_client"
                )
    return found


def test_the_checker_flags_each_way_round_the_gate() -> None:
    bad = (
        "from engine.llm import build_client\n"
        "client = build_client(cfg)\n"
        "client.complete('x')\n"
        "ai = resolve_slot(llm, slot='product', storage=s, settings=t)\n"
        "m = Meter(client, job_id='j', llm=llm, budget=b)\n"
    )
    found = violations("engine/new_module.py", bad)
    assert len(found) >= 5, found
    good = (
        "ai = resolve_slot(llm, slot='product', storage=s, settings=t, consent=ClientData('default'))\n"
        "meter = Meter(ai.client, job_id='j', llm=llm, budget=b)\n"
        "meter.complete(rendered, purpose)\n"
    )
    assert violations("engine/new_module.py", good) == []


def test_no_module_calls_a_provider_without_the_consent_gate() -> None:
    found: list[str] = []
    for top in SOURCES:
        for file in sorted((ROOT / top).rglob("*.py")):
            relative = file.relative_to(ROOT).as_posix()
            found += violations(relative, file.read_text(encoding="utf-8"))
    assert not found, "these reach a provider around the consent gate:\n" + "\n".join(found)
