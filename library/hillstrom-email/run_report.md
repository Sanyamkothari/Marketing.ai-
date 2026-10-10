# hillstrom-email — the Plan J journey on real randomised data

**Public dataset, retrospective.** Produced only by `library/run_engine.py`; every number below is read
from [`../.runs/hillstrom-email/journey/journey.results.json`](../.runs/hillstrom-email/journey/journey.results.json) of the run this report names, which is git-ignored and
rebuilt by the command at the end. Nothing here is synthetic and nothing was tuned on the evaluation rows.

| | |
|---|---|
| Data | `library/hillstrom-email/data/prepared.csv`, 64,000 rows (SHA-256 `434bc95c6e096dbe…`; the raw file it was prepared from is verified by `fetch.py`) |
| Use case | `hillstrom-email` |
| Split | 32,000 training rows, 32,000 evaluation rows (seed 20261010, stratified by segment and conversion) |
| Risk model run | `r_20261010_11100001`, 57.9 s |
| Campaign-effect run | `r_20261010_11100002`, 20.1 s |
| Scoring run | `r_20261010_11100003` |
| Whole journey | 124.4 s |

## The answer in one table

| Step | What happened |
|---|---|
| Readiness | the uplift checks on the training rows passed, on the evaluation rows passed |
| Risk model (Phase 1, LightGBM) | test ROC-AUC 0.5341; beats its baseline: no |
| Campaign-effect model (M100, 2 offers) | AUUC of the first offer 0.0004 (-0.0013 to 0.0017); status `candidate` |
| Beats risk ranking (M96) | **failed** |
| Stable across folds (M96) | **passed** |
| Calibrated by decile (M96) | **failed** |
| Treat list (M97/M100) | 12,862 of 32,000 customers get an e-mail (Mens E-Mail: 7,597, Womens E-Mail: 5,265) |
| Off-policy value on the evaluation rows | chosen list against no e-mail, conversion rate +0.45 pts (+0.23 pts to +0.67 pts): **above zero** |
| Campaign on `conversion` (replay) | difference +0.60 pts (+0.21 pts to +1.00 pts): **above zero** |
| Value Proof Pack on `conversion` (M104) | built, provenance verified: True; claim: Causal: the customers held back were chosen at random |
| Campaign on `spend` (replay, CUPED) | adjusted difference per customer 0.84 (0.23 to 1.45): **above zero** |
| Value Proof Pack on `spend` (M104) | built, provenance verified: True; claim: Causal: the customers held back were chosen at random |

## What this says, plainly

The campaign-effect model does **not** beat plain risk ranking on its hold-out: knowing who an e-mail moves is not, on this file, better established than knowing who buys anyway. The chosen list measurably raises the conversion rate against sending nothing, on rows no model saw. It does measurably **worse** than sending everyone the Mens E-Mail: the simple rule wins on this file. Every step ran end to end through the product's own API, and each Value Proof Pack rests only on the measured numbers of its campaign, with every figure traced.

## 1. Readiness and the power sheet

The data, by group (the outcomes as the file records them):

| Part | Group | Rows | Conversions | Conversion rate | Visit rate | Mean spend ($) |
|---|---|---|---|---|---|---|
| training | No E-Mail | 10,653 | 61 | 0.57 % | 10.73 % | 0.706 |
| training | Mens E-Mail | 10,653 | 133 | 1.25 % | 18.23 % | 1.352 |
| training | Womens E-Mail | 10,694 | 95 | 0.89 % | 15.11 % | 1.139 |
| evaluation | No E-Mail | 10,653 | 61 | 0.57 % | 10.50 % | 0.600 |
| evaluation | Mens E-Mail | 10,654 | 134 | 1.26 % | 18.32 % | 1.493 |
| evaluation | Womens E-Mail | 10,693 | 94 | 0.88 % | 15.17 % | 1.015 |

**Uplift checks on the training rows** (passed; the same function `POST /uplift/runs` runs):

- no finding

**Uplift checks on the evaluation rows** (passed; the same function `POST /uplift/runs` runs):

- no finding

