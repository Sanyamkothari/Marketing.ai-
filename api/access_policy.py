"""Which role every API route requires, and what the audit trail calls it (Phase 4b M46/M47).

One table, keyed by `(HTTP method, route path template)` exactly as FastAPI spells them, so the
whole access model can be reviewed on one screen and a route without an entry is a red test
(`tests/unit/production/test_route_policies.py`) rather than a route that is quietly open.

Phase 1-4a routes are declared in `LEGACY_POLICIES` here, because their router modules belong to
other workstreams and must not be edited (PARALLEL_WORK_PROTOCOL.md §3). A Phase 4b router declares
its own routes by calling `register(...)` at import time, next to the routes themselves.

`role=None` means public: the route answers without a sign-in (the liveness probe, signing in).
Every mutating route is audited; a GET is audited only when its policy says so (an export, a
privacy access request), because reading is not an event worth a row per poll (DEC-704).

How the legacy table was filled (DEC-716). Every GET is Viewer: seeing a result is what the role is
for. Everything that creates or changes work - uploads, clients, sources, mappings, onboarding specs,
datasets, runs and their cancellation, and every generative action that spends tokens (an index, an
evaluation, a question, a root-cause analysis, campaign copy and its regeneration) - is Analyst.
Approving or promoting a champion and approving a campaign-copy template are Approver, and only
Approver: the person who trained a model is not, by holding Analyst, able to crown it (DEC-703).
The AWS connection's writes and its test are Admin, because they choose whose account is billed.
The GETs that hand out row-level data - scores, campaign messages, any run artefact, a dataset's
sample rows - are `audit_reads`, so the trail records who took a copy of customer data.

`purpose` is the words the refusal and the UI use: "Only an Approver can *approve a champion*."
"""

from __future__ import annotations

import threading
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from engine.access.roles import ROLE_DESCRIPTIONS, Role

__all__ = [
    "LEGACY_POLICIES",
    "MUTATING_METHODS",
    "RoutePolicy",
    "all_policies",
    "policy_for",
    "refusal_message",
    "register",
]

