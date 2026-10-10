"""The audit readout on a real past randomised campaign, through the product's public route (Plan J, DEC-1322).

M103 lets a prospect upload a campaign another tool ran - an **assignment file** (who was in which group,
with the customer details) and an **outcomes file** (what happened) - and read out what it changed
(`POST /campaigns/audit`). Until now that route had only been run on fixtures. The Phase 3 gate asks for
a readout on a real past campaign "if a prospect or Minfy team can supply one (otherwise on Hillstrom in
M110)"; none has been supplied, so this module runs it on Hillstrom's own e-mail test, whose assignment
(who got which e-mail) is exactly such a file.

**What a client does, and so what this does.** The original campaign is split into the two files a client
would export from the tool that ran it, and posted as the route documents:

* the **assignment file**: `customer_id`, `segment` (the e-mail the customer was sent: `Mens E-Mail`,
  `Womens E-Mail`, or `No E-Mail` for the held-back group) and the customer details the file carries
  (`recency`, `history_segment`, `history`, `mens`, `womens`, `zip_code`, `newbie`, `channel`). The details
  stay in the file on purpose: the engine tries to predict who got an e-mail from them, and the claim
  "chosen at random" is **Causal** only when it cannot (`engine.measurement.audit.check_randomness`);
* the **outcomes file**: `customer_id`, `visit`, `conversion`, `spend`, uploaded once and read for each
  outcome.

No model is trained, nothing is split and nothing is tuned: an audit measures the campaign as it ran, on
all 64,000 customers. The route is `POST /uploads` twice, then `POST /campaigns/audit`, then (for the
Value Proof Pack) `PUT /pilot/proof/{id}/value` and `GET /pilot/proof/{id}`.

**The campaigns audited** (`AUDIT_CASES`), each its own `POST /campaigns/audit`, because the route measures
one outcome at a time and, for an amount, one contrast:

1. `conversion`: the two e-mails against no e-mail, on the yes/no outcome. Several offers against one
   shared control is what the route is for (M100/M103).
2. `spend_any`: any e-mail against none, on the amount. The route refuses several offers on an amount
   (`CAMPAIGN_INVALID`: "measure the campaign as a whole on the amount"), so the assignment file is sent
   with a 0/1 `emailed` column in place of `segment` (the three-valued column would be read as two offers,
   and kept beside `emailed` it would give the groups away to the randomness check).
3. `spend_mens` and 4. `spend_womens`: each e-mail against none on the amount, from a file of the two
   groups concerned, which is how an amount per offer can be read today.
5. `spend_offers`: the three-group file on the amount, which is what one would try first. The route
   refuses it, and the refusal is recorded as the result (it is posted last, so the four campaigns above
   keep ids 01 to 04).

**Assumptions** (each printed in the report): the file carries no dates, so the campaign is dated
2008-03-20, the date in the published file's name, only so the engine has a start date and the 14-day
outcome window the file's outcomes cover; no result depends on it. The file's money is 2008 dollars and the
engine's is rupees, so the Pack's value inputs convert at the journey's rate (`library.journey.HILLSTROM`).

**Programme readout.** `POST /campaigns/programme` reads a whole customer base against the universal
hold-out of M92, which only the engine's own scoring runs draw. Hillstrom's file has none, so the readout
does not apply; the module asks the route once and records its refusal (`PROGRAMME_NO_HOLDOUT`) as the
evidence, and produces no programme numbers.

Nothing here is engine code. Campaign ids are pinned (`c_<date>_<prefix><nn>`), the randomness check is
seeded by the engine (`AUDIT_SEED`), and the files are read as they are, so a re-run on the same file
reproduces every number. The numbers are written to `journey.results.json`'s sibling, `audit.results.json`;
`library/audit_report.py` renders them into `run_report.md` and nothing else does.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from library.journey import HILLSTROM, JourneyError, JourneySpec, _app, _sha256

BOOTSTRAP_RESAMPLES: Final = 2000
"""Percentile-bootstrap resamples of an amount's difference in means (seeded by the journey's split seed)."""

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt
    import pandas as pd
    from fastapi.testclient import TestClient


@dataclass(frozen=True)
class AuditCase:
    """One `POST /campaigns/audit`: which outcome, which groups, and how the assignment file is cut."""

    key: str
    """Short id; also the key of the case in the results."""
    title: str
    outcome: str
    kind: str
    """`binary` or `continuous`."""
    kept: tuple[str, ...] | None
    """The `segment` values kept in the assignment file (the control first); None keeps all three."""
    arm_column: str
    """The group column of the assignment file (`segment`, or the 0/1 `emailed` for the whole campaign)."""
    treated: tuple[str, ...] | None
    """The `treated_values` sent with the request; None when the column is a plain 0/1."""
    contrast: str
    """Plain words for what is compared with what."""


@dataclass(frozen=True)
class AuditSpec:
    """Everything dataset-specific about an audit. Nothing in the steps branches on the dataset."""

    dataset: str
    use_case: str
    journey: JourneySpec
    """The dataset's journey spec: columns, levels, exchange rate and contact cost are not stated twice."""
    outcomes_file: tuple[str, ...]
    """Columns of the outcomes file, the key first."""
    cases: tuple[AuditCase, ...]
    treatment_start: str
    """The campaign's start, ISO-8601 with its offset: the file has no dates (an assumption, recorded)."""
    start_note: str
    outcome_window_days: int
    campaign_label: str
    campaign_prefix: str
    """Campaign ids are `c_<run_date>_<prefix><nn>`; 6 hex characters."""
    run_date: str
    """`yyyymmdd` of the pinned campaign ids."""


