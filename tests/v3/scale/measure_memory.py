"""Print the sweep's peak and retained memory (tracemalloc) for a few sizes, whole audience and in batches.

    python -m tests.v3.scale.measure_memory

The same measurement as `test_the_sweeps_peak_memory_follows_the_batch_not_the_customer_count`, with
the numbers shown. Peak is above where the sweep started; kept is what the sweep still holds when it
ends (the day's decided-set).
"""

from __future__ import annotations

import time

from tests.v3.scale.test_sweep_batches import _shadow_sweep

SIZES: tuple[tuple[int, int | None], ...] = (
    (2_000, 100),
    (2_000, None),
    (5_000, 500),
    (20_000, 500),
    (20_000, None),
)


def main() -> None:
    print(f"{'customers':>10} {'batch':>8} {'peak MB':>9} {'kept MB':>9} {'peak B/cust':>12} {'seconds':>8}")
    for n, batch in SIZES:
        t0 = time.perf_counter()
        peak, kept = _shadow_sweep(n, batch)
        print(
            f"{n:>10} {batch or 'whole':>8} {peak / 1e6:>9.2f} {kept / 1e6:>9.2f} "
            f"{peak / n:>12.0f} {time.perf_counter() - t0:>8.1f}"
        )


if __name__ == "__main__":
    main()
