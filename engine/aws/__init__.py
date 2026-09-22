"""The AWS implementations of the Phase 1 protocols. Nothing here changes what the engine *does*.

`Storage`, `JobRunner` and `ModelRegistry` were written as protocols in Phase 1 precisely so that
moving to AWS would be a set of new implementations rather than a rewrite (plan section 12). This
package is those implementations: `s3_storage` for artefacts, `sagemaker_jobs` for compute,
`postgres` and `s3_registry` for metadata and models, `secrets` for configuration, `metrics` for
CloudWatch. Every one of them satisfies a protocol that already existed; none of them adds a
required member to one.

**One rule holds for every module in this package: no boto3 import at module scope.** A laptop
checkout does not install the `aws` extra, and `engine.settings`, `engine.storage` and
`engine.registry` must stay importable there. Import boto3 inside the function or the constructor
that needs it, so the cost and the failure both land on the caller who actually asked for AWS
(DEC-306). `tests/integration/test_design_rules.py` asserts this rather than trusting it.
"""

from __future__ import annotations
