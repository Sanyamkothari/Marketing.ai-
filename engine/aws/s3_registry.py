"""The deployed registry: facts in SQL, files in the object store, and no policy of its own.

Phase 1's registry stores a model version as a row of storage **keys**, and those keys point into
the run directory that produced them - `runs/<run id>/model`, `runs/<run id>/schema.json`. That is
exactly right while the model and the run are the same thing on the same disk, and it stops being
right the moment a run directory has a lifecycle: a bucket lifecycle rule that expires old runs, a
cleanup job, or a human tidying up, and the champion of a use case loses its predictor while the
registry goes on claiming to have one.

So a deployment publishes. `register` copies a version's files to
`engine.storage.published_model_key(use_case_id, version, ...)` - `models/<use case>/<version>/…` -
and stores *those* keys, which is a prefix a lifecycle rule and an IAM condition can both be written
against, and which no run cleanup touches (DEC-346).

That is this class's **only** addition. It owns no policy:

- the champion rule is `engine.registry.should_promote`, a pure function nobody here calls (DEC-028);
- the transitions, the single-transaction champion swap and the stale-approval refusal are
  `SqlRegistryStore`'s, unchanged, because a champion swap has to mean the same thing on both
  backends (DEC-338);
- the layout is `engine.storage.published_model_key`'s, called and not re-spelled, because two
  spellings of one prefix is an invitation to use the wrong one (DEC-310).

`engine.settings.build_registry` builds it as `S3ModelRegistry(postgres_store(settings), storage)`.
The optional mirror is the SageMaker Model Registry, off unless somebody passes one (DEC-348).
"""

from __future__ import annotations

import shutil
from posixpath import basename
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from engine.registry import ModelRegistry, RegistryError, SqlRegistryStore
from engine.storage import Storage, StorageError, published_model_key
from engine.utils.logging import get_logger, log_failure

if TYPE_CHECKING:
    from engine.contracts import ModelVersion

__all__ = ["ModelMirror", "S3ModelRegistry"]

_LOGGER = get_logger(__name__)


@runtime_checkable
class ModelMirror(Protocol):
    """Somewhere a registered version is *also* written, one way, for somebody else to read.

    Declared here, where it is consumed, rather than beside its one implementation: this module
    must not import `engine.aws.sagemaker_registry`, and a protocol at the consumer is how a
    capability gets added without the consumer learning who provides it.

    `mirror` returns an identifier for the mirrored copy, or `None` when there is nothing to say.
    It may not raise - see `S3ModelRegistry._mirror`, which treats a raise as a failure and carries
    on either way (DEC-348).
    """

    def mirror(self, version: ModelVersion) -> str | None: ...


