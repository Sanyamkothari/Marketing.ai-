"""WF1 data intake: client tables and sample datasets in, events out, then events folded into state.

`mapping` turns any table into `Event`s through a declarative `EventMapping`, collecting bad rows as
`MappingProblem`s instead of failing. `loaders` applies it to the committed samples (online-retail,
Hillstrom). `folder` folds events into `CustomerState`, and `rebuild` replays a whole event store into
the state store or applies new events on top.

    result = load_online_retail_sample()
    events.append(result.events)
    rebuild_states(events, states, StateFolder(), now)
"""

from autonomy.data.folder import SegmentRules, StateFolder
from autonomy.data.loaders import (
    LoadResult,
    hillstrom_decision_id,
    hillstrom_events,
    load_hillstrom,
    load_online_retail_log,
    load_online_retail_sample,
    online_retail_log_events,
    online_retail_sample_events,
)
from autonomy.data.mapping import (
    EventMapping,
    MappingError,
    MappingProblem,
    ProblemCode,
    customer_key,
    map_frame,
    problem_counts,
)
from autonomy.data.rebuild import apply_events, rebuild_states

__all__ = [
    "EventMapping",
    "LoadResult",
    "MappingError",
    "MappingProblem",
    "ProblemCode",
    "SegmentRules",
    "StateFolder",
    "apply_events",
    "customer_key",
    "hillstrom_decision_id",
    "hillstrom_events",
    "load_hillstrom",
    "load_online_retail_log",
    "load_online_retail_sample",
    "map_frame",
    "online_retail_log_events",
    "online_retail_sample_events",
    "problem_counts",
    "rebuild_states",
]