_MENS, _WOMENS, _NONE = "Mens E-Mail", "Womens E-Mail", "No E-Mail"

AUDIT_CASES: Final[tuple[AuditCase, ...]] = (
    AuditCase(
        key="conversion",
        title="Conversion: each e-mail against no e-mail",
        outcome="conversion",
        kind="binary",
        kept=None,
        arm_column="segment",
        treated=(_MENS, _WOMENS),
        contrast="the men's e-mail and the women's e-mail, each against the group sent nothing",
    ),
    AuditCase(
        key="spend_any",
        title="Spend: any e-mail against no e-mail",
        outcome="spend",
        kind="continuous",
        kept=None,
        arm_column="emailed",
        treated=None,
        contrast="every customer sent an e-mail against the group sent nothing",
    ),
    AuditCase(
        key="spend_mens",
        title="Spend: the men's e-mail against no e-mail",
        outcome="spend",
        kind="continuous",
        kept=(_NONE, _MENS),
        arm_column="segment",
        treated=(_MENS,),
        contrast="the men's e-mail against the group sent nothing",
    ),
    AuditCase(
        key="spend_womens",
        title="Spend: the women's e-mail against no e-mail",
        outcome="spend",
        kind="continuous",
        kept=(_NONE, _WOMENS),
        arm_column="segment",
        treated=(_WOMENS,),
        contrast="the women's e-mail against the group sent nothing",
    ),
    AuditCase(
        key="spend_offers",
        title="Spend: each e-mail against no e-mail, in one audit",
        outcome="spend",
        kind="continuous",
        kept=None,
        arm_column="segment",
        treated=(_MENS, _WOMENS),
        contrast="the men's and the women's e-mail, each against the group sent nothing",
    ),
)

HILLSTROM_AUDIT: Final = AuditSpec(
    dataset="hillstrom-email",
    use_case="hillstrom-email",
    journey=HILLSTROM,
    outcomes_file=("customer_id", "visit", "conversion", "spend"),
    cases=AUDIT_CASES,
    treatment_start="2008-03-20T00:00:00Z",
    start_note=(
        "The file carries no date. The campaign is dated 2008-03-20, the date in the published file's name "
        "(`...DataMiningChallenge_2008.03.20.csv`), only so the engine has a start date and a 14-day window "
        "(the file's outcomes cover two weeks). Nothing but the Pack's printed dates depends on it."
    ),
    outcome_window_days=14,
    campaign_label="public dataset, retrospective audit",
    campaign_prefix="112000",
    run_date="20261010",
)

AUDITS: Final[dict[str, AuditSpec]] = {HILLSTROM_AUDIT.dataset: HILLSTROM_AUDIT}


@dataclass
class AuditOutcome:
    results: dict[str, Any]
    directory: Path


