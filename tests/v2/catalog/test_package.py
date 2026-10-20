"""The package's re-exports: the everyday names are importable from `autonomy.catalog` itself."""

from __future__ import annotations

import importlib
import types

import autonomy.catalog as catalog


def test_the_package_exports_the_everyday_names_and_keeps_its_modules_reachable() -> None:
    for name in catalog.__all__:
        assert hasattr(catalog, name), name
    # No re-export may hide a submodule of the same name (`import autonomy.catalog.recommend as m`).
    for module in ("products", "cycle", "enrich", "recommend"):
        assert isinstance(importlib.import_module(f"autonomy.catalog.{module}"), types.ModuleType)
        assert isinstance(getattr(catalog, module), types.ModuleType), module
