# Does Autonomy work for big companies? The verdict

Sources:

- [01 enterprise data](01_enterprise_data.md)
- [02 engagement and decisioning stacks](02_enterprise_engagement.md)
- [03 power and learnability](03_power_learnability.md)
- our own [real-data results](../../../v2/REAL_DATA_RESULTS.md) and [scoreboard](../../../v2/REAL_BENCH.md)

Vendor facts come from search extracts (page fetches were blocked), so check them before using them
outside the team. In 03, most play lifts are assumptions; only MegaFon's is measured.

## What big data changes

| Question | Small shop (5k-100k) | Big company (1M-50M users) |
|---|---|---|
| Can we prove a play works (lift vs a holdout)? | Years to about 1 year, often never | **1-5 weeks** for frequent plays; bank cross-sell about 5-16 weeks |
| Can we pick between two variants? | Usually never | **Weeks** at 10M+. Small gaps need a pre-period covariate (CUPED) |
| Can we target by segment or feature (uplift)? | Rarely | **Yes**: beats blanket from about 10-30k rows *when features carry signal*. Gate it on a held-out comparison, because with cheap contact a T-learner can lose to send-to-all |
| Can we learn per person from their own history? | No | **Still no.** After 260 interactions a per-person policy captures about 6% of the per-person gain. Value has to come from features shared across people |
| Do randomised controls already exist? | Rarely | **Usually.** Braze, MoEngage, CleverTap, WebEngage, Adobe and SAS keep global control groups; Pega randomises 2% of its choices; experimentation platforms log assignment |
| Does more data make targeting much better? | n/a | **Slowly.** On Criteo, 100x more data added 19% to the targeting gain |

## So

- **Big data makes the measure-and-decide loop fast and honest.** That loop is the scientist loop:
  try a few policies, prove each in weeks against a control, keep the winners.
- **Big data does not make the per-person brain win.** Per-person learning fails even at enterprise
  scale.
- **Our value at enterprise scale is fast, calibrated, evidence-gated decisions on top of their
  stack:**
  - whether to contact, how often, which play
  - uplift by features where it is proven
  - proof that stays independent
- The magic per-person brain is not the value.

## Competition and gap

- **Incumbents:**
  - Pega (banks, telecoms)
  - BrazeAI Decisioning Studio / OfferFit (Salesforce has announced it will acquire Braze)
  - Adobe Journey Optimizer and Target
  - in-house data science teams at the largest companies
- **Gaps:**
  - a vendor-neutral decision layer across their tools
  - "whether and how much to contact", calibrated on their randomised controls; the incumbents
    mostly optimise *which* message
  - independent lift measurement
  - mid-size enterprises (about 1-20M users) on MoEngage, CleverTap, WebEngage or Braze, which
    lack OfferFit/Pega-grade decisioning (MoEngage Sherpa does variant and timing only) and have no
    big in-house team

## What would have to change

1. **Architecture.**
   - Warehouse-native: features and scoring as SQL in their Snowflake or BigQuery (a Native App or
     data share), plus reverse ETL or API-triggered sends.
   - Today the in-memory engine runs out of memory at about 1M customers.
2. **Brain.**
   - Keep the scientist loop, feature-based uplift gated on held-out evidence, CUPED, calibration
     against their controls, and proof.
   - Park the per-person Bayesian machinery: fatigue per person, sleeping dogs per person, the
     click model.
3. **Integrations.**
   - Braze, MoEngage, CleverTap, Iterable, Adobe for sends.
   - Read their control groups and experiment logs.
   - Make sure our sends enter their control logic. In Braze and CleverTap, API-triggered campaigns
     may bypass the global control.
4. **Trust.**
   - SOC 2 Type II and ISO 27001, data residency, deployment inside their VPC, masked PII.
   - In India: RBI rules for payment data, and the DPDP rules.
5. **Go-to-market.**
   - Long cycles. Start read-only: an independent audit of their existing campaigns using their own
     controls (low security friction, value in weeks).
   - Then decisions for one play, then more.
