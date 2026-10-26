"""The scoring-model numbers in docs/v3/COPY.md, over many seeds of the planted synthetic signal.

Run from the repo root (one BLAS thread keeps it quick on a shared machine):

    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 python -m tests.v3.copy.scoring_seeds 40

For seeds 100 .. 100+n-1 it fits `ScoringModel(CopyConfig(seed=1))` on `synth_results(seed, signal=...)`,
with and without the signal, and prints how often the model was used and the mean held-out AUCs.
Not a test: pytest does not collect it.
"""

from __future__ import annotations

import sys

import numpy as np

from autonomy.content.config import CopyConfig
from autonomy.content.scoring import ScoringModel

from .synth import synth_results


def main(n: int) -> None:
    for signal in (True, False):
        used = 0
        aucs: list[float] = []
        themes: list[float] = []
        for i in range(n):
            results, traits = synth_results(100 + i, signal=signal)
            report = ScoringModel(CopyConfig(seed=1)).fit(results, traits)
            used += int(report.used)
            if report.auc is not None and report.theme_auc is not None:
                aucs.append(report.auc)
                themes.append(report.theme_auc)
        print(
            f"signal={signal}: used {used} of {n}; mean held-out AUC {np.mean(aucs):.3f} "
            f"vs {np.mean(themes):.3f} for theme labels ({len(aucs)} seeds with an AUC)"
        )


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 40)
