# AWS deployment — Phase 4 notes

Phase 1 runs entirely locally. Nothing in this document is built yet: it records what Phase 4 replaces
and what must already be true so that the move is a set of new *implementations*, not a rewrite
(plan §12).

---

## What Phase 4 changes

- **S3 storage** in place of the local filesystem artefact store.
- **SageMaker Training / Processing** for jobs, in place of the in-process thread pool.
- **SageMaker Model Registry** in place of the local model registry.
- **Batch Transform** for scoring.
- **Postgres** metadata in place of SQLite.
- **IAM per tenant**; deployment as an accelerator in the customer's own AWS account.
- **Drift monitoring on a schedule** and retraining triggers.
- **Audit log** and **DPDP controls**: retention, consent, deletion.

The deployment model assumed for Phase 1 is an in-customer AWS account, with the product running
locally until then (plan §14).

## What keeps the swap cheap

Three protocols are the only places that know where anything physically lives. Keeping them clean is
the Phase 4 plan:

| Protocol | Phase 1 | Phase 4 |
|---|---|---|
| `Storage` (`engine/storage.py`) | `LocalStorage` over a directory | `S3Storage` |
| `JobRunner` (`engine/jobs.py`) | `ThreadJobRunner` | SageMaker Processing / Training jobs |
| `ModelRegistry` (`engine/registry.py`) | `LocalModelRegistry` over SQLite | SageMaker Model Registry / Postgres |

Storage keys are opaque posix strings validated by one gate, writes are atomic, and `local_path()` is
the only escape hatch (DEC-016) — so `S3Storage` materialises an object into a temp directory when a
library insists on a real path, and nothing else in the engine changes. Cancellation is cooperative
(DEC-017), which is the same contract a remote job gives: a stop request lands between stages.

`should_promote` is a pure function rather than a registry method (DEC-028), so the champion policy is
tested without a database now and is unchanged when the registry becomes a managed service.

## CORS must be tightened before Phase 4

Phase 1 serves the API with **open CORS** (DEC-024). The UI is the prototype opened as a local file,
with no origin and no auth, so a permissive policy is the only thing that works and nothing is at risk:
there is no authentication, no multi-tenancy and no customer data behind the API on a laptop
(plan §1.3).

That stops being true the moment the API is reachable in an AWS account. Before Phase 4:

- replace the open policy with an explicit allow-list of the UI's origin(s);
- add authentication and per-tenant IAM ahead of, or together with, that change — an allow-list is not
  an authorisation mechanism;
- re-check the two routes DEC-024 added beyond plan §8 (`GET /healthz` and
  `GET /use-cases/{id}/template_README.md`): `/healthz` returns the version and must stay free of any
  other detail.
