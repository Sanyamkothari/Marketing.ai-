# hillstrom-email — the Plan J journey on real randomised data

**Public dataset, retrospective replay, a third of each group kept.** Produced only by `library/run_engine.py`; every number below is read
from [`../.runs/hillstrom-email/journey/journey.results.json`](../.runs/hillstrom-email/journey/journey.results.json) of the run this report names, which is git-ignored and
rebuilt by the command at the end. Nothing here is synthetic and nothing was tuned on the evaluation rows.

| | |
|---|---|
| Data | `library/hillstrom-email/data/prepared.csv`, 64,000 rows (SHA-256 `434bc95c6e096dbe…`; the raw file it was prepared from is verified by `fetch.py`) |
| Use case | `hillstrom-email` |
| Split | 32,000 training rows, 32,000 evaluation rows (seed 20261010, stratified by segment and conversion) |
| Risk model run | `r_20261010_11100001`, 62.1 s |
| Campaign-effect run | `r_20261010_11100002`, 17.6 s |
| Scoring run | `r_20261010_11100003` |
| Risk model's scoring run (section 3) | `r_20261010_11100004` |
| Whole journey | 128.8 s |

## The answer in one table

| Step | What happened |
|---|---|
| Readiness | the uplift checks on the training rows passed, on the evaluation rows passed |
| Risk model (Phase 1, LightGBM) | test ROC-AUC 0.5341; beats its baseline: no |
| Campaign-effect model (M100, 2 offers) | AUUC of the first offer 0.0004 (-0.0013 to 0.0017); status `candidate` |
| Beats risk ranking (M96, the engine's check) | **failed** (its risk comparator was fitted on 85 % of the rows it is judged on; see section 3) |
| Beats risk ranking, both models out of sample (Mens E-Mail, evaluation rows) | **failed**: AUUC difference against the approved propensity model's score -0.0000 (-0.0010 to 0.0007) |
| Beats risk ranking, both models out of sample (Womens E-Mail, evaluation rows) | **failed**: AUUC difference against the approved propensity model's score 0.0001 (-0.0007 to 0.0010) |
| Stable across folds (M96) | **passed** |
| Calibrated by decile (M96) | **failed** |
| Treat list (M97/M100) | 12,862 of 32,000 customers get an e-mail (Mens E-Mail: 7,597, Womens E-Mail: 5,265) |
| Off-policy value on the evaluation rows | chosen list against no e-mail, conversion rate +0.45 pts (+0.23 pts to +0.67 pts): **above zero** |
| Campaign on `conversion` (replay) | difference +0.60 pts (+0.21 pts to +1.00 pts): **above zero** |
| Value Proof Pack on `conversion` (M104) | built, provenance verified: True; claim: Causal: the customers held back were chosen at random |
| Campaign on `spend` (replay, CUPED) | adjusted difference per customer 0.84 (0.23 to 1.45): **above zero** |
| Value Proof Pack on `spend` (M104) | built, provenance verified: True; claim: Causal: the customers held back were chosen at random |

## What this says, plainly

The campaign-effect model does **not** beat plain risk ranking on its hold-out: knowing who an e-mail moves is not, on this file, better established than knowing who buys anyway. Repeated on the evaluation rows, where neither model saw a row, it does not beat the risk model's ranking for any of the e-mails. The chosen list measurably raises the conversion rate against sending nothing, on rows no model saw. It does measurably **worse** than sending everyone the Mens E-Mail: the simple rule wins on this file. Every step ran end to end through the product's own API, and each Value Proof Pack rests only on the measured numbers of its campaign, with every figure traced.

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

**Risk model** (`POST /runs`, Phase 1): Ensemble (LightGBM), 6 models trained, 62.1 s. Test metrics: f1 0.0145, pr_auc 0.0097, precision 0.0086, recall 0.0465, roc_auc 0.5341. Against its baseline (baseline (logistic regression)): does not beat it.

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

The engine's comparison on the campaign-effect run's hold-out (6,392 training rows, first offer against no e-mail, 200 paired resamples); the check is decided by `propensity_model`:

| Ranking by | AUUC (95 %) | Uplift minus it (95 %) | Uplift better |
|---|---|---|---|
| predicted uplift | 0.0004 (-0.0013 to 0.0017) | | |
| `p_control` (the model's own chance of the outcome without contact) | 0.0005 (-0.0011 to 0.0019) | -0.0000 (-0.0022 to 0.0023) | no |
| `p_treated` (the model's own chance of the outcome with contact) | 0.0005 (-0.0009 to 0.0022) | -0.0001 (-0.0014 to 0.0010) | no |
| `propensity_model` (the approved propensity model's score) | 0.0002 (-0.0011 to 0.0019) | 0.0002 (-0.0016 to 0.0019) | no |

**The comparator saw most of those rows.** The risk model was trained on the same 32,000-row training upload, so 5,403 of the 6,392 hold-out rows (84.5 %) were in its training or validation part; only 989 were in its test part. The `propensity_model` row is therefore scored partly in-sample, which may flatter the risk ranking. The `p_control` row is the campaign-effect model's own, out of sample on this hold-out, and it reaches the same verdict. The comparison below repeats the check where neither model saw a row.

**Beats risk, both models out of sample.** The 32,000 evaluation rows were scored by the risk model too (run `r_20261010_11100004`, column `conversion_prob`). For each offer, the rows the file sent that offer or no e-mail, ranked by the offer's predicted uplift against the risk score and `p_control`, by the same function and rule as M96 (`compare_with_baselines`, 200 paired resamples, seed 20261010):

*Mens E-Mail* (21,307 rows, 10,654 sent it; uplift AUUC -0.0001 (-0.0008 to 0.0007)): This model does not beat risk ranking (by the approved propensity model's score): the AUUC difference is 0.0000 (95% CI -0.0010 to 0.0007), a range that includes zero.

| Ranking by | AUUC (95 %) | Uplift minus it (95 %) | Uplift better |
|---|---|---|---|
| predicted uplift | -0.0001 (-0.0008 to 0.0007) | | |
| `p_control` (the model's own chance of the outcome without contact) | -0.0006 (-0.0014 to 0.0002) | 0.0004 (-0.0008 to 0.0017) | no |
| `propensity_model` (the approved propensity model's score) | -0.0001 (-0.0010 to 0.0006) | -0.0000 (-0.0010 to 0.0007) | no |

*Womens E-Mail* (21,346 rows, 10,693 sent it; uplift AUUC 0.0008 (0.0002 to 0.0014)): This model does not beat risk ranking (by the approved propensity model's score): the AUUC difference is 0.0001 (95% CI -0.0007 to 0.0010), a range that includes zero.

| Ranking by | AUUC (95 %) | Uplift minus it (95 %) | Uplift better |
|---|---|---|---|
| predicted uplift | 0.0008 (0.0002 to 0.0014) | | |
| `p_control` (the model's own chance of the outcome without contact) | -0.0004 (-0.0009 to 0.0004) | 0.0011 (-0.0001 to 0.0021) | no |
| `propensity_model` (the approved propensity model's score) | 0.0007 (0.0001 to 0.0012) | 0.0001 (-0.0007 to 0.0010) | no |

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

| Policy | E-mails | Value | Difference from no e-mail (95 %, normal) | Interval | Bootstrap 95 % (percentile) | Interval |
|---|---|---|---|---|---|---|
| no_email | 0 | 0.600 | 0.000 (0.000 to 0.000) |  | 0.000 to 0.000 |  |
| chosen | 25,599 | 1.214 | 0.615 (0.282 to 0.947) | **above zero** | 0.294 to 0.943 | **above zero** |
| everyone_Mens E-Mail | 32,000 | 1.493 | 0.893 (0.479 to 1.307) | **above zero** | 0.483 to 1.286 | **above zero** |
| everyone_Womens E-Mail | 32,000 | 1.015 | 0.416 (0.088 to 0.744) | **above zero** | 0.097 to 0.739 | **above zero** |

Chosen list against each e-mail sent to everyone (paired on the same rows): everyone_Mens E-Mail -0.278 (-0.611 to 0.055) (includes zero), bootstrap -0.598 to 0.056 (includes zero); everyone_Womens E-Mail 0.199 (-0.146 to 0.544) (includes zero), bootstrap -0.128 to 0.541 (includes zero).

The bootstrap: percentile, rows drawn with replacement, the same draws for every policy (paired); 2,000 resamples, seed 20261010.

**Caution: the amount is skewed.** The engine flagged `OUTCOME_SKEWED` on the `spend` campaign: a few very large amounts dominate the averages, so the normal intervals above may be too narrow. The bootstrap column is the check on them; neither is exact on so few non-zero amounts.

**In rupees** (spend difference × 32,000 customers × ₹83 per dollar, less ₹0.05 per e-mail; revenue before margin):

| Policy | Incremental revenue | E-mail cost | Net (95 %, normal) | Net, bootstrap 95 % |
|---|---|---|---|---|
| no_email | ₹0 | ₹0 | ₹0 (₹0 to ₹0) | ₹0 to ₹0 |
| chosen | ₹1,632,289 | ₹1,280 | ₹1,631,009 (₹746,838 to ₹2,515,180) | ₹780,766 to ₹2,503,431 |
| everyone_Mens E-Mail | ₹2,371,616 | ₹1,600 | ₹2,370,016 (₹1,269,896 to ₹3,470,135) | ₹1,280,733 to ₹3,413,452 |
| everyone_Womens E-Mail | ₹1,103,899 | ₹1,600 | ₹1,102,299 (₹231,106 to ₹1,973,493) | ₹256,102 to ₹1,961,436 |

The skew caution above applies to these rupee ranges too.

## 6. Measured as a campaign, and the Value Proof Pack

The treat list was recorded as a campaign (`POST /campaigns`) and its test plan registered before the journey read any evaluation row's outcome (the evaluation readiness checks, section 3's out-of-sample comparison and section 5 all come after it); every plan input comes from training rows. Its outcomes were then uploaded by **replay**: a customer keeps their outcome only when the e-mail the file randomly sent them is the one the list gave them (no e-mail for the engine's control group). The file's e-mail was drawn independently of the list, so the kept customers are a random third of each arm and the comparison stays randomised; the others are counted by the engine as customers without an outcome, never as non-converters.

### `conversion` — Hillstrom e-mail (conversion) - public dataset, retrospective replay, a third of each group kept

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

### `spend` — Hillstrom e-mail (spend) - public dataset, retrospective replay, a third of each group kept

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

**Finding.** Each Pack's *Method and limits* says: “The engine chose who was held back, at random, before the campaign went out.” “Outcomes are counted for everyone the campaign was meant to reach, whether or not the message arrived, so the result is the effect of running the campaign.” On this replay neither holds as written: nothing went out (the file is a 2008 log, and the engine's control group was drawn when the evaluation rows were scored), and outcomes are counted only for the customers the replay kept, while the rest are left out as having no outcome (`conversion`: 17,165 of 25,599 intended customers; `spend`: 17,165 of 25,599 intended customers). The Pack does print that count, and its campaign name says it is a retrospective replay of a public dataset with a third of each group kept, but its method sentences are fixed by `causal_basis` alone. The plan's `expectation` text, which says the same, is stored with the plan and does not reach the Pack. Open question for M104 (DEC-1314): the Method text should depend on whether outcomes are missing by design (a replay) or by loss. No engine code was changed.

**What the pack's money covers.** The replay keeps outcomes for about a third of the contacted customers, and the pack credits what it measured on those, while it costs the e-mails of every customer meant to be contacted. Its net value is therefore an understatement of the list's; section 5's off-policy figure uses every evaluation row.

## 7. Assumptions and settings, all of them

- **Exchange rate.** 83 rupees per US dollar: an input assumption close to the 2024 average reference rate, used only to express the file's 2008 dollars in the engine's rupees. Every rupee figure scales with it.
- **Value per conversion.** `value_inr = history x scale x fx_inr_per_usd`; scale = mean spend of the 289 training converters ($118.01) over their mean `history` ($304.38) = 0.3877. Revenue, before margin. The pack's value per conversion is the same mean order in rupees, ₹9,794.83.
- **E-mail cost.** ₹0.05 per e-mail (`configs/pilot/value.yaml`'s e-mail benchmark), no offer cost.
- **The covariate's date.** The file has no dates. `history` is dated the day before the campaign record's start, by assumption (the file defines it as the spend of the year before the e-mail), so the engine's point-in-time rule (`COVARIATE_NOT_BEFORE_CAMPAIGN`) passes by construction: it is not a check here.
- **The outcome window.** The campaigns were recorded with an outcome window of 0 days, and each Pack prints that figure, while the file's outcomes cover two weeks. `POST /campaigns` refuses a treatment start before the scoring run finished and `measure` refuses while a window is open, so a replayed campaign cannot carry the real window. A limit of the replay; the outcomes themselves are the file's two weeks.
- **Risk model overrides:** `model_search.strategy=fast`, `model_search.time_limit_minutes=10`, `model_search.tuning_trials=5`, `model_search.candidates=['LightGBM']`, `governance.approval_required=False`.
- **Campaign-effect overrides:** `uplift.treatment_levels=['No E-Mail', 'Mens E-Mail', 'Womens E-Mail']`, `uplift.policy.value_column=value_inr`, `governance.approval_required=False`.
- **Configuration in the use case:** X-learner on LightGBM, persuadable at +0.5 points and sleeping dog at −0.2 points of conversion (the defaults are set for rates near 20 %), fold stability on, engine control group 50 %.

## Reproduce

```bash
python library/hillstrom-email/fetch.py
python -m library.run_engine journey --dataset hillstrom-email
```

## 8. The audit readout on the original campaign (M103, public dataset, retrospective audit)

**Public dataset, retrospective audit.** The journey above measured a list the engine made. This section reads the campaign that **actually ran**: Hillstrom's own e-mail test, with who got which e-mail as the assignment and visit, conversion and spend as the outcomes, posted to `POST /campaigns/audit` the way a client's past campaign would be. No model is trained and nothing is tuned; it is the whole file, as it ran. Every number below is read from [`audit.results.json`](audit.results.json), committed beside this report (aggregates only: no customer row) and rewritten by the command at the end of this section.

| | |
|---|---|
| Data | `library/hillstrom-email/data/prepared.csv`, 64,000 rows (SHA-256 `434bc95c6e096dbe…`) |
| Groups in the original campaign | No E-Mail 21,306, Mens E-Mail 21,307, Womens E-Mail 21,387 |
| Outcomes file | 64,000 rows: `customer_id`, `visit`, `conversion`, `spend` |
| Campaign dated | 2008-03-20, outcome window 14 days |
| Campaign ids | `c_20261010_11200001`, `c_20261010_11200002`, `c_20261010_11200003`, `c_20261010_11200004` |
| Whole audit | 20.4 s |

### 8.1 The answer in one table

| Campaign audited | Label | Randomness check | Effect (95 %) |
|---|---|---|---|
| Conversion: each e-mail against no e-mail | **Causal** | passed: guessing score 0.506 (chance 0.500, limit 0.60) | Mens E-Mail +0.68 pts (+0.50 pts to +0.86 pts): **above zero**; Womens E-Mail +0.31 pts (+0.15 pts to +0.47 pts): **above zero** |
| Spend: any e-mail against no e-mail | **Causal** | passed: guessing score 0.502 (chance 0.500, limit 0.60) | $0.60 ($0.38 to $0.82) per customer: **above zero** |
| Spend: the men's e-mail against no e-mail | **Causal** | passed: guessing score 0.503 (chance 0.500, limit 0.60) | $0.77 ($0.49 to $1.05) per customer: **above zero** |
| Spend: the women's e-mail against no e-mail | **Causal** | passed: guessing score 0.506 (chance 0.500, limit 0.60) | $0.42 ($0.17 to $0.68) per customer: **above zero** |
| Spend: each e-mail against no e-mail, in one audit | refused: `CAMPAIGN_INVALID` | | |

### 8.2 What the numbers may claim

The label is **Causal** for every campaign audited: it was stated that the e-mail was assigned at random (the route requires the statement and never assumes it), and the engine **verified** it. It tried to predict who was e-mailed from the customer details in the assignment file (8 columns: `recency`, `history_segment`, `history`, `mens`, `womens`, `zip_code`, `newbie`, `channel`) and could not do better than a guessing score of 0.506 at worst (0.500 is chance; the limit is 0.60). With several offers it tests each against the shared control and reports the worst. Because the outcomes are in a separate file, the test could not have used them.

The engine's own notes on these audits:

- Every customer in the assignment file was compared, whether or not they were meant to be reached (the usual way to read a campaign: the groups as they were chosen).
- Every customer was taken to be contacted on the date the campaign went out.

### 8.3 What each e-mail changed

**Conversion: each e-mail against no e-mail** (campaign `c_20261010_11200001`; the men's e-mail and the women's e-mail, each against the group sent nothing).

| Offer | E-mailed | Converted | Held back | Converted | Difference (95 %) | Extra conversions (95 %) | p |
|---|---|---|---|---|---|---|---|
| Mens E-Mail | 21,307 | 267 (1.25 %) | 21,306 | 122 (0.57 %) | +0.68 pts (+0.50 pts to +0.86 pts): **above zero** | 145 (107 to 184) | < 0.001 |
| Womens E-Mail | 21,387 | 189 (0.88 %) | 21,306 | 122 (0.57 %) | +0.31 pts (+0.15 pts to +0.47 pts): **above zero** | 67 (32 to 101) | < 0.001 |

The engine's verdict sentence reads the first offer only (Mens E-Mail): The campaign added about 145 conversions. Likely between 107 and 184. The per-offer table above is the reading for each e-mail.

**Spend, in dollars** (the file's money; the Packs convert it at the stated rate).

| Contrast | E-mailed | Mean spend | Held back | Mean spend | Difference per customer (engine, 95 %) | Bootstrap (harness, 95 %) | p | In total, dollars (engine) |
|---|---|---|---|---|---|---|---|---|
| every customer sent an e-mail against the group sent nothing | 42,694 | $1.25 | 21,306 | $0.65 | $0.60 ($0.38 to $0.82): **above zero** | $0.38 to $0.81 | < 0.001 | +25,480 (likely +16,061 to +34,898) |
| the men's e-mail against the group sent nothing | 21,307 | $1.42 | 21,306 | $0.65 | $0.77 ($0.49 to $1.05): **above zero** | $0.49 to $1.06 | < 0.001 | +16,403 (likely +10,337 to +22,469) |
| the women's e-mail against the group sent nothing | 21,387 | $1.08 | 21,306 | $0.65 | $0.42 ($0.17 to $0.68): **above zero** | $0.17 to $0.69 | 0.001 | +9,077 (likely +3,613 to +14,540) |

**Caution: the amount is skewed.** The engine flagged `OUTCOME_SKEWED` on `spend_any`, `spend_mens`, `spend_womens`: a few very large amounts dominate the averages, so its normal interval may be too narrow. The bootstrap column is the harness's check on it (a seeded percentile bootstrap of the difference in means, 2,000 resamples); neither is exact with so few customers who spent anything.

The route measures a yes/no outcome for each of several offers but an amount only for one contrast: the three-group file on the amount was posted as well and refused (below), so spend is read for any e-mail against none and, from a file of the two groups concerned, for each e-mail against none.
No adjustment by an earlier amount is made: an audit has no test plan registered in advance, and the adjusted estimate (M102) must be named in one.

`spend_offers` (Spend: each e-mail against no e-mail, in one audit) was refused by the route: `CAMPAIGN_INVALID`: Several offers are measured on a yes/no outcome only: measuring each offer on an amount, or adjusting it by an amount from before the campaign, is not offered yet. Measure the campaign as a whole on the amount, or each offer on a yes/no outcome.

### 8.4 Value Proof Packs (M104)

A Pack was built for each campaign, named "... - public dataset, retrospective audit" in the campaign name the Pack prints, and its provenance re-verified (`engine.pilot.proof.verify_provenance`: every figure resolves to a measured record).

| Pack | Built | Claim | Headline |
|---|---|---|---|
| `conversion` | provenance verified: True; HTML 200, PDF 200 | Causal: the customers held back were chosen at random | Extra outcomes because of the campaign: +145 (likely +107 to +184). Net value ₹10,29,750 (10.30 lakh) to ₹17,76,011 (17.76 lakh). |
| `spend_any` | provenance verified: True; HTML 200, PDF 200 | Causal: the customers held back were chosen at random | Extra amount because of the campaign: +25,479.61 (likely +16,060.85 to +34,898.37). Net value ₹13,30,916 (13.31 lakh) to ₹28,94,430 (28.94 lakh). |
| `spend_mens` | provenance verified: True; HTML 200, PDF 200 | Causal: the customers held back were chosen at random | Extra amount because of the campaign: +16,402.71 (likely +10,336.87 to +22,468.54). Net value ₹8,56,895 (8.57 lakh) to ₹18,63,824 (18.64 lakh). |
| `spend_womens` | provenance verified: True; HTML 200, PDF 200 | Causal: the customers held back were chosen at random | Extra amount because of the campaign: +9,076.90 (likely +3,613.48 to +14,540.33). Net value ₹2,98,849 (2.99 lakh) to ₹12,05,778 (12.06 lakh). |

**Finding (`conversion`).** With 2 offers the Pack's headline ('Extra outcomes because of the campaign: +145 (likely +107 to +184). Net value ₹10,29,750 (10.30 lakh) to ₹17,76,011 (17.76 lakh).'), its "Extra outcomes" (+145) and the value of what the campaign changed are the first offer's, Mens E-Mail, alone; the other offer's (Womens E-Mail +67) is in the offer table and the backfire table but not in the headline or the net value, while the cost of contacts (₹2,135) is for all 42,694 e-mails sent. The net value therefore credits one offer and charges both. Every figure is traced to a measured record, so provenance passes; it is the scope of the headline that is partial (M100 keeps the first offer in the single-offer fields, DEC-668 (3), and the Pack reads those fields). Raised for M104 (DEC-1314) in docs/CROSS_BRANCH_REQUESTS.md on 2026-10-10; no engine code was changed.

Value inputs: one conversion is worth ₹9,658.17 (the mean spend of a customer who bought, over the whole file, at the assumed rate; revenue before margin; 578 buyers, mean spend $116.36), one dollar of spend ₹83, and one e-mail costs ₹0.05. 83 rupees per US dollar: an input assumption close to the 2024 average reference rate, used only to express the file's 2008 dollars in the engine's rupees. Every rupee figure scales with it.

### 8.5 The programme readout does not apply

`POST /campaigns/programme` reads the whole customer base against the **universal hold-out** (M92) over a finished period, intent to treat. That hold-out is drawn by the engine when it scores; Hillstrom's file was randomised by someone else, once, three ways, and has none. The route was asked once and refused: `PROGRAMME_NO_HOLDOUT` (409): "The programme is read against the universal holdout, and none has been used yet: set actions.holdout.scope to universal on the use cases, with the holdout secret set, and score once." No programme number is produced or claimed.

### 8.6 Assumptions and limits

- **Dates.** The file carries no date. The campaign is dated 2008-03-20, the date in the published file's name (`...DataMiningChallenge_2008.03.20.csv`), only so the engine has a start date and a 14-day window (the file's outcomes cover two weeks). Nothing but the Pack's printed dates depends on it.
- **Randomisation is stated and verified, not proven by the file.** The request said the e-mail was assigned at random, as the route requires; the engine's check could not contradict it. A check that passes cannot prove randomness, it can only fail to find a pattern in the customer details the file carries.
- **Intent to treat.** Every customer in the assignment file is compared, whether or not an e-mail reached them: the file does not say who opened or received one, so no contact file was added and the Packs' delivery section says so.
- **A different question from the journey's.** The journey measured the model's treat list on the half of the file no model saw (a third of each arm kept). This measures the e-mails themselves on all 64,000 customers, the way the test was designed; the two are not the same estimate and are not meant to agree.
- **Rupee figures** in the Packs are estimates that move with the stated value inputs; the measured counts and dollar amounts do not.
- **Visits** were not audited; the outcomes file carries them and the route would read them the same way as conversion.

### 8.7 Reproduce

```bash
python library/hillstrom-email/fetch.py
python -m library.run_engine audit --dataset hillstrom-email
```