MUTATING_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class RoutePolicy(BaseModel):
    """What one route requires and how it is recorded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Role | None = Field(description="Role required; None means the route is public.")
    action: str = Field(description="Dotted audit action name, e.g. `models.approve`.")
    object_type: str | None = Field(default=None, description="What the path's identifier names.")
    object_param: str | None = Field(default=None, description="Path parameter holding the object id.")
    audit_reads: bool = Field(default=False, description="Audit this route even though it is a read.")
    purpose: str | None = Field(
        default=None,
        description='What the route does, as the end of "Only an Approver can …", e.g. `approve a champion`.',
    )


PolicyKey = tuple[str, str]
"""`(method, path template)`, e.g. `("POST", "/models/{model_id}/approve")`."""


def _policy(
    role: Role | None,
    action: str,
    purpose: str,
    *,
    object_type: str | None = None,
    object_param: str | None = None,
    audit_reads: bool = False,
) -> RoutePolicy:
    return RoutePolicy(
        role=role,
        action=action,
        purpose=purpose,
        object_type=object_type,
        object_param=object_param,
        audit_reads=audit_reads,
    )


_V, _AN, _AP, _AD = Role.VIEWER, Role.ANALYST, Role.APPROVER, Role.ADMIN

LEGACY_POLICIES: Final[dict[PolicyKey, RoutePolicy]] = {
    # --- the probe -------------------------------------------------------------------------------
    ("GET", "/healthz"): _policy(None, "health.read", "check the service is up"),
    # --- Phase 1: catalogue ----------------------------------------------------------------------
    ("GET", "/industries"): _policy(_V, "industries.list", "see the industries"),
    ("GET", "/use-cases/{use_case_id}"): _policy(
        _V, "use_cases.read", "see a use case", object_type="use_case", object_param="use_case_id"
    ),
    ("GET", "/use-cases/{use_case_id}/template.csv"): _policy(
        _V,
        "use_cases.template",
        "download a data template",
        object_type="use_case",
        object_param="use_case_id",
    ),
    ("GET", "/use-cases/{use_case_id}/template_README.md"): _policy(
        _V,
        "use_cases.template_readme",
        "download a template's notes",
        object_type="use_case",
        object_param="use_case_id",
    ),
    # --- Phase 1: uploads, runs, models ----------------------------------------------------------
    ("POST", "/uploads"): _policy(_AN, "uploads.create", "upload data", object_type="upload"),
    ("GET", "/uploads/{upload_id}/profile"): _policy(
        _V, "uploads.profile", "see an upload's profile", object_type="upload", object_param="upload_id"
    ),
    ("POST", "/runs"): _policy(_AN, "runs.create", "start a run", object_type="run"),
    ("GET", "/runs"): _policy(_V, "runs.list", "see runs"),
    ("GET", "/runs/{run_id}"): _policy(
        _V, "runs.read", "see a run", object_type="run", object_param="run_id"
    ),
    ("GET", "/runs/{run_id}/artefacts/{name}"): _policy(
        _V,
        "runs.artefact_download",
        "download a run artefact",
        object_type="run",
        object_param="run_id",
        audit_reads=True,
    ),
    ("GET", "/runs/{run_id}/scores.csv"): _policy(
        _V,
        "runs.scores_download",
        "download scores",
        object_type="run",
        object_param="run_id",
        audit_reads=True,
    ),
    ("POST", "/runs/{run_id}/cancel"): _policy(
        _AN, "runs.cancel", "cancel a run", object_type="run", object_param="run_id"
    ),
    ("GET", "/models"): _policy(_V, "models.list", "see models"),
    ("POST", "/models/{model_id}/approve"): _policy(
        _AP, "models.approve", "approve a champion", object_type="model", object_param="model_id"
    ),
    ("POST", "/models/{model_id}/promote"): _policy(
        _AP, "models.promote", "promote a model to champion", object_type="model", object_param="model_id"
    ),
    # --- Phase 2: onboarding ---------------------------------------------------------------------
    ("POST", "/clients"): _policy(_AN, "clients.create", "add a client", object_type="client"),
    ("GET", "/clients"): _policy(_V, "clients.list", "see clients"),
    ("GET", "/clients/{client_id}"): _policy(
        _V, "clients.read", "see a client", object_type="client", object_param="client_id"
    ),
    ("GET", "/use-cases/{use_case_id}/standard-schema"): _policy(
        _V,
        "use_cases.standard_schema",
        "see a use case's standard schema",
        object_type="use_case",
        object_param="use_case_id",
    ),
    ("POST", "/clients/{client_id}/sources"): _policy(
        _AN, "sources.create", "add a data source", object_type="client", object_param="client_id"
    ),
    ("GET", "/clients/{client_id}/sources"): _policy(
        _V, "sources.list", "see a client's data sources", object_type="client", object_param="client_id"
    ),
    ("PATCH", "/clients/{client_id}/sources/{source_id}"): _policy(
        _AN, "sources.update", "change a data source", object_type="source", object_param="source_id"
    ),
    ("DELETE", "/clients/{client_id}/sources/{source_id}"): _policy(
        _AN, "sources.delete", "remove a data source", object_type="source", object_param="source_id"
    ),
    ("POST", "/clients/{client_id}/mappings/suggest"): _policy(
        _AN, "mappings.suggest", "suggest a mapping", object_type="client", object_param="client_id"
    ),
    ("PUT", "/clients/{client_id}/mappings/{mapping_id}"): _policy(
        _AN, "mappings.save", "save a mapping", object_type="mapping", object_param="mapping_id"
    ),
    ("GET", "/clients/{client_id}/mappings"): _policy(
        _V, "mappings.list", "see a client's mappings", object_type="client", object_param="client_id"
    ),
    ("POST", "/clients/{client_id}/onboarding-specs"): _policy(
        _AN,
        "onboarding_specs.create",
        "save an onboarding spec",
        object_type="client",
        object_param="client_id",
    ),
    ("GET", "/clients/{client_id}/onboarding-specs"): _policy(
        _V, "onboarding_specs.list", "see onboarding specs", object_type="client", object_param="client_id"
    ),
    ("POST", "/clients/{client_id}/onboarding-specs/{spec_id}/preview"): _policy(
        _AN,
        "onboarding_specs.preview",
        "preview an onboarding spec",
        object_type="onboarding_spec",
        object_param="spec_id",
    ),
    ("POST", "/datasets"): _policy(_AN, "datasets.create", "build a dataset", object_type="dataset"),
    ("GET", "/datasets"): _policy(_V, "datasets.list", "see datasets"),
    ("GET", "/datasets/{dataset_id}"): _policy(
        _V, "datasets.read", "see a dataset", object_type="dataset", object_param="dataset_id"
    ),
    ("GET", "/datasets/{dataset_id}/report"): _policy(
        _V, "datasets.report", "see a dataset's report", object_type="dataset", object_param="dataset_id"
    ),
    ("GET", "/datasets/{dataset_id}/sample"): _policy(
        _V,
        "datasets.sample",
        "see a dataset's sample rows",
        object_type="dataset",
        object_param="dataset_id",
        audit_reads=True,
    ),
    ("GET", "/datasets/{dataset_id}/features.sql"): _policy(
        _V,
        "datasets.features_sql",
        "download a dataset's SQL",
        object_type="dataset",
        object_param="dataset_id",
    ),
    # --- Phase 3a: generative --------------------------------------------------------------------
    ("POST", "/use-cases/{use_case_id}/reference-sets"): _policy(
        _AN,
        "reference_sets.create",
        "add a reference set",
        object_type="use_case",
        object_param="use_case_id",
    ),
    ("POST", "/use-cases/{use_case_id}/indexes"): _policy(
        _AN, "indexes.create", "build a knowledge index", object_type="use_case", object_param="use_case_id"
    ),
    ("GET", "/use-cases/{use_case_id}/indexes"): _policy(
        _V, "indexes.list", "see knowledge indexes", object_type="use_case", object_param="use_case_id"
    ),
    ("GET", "/indexes/{index_id}"): _policy(
        _V, "indexes.read", "see a knowledge index", object_type="index", object_param="index_id"
    ),
    ("POST", "/indexes/{index_id}/evaluate"): _policy(
        _AN, "indexes.evaluate", "evaluate a knowledge index", object_type="index", object_param="index_id"
    ),
    ("POST", "/indexes/{index_id}/ask"): _policy(
        _AN, "indexes.ask", "ask the assistant", object_type="index", object_param="index_id"
    ),
    ("POST", "/runs/{run_id}/root-cause"): _policy(
        _AN, "runs.root_cause", "run a root-cause analysis", object_type="run", object_param="run_id"
    ),
    ("POST", "/runs/{run_id}/campaign-copy"): _policy(
        _AN, "copy.generate", "generate campaign copy", object_type="run", object_param="run_id"
    ),
    ("POST", "/runs/{run_id}/campaign-copy/templates/{template_id}/approve"): _policy(
        _AP, "copy.approve", "approve campaign copy", object_type="copy_template", object_param="template_id"
    ),
    ("POST", "/runs/{run_id}/campaign-copy/templates/{template_id}/regenerate"): _policy(
        _AN,
        "copy.regenerate",
        "regenerate campaign copy",
        object_type="copy_template",
        object_param="template_id",
    ),
    ("GET", "/runs/{run_id}/copy_messages.csv"): _policy(
        _V,
        "copy.messages_download",
        "download campaign messages",
        object_type="run",
        object_param="run_id",
        audit_reads=True,
    ),
    # --- Phase 3a: the AWS connection (settings) -------------------------------------------------
    ("GET", "/connection/aws"): _policy(_V, "settings.aws_connection.read", "see the AWS connection"),
    ("PUT", "/connection/aws"): _policy(
        _AD, "settings.aws_connection.update", "change the AWS connection", object_type="setting"
    ),
    ("DELETE", "/connection/aws"): _policy(
        _AD, "settings.aws_connection.reset", "reset the AWS connection", object_type="setting"
    ),
    ("POST", "/connection/aws/test"): _policy(
        _AD, "settings.aws_connection.test", "test the AWS connection", object_type="setting"
    ),
    # --- Phase 3b: the uplift routes, which merged after this table was written (DEC-801) --------
    # Phase 3b's router is another workstream's file, so its routes are declared here like every
    # other legacy route, by DEC-716's rules. Plan A M35's three routes are registered next to the
    # routes themselves (`api/routes/clients.py`, `api/routes/datasets.py`).
    ("GET", "/uploads/{upload_id}/treatment-candidates"): _policy(
        _V,
        "uploads.treatment_candidates",
        "see an upload's treatment columns",
        object_type="upload",
        object_param="upload_id",
    ),
    ("POST", "/uplift/runs"): _policy(_AN, "uplift.runs_create", "start an uplift run", object_type="run"),
    ("GET", "/runs/{run_id}/uplift/{name}"): _policy(
        _V,
        "uplift.artefact_download",
        "download an uplift artefact",
        object_type="run",
        object_param="run_id",
        audit_reads=True,
    ),
    ("POST", "/runs/{run_id}/campaign-results"): _policy(
        _AN,
        "uplift.campaign_results",
        "measure a campaign's results",
        object_type="run",
        object_param="run_id",
    ),
    ("GET", "/runs/{run_id}/campaign-results"): _policy(
        _V,
        "uplift.campaign_results_read",
        "see a campaign's results",
        object_type="run",
        object_param="run_id",
    ),
    ("POST", "/runs/{run_id}/uplift/ope"): _policy(
        _AN, "uplift.ope", "evaluate a targeting policy", object_type="run", object_param="run_id"
    ),
}
"""Every route that existed before Phase 4b (DEC-716), and Phase 3b's, which merged after it (DEC-801)."""

