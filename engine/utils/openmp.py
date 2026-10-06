"""Load LightGBM before PyTorch, the one import order in which both work on macOS.

LightGBM and PyTorch each ship their own OpenMP runtime on macOS (Apple Silicon included). Loaded
torch-first, the second runtime aborts the process the moment LightGBM trains (`OMP: Error #15` or a
plain segmentation fault), taking the API server down with it; loaded LightGBM-first, both work. The
engine reaches PyTorch in three places - the neural-network family's accelerator check
(`engine.stages.train.get_hardware_accelerator`), the local embedding model (`sentence-transformers`
imports torch) and AutoGluon's own NN model - so each place that can import torch calls
`import_lightgbm_before_torch` first, and the API calls it once when the app is built (DEC-1268).

Importing a module Python has already imported is a dictionary lookup, so calling this more than once
costs nothing. A machine without LightGBM simply has nothing to order.
"""

from __future__ import annotations

import importlib

__all__ = ["import_lightgbm_before_torch"]


def import_lightgbm_before_torch() -> None:
    """Import `lightgbm` now, if it is installed, so a later `import torch` cannot load first."""
    try:
        importlib.import_module("lightgbm")
    except Exception:  # absent or broken: training reports that in its own words (D10)
        return
