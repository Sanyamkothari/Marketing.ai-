"""Plan J's measurement layer (M93, M94, M95, M96).

`engine.measurement.planner` answers the questions asked *before* a campaign runs (pure functions of
counts). `campaign`, `measure` and `plan` hold the campaign record, the one measurement path and the
registered test plan. `simulate` (M95) builds populations with a known effect, which the nightly
statistical suite measures. `compare` (M96) weighs uplift ranking against risk ranking at equal
budget, cross-fitted on randomised rows. `segments` (M104) reads a measured campaign once per band, segment
and offer, with the false-alarm guard the Value Proof Pack's backfire check uses. Import from the
submodules; this package re-exports nothing.
"""
