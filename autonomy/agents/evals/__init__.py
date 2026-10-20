"""Fixed eval cases for the outer-loop agents and the runner that scores them (plan-v2 section 7).

`cases.yaml` holds the cases; `runner.run_evals` runs each one k times and reports pass rate, pass^k
and consistency. `python -m autonomy.agents.evals.runner --fake` runs everything on scripted models.
"""
