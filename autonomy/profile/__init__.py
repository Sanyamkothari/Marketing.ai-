# ruff: noqa: A005
"""The customer profile (plan-v3 foundation A): about 50 numbers per customer, built the same way for
tonight's decisions and for the slow models' training rows.

    features = profile_for(customer_events, as_of)            # one customer, no event after as_of
    build_profiles(events, states, now)                        # every state, in batches
    rows = training_rows(events, [month_end_1, month_end_2])   # the same features at several dates

`FEATURES` is the one list of features: name, group, plain description, default, computation. See
`docs/v3/PROFILE.md` for the table and the measured cost.
"""

from autonomy.profile.build import (
    DEFAULT_BATCH,
    ProfileRow,
    build_profiles,
    profile_for,
    training_rows,
    training_rows_from_store,
    with_profile,
)
from autonomy.profile.facts import Facts, gather
from autonomy.profile.features import (
    BY_NAME,
    FEATURES,
    NAMES,
    Feature,
    FeatureGroup,
    compute_all,
    defaults,
)
from autonomy.profile.shop import MIN_SHOP_ORDERS, ShopPrices

__all__ = [
    "BY_NAME",
    "DEFAULT_BATCH",
    "FEATURES",
    "MIN_SHOP_ORDERS",
    "NAMES",
    "Facts",
    "Feature",
    "FeatureGroup",
    "ProfileRow",
    "ShopPrices",
    "build_profiles",
    "compute_all",
    "defaults",
    "gather",
    "profile_for",
    "training_rows",
    "training_rows_from_store",
    "with_profile",
]
