"""Plan J's measurement layer (M93, M94, M95).

`engine.measurement.planner` answers the questions asked *before* a campaign runs (pure functions of
counts). `campaign`, `measure` and `plan` hold the campaign record, the one measurement path and the
registered test plan. `simulate` (M95) builds populations with a known effect, which the nightly
statistical suite measures. Import from the submodules; this package re-exports nothing.
"""
