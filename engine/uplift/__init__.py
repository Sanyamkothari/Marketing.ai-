"""Phase 3b: uplift modelling and measured impact (plan B).

Propensity asks *who will convert*; uplift asks *who converts because we acted*. This package adds
the `uplift` problem type beside classification and regression without touching either: its own
checks, learners (S, T and X), evaluator (Qini, AUUC, uplift by decile, bootstrap intervals),
champion rule, four-segment view, budgeted targeting policy, explanations, and two measurements
of a finished campaign - incrementality against the control group, and off-policy evaluation of a
policy that was never run.

The modules import `pandas`, `numpy` and the model libraries inside function bodies, as the rest of
the engine does, so `import engine` stays fast. `config` imports nothing from `engine` at all,
because `engine.config` imports it.
"""
