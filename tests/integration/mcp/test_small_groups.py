"""Small-group suppression over MCP, and the differencing attack (Plan K M133; design §7.3 "data exfiltration by
aggregation").

A propensity scoring run with a value column, written by the product's own actions stage and value report
(`tests/fixtures/decide/treat_runs.write_run`, M97), has three bands: Low (24 customers), Medium (14) and High
(9). High is a small group; withholding it alone would let the total give it back, so Medium is withheld with
it (Plan I's complementary rule). The test then asks the overlapping questions an attacker would - every band,
then each band on its own - and checks that no answer, and no difference of two answers, isolates a group of
fewer than 10 customers. The run is demo data because this deployment's `mcp.demo_use_cases` names its use case.

A campaign whose totals are thousands but one of whose offers is 5 customers is refused by every tool that reads
it: the Value Proof Pack prints that offer's figures as text, where suppression cannot reach.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.mcp.transport import AsgiTransport
from engine.access.roles import Role
from engine.storage import LocalStorage
from tests.fixtures.decide.treat_runs import USE_CASE, write_run
from tests.integration.mcp.conftest import PASSWORD, Demo, audit_hillstrom, mcp_config_root, signed_in_app

pytestmark = pytest.mark.integration

RUN = "r_20261010_a1330001"
SMALL_OFFER = 5


async def _ask(app: Any, token: str, calls: list[dict[str, Any]]) -> list[Any]:
    async with AsgiTransport(app, token).connect() as client:
        return [await client.call_tool("value_segment", arguments) for arguments in calls]


def test_overlapping_questions_cannot_isolate_a_small_band(config_root: Path, tmp_path: Path) -> None:
    root = mcp_config_root(config_root, tmp_path / "configs", demo_use_cases=["hillstrom-email", USE_CASE])
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_run(LocalStorage(data_dir), RUN, kind="propensity", rows=60, value=True)
    app = signed_in_app(root, data_dir)
    store = app.state.user_store
    user = store.create_user("asha", PASSWORD, roles=[Role.ANALYST], created_by="test").user_id
    token = store.create_session(user).token

    every, low, medium, high = anyio.run(
        _ask,
        app,
        token,
        [
            {"run_id": RUN},
            {"run_id": RUN, "band": "Low"},
            {"run_id": RUN, "band": "Medium"},
            {"run_id": RUN, "band": "High"},
        ],
    )
    full = every.structured_content
    assert [band["band"] for band in full["bands"]] == ["Low"], "High (9) is small; Medium joins it"
    assert every.meta["marketing-ai/withheld"] == {"bands": 2}
    shown_rows = sum(band["rows"] for band in full["bands"])
    assert full["contactable_rows"] - shown_rows >= 10, "what the total gives back is at least 10 customers"

    assert low.structured_content["bands"] == full["bands"] and low.structured_content["total_inr"] is None
    for narrowed in (medium, high):
        out = narrowed.structured_content
        assert (
            out["bands"] == [] and out["total_inr"] is None
        ), "a band withheld in full is withheld when asked alone"
        assert "fewer than 10" in out["reason"]
    # A band that does not exist is answered like a withheld one: the answer says nothing about which it was.
    (missing,) = anyio.run(_ask, app, token, [{"run_id": RUN, "band": "Platinum"}])
    assert missing.structured_content["reason"] == high.structured_content["reason"]


def test_a_campaign_with_a_small_offer_is_refused_by_every_tool_that_reads_it(demo: Demo) -> None:
    """Hillstrom's two e-mails, with the women's e-mail cut to 5 customers: the campaign's totals are thousands,
    but one offer is a small group. The Value Proof Pack prints that offer's figures as text (its offer table,
    and, for a report that does not add the offers together, every whole-campaign line), which suppression
    cannot reach; so `get_proof_pack` is refused, and `measure` and `audit_campaign` answer the same."""
    assert demo.frame is not None
    womens = demo.frame[demo.frame["segment"] == "Womens E-Mail"].head(SMALL_OFFER)
    frame = pd.concat([demo.frame[demo.frame["segment"] != "Womens E-Mail"], womens], ignore_index=True)
    with TestClient(demo.app()) as client:
        campaign_id = audit_hillstrom(client, demo.headers("analyst"), frame, name="A small offer")
        view = client.get(f"/campaigns/{campaign_id}", headers=demo.headers("analyst")).json()
    arms = {arm["arm"]: arm["treated_rows"] for arm in view["report"]["arms"]}
    assert arms["Womens E-Mail"] == SMALL_OFFER and view["campaign"]["counts"]["treated"] > 1_000

    async def ask() -> list[Any]:
        async with AsgiTransport(demo.app(), demo.token("analyst")).connect() as client:
            return [
                await client.call_tool(name, {"campaign_id": campaign_id})
                for name in ("get_proof_pack", "measure", "audit_campaign")
            ]

    for result in anyio.run(ask):
        assert result.is_error is True and result.structured_content is None
        assert result.meta["marketing-ai/code"] == "MCP_GROUP_TOO_SMALL"
