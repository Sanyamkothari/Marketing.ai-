"""S3 lifecycle rules per client prefix: a backstop behind the retention job, never a replacement.

The retention job (`engine.privacy.retention`) is what enforces `governance.retention_days`: it
dates every item from its own record, keeps what an unfinished run is reading, and deletes
row-level run artefacts while keeping the reports beside them. An S3 lifecycle rule can do none of
that - it knows an object's age in the bucket and its key prefix, nothing else. So these rules are a
**backstop** (DEC-740): they expire what the job should already have deleted, `grace_days` after the
job's own deadline, in case the job has stopped running and nobody noticed. On a healthy deployment
they never fire.

What the rules cover, per client prefix (`Settings.s3_prefix`, which is how a bucket is shared
between clients, DEC-305): `uploads/`, `datasets/` and `clients/<client>/sources/`. Each gets an
expiration of the **longest** `retention_days` any use case configures plus `grace_days` - a prefix
is shared by every use case, so the shortest would delete one use case's data under another's
promise. Every rule also expires *noncurrent* versions after `grace_days`: on a versioned bucket a
delete only adds a delete marker, and without this the erased and expired bytes would live on as old
versions indefinitely. Row-level run artefacts sit under `runs/<id>/` beside the reports that must be
kept, and a lifecycle filter cannot say "scores.csv but not run.json" by prefix, so they are the
job's alone.

**Other rules are preserved.** `put_bucket_lifecycle_configuration` replaces the bucket's whole
configuration, so `apply_lifecycle` reads the current one, keeps every rule whose ID does not start
with this code's prefix (`retention.lifecycle.rule_id_prefix`) - an access-log expiry, an
incomplete-multipart cleanup, anything the infrastructure stack or an operator added - and replaces
only its own. "Its own" is **this client prefix's** rules: a bucket shared between clients carries
one set per `s3_prefix` (`<rule_id_prefix><label>-uploads`, ...), and applying client A's must not
drop client B's (DEC-708). A rule under the prefix whose ID has the shape of another label's rule
is kept; any other rule under the prefix (an older shape) is replaced. Tested with moto
(`tests/unit/production/test_lifecycle.py`); never against real AWS.

Noncurrent versions under `runs/` are not a lifecycle matter here: erasure and retention delete the
old versions of what they rewrite or delete themselves (`engine.privacy.layout.purge_old_versions`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from engine.privacy.config import load_privacy_config
from engine.privacy.errors import PrivacyError
from engine.privacy.retention import retention_days_by_use_case
from engine.utils.logging import get_logger

__all__ = [
    "LifecycleClient",
    "LifecycleResult",
    "LifecycleRule",
    "apply_lifecycle",
    "build_lifecycle_rules",
    "rule_document",
]

_LOGGER = get_logger(__name__)

_NO_CONFIGURATION = "NoSuchLifecycleConfiguration"

_RULE_SHAPE = re.compile(r"(?:(?P<label>.+)-)?(?P<kind>uploads|datasets|sources(?:-.+)?)")
"""What follows `rule_id_prefix` in an ID `build_lifecycle_rules` writes: an optional label, a kind."""


def _label_of(rule_id: str, rule_id_prefix: str) -> str | None:
    """The client-prefix label a rule ID of ours carries (`""` for none); None for another shape."""
    match = _RULE_SHAPE.fullmatch(rule_id[len(rule_id_prefix) :])
    return None if match is None else (match.group("label") or "")


@runtime_checkable
class LifecycleClient(Protocol):
    """The two S3 calls this module makes; a boto3 S3 client satisfies it."""

    def get_bucket_lifecycle_configuration(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def put_bucket_lifecycle_configuration(self, **kwargs: Any) -> Mapping[str, Any]: ...


class LifecycleRule(BaseModel):
    """One rule this code owns."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str = Field(
        description="Starts with the configured prefix; how this code recognises its own rules."
    )
    prefix: str = Field(description="Key prefix the rule applies to.")
    expiration_days: int = Field(ge=1, description="Days after creation that a current object expires.")
    noncurrent_days: int = Field(
        ge=1, description="Days after it stops being current that an old version expires."
    )


