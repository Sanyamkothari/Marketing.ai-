"""Python-heap bytes per `CustomerState` read back from JSON, with and without the profile.

    /home/user/Marketing.ai-/.venv/bin/python -m tests.v3.profile.state_size

The daily sweep holds every refreshed state in one list, so this is what turning `nightly_profile` on
adds to the sweep's peak, per customer. A few seconds; not a test.
"""

from __future__ import annotations

import argparse
import json
import tracemalloc
from datetime import UTC, datetime
from typing import Any

from autonomy.contracts import CustomerState
from autonomy.profile import profile_for, with_profile
from tests.v3.profile.synthetic import customer_events, customer_id

AS_OF = datetime(2025, 9, 30, tzinfo=UTC)


def _per_state(bodies: list[dict[str, Any]]) -> float:
    tracemalloc.start()
    try:
        states = [CustomerState.model_validate(b) for b in bodies]
        current, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(states) == len(bodies)
    return current / len(bodies)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customers", type=int, default=2_000)
    n = parser.parse_args().customers
    plain = [CustomerState(customer_id=customer_id(i), as_of=AS_OF) for i in range(n)]
    filled = [with_profile(s, profile_for(customer_events(i), AS_OF), AS_OF) for i, s in enumerate(plain)]
    without = _per_state([s.model_dump(mode="json") for s in plain])
    with_ = _per_state([s.model_dump(mode="json") for s in filled])
    print(
        json.dumps(
            {
                "customers": n,
                "bytes_per_state": round(without),
                "bytes_per_state_with_profile": round(with_),
                "extra_bytes_per_state": round(with_ - without),
                "extra_gb_at_1m": round((with_ - without) * 1e6 / 1e9, 2),
            }
        )
    )


if __name__ == "__main__":
    main()
