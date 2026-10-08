"""Channel-aware consent on a real propensity scoring run: the band path (Plan J M99, DEC-1309).

Slow: it reuses the trained AutoGluon champion of `tests/integration/test_score_flow.py` (a real
`Pipeline.run_train`, about a minute) and scores a fresh file with `Pipeline.run_score` under a use case
that configures two channels and points its top band at a catalogue action sent only by SMS. Everything
the treat list reads is what the run wrote, including the catalogue it ran under (`catalogue_stamp.json`:
the run is scored with `MARKETING_AI_CONFIG_DIR` at the test's config root, as a deployment runs). The fast counterpart, on uplift runs and with the consent
ledger, is `test_channel_consent_real_run.py`.
"""

# ruff: noqa: F811, F401 - the imported pytest fixtures are used by name
from __future__ import annotations

import hashlib
import io
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from engine.config import resolve_config
from engine.contracts import RunState
from engine.decide.treat_list import TREAT_LIST_CSV, build_treat_list
from engine.holdout.assign import selection_masks
from engine.storage import run_key
from tests.fixtures.make_data import GenerationSpec, generate
from tests.integration.test_score_flow import (
    OVERRIDES,
    PRIMARY_KEY,
    SCORE_ROWS,
    SCORE_SEED,
    USE_CASE,
    Flow,
    Trained,
    score_file,
    trained,
)

pytestmark = [pytest.mark.slow, pytest.mark.integration]

CATALOGUE = "actions:\n  - {action_id: ad_sms, label: Ad by SMS, channels: [sms], offer_cost: 0.0}\n"


@pytest.fixture(scope="module")
def channel_run(
    trained: Trained, config_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> tuple[Flow, Path]:
    root = tmp_path_factory.mktemp("channels") / "configs"
    shutil.copytree(config_root, root)
    (root / "decide" / "catalogue.yaml").write_text(CATALOGUE, encoding="utf-8")
    path = root / "use_cases" / "targeted_advertisement.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["actions"]["bands"] = [
        {"name": "High", "min_score": 0.30, "action": "Serve ad", "action_id": "ad_sms"},
        {"name": "Medium", "min_score": 0.10, "action": "Retarget"},
        {"name": "Low", "min_score": 0.00, "action": "Suppress"},
    ]
    document["actions"].setdefault("suppression", {})["channels"] = {
        "email": {"consent_column": "email_opt_in"},
        "sms": {"consent_column": "sms_opt_in"},
    }
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    resolved = resolve_config(USE_CASE, OVERRIDES, root=root)
    frame = generate(GenerationSpec(USE_CASE, rows=SCORE_ROWS, variant="scoring", seed=SCORE_SEED))
    position = np.arange(len(frame.index))
    frame["sms_opt_in"] = np.where(position % 3 == 1, "no", "yes")
    frame["email_opt_in"] = np.where(position % 4 == 2, "false", "true")
    with pytest.MonkeyPatch.context() as patch:
        # As a deployment runs: the run reads `decide/catalogue.yaml` from its config root and stamps it.
        patch.setenv("MARKETING_AI_CONFIG_DIR", str(root))
        flow = score_file(trained.storage, trained.registry, resolved, frame, name="channels")
    assert flow.record.state is RunState.DONE, flow.record.error
    return flow, root


def test_each_band_is_sent_on_its_planned_channels_only(channel_run: tuple[Flow, Path]) -> None:
    flow, root = channel_run
    summary = build_treat_list(flow.storage, flow.run_id, config_root=root)
    out = pd.read_csv(
        io.BytesIO(flow.storage.read_bytes(run_key(flow.run_id, TREAT_LIST_CSV))),
        dtype=str,
        keep_default_na=False,
    )
    scores = pd.read_parquet(io.BytesIO(flow.storage.read_bytes(flow.key("scores.parquet"))))
    assert list(out[PRIMARY_KEY]) == [str(value) for value in scores[PRIMARY_KEY]]
    uploaded = flow.uploaded.astype({PRIMARY_KEY: str}).set_index(PRIMARY_KEY).loc[out[PRIMARY_KEY]]
    sms_ok = (uploaded["sms_opt_in"] == "yes").to_numpy()
    email_ok = (uploaded["email_opt_in"] == "true").to_numpy()
    selected, sleeping = selection_masks(scores, flow.resolved.config)
    eligible = (
        scores["suppressed_reason"].isna().to_numpy() & ~scores["control_group"].astype(bool).to_numpy()
    )
    base = selected & eligible & ~sleeping
    high = (scores["band"] == "High").to_numpy()
    medium = (scores["band"] == "Medium").to_numpy()
    treat = (out["treat"] == "1").to_numpy()
    channel = out["channel"].to_numpy(dtype=object)

    # High sends the catalogue's SMS-only action: an SMS opt-out is not treated, and not suppressed.
    assert (treat[high] == (base & sms_ok)[high]).all()
    assert (channel[high & treat] == "sms").all()
    lost = high & base & ~sms_ok
    assert lost.any(), "the top band holds customers the SMS opt-out takes out of treatment"
    assert (out.loc[lost, "suppression_reason"] == "").all()
    # Medium names no catalogue action: the configured channels, email first, then SMS.
    assert (treat[medium] == (base & (email_ok | sms_ok))[medium]).all()
    assert (channel[medium & treat & email_ok] == "email").all()
    assert (channel[medium & treat & ~email_ok] == "sms").all()
    assert (medium & treat & ~email_ok).any()
    assert summary.channel_counts == {
        "email": int((eligible & ~email_ok).sum()),
        "sms": int((eligible & ~sms_ok).sum()),
    }
    assert summary.uncontactable_rows == int(
        (base & ((high & ~sms_ok) | (medium & ~email_ok & ~sms_ok))).sum()
    )
    assert summary.catalogue_sha256 is not None
    assert summary.catalogue_sha256 == hashlib.sha256(CATALOGUE.encode("utf-8")).hexdigest()
    assert summary.catalogue_note is None
    assert flow.storage.exists(run_key(flow.run_id, "catalogue_stamp.json"))