class S3ModelRegistry:
    """`SqlRegistryStore` for the facts, `Storage` for the files, and the publishing step between.

    Every method but `register` is the store's, delegated verbatim. `register` publishes first and
    stores second, so a row never names a key that was not written: if the copy fails the version is
    not registered at all, which is the honest outcome - a registry row for a model whose predictor
    is missing is worse than no row, because the champion rule will happily crown it and the first
    scoring run will be what discovers the truth (DEC-346).
    """

    def __init__(
        self, store: SqlRegistryStore, storage: Storage, *, mirror: ModelMirror | None = None
    ) -> None:
        self._store = store
        self._storage = storage
        self._mirror_target = mirror

    @property
    def store(self) -> SqlRegistryStore:
        """The store holding the facts; `scripts/` and the tests need it and nothing else."""
        return self._store

    def register(self, version: ModelVersion) -> ModelVersion:
        """Publish the version's files, then store the version that names the published copies."""
        published = self._publish(version)
        stored = self._store.register(published)
        self._mirror(stored)
        return stored

    def get(self, model_id: str) -> ModelVersion:
        """One model version; raises `MODEL_NOT_FOUND` when the id is unknown."""
        return self._store.get(model_id)

    def list_versions(self, use_case_id: str | None = None) -> tuple[ModelVersion, ...]:
        """Every version, newest first, optionally restricted to one use case."""
        return self._store.list_versions(use_case_id)

    def get_champion(self, use_case_id: str) -> ModelVersion | None:
        """The current champion of a use case, or None while there is none."""
        return self._store.get_champion(use_case_id)

    def next_version(self, use_case_id: str) -> int:
        """The version number the next model of this use case gets (1 when there is none)."""
        return self._store.next_version(use_case_id)

    def approve(self, model_id: str, *, by: str) -> ModelVersion:
        """The store's approval, including its refusal of a stale comparison (DEC-047)."""
        approved = self._store.approve(model_id, by=by)
        self._mirror(approved)
        return approved

    def promote(self, model_id: str, *, by: str, note: str) -> ModelVersion:
        """The store's manual override, unchanged (plan §8)."""
        promoted = self._store.promote(model_id, by=by, note=note)
        self._mirror(promoted)
        return promoted

    def archive(self, model_id: str) -> ModelVersion:
        """Retire a version. The published files are left alone: a lifecycle rule owns their end."""
        archived = self._store.archive(model_id)
        self._mirror(archived)
        return archived

    # publishing ---------------------------------------------------------------
    def _publish(self, version: ModelVersion) -> ModelVersion:
        """`version` with every key pointing at the published copy under `models/<uc>/<v>/`."""
        moved: dict[str, str] = {}
        schema_key = self._publish_object(version, version.schema_key, required=True, moved=moved)
        run_config_key = self._publish_object(version, version.run_config_key, required=True, moved=moved)
        predictor_key = self._publish_tree(version, version.predictor_key, moved=moved)
        drift_baseline_key = (
            None
            if version.drift_baseline_key is None
            else self._publish_object(version, version.drift_baseline_key, required=False, moved=moved)
        )
        artefact_keys = {
            name: moved.get(key) or self._publish_named(version, name, key, moved=moved)
            for name, key in version.artefact_keys.items()
        }
        return version.model_copy(
            update={
                "schema_key": schema_key,
                "run_config_key": run_config_key,
                "predictor_key": predictor_key,
                "drift_baseline_key": drift_baseline_key,
                "artefact_keys": artefact_keys,
            }
        )

    def _publish_named(self, version: ModelVersion, name: str, key: str, *, moved: dict[str, str]) -> str:
        """An `artefact_keys` entry that was not one of the four named keys.

        The map's own key is the published file name, because that is what it already means: the
        map is "artefact filename -> where it is". A trailing slash marks a directory.
        """
        if name.endswith("/"):
            return self._publish_tree(version, key, moved=moved, name=name.rstrip("/"))
        return self._publish_object(version, key, required=False, moved=moved, name=name)

    def _publish_object(
        self,
        version: ModelVersion,
        key: str,
        *,
        required: bool,
        moved: dict[str, str],
        name: str | None = None,
    ) -> str:
        """Copy one object under the published prefix and return its new key.

        A key that is already published is returned untouched, so registering a version twice - or
        re-registering one read back out of the registry - copies nothing.

        A `required` source that is not there is `MODEL_ARTEFACT_MISSING`. An optional one that is
        not there keeps the key it had and is logged: `drift_baseline_key` is set by the register
        stage whether or not a baseline was written, so its absence is a normal state of the world
        and not a reason to refuse a model (DEC-346).
        """
        if key in moved:
            return moved[key]
        target = published_model_key(version.use_case_id, version.version, name or basename(key))
        if key == target:
            moved[key] = target
            return target
        if not self._storage.exists(key):
            if required:
                raise RegistryError(
                    "MODEL_ARTEFACT_MISSING",
                    f"Model {version.model_id} names an artefact that is not in storage, so it "
                    "cannot be published or scored with. The training run did not finish writing it.",
                    model_id=version.model_id,
                )
            _LOGGER.info("registry.publish.absent model=%s", version.model_id)
            return key
        self._copy(key, target, version)
        moved[key] = target
        return target

    def _publish_tree(
        self, version: ModelVersion, prefix: str, *, moved: dict[str, str], name: str | None = None
    ) -> str:
        """Copy every object under `prefix` (a predictor directory) and return the new prefix.

        A saved AutoGluon predictor is a directory, not a file, so `exists()` is always False for
        it and the only honest test of "is it there" is whether anything is stored beneath it.
        """
        if prefix in moved:
            return moved[prefix]
        target = published_model_key(version.use_case_id, version.version, name or basename(prefix))
        if prefix == target:
            moved[prefix] = target
            return target
        members = self._storage.list_keys(f"{prefix}/")
        if not members:
            raise RegistryError(
                "MODEL_ARTEFACT_MISSING",
                f"Model {version.model_id} names a predictor directory with nothing in it, so the "
                "model cannot be scored with. The training run did not finish saving it.",
                model_id=version.model_id,
            )
        for member in members:
            self._copy(member, f"{target}/{member[len(prefix) + 1 :]}", version)
        moved[prefix] = target
        _LOGGER.info("registry.publish model=%s files=%d", version.model_id, len(members))
        return target

    def _copy(self, source: str, target: str, version: ModelVersion) -> None:
        """Stream one object from `source` to `target`.

        Streamed rather than read into memory because a predictor directory holds model files whose
        size is the model's business, and a registry has no reason to hold one of them whole.
        """
        try:
            with self._storage.open_read(source) as reader, self._storage.open_write(target) as writer:
                shutil.copyfileobj(reader, writer)
        except StorageError as exc:
            raise RegistryError(
                "MODEL_ARTEFACT_MISSING" if exc.code == "KEY_NOT_FOUND" else "MODEL_PUBLISH_FAILED",
                f"Model {version.model_id}'s files could not be published, so it was not registered. "
                f"The artefact store reported {exc.code}.",
                model_id=version.model_id,
            ) from exc

    def _mirror(self, version: ModelVersion) -> None:
        """Offer the version to the mirror, if there is one. Never fails a registry write (DEC-348)."""
        if self._mirror_target is None:
            return
        try:
            identifier = self._mirror_target.mirror(version)
        except Exception as exc:
            log_failure(_LOGGER, f"registry.mirror model={version.model_id}", exc)
            return
        if identifier is not None:
            _LOGGER.info("registry.mirror model=%s mirrored=%s", version.model_id, identifier)


_: type[ModelRegistry] = S3ModelRegistry
"""`S3ModelRegistry` must satisfy the protocol; mypy checks this line so no test has to."""