**The power sheet** (`POST /measurement/power-preview`, base rate from the training rows' no-e-mail group):

*every evaluation row* (32,000 customers, base rate 0.57 %):

| Control share | Contacted | Held back | Smallest rise it is sure to see | Cost of holding back |
|---|---|---|---|---|
| 10 % | 28,800 | 3,200 | 0.49 pts | ₹153,230 |
| 20 % | 25,600 | 6,400 | 0.35 pts | ₹216,381 |
| 30 % | 22,400 | 9,600 | 0.29 pts | ₹275,699 |
| 50 % | 16,000 | 16,000 | 0.26 pts | ₹410,260 |

*a third of them, as the replay keeps* (10,666 customers, base rate 0.57 %):

| Control share | Contacted | Held back | Smallest rise it is sure to see | Cost of holding back |
|---|---|---|---|---|
| 10 % | 9,599 | 1,067 | 0.98 pts | ₹102,251 |
| 20 % | 8,533 | 2,133 | 0.67 pts | ₹139,288 |
| 30 % | 7,466 | 3,200 | 0.56 pts | ₹174,444 |
| 50 % | 5,333 | 5,333 | 0.49 pts | ₹255,043 |

## 2. The models

**Risk model** (`POST /runs`, Phase 1): Ensemble (LightGBM), 6 models trained, 57.9 s. Test metrics: f1 0.0145, pr_auc 0.0097, precision 0.0086, recall 0.0465, roc_auc 0.5341. Against its baseline (baseline (logistic regression)): does not beat it.

**Campaign-effect model** (`POST /uplift/runs`, x_learner on lightgbm), measured on its own hold-out of 6,392 training rows. Registered as `candidate`: a model of several offers is never champion (DEC-1310 (h)).

| Offer | Hold-out rows | Effect on conversion (95 %) | AUUC (95 %) |
|---|---|---|---|
| Mens E-Mail | 6,392 | +0.69 pts (+0.28 pts to +1.13 pts) | 0.0004 (-0.0013 to 0.0017) |
| Womens E-Mail | 6,405 | +0.34 pts (-0.13 pts to +0.75 pts) | 0.0000 (-0.0013 to 0.0014) |

`arm_policy_value.json` (the training hold-out, value-weighted): choosing the e-mail per customer against the first offer alone, per customer, 8.56 (-20.14 to 37.95) rupees of value before costs — Choosing the offer per customer cannot be shown to beat the first offer alone on this hold-out (8.5642 value per customer, 95% interval -20.1361 to 37.9495).

## 3. The approval checks (M96)

As the Approver's screen shows them (`engine.model_gates.approval_checks`); advisory, never blocking:

- `UPLIFT_NOT_BETTER_THAN_RISK` — **failed**. This model does not beat risk ranking (by the approved propensity model's score): the AUUC difference is 0.0002 (95% CI -0.0016 to 0.0019), a range that includes zero.
- `UPLIFT_UNSTABLE_ACROSS_FOLDS` — **passed**. Stable across folds: none of the 5 refitted models is clearly worse than random, and they differ no more than their sampling noise explains. AUUC by fold with 95% CI: fold 1 -0.0007 (-0.0024 to 0.0008); fold 2 -0.0008 (-0.0027 to 0.0011); fold 3 -0.0005 (-0.0020 to 0.0011); fold 4 0.0002 (-0.0014 to 0.0019); fold 5 -0.0004 (-0.0021 to 0.0012); -0.0004 on average.
- `UPLIFT_MISCALIBRATED` — **failed**. Predicted uplift does not match what was measured: 6 of 10 groups contain the prediction in their 95% range; the average gap is 1.3 points.

## 4. The treat list (M97, M100)

The 32,000 evaluation rows were scored without their e-mail group or outcomes. Each customer's e-mail was chosen by net value: predicted uplift × `value_inr` − ₹0.05 per e-mail.

| Offer | Eligible | Sleeping dogs for it | Given it | Predicted net value | Cost |
|---|---|---|---|---|---|
| Mens E-Mail | 16,000 | 4,392 | 7,597 | ₹1,658,844 | ₹380 |
| Womens E-Mail | 16,000 | 5,380 | 5,265 | ₹859,460 | ₹263 |

Customers the policy meant to contact: 25,599, of whom 12,737 are in the engine's own random control group. Reasons per row: below_cost: 787, offer: 12,862, sleeping_dog: 2,351.

**The ranking choice (J5).** `ranking_choice.json` says `propensity_model` (UPLIFT_NOT_BETTER_THAN_RISK): This model does not beat risk ranking (by the approved propensity model's score): the AUUC difference is 0.0002 (95% CI -0.0016 to 0.0019), a range that includes zero. So this contact list is ranked by the approved propensity model (Ensemble (LightGBM), version 1), contacting the same number of customers the uplift model chose.

**Finding.** The fallback re-ranks the run's single-offer contact list (the `action` column of the scores) by the risk model, but the offer chosen per customer (`offer_choice.parquet`, M100 part B), and therefore the treat list, is still chosen by the campaign-effect model's net value. On a model of several offers the J5 fallback does not reach the list that goes out. Recorded as an open question for DEC-1306 / DEC-1310; no engine code was changed.

| `action` in the scores | Given an e-mail by the offer choice | Not given one |
|---|---|---|
| Control (hold out) | 0 | 16,000 |
| Don't treat (converts anyway) | 6 | 1 |
| Don't treat (over budget) | 2,571 | 0 |
| Don't treat (won't convert) | 1,275 | 159 |
| Never treat (contact makes it worse) | 1,703 | 2,689 |
| Treat | 7,307 | 289 |

## 5. Measured off-policy on the evaluation rows

Inverse probability weighting with the evaluation rows' own arm shares; 95% normal intervals. The evaluation rows' own random e-mail is the logged action, with shares No E-Mail 33.29 %, Mens E-Mail 33.29 %, Womens E-Mail 33.42 %. The chosen list gives No E-Mail 20.00 %, Mens E-Mail 47.27 %, Womens E-Mail 32.73 % of the rows.

**conversion** (conversion rate, per customer, against sending no e-mail):

| Policy | E-mails | Value | Difference from no e-mail (95 %) | Interval |
|---|---|---|---|---|
| no_email | 0 | 0.57 % | +0.00 pts (+0.00 pts to +0.00 pts) |  |
| chosen | 25,599 | 1.02 % | +0.45 pts (+0.23 pts to +0.67 pts) | **above zero** |
| everyone_Mens E-Mail | 32,000 | 1.26 % | +0.69 pts (+0.43 pts to +0.94 pts) | **above zero** |
| everyone_Womens E-Mail | 32,000 | 0.88 % | +0.31 pts (+0.08 pts to +0.53 pts) | **above zero** |

Chosen list against each e-mail sent to everyone (paired on the same rows): everyone_Mens E-Mail -0.24 pts (-0.43 pts to -0.04 pts) (**below zero**); everyone_Womens E-Mail +0.14 pts (-0.08 pts to +0.36 pts) (includes zero).

**visit** (visit rate, per customer, against sending no e-mail):

| Policy | E-mails | Value | Difference from no e-mail (95 %) | Interval |
|---|---|---|---|---|
| no_email | 0 | 10.50 % | +0.00 pts (+0.00 pts to +0.00 pts) |  |
| chosen | 25,599 | 16.16 % | +5.66 pts (+4.77 pts to +6.55 pts) | **above zero** |
| everyone_Mens E-Mail | 32,000 | 18.32 % | +7.82 pts (+6.80 pts to +8.83 pts) | **above zero** |
| everyone_Womens E-Mail | 32,000 | 15.17 % | +4.66 pts (+3.70 pts to +5.62 pts) | **above zero** |

Chosen list against each e-mail sent to everyone (paired on the same rows): everyone_Mens E-Mail -2.16 pts (-2.93 pts to -1.39 pts) (**below zero**); everyone_Womens E-Mail +1.00 pts (+0.12 pts to +1.87 pts) (**above zero**).

**spend** (dollars, per customer, against sending no e-mail):

| Policy | E-mails | Value | Difference from no e-mail (95 %) | Interval |
|---|---|---|---|---|
| no_email | 0 | 0.600 | 0.000 (0.000 to 0.000) |  |
| chosen | 25,599 | 1.214 | 0.615 (0.282 to 0.947) | **above zero** |
| everyone_Mens E-Mail | 32,000 | 1.493 | 0.893 (0.479 to 1.307) | **above zero** |
| everyone_Womens E-Mail | 32,000 | 1.015 | 0.416 (0.088 to 0.744) | **above zero** |

Chosen list against each e-mail sent to everyone (paired on the same rows): everyone_Mens E-Mail -0.278 (-0.611 to 0.055) (includes zero); everyone_Womens E-Mail 0.199 (-0.146 to 0.544) (includes zero).

**In rupees** (spend difference × 32,000 customers × ₹83 per dollar, less ₹0.05 per e-mail; revenue before margin):

| Policy | Incremental revenue | E-mail cost | Net (95 %) |
|---|---|---|---|
| no_email | ₹0 | ₹0 | ₹0 (₹0 to ₹0) |
| chosen | ₹1,632,289 | ₹1,280 | ₹1,631,009 (₹746,838 to ₹2,515,180) |
| everyone_Mens E-Mail | ₹2,371,616 | ₹1,600 | ₹2,370,016 (₹1,269,896 to ₹3,470,135) |
| everyone_Womens E-Mail | ₹1,103,899 | ₹1,600 | ₹1,102,299 (₹231,106 to ₹1,973,493) |

## 6. Measured as a campaign, and the Value Proof Pack

The treat list was recorded as a campaign (`POST /campaigns`), its test plan registered before any outcome was read, and its outcomes uploaded by **replay**: a customer keeps their outcome only when the e-mail the file randomly sent them is the one the list gave them (no e-mail for the engine's control group). The file's e-mail was drawn independently of the list, so the kept customers are a random third of each arm and the comparison stays randomised; the others are counted by the engine as customers without an outcome, never as non-converters.

### `conversion` — Hillstrom e-mail (conversion) - public dataset, retrospective

Intended 25,599 (contacted 12,862, held back 12,737); kept by the replay: contacted 4,263 (Mens E-Mail 2,533, Womens E-Mail 1,730), held back 4,171.

> Treated customers converted at 1.1% against 0.5% for the control group: a lift of +0.6 points (95% CI +0.2 points to +1.0 points; p = 0.002), about 26 extra conversions caused by the campaign.

Verdict: **The campaign added about 26 conversions** — Likely between 9 and 43. Counted: The customer bought in the two weeks after the e-mail (or after the day it would have gone out).

Value Proof Pack: built (HTML 200, PDF 200), every figure re-verified by `verify_provenance`. Claim: *Causal: the customers held back were chosen at random*. Headline: *Extra outcomes because of the campaign: +26 (likely +9 to +43). Net value ₹88,187 to ₹4,18,481 (4.18 lakh).*

| Section | Status | Reason when not measured |
|---|---|---|
| The plan as registered, against what ran | measured |  |
| Did the list go out as planned? | not_measured | No contact file was added, so whether the customers on the list were really contacted, and whether any held-back customer was contacted anyway, is not measured. Add one on the campaign's page. |
| What the campaign changed, with ranges | measured |  |
| Gross against incremental | measured |  |
| Naive credit against measured credit | measured |  |
| Offer money spent on sure things and sleeping dogs | measured |  |
| What the control group and the explore slice cost | measured |  |
| Groups where the campaign backfired | measured |  |
| Net value in rupees, as a range | measured |  |
| Method and limits | measured |  |

Value inputs entered for the pack: ₹9,794.83 per conversion (Mean spend of a converting customer in the training rows, in rupees at the assumed rate; revenue before margin), ₹0.05 per e-mail.

### `spend` — Hillstrom e-mail (spend) - public dataset, retrospective

Intended 25,599 (contacted 12,862, held back 12,737); kept by the replay: contacted 4,263 (Mens E-Mail 2,533, Womens E-Mail 1,730), held back 4,171.

> Contacted customers averaged 1.39 against 0.55 for the control group: a difference of +0.84 per customer (95% CI +0.23 to +1.45; p = 0.007), about 3,578 more in total across the contacted customers. Taking account of each customer's 'history' from before the campaign, the difference is +0.84 per customer (95% CI +0.23 to +1.45); this removed 0% of the chance variation. A few very large amounts dominate these averages, so the range may be too narrow; more customers in each group would make it reliable.

Unadjusted difference per customer 0.839 (0.230 to 1.449); adjusted by `history` 0.840 (0.231 to 1.449); variance removed 0.07 %; warnings ['OUTCOME_SKEWED'].

Verdict: **The campaign added about 3,581 to The customer bought in the two weeks after the e-mail (or after the day it would have gone out)** — Likely between 984 and 6,178 in total (+0.84 per contacted customer). Adjusted for each customer's 'history' before the campaign.

**Finding.** The verdict names the use case's own outcome definition, not `spend`, the column this campaign was measured on: the campaign page and step 4 both label a verdict with `target.definition` whatever column was named. The number is right; its words are not. Recorded as an open question; no engine code was changed.

Value Proof Pack: built (HTML 200, PDF 200), every figure re-verified by `verify_provenance`. Claim: *Causal: the customers held back were chosen at random*. Headline: *Extra amount because of the campaign: +3,580.84 (likely +984.06 to +6,177.63). Net value ₹81,034 to ₹5,12,100 (5.12 lakh).*

| Section | Status | Reason when not measured |
|---|---|---|
| The plan as registered, against what ran | measured |  |
| Did the list go out as planned? | not_measured | No contact file was added, so whether the customers on the list were really contacted, and whether any held-back customer was contacted anyway, is not measured. Add one on the campaign's page. |
| What the campaign changed, with ranges | measured |  |
| Gross against incremental | measured |  |
| Naive credit against measured credit | measured |  |
| Offer money spent on sure things and sleeping dogs | measured |  |
| What the control group and the explore slice cost | measured |  |
| Groups where the campaign backfired | measured |  |
| Net value in rupees, as a range | measured |  |
| Method and limits | measured |  |

Value inputs entered for the pack: ₹83.00 per dollar of spend (One dollar of spend, in rupees at the assumed rate; revenue before margin), ₹0.05 per e-mail.

**What the pack's money covers.** The replay keeps outcomes for about a third of the contacted customers, and the pack credits what it measured on those, while it costs the e-mails of every customer meant to be contacted. Its net value is therefore an understatement of the list's; section 5's off-policy figure uses every evaluation row.

## 7. Assumptions and settings, all of them

- **Exchange rate.** 83 rupees per US dollar: an input assumption close to the 2024 average reference rate, used only to express the file's 2008 dollars in the engine's rupees. Every rupee figure scales with it.
- **Value per conversion.** `value_inr = history x scale x fx_inr_per_usd`; scale = mean spend of the 289 training converters ($118.01) over their mean `history` ($304.38) = 0.3877. Revenue, before margin. The pack's value per conversion is the same mean order in rupees, ₹9,794.83.
- **E-mail cost.** ₹0.05 per e-mail (`configs/pilot/value.yaml`'s e-mail benchmark), no offer cost.
- **The covariate's date.** `history` is dated the day before the campaign record's start (the file defines it as the year before the e-mail).
- **Risk model overrides:** `model_search.strategy=fast`, `model_search.time_limit_minutes=10`, `model_search.tuning_trials=5`, `model_search.candidates=['LightGBM']`, `governance.approval_required=False`.
- **Campaign-effect overrides:** `uplift.treatment_levels=['No E-Mail', 'Mens E-Mail', 'Womens E-Mail']`, `uplift.policy.value_column=value_inr`, `governance.approval_required=False`.
- **Configuration in the use case:** X-learner on LightGBM, persuadable at +0.5 points and sleeping dog at −0.2 points of conversion (the defaults are set for rates near 20 %), fold stability on, engine control group 50 %.

## Reproduce

```bash
python library/hillstrom-email/fetch.py
python -m library.run_engine journey --dataset hillstrom-email
```
