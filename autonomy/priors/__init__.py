"""WF1 cross-client priors (plan-v2 section 7): starting points that a client's own data overrides.

`pooling` turns per-client pattern summaries into a shared prior under the safety rules (at least 5
clients, no client above 30% weight, no identities kept), blends a prior with local data, and seeds
priors from the Gym and public datasets. `store` keeps priors in SQL or in a directory of JSON files.

    store = SqlPriorStore(engine)            # after create_all(engine)
    store.put(seed_from(PriorSource.GYM, {"half_life_days": 9.0}, "fatigue/win_back", now))
    start = store.get("fatigue/win_back")
"""

from autonomy.priors.pooling import (
    MAX_CLIENT_WEIGHT,
    ClientStat,
    NotEnoughClientsError,
    blend,
    capped_weights,
    hash_client,
    pool,
    seed_from,
)
from autonomy.priors.store import (
    MAX_KEY_LENGTH,
    JsonPriorStore,
    SqlPriorStore,
    create_all,
    prior_metadata,
    prior_table,
)

__all__ = [
    "MAX_CLIENT_WEIGHT",
    "MAX_KEY_LENGTH",
    "ClientStat",
    "JsonPriorStore",
    "NotEnoughClientsError",
    "SqlPriorStore",
    "blend",
    "capped_weights",
    "create_all",
    "hash_client",
    "pool",
    "prior_metadata",
    "prior_table",
    "seed_from",
]