class LifecycleResult(BaseModel):
    """What `apply_lifecycle` wrote."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bucket: str
    written: tuple[str, ...] = Field(description="IDs of this code's rules now on the bucket.")
    preserved: tuple[str, ...] = Field(description="IDs of rules this code does not own, kept as they were.")
    replaced: tuple[str, ...] = Field(
        description="IDs of this code's earlier rules that were replaced or dropped."
    )


def build_lifecycle_rules(
    config_root: Path | None = None,
    *,
    s3_prefix: str = "",
    client_ids: Iterable[str] = (),
) -> tuple[LifecycleRule, ...]:
    """The backstop rules for one client prefix, from the retention settings of `config_root`.

    `client_ids` names the onboarding clients whose sources live under `clients/<id>/sources/`; with
    none, one rule covers `clients/` as a whole.
    """
    policy = load_privacy_config(config_root).retention.lifecycle
    by_use_case, default = retention_days_by_use_case(config_root)
    longest = max([default, *by_use_case.values()])
    expire = longest + policy.grace_days
    base = f"{s3_prefix.strip('/')}/" if s3_prefix.strip("/") else ""
    label = s3_prefix.strip("/").replace("/", "-")
    stem = f"{policy.rule_id_prefix}{label + '-' if label else ''}"
    rules = [
        LifecycleRule(
            rule_id=f"{stem}uploads",
            prefix=f"{base}uploads/",
            expiration_days=expire,
            noncurrent_days=policy.grace_days,
        ),
        LifecycleRule(
            rule_id=f"{stem}datasets",
            prefix=f"{base}datasets/",
            expiration_days=expire,
            noncurrent_days=policy.grace_days,
        ),
    ]
    clients = sorted(set(client_ids))
    if clients:
        rules.extend(
            LifecycleRule(
                rule_id=f"{stem}sources-{client_id}"[:255],
                prefix=f"{base}clients/{client_id}/sources/",
                expiration_days=expire,
                noncurrent_days=policy.grace_days,
            )
            for client_id in clients
        )
    else:
        rules.append(
            LifecycleRule(
                rule_id=f"{stem}sources",
                prefix=f"{base}clients/",
                expiration_days=expire,
                noncurrent_days=policy.grace_days,
            )
        )
    return tuple(rules)


def rule_document(rule: LifecycleRule) -> dict[str, Any]:
    """The rule as `put_bucket_lifecycle_configuration` takes it."""
    return {
        "ID": rule.rule_id,
        "Status": "Enabled",
        "Filter": {"Prefix": rule.prefix},
        "Expiration": {"Days": rule.expiration_days},
        "NoncurrentVersionExpiration": {"NoncurrentDays": rule.noncurrent_days},
    }


def apply_lifecycle(
    client: LifecycleClient,
    bucket: str,
    rules: Sequence[LifecycleRule],
    *,
    rule_id_prefix: str,
) -> LifecycleResult:
    """Put `rules` on `bucket`, keeping every rule whose ID does not start with `rule_id_prefix`."""
    existing = _current_rules(client, bucket)
    labels = {_label_of(rule.rule_id, rule_id_prefix) for rule in rules}
    label = next(iter(labels)) if len(labels) == 1 else None

    def owned(rule_id: str) -> bool:
        if not rule_id.startswith(rule_id_prefix):
            return False
        other = _label_of(rule_id, rule_id_prefix)
        return label is None or other is None or other == label  # another client prefix's rule stays

    foreign = [rule for rule in existing if not owned(str(rule.get("ID", "")))]
    ours_before = [str(rule.get("ID", "")) for rule in existing if owned(str(rule.get("ID", "")))]
    documents = [*foreign, *(rule_document(rule) for rule in rules)]
    client.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": documents})
    result = LifecycleResult(
        bucket=bucket,
        written=tuple(rule.rule_id for rule in rules),
        preserved=tuple(str(rule.get("ID", "")) for rule in foreign),
        replaced=tuple(ours_before),
    )
    _LOGGER.info(
        "privacy.lifecycle bucket=%s written=%d preserved=%d replaced=%d",
        bucket,
        len(result.written),
        len(result.preserved),
        len(result.replaced),
    )
    return result


def _current_rules(client: LifecycleClient, bucket: str) -> list[dict[str, Any]]:
    """The bucket's rules today; an empty list when it has no lifecycle configuration at all."""
    try:
        response = client.get_bucket_lifecycle_configuration(Bucket=bucket)
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code == _NO_CONFIGURATION:
            return []
        raise PrivacyError(
            "LIFECYCLE_READ_FAILED",
            f"The lifecycle configuration of the bucket could not be read ({code or type(exc).__name__}); "
            "nothing was changed, because writing without it would delete rules this code does not own.",
        ) from exc
    rules = response.get("Rules", [])
    return [dict(rule) for rule in rules] if isinstance(rules, list) else []
