# The public dataset library

Seven public datasets, five industries, seven use cases, **one engine and no engine code**. The
seventh, the MineThatData e-mail test (Plan J M110), is the first randomised one the library could
obtain, and the whole Plan J journey is validated on it: [section 7](#7-plan-j-m110-the-whole-journey-on-real-randomised-data).

One industry and one use case already shipped (Telecom / `telco-churn`). The library adds four
industries and four use cases, drafts a fifth, and changes no engine code to do it.

Everything in [`library/`](../library/) exists to answer one question: how much of "one engine,
many industries" is true today, by configuration alone? The answer is in the two tables below —
the first is what the engine did, the second is everything it could not do without a change
somebody else owns.

Every number here came from a real run. Where a run failed, or lost to its own baseline, or never
happened, the table says so.

---

## 1. The library

| # | Dataset | Industry | Use case | Rows | Test metric | D1 lift | Story |
|---|---|---|---|---|---|---|---|
| 1 | [Telco Customer Churn](../library/telco-customer-churn/) | Telecom | `telco-churn` | 7,043 | ROC-AUC **0.8448** (baseline 0.8409) | **2.99×** | Call the top 10 % and four in five are leaving; tenure, contract and internet service are 74 % of the answer. |
| 2 | [UCI Bank Marketing](../library/uci-bank-marketing/) | Banking | `bank-term-deposit` | 41,188 | ROC-AUC **0.7906** (baseline 0.7831) *[0.9509 before the leaking column was excluded]* | **4.52×** *[5.89× before]* | The 0.95 model is worthless and the 0.79 model works — and no validation check could tell them apart. |
| 3 | [UCI Online Retail](../library/online-retail/) | E-commerce | `retail-win-back` | 1,463 | PR-AUC **0.4975** (baseline 0.4985) — **loses** | 1.33× | An invoice log alone cannot predict who comes back; the engine ran the whole path and returned an honest no. |
| 4 | [Health Insurance Cross-Sell](../library/health-insurance-cross-sell/) | Insurance | `insurance-cross-sell` | 381,109 | ROC-AUC **0.8581** (baseline 0.8380) | **3.23×** | Half the book is not worth a call; "already insured" is 55 % of the model on its own. |
| 5 | [UCI Credit Default](../library/uci-credit-default/) | Banking | `card-default-propensity` | 30,000 | ROC-AUC **0.7957** (baseline 0.7279) | **3.25×** | The widest win over a linear baseline in the library, +0.068: last month's payment status is half the answer. |
| 6 | [Criteo Uplift](../library/criteo-uplift/) | Ad-tech | `criteo-uplift` — **planned** | 13,979,592 | **not run** | — | Configured for Phase 3b's `uplift` problem type since 2026-09-23, but the data is still unreachable from this environment (re-tested that day), so nothing was trained and nothing is reported. |
| 7 | [MineThatData E-Mail (Hillstrom)](../library/hillstrom-email/) | E-commerce | `hillstrom-email` (Plan J M110) | 64,000 | campaign-effect AUUC **0.0004** (−0.0013 to 0.0017); does **not** beat risk ranking | — | Randomised into men's, women's and no e-mail: the chosen list adds **+0.45 pts** of conversion over no e-mail on rows no model saw, but loses to sending everyone the men's e-mail. See section 7. |

Every run used **engine defaults** — `strategy: balanced`, `time_limit_minutes: 30`,
`tuning_trials: 50`, the default candidate pool — with no overrides, except the second bank run,
which adds exactly one: `prepare.exclude_columns: ["duration"]`.

### The same table, with the parts a demo cares about

| Dataset | Wall clock | Models trained | Winner | Validation findings |
|---|---|---|---|---|
| Telco Customer Churn | 624.3 s | 111 | Ensemble (XGBoost + Logistic Regression) | none |
| UCI Bank Marketing (defaults) | 1314.0 s | 112 | Ensemble (LightGBM + XGBoost + Random Forest) | none |
| UCI Bank Marketing (`duration` excluded) | 1147.6 s | 144 | Ensemble (LightGBM) | none |
| UCI Online Retail | 910.4 s | 106 | Ensemble (XGBoost + Random Forest) | 1 warning: `CONSTANT_COLUMN` on `snapshot_date` |
| Health Insurance Cross-Sell | 1245.6 s | 77 | Ensemble (XGBoost + LightGBM + Logistic Regression) | none |
| UCI Credit Default | 888.6 s | 152 | Ensemble (LightGBM + XGBoost + Random Forest) | none |

Six runs, none longer than twenty-two minutes, the largest on 381,109 rows. Across all six: **zero
validation errors and one warning**, and that warning was correct.

### These numbers were measured twice, and the second time is the one that counts

The library was first measured against the engine as it stood before this branch merged the
contracts-first surface and Phase 3a. That merge brought `hyperparameter_tune_kwargs` into
`engine/stages/train.py` (DEC-073): `model_search.tuning_trials` had been inert, and is now honoured.
Nothing in any library config changed, and the search changed completely — telco went from 5 models
in 21 s to 111 in 624 s, and its ROC-AUC moved 0.8573 → 0.8448.

So every report was re-run and rewritten from artefacts produced against the merged tree. The
figures above are those. It is also, incidentally, the cleanest demonstration in this repository of
what the library is for: the same six configurations, unchanged, measured a real difference in the
engine underneath them.

Row 7 is not one of the six default-settings runs above: it is the whole Plan J journey, with its own
settings, recorded in [section 7](#7-plan-j-m110-the-whole-journey-on-real-randomised-data).

## 2. What needed code changes

This is the evidence for how config-only the engine really is, so it is the section to read
sceptically. Five entries were written in
[`docs/CROSS_BRANCH_REQUESTS.md`](CROSS_BRANCH_REQUESTS.md), in the format
`PARALLEL_WORK_PROTOCOL.md` §5.10 asks for — what is needed, and what the branch did meanwhile.
**One of them blocked the work; four did not.**

| Entry (2026-09-22, from `library-datasets`) | What | Owner | Blocked the library? |
|---|---|---|---|
| *two assertions forbid a second industry* | `test_industries_list_and_telecom_loads` pins the config directory to exactly one industry, and `test_industry_available_entries_have_files_and_matching_stage_names` pins the telecom file to listing every shipped use case. So no second industry and no further use case can be added to `configs/`. | `tests/` | **Yes** — see DEC-400; resolved by DEC-085 |
| *two PII detectors that disagree* | `prepare` has a second, looser PII detector than `ingest`; its phone-number regex matches ISO dates, so `snapshot_date` was redacted with nothing in the validation report to say so. | `engine/` | No |
| *a template column name cannot contain a dot* | `emp.var.rate` and `default.payment.next.month` must be renamed before a use case can carry a template. | `engine/` | No |
| *`threshold.mode: auto` can call every row positive* | On a weak model at a ~50 % base rate the F1-maximising threshold gives recall 1.0 and specificity 0.0, which reads as a triumph and is no decision at all. | `engine/` | No |
| *`README.md` has no block a non-phase branch may write in* | The protocol gives `README.md` three phase marker blocks and this branch is none of them, so `docs/LIBRARY.md` is unreachable from the repository's front door. | `README.md` / human reviewer | No |

### What this means, said plainly

**No modelling change was needed for any dataset.** Nothing in `engine/stages/` had to move for
five public files from four industries — telecom, banking, e-commerce and insurance — to be
ingested, validated, prepared, split, trained, evaluated, explained, banded and exported. All five
ran end to end on the first attempt with no overrides at all. That is the claim, and it held. The
sixth, Criteo Uplift, was not run for reasons that have nothing to do with the engine (DEC-406).

**Criteo Uplift after Plan D M57 (2026-09-23).** Re-tested once more for Plan D M57: `go.criteo.net`
answered 403 and the `huggingface.co` and `ailab.criteo.com` tunnels were refused with 403, so the run
was skipped, as the plan says to when the network stays blocked. Any future run is **internal
validation, non-commercial licence** (ruling R2, DEC-852): its report is committed to
[`run_report.md`](../library/criteo-uplift/run_report.md) marked that way, and Criteo is never used in
demos, screenshots, sales material or the demo dataset.

**Criteo Uplift after Phase 3b (2026-09-23).** Of the two reasons DEC-406 recorded, the first — the
question is uplift and the engine could only answer propensity — is gone: Phase 3b added the
`uplift` problem type ([`docs/UPLIFT.md`](UPLIFT.md)), and
[`library/criteo-uplift/use_case.yaml`](../library/criteo-uplift/use_case.yaml) now uses it
(`problem_type: uplift`, `uplift.treatment_column: treatment`, outcome `conversion`, X-learner on
LightGBM, `exposure` and `visit` excluded). It resolves with the engine's own config loader. The
second reason is unchanged: on 2026-09-23 `fetch.py` and `curl` were refused by the egress proxy
(403) for `go.criteo.net`, `huggingface.co` and `ailab.criteo.com`, so there is still no sample, no
run and no number. The use case therefore stays **planned** and uninstalled, its tests keep pinning
that, and plan B's Criteo acceptance criterion (an AUUC interval above zero on the Criteo sample)
is **not met here**. The licence is still CC BY-NC-SA 4.0, non-commercial.

**The one blocker is a test, not the engine.** `load_industry`, `list_industries` and every loader
already take a config root and already validate each file on its own (DEC-038). The engine is
perfectly happy with four industries; two test assertions were not. Until they were relaxed, the
library's configs sat in `library/configs/` — a second root the engine already supports, with
`engine.yaml` symlinked so there was nothing to drift (DEC-400).

**Resolved in Plan A M38 (DEC-085, ruling D3).** The two tests now validate every industry file
instead of asserting there is exactly one, and the move was exactly what was promised: `git mv` of
the four industry files into `configs/industries/` and the four use-case files into
`configs/use_cases/`, then `python -m scripts.gen_templates`, which wrote the same eight template
files byte for byte into `templates/`. No engine or use-case file changed. `library/configs/` is
gone — its `engine.yaml` symlink had nothing left to serve — and the overview has an industry
selector that opens on Telecom.

**Three preparation steps were needed, and none of them was modelling.** Re-writing a
semicolon-separated file as comma CSV; adding a primary key to a file that ships none; renaming
four dotted column names. All three are format, all three are recorded in the dataset's
`mapping.yaml`, and all three are exactly what Phase 2's column-mapping UI is designed to absorb.

**One dataset needed real work, and the brief predicted it.** UCI Online Retail is a transaction
log, and the engine cannot aggregate (plan §12). `library/online-retail/fetch.py` builds the
one-row-per-customer file in the open, every derived column is explained in that directory's
README, and `mapping.yaml` states the aggregation for each one so Phase 2 has a specification to
reproduce.

### What the engine got right that is worth saying

* **It found the leaking column's absence honestly.** On the bank file it did *not* flag
  `duration`, and it was right not to: none of `LEAKAGE_SUSPECTED`'s three branches describes a
  column that does not contain the answer but simply does not exist yet. The library says so
  rather than claiming a catch it did not make — and shows that the fix is one line of
  configuration.
* **It returned a negative result rather than a flattering one.** On the win-back file a
  fifteen-minute, 106-candidate search tied with its own logistic-regression baseline on the
  primary metric, and `baseline.json` says `model_beats_baseline: false`. A system that could not
  do that would be worse than useless.
* **It refused nothing it should have accepted.** Five files trained, zero validation *errors*,
  one warning in total across all six runs.

### What the library could not demonstrate

* **Suppression and consent.** No public dataset carries an opt-out flag or a contact log, so all
  four library use cases set `opt_out_column` and `recently_contacted_column` to `null`
  (DEC-407, following `telco_churn.yaml`'s own precedent). `SUPPRESSION_COLUMN_MISSING` and
  `CONSENT_COLUMN_MISSING` are therefore never exercised here. That is a gap in the demo, not in
  the engine.
* **Time-based splits.** Not one of the six files carries a usable date. The bank file has a month
  with no year; the credit-card file has an ordered six-month series with no dates; the win-back
  file is a single snapshot. Every run used `random_stratified`, and on the bank file in particular
  a time-based split would be the more honest evaluation.
* **Scoring and drift.** The library trains; it does not exercise the score flow, `SCHEMA_MISMATCH`
  or the drift report. A scoring pass over a held-back slice of each file would be the obvious next
  addition.
* **Uplift.** See dataset 6 and DEC-406.

---

## 3. Running it

```bash
# one dataset, end to end
python library/uci-bank-marketing/fetch.py
python -m library.run_engine \
  --dataset uci-bank-marketing --use-case bank-term-deposit \
  --csv library/uci-bank-marketing/data/prepared.csv --primary-key client_id --target y

# the library's own tests: validate + a one-minute train on each committed sample
python -m pytest library/tests
```

The tests are **not** in pytest's default `testpaths`, so `make test` does not run them
(DEC-409). Downloads and run directories are git-ignored; each dataset's `sample.csv` is committed
so the tests work with no network.

## 4. The demo script

Which dataset to show to whom, and what to click:
**[`library/DEMO_SCRIPT.md`](../library/DEMO_SCRIPT.md)**.

## 5. Decisions

DEC-400 … DEC-411 in [`docs/DECISIONS.md`](DECISIONS.md), covering the config root, the win-back
label definition, the `duration` call, the derived keys, the Criteo position, and what the library's
tests assert.

**On that last one, briefly, because it is the least comfortable decision here.** The brief asks
each test to assert that test AUC beats the baseline. On a one-minute search over a few thousand
rows, that comparison is a coin flip for three of the five datasets. Measured over thirty-three
repeated runs, fifteen of them re-done after the engine gained tuning:

Every column below covers the same set: all runs on both engines.

| dataset | beats baseline | worst margin | ROC-AUC range | test asserts |
|---|---|---|---|---|
| `uci-credit-default` | 6 of 6 | **+0.0452** | 0.751 – 0.790 | **strictly beats**, plus a floor |
| `uci-bank-marketing` | 6 of 6 | +0.0045 | 0.931 – 0.943 | floor + not materially worse |
| `telco-customer-churn` | 4 of 6 | −0.0025 | 0.822 – 0.855 | floor + not materially worse |
| `health-insurance-cross-sell` | 4 of 6 | −0.0054 | 0.807 – 0.866 | floor + not materially worse |
| `online-retail` | 3 of 9 | −0.0627 | 0.531 – 0.691 | **no score at all** |

"Not materially worse" is `>= baseline - 0.02`, about four times the largest shortfall on the four
datasets that use it (0.0054). `online-retail` is excluded from even that, because its worst run lands more
than twice the tolerance below its baseline; its test checks the aggregation shape, the derived
target and the validation finding instead. Lowering a floor until a test stops failing would have
been quicker and would have meant nothing — and the proof that this was the right call is that not
one floor had to move when hyperparameter tuning appeared underneath the suite. DEC-410 has every
measurement.

## 6. Licences at a glance

| Dataset | Licence | Commercial use |
|---|---|---|
| Telco Customer Churn | Apache-2.0 (repository fetched); IBM sample data | yes |
| UCI Bank Marketing | CC BY 4.0 | yes, with attribution |
| UCI Online Retail | CC BY 4.0 | yes, with attribution |
| UCI Credit Default | CC BY 4.0 | yes, with attribution |
| Health Insurance Cross-Sell | reported **GNU GPL v2**, **unverified** — kaggle.com was unreachable | **check first** |
| Criteo Uplift | **CC BY-NC-SA 4.0 — non-commercial** | **no** |
| MineThatData E-Mail (Hillstrom) | **no explicit licence** — released publicly by its author for a 2008 challenge; listed in TensorFlow Datasets | **check before commercial redistribution** |

Each dataset's `LICENSE.txt` carries the citation, the attribution requirement and, where a page
could not be reached to confirm the licence, says exactly that.

## 7. Plan J M110: the whole journey on real randomised data

Before anyone outside the team sees a number, the full Plan J journey has to work on real randomised
data with real effects, not on planted ones (DEC-1320). The library had one randomised dataset, Criteo,
and it still cannot be fetched here. The product owner approved obtaining the MineThatData e-mail test
(Kevin Hillstrom, 2008) on 2026-10-09; it is now [`library/hillstrom-email/`](../library/hillstrom-email/),
with its use case [`hillstrom-email`](../configs/use_cases/hillstrom_email.yaml), and the journey runs
end to end on it.

**How it was run.** `python -m library.run_engine journey --dataset hillstrom-email` drives every step
through the product's own API in-process (`library/journey.py`) and renders
[`run_report.md`](../library/hillstrom-email/run_report.md) from what the run wrote
(`library/journey_report.py`); nothing in that report was typed by hand. The 64,000 customers are split
once, by a fixed seed, into 32,000 training rows (every model, check and setting) and 32,000 evaluation
rows (measurement only). Run ids are pinned, so three runs on the same file gave identical numbers;
the whole journey takes about two minutes (risk model capped at 10 minutes and 5 tuning trials;
the campaign-effect model is LightGBM).

**What it found, whichever way it fell** (all from the run report):

| Step | Result |
|---|---|
| Readiness | the uplift checks pass on both halves (random assignment not predictable from the customer details; every group large enough) |
| Risk model | test ROC-AUC 0.5341, and it does **not** beat its logistic-regression baseline: who buys in two weeks is barely predictable from this file |
| Campaign-effect model (two e-mails against no e-mail, M100) | AUUC 0.0004 (−0.0013 to 0.0017) for the men's e-mail; registered as a candidate, never champion |
| Beats risk ranking (M96, the engine's check) | **failed**: the difference 0.0002 (−0.0016 to 0.0019) includes zero. Its comparator, the risk model, was trained on the same training upload and had fitted 5,403 of the 6,392 hold-out rows (84.5 %), so it was scored partly in-sample; the out-of-sample `p_control` row agrees (−0.0000, −0.0022 to 0.0023) |
| Beats risk ranking, both models out of sample (evaluation rows) | **failed** for both e-mails: uplift minus the risk model's score −0.0000 (−0.0010 to 0.0007) for the men's e-mail and 0.0001 (−0.0007 to 0.0010) for the women's, by the same function and rule as M96 |
| Stable across folds (M96) | passed |
| Calibrated by decile (M96) | **failed**: 6 of 10 deciles contain the prediction |
| Treat list (M97, M100) | 12,862 of 32,000 evaluation customers get an e-mail outside the engine's control group (men's 7,597, women's 5,265) |
| Off-policy value on the evaluation rows | the chosen list adds **+0.45 pts** of conversion over no e-mail (+0.23 to +0.67), +5.66 pts of visits and +$0.62 of spend per customer (normal interval 0.28 to 0.95; a 2,000-resample bootstrap gives 0.29 to 0.94, since spend is long-tailed and flagged `OUTCOME_SKEWED`); but sending everyone the men's e-mail adds +0.69 pts, and the chosen list is measurably **worse** than that (−0.24 pts, −0.43 to −0.04) |
| Campaign measurement (replay, M94) | conversion **+0.60 pts** (+0.21 to +1.00, p = 0.002), about 26 extra conversions among the 4,263 contacted customers the replay kept |
| Spend with CUPED (M102) | +$0.84 per customer (+0.23 to +1.45); the pre-registered `history` covariate removed **0.07 %** of the variance, so the adjustment was honest and useless here |
| Value Proof Pack (M104) | built for both campaigns, labelled "public dataset, retrospective replay, a third of each group kept" in the campaign name the Pack prints, provenance verified; claim "Causal: the customers held back were chosen at random". Its Method text overstates a replay: see finding (3) |

**Read plainly.** The engine measured real effects correctly and proved them with traced numbers; the
e-mails work. What it did **not** show is that targeting adds anything on this file: the campaign-effect
model does not beat risk ranking, is not calibrated, and its list loses to the blanket men's e-mail. That
is the honest outcome on 32,000 training customers with about a hundred conversions per group, and the
approval checks said so before the list was measured. The engine's beats-risk check compared against a risk
model that had seen most of the hold-out, which may have tilted it towards "does not beat"; repeated
on the evaluation rows with both models out of sample, the answer is the same.

**How the campaign is measured on a retrospective file.** The engine draws its own random control group
(here half the list) when it scores. The file's e-mail was drawn independently, so a customer keeps their
outcome only when the e-mail the file sent them is the one the list gave them (or none, for the control
group): the replay estimator for uniformly randomised logs. The kept customers are a random third of each
arm, the rest are counted by the engine as customers without an outcome (never as non-converters), and the
comparison stays randomised. The pack credits what it measured on that third but costs every e-mail the
list meant to send, so its net value understates the list's. Both campaigns and their test plans are
recorded before the journey reads any evaluation row's outcome; every plan input comes from training rows.

**Assumptions** (each stated in the report): the file's money is 2008 dollars and the engine's is rupees,
so the journey converts at ₹83 per dollar (an input assumption, not a market quote); a customer's value per
conversion is their past-year spend scaled to one order (the scale measured on training rows only), revenue
before margin; an e-mail costs ₹0.05 (`configs/pilot/value.yaml`); `history` is dated the day before the
campaign by assumption (the file has no dates), so the point-in-time rule passes by construction and is not
a check here; and the campaigns carry an outcome window of 0 days, which each Pack prints, because
`POST /campaigns` refuses a treatment start before the scoring run and `measure` refuses while a window is
open, while the file's outcomes cover two weeks.

**Three findings for other milestones, no engine code changed.** (1) When uplift does not beat risk, the J5
fallback (DEC-1306 (h)) re-ranks the scores' single-offer contact list by the risk model, but the offer
choice of a model of several offers (DEC-1310 part B), and so the treat list, ignores it: on this run
1,703 customers the fallback marked "Never treat" for the first offer were given an e-mail. (2) A
campaign measured on an amount other than the use case's target (`spend`) gets a verdict headline worded
with the target's definition ("added about 3,581 to The customer bought…"): the campaign page and step 4
both label verdicts with `target.definition` whatever column was named. (3) The Value Proof Pack's
*Method and limits* text is fixed by `causal_basis`: on this replay it says "The engine chose who was held
back, at random, before the campaign went out" and "Outcomes are counted for everyone the campaign was meant
to reach, whether or not the message arrived", while nothing went out and 17,165 of the 25,599 intended
customers were left out as having no outcome (the Pack prints that count). The plan's `expectation` text,
which says it is a replay, does not reach the Pack. Open question for M104 (DEC-1314): the Method text
should depend on whether outcomes are missing by design (a replay) or by loss.

**The other randomised datasets: skipped, with the reason.**

| Dataset | Reachable from here (2026-10-10) | Licence | Outcome |
|---|---|---|---|
| Criteo Uplift | no: `go.criteo.net` does not resolve, `huggingface.co` 403, scikit-uplift's S3 mirror 403 | CC BY-NC-SA 4.0, internal validation only (R2) | skipped; no owner download either ([run report](../library/criteo-uplift/run_report.md)) |
| X5 RetailHero | no: `ods.ai` and its storage 403, scikit-uplift's S3 mirror 403 | competition terms, not readable from here | skipped |
| Lenta | the scikit-uplift S3 mirror answers (200, not downloaded) | none published that could be found; no owner approval | skipped: a file whose licence is unknown and not approved is not fetched |

```bash
python library/hillstrom-email/fetch.py                           # verified by SHA-256
python -m library.run_engine journey --dataset hillstrom-email     # about two minutes; rewrites run_report.md
python -m pytest library/tests/test_hillstrom_email.py             # the sample journey; no download
```

