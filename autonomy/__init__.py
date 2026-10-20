"""Marketing AI v2: the autonomous marketing agent.

`autonomy` is the v2 package. It imports v1 (`engine`) as a library where that helps, but its core
(contracts, interfaces, fakes, the inner loop, the Gym) depends only on pydantic, numpy and the
standard library, so it stays light to install and quick to test. Anything that needs v1's heavy
dependencies (AutoGluon, LightGBM, DuckDB) lives under `autonomy/bridges/` and is tested with the
`v1bridge` marker.

Read `plan-v2.md` for the product, `docs/v2/PARALLEL_BUILD.md` for how it is built, and
`docs/v2/CONTRACTS.md` before writing code against the shared shapes.
"""

__version__ = "0.1.0"