# ---------------------------------------------------------------------------
# The two files a client would send
# ---------------------------------------------------------------------------
def assignment_file(frame: pd.DataFrame, spec: AuditSpec, case: AuditCase) -> pd.DataFrame:
    """The assignment file of `case`: the id, the group column and the customer details; no outcome.

    The outcomes are never in it, so the randomness check cannot use them; the group column of a whole-
    campaign audit is 0/1 and `segment` is left out (it would give the groups away).
    """
    journey = spec.journey
    details = [c for c in frame.columns if c != journey.primary_key and c not in spec.outcomes_file]
    details = [c for c in details if c != journey.treatment]
    rows = frame
    if case.kept is not None:
        rows = frame[frame[journey.treatment].isin(case.kept)]
    out = rows[[journey.primary_key]].copy()
    if case.arm_column == journey.treatment:
        out[journey.treatment] = rows[journey.treatment].to_numpy()
    else:
        out[case.arm_column] = (rows[journey.treatment] != journey.levels[0]).astype(int).to_numpy()
    for column in details:
        out[column] = rows[column].to_numpy()
    return out.reset_index(drop=True)


def outcomes_file(frame: pd.DataFrame, spec: AuditSpec) -> pd.DataFrame:
    """The outcomes file: the id and what happened in the two weeks after."""
    return frame[list(spec.outcomes_file)].copy()


def bootstrap_mean_difference(
    treated: npt.NDArray[np.float64],
    control: npt.NDArray[np.float64],
    *,
    samples: int,
    seed: int,
) -> dict[str, float]:
    """Percentile 95% interval of `mean(treated) - mean(control)` from `samples` seeded resamples of each group.

    A check beside the engine's Welch interval, for an amount with a few very large values (the engine
    flags it `OUTCOME_SKEWED`). The groups are resampled apart, each to its own size, by
    `numpy.random.default_rng(seed)`; it is the harness's, not the engine's, and is labelled so wherever
    it is printed.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    draws = np.empty(samples, dtype=np.float64)
    for i in range(samples):
        draws[i] = (
            treated[rng.integers(0, len(treated), size=len(treated))].mean()
            - control[rng.integers(0, len(control), size=len(control))].mean()
        )
    return {
        "estimate": float(treated.mean() - control.mean()),
        "ci_low": float(np.percentile(draws, 2.5)),
        "ci_high": float(np.percentile(draws, 97.5)),
        "resamples": samples,
        "seed": seed,
    }


def _spend_bootstrap(frame: pd.DataFrame, spec: AuditSpec, case: AuditCase) -> dict[str, float]:
    """The bootstrap interval of the contrast of `case` on the raw amounts of `frame`."""
    journey = spec.journey
    column = journey.treatment
    rows = frame if case.kept is None else frame[frame[column].isin(case.kept)]
    held = rows[column] == journey.levels[0]
    amounts = rows[case.outcome].to_numpy(dtype="float64")
    return bootstrap_mean_difference(
        amounts[~held.to_numpy()],
        amounts[held.to_numpy()],
        samples=BOOTSTRAP_RESAMPLES,
        seed=journey.split_seed,
    )


def value_per_conversion(frame: pd.DataFrame, spec: AuditSpec) -> dict[str, Any]:
    """What one conversion is worth for the Pack, in rupees: the file's mean spend of a buyer at the stated rate."""
    journey = spec.journey
    buyers = frame[frame[journey.target] == 1]
    mean_order = float(buyers[journey.amount].mean())
    return {
        "buyers": len(buyers),
        "mean_order_usd": mean_order,
        "fx_inr_per_usd": journey.fx_inr_per_usd,
        "fx_note": journey.fx_note,
        "value_per_conversion_inr": mean_order * journey.fx_inr_per_usd,
        "basis": "the mean spend of a customer who bought, over the whole file, at the assumed rate; revenue before margin",
    }


# ---------------------------------------------------------------------------
# Driving the route
# ---------------------------------------------------------------------------
@contextmanager
def _pinned_campaign_ids(prefix: str, run_date: str) -> Iterator[None]:
    """`POST /campaigns/audit` creates its campaigns under `c_<run_date>_<prefix><nn>`, in order.

    The route draws its id from `new_campaign_id` (a random 8-hex suffix); pinning it is how the journey
    pins run ids, and makes the stored campaign directory the same on every run.
    """
    from api.routes import campaigns as routes

    names = vars(routes)
    original = names["new_campaign_id"]
    count = 0

    def pinned(_moment: Any = None) -> str:
        nonlocal count
        count += 1
        return f"c_{run_date}_{prefix}{count:02d}"

    names["new_campaign_id"] = pinned
    try:
        yield
    finally:
        names["new_campaign_id"] = original


