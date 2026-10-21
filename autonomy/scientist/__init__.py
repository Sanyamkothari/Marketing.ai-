"""The Scientist: the AI layer proposes and explains, held-out evidence decides.

Parts, each usable alone (no model is called here):

* `campaign`: a campaign the shop already ran, one row per customer (`Campaign`), or read from events.
* `menu`: the typed menu of candidate policies with settings in safe ranges.
* `evaluate`: fit a candidate on a train part, score it on customers it was not fitted on, off-policy
  from the campaign's logged probabilities, with an interval.
* `select`: the rule, written down beforehand, that picks one candidate (or keeps the safe default, or
  says there is not enough data).
* `monitor`: the weekly check of forecast against actual, and when to run it all again.
* `chooser`: the hook by which the runtime runs the selected policy, in Shadow first.
"""

from autonomy.scientist.campaign import Campaign, campaign_from_history, campaign_from_table
from autonomy.scientist.chooser import ArmAction, ScientistChooser, chooser_from_selection
from autonomy.scientist.evaluate import Evaluation, Evaluations, evaluate_candidates, split_campaign
from autonomy.scientist.menu import (
    BanditAgent,
    BlanketPolicy,
    Candidate,
    MenuError,
    SegmentRule,
    UpliftPolicy,
    candidate_from_spec,
    default_candidates,
)
from autonomy.scientist.monitor import Forecast, Monitor, MonitorSignal, WeeklyActual, check_week, forecast_of
from autonomy.scientist.select import (
    ALPHA,
    MAX_CANDIDATES,
    SafeDefault,
    Selection,
    Verdict,
    holm_adjust,
    run_scientist,
    safe_default,
    select,
)

__all__ = [
    "ALPHA",
    "MAX_CANDIDATES",
    "ArmAction",
    "BanditAgent",
    "BlanketPolicy",
    "Campaign",
    "Candidate",
    "Evaluation",
    "Evaluations",
    "Forecast",
    "MenuError",
    "Monitor",
    "MonitorSignal",
    "SafeDefault",
    "ScientistChooser",
    "SegmentRule",
    "Selection",
    "UpliftPolicy",
    "Verdict",
    "WeeklyActual",
    "campaign_from_history",
    "campaign_from_table",
    "candidate_from_spec",
    "check_week",
    "chooser_from_selection",
    "default_candidates",
    "evaluate_candidates",
    "forecast_of",
    "holm_adjust",
    "run_scientist",
    "safe_default",
    "select",
    "split_campaign",
]
