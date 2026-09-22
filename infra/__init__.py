"""The CDK application that describes a Marketing AI deployment.

This package is imported by `infra/app.py` and by `tests/infra/`, and by nothing else. Nothing
under `engine/`, `api/` or `scripts/` may import it: the deploy extra pulls jsii and a Node bridge,
and the product must keep installing without them (DEC-364).
"""

from __future__ import annotations
