# Stream click_harm: purchases weigh in on a gain read from clicks

One task. The agent's response model reads a message's gain from clicks, and a sleeping dog still
clicks, so it was credited with a full gain while its buying dropped. Now each customer's click gain
is weighed against their own purchases inside and outside message weeks, by how well each is
measured. The same task chunked the nightly harm refit (peak memory at 50k customers 694 MB -> 77
MB, same answers to 1e-12) and made `leave_alone`, `likely()` and `describe()` read one number. No
LLM in the per-customer loop; time and seeds are passed in. Full write-up with Gym numbers and the
rejected variants: `docs/v2/CLICK_HARM.md`.

## What was built

| Module | What changed |
|---|---|
| `autonomy/decide/bidders.py` | `ContactHarm.purchase_shift` does a Bayes update on the ratio grid: the click gain is the prior (log-Normal, spread = click sd plus the learned gap spread), the customer's own purchase record is the likelihood. New `ClickGap` (empirical Bayes, fitted at each refit from the click readings bidders noted with `note_click`, at most `CLICK_GAP_SAMPLE` customers by a fixed hash) takes off the bias all records share. `GoalBidder` charges each option `max(purchase_shift, dent x organic, 0)`, never both, and purchases only ever lower a gain. The refit runs in blocks of `HARM_BLOCK` (2048). `leave_alone` reads the exact-grid `p_harm`. The `MappingProxyType` dataclass default is now a `default_factory` (Python 3.11). |
| `autonomy/runtime/agent.py` | `ClickResponseModel.uplift_from_clicks = True` turns the weighting on. Other models keep the dent-only charge. |
| `autonomy/decide/__init__.py` | Re-exports `ClickGap`, `CLICK_GAP_MIN`, `HARM_BLOCK`. |
| `autonomy/learning/` | Unchanged. |

## How other streams use it

```python
from autonomy.decide import ClickGap, ContactHarm, contact_harm_of, leave_alone, HARM_BLOCK

harm = contact_harm_of(bundle.response)       # ContactHarm | None, shared by every bidder
ContactHarm(*, ..., block: int = HARM_BLOCK)  # refit block size; must be >= 1
harm.note_click(customer_id, organic: float, gain: float, gain_sd: float) -> None
harm.purchase_shift(customer_id, organic: float, gain: float, gain_sd: float) -> float
    # what the customer's own purchases take off the click gain (negative: would add, which the
    # bidder ignores); exactly 0 with no record or before the first fit
harm.click_gap -> ClickGap                    # offset, spread, customers (0 = not read yet)
harm.p_harm(customer_id) -> float             # exact grid; leave_alone, likely, describe all use it
leave_alone(harm, customer_id, at_stake: float) -> bool

class MyModel(ResponseModel):
    uplift_from_clicks = True                 # opt in: GoalBidder weighs this model's gains
```

`GoalBidder.bids()` notes click readings; `quote()` does not. Nothing else changes for callers: the
new constructor argument is keyword-only with a default, and models without
`uplift_from_clicks` behave as before.

## Contract additions requested

None.

## Dependencies requested

None.

## Known gaps

- Sleeping dogs are still not found at 1,000 customers (`sleeping_dogs_15` contacts 95.2% of
  them, the same as before; it stays at Stage 2). A recency-weighted record holds about one
  purchase, so the learned gap spread comes out 0 and a dog's gain is cut by only 3-20%. It should
  grow with longer histories and CI's 3,000 customers.
- Purchase noise lowers average gains a little, since purchases only take off: -$26 to -$116 on the
  two-seed mean in plain scenarios. Every Stage held.
- The click gap is read at the nightly refit, so the first decision day keeps the click gains.
- A record that reads more buying than the clicks predict is never credited.
- Only the best plain option's gain is weighed; every option moves by the same amount.
- At the grid edges (tiny organic chance) the click prior is clipped, so less is taken off than the
  click reading alone would say. Cautious direction; left as is.
- mypy --strict was checked locally with a 3.13 target only (local numpy stubs need 3.12+); the
  files compile under Python 3.11. CI's 3.11 run is the record.
- Only `tests/v2/click_harm` was run locally. Gym numbers are 1,000 customers x 2 seeds; CI's
  3,000 x 3 Gym has the final say.