_ARTICLE: Final[dict[Role, str]] = {
    Role.VIEWER: "a",
    Role.ANALYST: "an",
    Role.APPROVER: "an",
    Role.ADMIN: "an",
}


def refusal_message(policy: RoutePolicy) -> str:
    """The sentence a refused person reads: "Only an Approver can approve a champion." """
    if policy.role is None:
        return "Anybody can do this."
    name = ROLE_DESCRIPTIONS[policy.role]
    return f"Only {_ARTICLE[policy.role]} {name} can {policy.purpose or 'do this'}."


_REGISTERED: dict[PolicyKey, RoutePolicy] = {}
_LOCK: Final[threading.Lock] = threading.Lock()


def register(policies: dict[PolicyKey, RoutePolicy]) -> None:
    """Declare the policies of a Phase 4b router. A key declared twice with a different policy is an error."""
    with _LOCK:
        for key, policy in policies.items():
            existing = _REGISTERED.get(key) or LEGACY_POLICIES.get(key)
            if existing is not None and existing != policy:
                raise ValueError(f"route {key} already has a different access policy")
            _REGISTERED[key] = policy


def policy_for(method: str, path: str) -> RoutePolicy | None:
    """The policy of one route, or None when the route declares none (which the enforcement refuses)."""
    key = (method.upper(), path)
    return _REGISTERED.get(key) or LEGACY_POLICIES.get(key)


def all_policies() -> dict[PolicyKey, RoutePolicy]:
    """Every declared policy, legacy and registered."""
    with _LOCK:
        return {**LEGACY_POLICIES, **_REGISTERED}