def _upload(client: TestClient, spec: AuditSpec, frame: pd.DataFrame, name: str) -> str:
    payload = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
    response = client.post(
        "/uploads",
        files={"file": (f"{name}.csv", payload, "text/csv")},
        data={"use_case": spec.use_case, "mode": "score"},
    )
    if response.status_code != 201:
        raise JourneyError(f"POST /uploads ({name}): {response.status_code} {response.text[:1500]}")
    return str(response.json()["upload_id"])


def _audit_body(
    spec: AuditSpec, case: AuditCase, *, assignment_id: str, outcomes_id: str, name: str
) -> dict[str, Any]:
    journey = spec.journey
    assignment: dict[str, Any] = {"upload_id": assignment_id, "arm_column": case.arm_column}
    if case.treated is not None:
        assignment["control_value"] = journey.levels[0]
        assignment["treated_values"] = list(case.treated)
    outcomes: dict[str, Any] = {
        "upload_id": outcomes_id,
        "outcome_column": case.outcome,
        "outcome_kind": case.kind,
    }
    if case.kind == "binary":
        outcomes["positive_label"] = "1"
    return {
        "name": name,
        "primary_key": journey.primary_key,
        "assignment": assignment,
        "outcomes": outcomes,
        "assignment_basis": "random",
        "treatment_start": spec.treatment_start,
        "outcome_window_days": spec.outcome_window_days,
        "outcome_is_good": True,
    }


def _pack(
    client: TestClient,
    data_dir: Path,
    directory: Path,
    spec: AuditSpec,
    case: AuditCase,
    campaign_id: str,
    value: dict[str, Any],
) -> dict[str, Any]:
    """Enter the Pack's value inputs, build it as JSON, HTML and PDF, and re-verify its provenance."""
    from engine.pilot.proof import ProofView, verify_provenance
    from engine.storage import LocalStorage

    journey = spec.journey
    if case.kind == "binary":
        inputs = {
            "value_per_outcome": round(value["value_per_conversion_inr"], 2),
            "outcome_is_good": True,
            "value_basis": "Mean spend of a buying customer in the file, in rupees at the assumed rate; revenue before margin",
            "contact_cost": journey.contact_cost_inr,
        }
    else:
        inputs = {
            "value_per_outcome": journey.fx_inr_per_usd,
            "outcome_is_good": True,
            "value_basis": "One dollar of spend, in rupees at the assumed rate; revenue before margin",
            "contact_cost": journey.contact_cost_inr,
        }
    put = client.put(f"/pilot/proof/{campaign_id}/value", json=inputs)
    if put.status_code != 200:
        raise JourneyError(f"PUT /pilot/proof/{campaign_id}/value: {put.status_code} {put.text[:1500]}")
    proof_json = client.get(f"/pilot/proof/{campaign_id}", params={"format": "json"})
    pack: dict[str, Any] = {"status": proof_json.status_code, "value_inputs": inputs}
    if proof_json.status_code != 200:
        pack["refusal"] = proof_json.json()
        return pack
    view = ProofView.model_validate_json(proof_json.content)
    verify_provenance(view, LocalStorage(data_dir))  # raises when any figure does not resolve
    html = client.get(f"/pilot/proof/{campaign_id}", params={"format": "html"})
    pdf = client.get(f"/pilot/proof/{campaign_id}", params={"format": "pdf"})
    (directory / f"proof_{case.key}.html").write_bytes(html.content)
    (directory / f"proof_{case.key}.pdf").write_bytes(pdf.content)
    pack.update(
        {
            "provenance_verified": True,
            "html_status": html.status_code,
            "pdf_status": pdf.status_code,
            "pdf_bytes": len(pdf.content),
            "view": json.loads(proof_json.content),
        }
    )
    return pack


