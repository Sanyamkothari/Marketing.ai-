"""Review finding 16: ordinary feature names are read as API keys, aliased, and never shown back.

`egress._is_token` treats a run of 20 or more `[A-Za-z0-9_-]` characters with a digit and a letter as a key
unless it is "readable": three or more segments that each match `[A-Za-z]+|\\d{1,4}|[A-Za-z]{1,10}\\d{1,2}`.
A window suffix such as `90d`, `30d` or `180d` (digits *then* letter) matches none of those, so
`total_spend_last_90d`, `num_support_tickets_30d`, `avg_basket_size_last_180d` are "tokens":

* `is_plain_label` is False, so the model is shown `column_<n>` instead of the header (docs §7.7 says a name
  "that reads as ordinary words" is shown as it is);
* `unalias` restores an alias only when `scrub(name) == name`, which is False for the same reason, so the
  reply the person reads says `column_5` for a column they named themselves;
* the same string as a categorical cell value is masked as `[REDACTED:token]`.

Not a leak; a false positive that degrades the chat for a very common naming pattern (the shipped `rca`
use case's own `billing_disputes_90d` is one, and its settings block already shows it as `column_5`).
"""

from __future__ import annotations

import pytest

from engine.agent import egress

NAMES = [
    "total_spend_last_90d",
    "num_support_tickets_30d",
    "avg_basket_size_last_180d",
    "billing_disputes_90d",
    "days_since_last_order_90d",
]


@pytest.mark.parametrize("name", NAMES)
def test_a_readable_feature_name_is_a_plain_label(name: str) -> None:
    assert egress.is_plain_label(name)


def test_the_model_is_shown_and_the_person_reads_the_name_the_file_uses() -> None:
    gate = egress.Egress.build(["customer_id", "total_spend_last_90d"])
    assert gate.aliases == {}, gate.aliases
    assert gate.label("total_spend_last_90d") == "total_spend_last_90d"
    # what the model would write back about it, in the reply the person reads
    assert (
        gate.person_text("The model wrote about total_spend_last_90d.")
        == "The model wrote about total_spend_last_90d."
    )
