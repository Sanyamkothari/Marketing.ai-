"""Plan E: what a pilot needs around the engine (M59-M64).

Nothing in this package trains, scores, checks or promotes anything. Every module reads artefacts
the engine already wrote - a dataset's build report, a run's evaluation, a campaign's measured
incrementality - and turns them into something a client's analyst or marketing head can read:
the data request kit (`data_request`), the pre-flight checker (`preflight`), the data readiness
report (`readiness`), the business results report (`results`), the value and ROI view (`roi`),
the demo environment (`demo`) and the feedback loop (`feedback`). `help` is the one plain-language
catalogue every report and every tooltip reads, and `document` is the one page model every report
is drawn from, as HTML and as PDF.

Heavy libraries are imported inside function bodies only: `import engine` stays fast, and the
pre-flight checker runs on a client laptop with pandas and nothing of AutoGluon's.
"""

from __future__ import annotations

__all__: list[str] = []
