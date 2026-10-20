# Stream dogs_storm: sleeping dogs per person, the complaint storm, and readable reasons

Two tasks. The first makes the contact-harm model (who messages put off) a per-person posterior
read against the holdout, so a season everyone shares is not blamed on messages, and leaves alone
anyone messages credibly put off. The second looked into why `complaint_storm` sends nothing and
whether unsubscribes are understated in the plain shop (no change in either, with the evidence),
and rewrote the reasons so a test reads as what is being tested. No LLM in the per-customer loop;
time and seeds are passed in. Full write-up with Gym numbers: `docs/v2/DOGS_STORM.md`.

## What was built

| Module | What changed |
|---|---|
| `autonomy/decide/bidders.py` | `ContactHarm` is now a per-person posterior: the holdout's buying since the start gives the organic trend per segment (`HARM_MIN_HOLDOUT` people needed), each person's own record is read against it, then shrunk to their neighbours on the clue index and those to the business, by as much as groups really differ. `leave_alone` gives no bids when it is `LEAVE_ALONE_CREDIBLE` (90%) credible that messages lower someone's buying; `leave_alone_retry` lets them back about one week in 50. |
| `autonomy/decide/explain.py` | Reasons for a non-favourite send say what they test ("testing whether WhatsApp beats email for them"), add the person's channel record, name exploration-floor picks as routine tests, and say when restraint held a rest. The point fits the Live screen's 70-character cut (`SHORT_WHY`). |
| `autonomy/decide/policy.py` | Passes the auction's per-channel prices, `state.channels` and whether restraint held the best option to `explain.reason`. Fixes a bug where the favourite was found before each channel's own price. Decisions are unchanged. |
| `autonomy/learning/observations.py` | `ContactEvidence` also records the part of each record since the agent started (`split_at`, `post_*` counts) and messages since the last click (`sends_since_click`). |
| `autonomy/learning/update.py` | `learn_contact_harm` takes the holdout, segments and the agent's start; `update_models` gains `harm_holdout` and `harm_split_at`. |
| `autonomy/runtime/agent.py` | `AgentPolicy.nightly` hands its holdout group and first-decision time to the harm model. |
| `autonomy/decide/__init__.py` | Re-exports `leave_alone`, `leave_alone_retry`, `HARM_MIN_HOLDOUT`, `LEAVE_ALONE_CREDIBLE`, `LEAVE_ALONE_RETRY`. |
| `autonomy/channels/breakers.py` | Unchanged (see gaps). |

## How other streams use it

```python
from autonomy.decide import ContactHarm, contact_harm_of, leave_alone, leave_alone_retry, reason
from autonomy.learning import learn_contact_harm, update_models

harm = contact_harm_of(bundle.response)            # ContactHarm | None, shared by every bidder
harm.p_harm(customer_id) -> float                  # P(messages lower their buying), exact grid
harm.p_below(customer_id, ratio) -> float          # Normal approximation, used by leave_alone
harm.posterior(customer_id) -> tuple[float, float] | None
harm.likely(threshold=0.5) -> int; harm.describe() -> str

leave_alone(harm, customer_id, at_stake: float) -> bool
leave_alone_retry(customer_id, now: datetime, rate=LEAVE_ALONE_RETRY) -> bool

learn_contact_harm(bundle, events_by_customer, content_by_id, now, horizon_days, *,
                   holdout: Iterable[str] = (), segment_of: SegmentOf | None = None,
                   split_at: datetime | None = None) -> ContactHarm | None
update_models(..., learn_harm=True, harm_holdout: Iterable[str] = (),
              harm_split_at: datetime | None = None) -> LearnReport

reason(decision, bids, *, ..., prices: Mapping[Channel, float] | None = None,
       channels: Mapping[Channel, ChannelStats] | None = None, restrained: bool = False) -> str
```

All new arguments are keyword-only with defaults, so existing callers keep working.

## Contract additions requested

None.

## Dependencies requested

None.

## Known gaps

- **Fix before merge, outside this stream's files:** `tests/v2/contact_less/test_discounts_dogs.py`
  line 328 picks a customer with `max()` over a set where 15 tie; under some hash seeds
  (`PYTHONHASHSEED=0`) it picks one the new model rightly does not call harmed, so CI fails about
  1 run in 15. Change to `one = max(sorted(harmed), key=lambda c: evidence[c].sends)`.
- `sleeping_dogs_15` stays at Stage 2 at 1,000 customers: dogs contacted 95%, the same as
  send-to-all. Contacted once in 90 days counts, and that is settled on day 0 from history alone.
- `complaint_storm` still sends nothing ($0, against the best fixed rule's loss). By the agent's own
  model every message draws 0.8-1.3% complaints against a 0.08% limit, so the target cannot be met.
  Judging the history by the agent's own rhythm is suggested, not done.
- `breakers.py` keeps its simple rate check; an exact Poisson tail would change behaviour that
  existing breaker tests pin.
- `leave_alone` (Normal approximation) and `p_harm`/`describe` (exact grid) can differ by up to
  0.03 near the 0.9 bar.
- The click model still credits put-off customers with a full gain.
- `refit` holds several n x 241 arrays at once; chunk it before a 100k-customer shop.
- The reason-share table in `DOGS_STORM.md` predates two review fixes to the wording.
- mypy --strict was checked only with a 3.13 target (or by reading); CI's 3.11 run is the record.
  Gym numbers are 1,000 x 2 seeds; CI runs the full 3,000 x 3 Gym after merge.