def _programme(client: TestClient, spec: AuditSpec, outcomes_id: str) -> dict[str, Any]:
    """Ask the programme route once. It needs the universal hold-out of M92; Hillstrom's file has none."""
    journey = spec.journey
    response = client.post(
        "/campaigns/programme",
        json={
            "period": {"start": spec.treatment_start[:10], "end": "2008-04-03"},
            "primary_key": journey.primary_key,
            "outcome": {
                "upload_id": outcomes_id,
                "outcome_column": journey.target,
                "positive_label": "1",
                "outcome_kind": "binary",
            },
            "name": f"Hillstrom e-mail programme question - {spec.campaign_label}",
        },
    )
    body = response.json() if response.content else {}
    detail = body.get("detail") if isinstance(body, dict) else None
    detail = detail if isinstance(detail, dict) else {}
    return {
        "applies": response.status_code == 201,
        "status": response.status_code,
        "code": detail.get("code"),
        "message": detail.get("message"),
    }


def run_audit(spec: AuditSpec, *, csv_path: Path, config_root: Path, runs_dir: Path) -> AuditOutcome:
    """Post every case of `spec` to `POST /campaigns/audit` as a client would; return what each one produced.

    `runs_dir` holds the API's data directory for this audit; the CLI uses `library/.runs/`, a test a
    temporary directory. A case the route refuses is a result (its status and message are recorded), not
    a crash; a refused value input or a Pack that fails provenance raises, as a harness error.
    """
    import pandas as pd

    started = time.perf_counter()
    directory = runs_dir / spec.dataset / "audit"
    data_dir = directory / "data"
    if data_dir.exists():
        import shutil

        shutil.rmtree(data_dir)  # pinned campaign ids: an audit always starts from an empty store
    data_dir.mkdir(parents=True)

    frame = pd.read_csv(csv_path)
    journey = spec.journey
    value = value_per_conversion(frame, spec)
    outcomes = outcomes_file(frame, spec)
    results: dict[str, Any] = {
        "dataset": spec.dataset,
        "use_case": spec.use_case,
        "csv": str(csv_path),
        "csv_sha256": _sha256(csv_path),
        "rows": len(frame),
        "label": spec.campaign_label,
        "treatment_start": spec.treatment_start,
        "start_note": spec.start_note,
        "outcome_window_days": spec.outcome_window_days,
        "groups": {level: int((frame[journey.treatment] == level).sum()) for level in journey.levels},
        "outcomes_file": {"columns": list(outcomes.columns), "rows": len(outcomes)},
        "value": value,
        "contact_cost_inr": journey.contact_cost_inr,
        "cases": {},
    }
    cases: dict[str, Any] = results["cases"]

    with _app(config_root, data_dir) as client, _pinned_campaign_ids(spec.campaign_prefix, spec.run_date):
        outcomes_id = _upload(client, spec, outcomes, "outcomes")
        for case in spec.cases:
            assignment = assignment_file(frame, spec, case)
            assignment_id = _upload(client, spec, assignment, f"assignment_{case.key}")
            name = f"Hillstrom e-mail test ({case.title.lower()}) - {spec.campaign_label}"
            body = _audit_body(spec, case, assignment_id=assignment_id, outcomes_id=outcomes_id, name=name)
            response = client.post("/campaigns/audit", json=body)
            entry: dict[str, Any] = {
                "key": case.key,
                "title": case.title,
                "contrast": case.contrast,
                "name": name,
                "outcome": case.outcome,
                "outcome_kind": case.kind,
                "assignment_columns": list(assignment.columns),
                "assignment_rows": len(assignment),
                "status": response.status_code,
            }
            cases[case.key] = entry
            if response.status_code != 201:
                entry["refusal"] = response.json()
                continue
            view = response.json()
            campaign_id = view["campaign"]["campaign_id"]
            entry.update(
                {
                    "campaign_id": campaign_id,
                    "campaign": view["campaign"],
                    "report": view["report"],
                    "verdict": view["verdict"],
                    "audit": view["audit"],
                }
            )
            if case.kind == "continuous":
                entry["bootstrap"] = _spend_bootstrap(frame, spec, case)
            entry["proof"] = _pack(client, data_dir, directory, spec, case, campaign_id, value)
        results["programme"] = _programme(client, spec, outcomes_id)

    results["wall_clock_seconds"] = round(time.perf_counter() - started, 1)
    return AuditOutcome(results=results, directory=directory)
