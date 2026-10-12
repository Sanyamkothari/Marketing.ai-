"""`BuilderPort` over the onboarding HTTP routes (Plan O M237, DEC-1807): the adapter `drive_builder` uses.

Each call is one of the routes the Manual setup screens call, in their order, so a dataset Data Doctor builds
is built by the same builder with the same checks a person would have run (DEC-1578's `next_steps`, made calls):

| Port call | Route |
|---|---|
| `add_source` | `POST /clients/{id}/sources` (multipart: the file and its `role`) |
| `suggest_mapping` | `POST /clients/{id}/mappings/suggest` |
| `save_mapping` | `PUT /clients/{id}/mappings/{mapping_id}` |
| `save_spec` | `POST /clients/{id}/onboarding-specs` |
| `build` | `POST /datasets` |

`http` is anything with `post` and `put` that returns a response with `status_code` and `json()`: FastAPI's
`TestClient` in the tests, an `httpx.Client` pointed at the API in a deployment. A refused call raises
`BuildDriveError` with the route's own code, never a guess.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from engine.doctor.build_driver import BuildDriveError

__all__ = ["ApiBuilderPort", "ResponseLike"]


class ResponseLike(Protocol):
    @property
    def status_code(self) -> int: ...

    def json(self) -> Any: ...


def _body(response: ResponseLike, expected: int, what: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code != expected:
        payload = body if isinstance(body, dict) else {}
        detail = payload.get("detail")
        code = detail.get("code") if isinstance(detail, dict) else None
        message = detail.get("message") if isinstance(detail, dict) else None
        if not code and "checks" in payload:
            # `POST /datasets` answers a 409 with the whole check list when a blocking error remains.
            errors = [c for c in payload["checks"] if isinstance(c, dict) and c.get("severity") == "error"]
            code = str(errors[0].get("code")) if errors else "VALIDATION_FAILED"
            message = str(errors[0].get("message")) if errors else None
        raise BuildDriveError(
            str(code or "BUILD_STEP_FAILED"),
            f"{what} was refused ({response.status_code}): {message or code or 'no reason given'}",
        )
    if not isinstance(body, dict):
        raise BuildDriveError("BUILD_STEP_FAILED", f"{what} answered something other than an object.")
    return body


@dataclass(frozen=True)
class ApiBuilderPort:
    """The builder, through its routes, for one client."""

    http: Any
    """`TestClient` or `httpx.Client`: anything whose `post` and `put` answer a `ResponseLike`."""
    client_id: str

    def add_source(self, *, file_name: str, data: bytes, role: str) -> str:
        response = self.http.post(
            f"/clients/{self.client_id}/sources",
            files={"file": (file_name, data, "text/csv")},
            data={"role": role},
        )
        return str(_body(response, 201, f"Adding {file_name}")["source_id"])

    def suggest_mapping(self, *, source_id: str, use_case: str) -> Mapping[str, Any]:
        response = self.http.post(
            f"/clients/{self.client_id}/mappings/suggest", json={"source_id": source_id, "use_case": use_case}
        )
        return _body(response, 200, "Suggesting a mapping")

    def save_mapping(
        self,
        *,
        mapping_id: str,
        source_id: str,
        use_case: str,
        role: str,
        columns: Sequence[Mapping[str, Any]],
    ) -> str:
        response = self.http.put(
            f"/clients/{self.client_id}/mappings/{mapping_id}",
            json={
                "client_id": self.client_id,
                "source_id": source_id,
                "use_case": use_case,
                "role": role,
                "columns": [dict(column) for column in columns],
            },
        )
        return str(_body(response, 200, "Saving a mapping")["mapping_id"])

    def save_spec(self, body: Mapping[str, Any]) -> str:
        response = self.http.post(f"/clients/{self.client_id}/onboarding-specs", json=dict(body))
        return str(_body(response, 201, "Saving the recipe")["spec_id"])

    def build(self, *, spec_id: str, mode: str) -> str:
        response = self.http.post(
            "/datasets", json={"client_id": self.client_id, "spec_id": spec_id, "mode": mode}
        )
        return str(_body(response, 202, "Building the dataset")["dataset_id"])
