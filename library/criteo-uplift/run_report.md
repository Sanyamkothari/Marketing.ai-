# Criteo Uplift — run report

**No run happened. No model was trained. No number in this library came from this dataset.**

Every other `run_report.md` in `library/` quotes figures a real engine run produced. This one has
none, and inventing them would be the single worst thing this library could do. What follows is
what was attempted, what stopped it, and what has to be true before a run is possible.

| | |
|---|---|
| Dataset | Criteo Uplift Prediction Dataset v2.1 |
| Use case | `criteo-uplift` — **planned**; [`use_case.yaml`](use_case.yaml) is written for `problem_type: uplift` and valid for the engine, not installed |
| Run attempted | **no** — there is no data to run on |
| Data fetched | **no** — re-tested 2026-09-23, still refused |
| Reported metrics | **none** |

## Status on 2026-09-23 (Phase 3b, milestone M45)

Plan B's M45 asks for a Criteo run report, and its acceptance test asks for an AUUC interval above
zero on the Criteo sample. **Neither could be produced here**, and this report says so instead of
substituting anything:

* The engine side is ready. Phase 3b added the `uplift` problem type, and
  [`use_case.yaml`](use_case.yaml) now uses it: `problem_type: uplift`, `uplift.treatment_column:
  treatment`, outcome `conversion`, X-learner on LightGBM, `exposure` and `visit` excluded. It was
  checked by resolving it with the engine's own config loader from a copy of `configs/`,
  and on a synthetic frame with the published columns (random numbers, not Criteo data) the uplift
  feature selection kept exactly `f0` … `f11`. No model was trained on that frame and no number from
  it is reported anywhere.
* The data side is not. On 2026-09-23 `python library/criteo-uplift/fetch.py` was started in this
  environment and the direct link was refused with HTTP 403; `--mirror` was refused with a 403 on
  the proxy tunnel. `curl` gave the same answers for every host in the table below. Not one row was
  read.

So the use case stays **planned** in `ad_tech.yaml` and out of `configs/use_cases/`, and
`library/tests/test_criteo_uplift.py` keeps pinning that state. Plan B's Criteo acceptance
criterion is **not met** in this environment.

## Why no run before Phase 3b: the brief said so

The library brief reserved this dataset for Phase 3b uplift and said *"prepare the config and a
mapped sample, do not run training"*. The config was prepared ([`use_case.yaml`](use_case.yaml))
and the mapping written ([`mapping.yaml`](mapping.yaml)).

The reason behind the instruction is the important part, and it is worth restating where a reader
will find it. Training a binary classifier on `conversion` would work — the engine would accept the
file, the leaderboard would fill up, and the ROC-AUC would look like the other five reports. It
would also answer the wrong question. 85 % of these rows were randomly made eligible for an ad and
15 % were held out; a propensity model over both groups ranks users by *how likely they are to
convert anyway*, and the people at the top of that ranking include everyone who would have
converted with no ad at all. Buying impressions for them is the exact mistake uplift modelling
exists to prevent. A respectable-looking leaderboard on the wrong target is worse than no model,
because somebody will act on it.

## Why no data either: the source is blocked here

Independently of the above, the file could not be obtained. Every reachable route was tried, first
when the library was built and again on 2026-09-23:

| Host | Attempted URL | First attempt | 2026-09-23 |
|---|---|---|---|
| `go.criteo.net` | `http://go.criteo.net/criteo-research-uplift-v2.1.csv.gz` | **403** from the egress proxy — organisation policy denial | **403** (`curl` and `fetch.py`); `https://` tunnel refused with 403 |
| `huggingface.co` | `.../datasets/criteo/criteo-uplift/resolve/main/criteo-research-uplift-v2.1.csv.gz` | connection refused by the egress proxy | tunnel refused with **403** (`curl` and `fetch.py --mirror`) |
| `huggingface.co` | `/api/datasets/criteo/criteo-uplift` | connection refused | tunnel refused with **403** |
| `cdn-lfs.huggingface.co` | root | connection refused | not re-tested |
| `ailab.criteo.com` | `/criteo-uplift-prediction-dataset/` | — | tunnel refused with **403** |
| `criteo-uplift.s3.amazonaws.com` | `/criteo-uplift-v2.1.csv.gz`, `/criteo-uplift.csv.gz` | **404** — no such key | **404** (`/criteo-uplift-v2.1.csv.gz`) |
| `s3.amazonaws.com` | `/criteo-uplift/criteo-uplift-v2.1.csv.gz` | **404** | not re-tested |
| `storage.googleapis.com` | `/criteo-cail-datasets/criteo-uplift-v2.1.csv.gz` | **403** | **403** |
| GitHub | searched for a committed sample in the repositories that use the dataset | none found; they all download it at runtime | not re-searched |

Per the environment's own guidance, an egress policy denial is reported, not routed around.

Consequences, stated so nobody has to infer them:

* there is **no `sample.csv`** in this directory, because there is no data to sample from;
* there is **no pytest** in `library/tests/` for this dataset, for the same reason;
* [`fetch.py`](fetch.py) has **never run to completion**: started on 2026-09-23, it was refused
  before reading a row. It is written against the published schema and carries a `verify()` step
  that fails loudly on the first real download if the schema has moved;
* the `examples` values in [`use_case.yaml`](use_case.yaml) are invented placeholders in the right
  shape, labelled as such in that file. No real value from this dataset appears anywhere in this
  library.

## What a run would need

**1. The data.** Run `python library/criteo-uplift/fetch.py` on a network that allows
`go.criteo.net` or `huggingface.co`. It streams the 311 MB gzip, samples at most 1,000,000 rows
(the brief's ceiling) proportionally so the 85/15 treated/control ratio survives, adds the
`impression_id` key the file lacks, and writes `data/prepared.csv` and a 5,000-row `sample.csv`.
Check the licence first: **CC BY-NC-SA 4.0, non-commercial**.

**2. The engine: done in Phase 3b.** The three things the Phase 1 schema could not express, as this
report listed them before Phase 3b, and what now answers each:

| Was missing | Now |
|---|---|
| A `treatment` column **role** | `uplift.treatment_column` names the randomised lever; the uplift engine never uses it as a feature, and six checks (`TREATMENT_NOT_RANDOM` among them) run on it before training. The template still has no treatment role, so `use_case.yaml` lists the column with role `feature` and says why. |
| An uplift **metric** | `auuc`, with the Qini curve, uplift@10/20/30 %, deciles and bootstrap intervals (`engine/uplift/metrics.py`). |
| Uplift **model families** | S-, T- and X-learners on LightGBM or AutoGluon (`engine/uplift/learners.py`). |

**3. Installing and running.** Replace the invented `examples` in `use_case.yaml`, copy it to
`configs/use_cases/criteo_uplift.yaml`, flip `ad_tech.yaml` to `available`, update
`library/tests/test_criteo_uplift.py`, and start the run from `#/uplift` or `POST /uplift/runs`.
`library/run_engine.py` reads Phase 1 artefacts (`prepare.json`, `leaderboard.json`,
`evaluation.json`) that an uplift run does not write, so a Criteo run report would be written from
`uplift_evaluation.json` and `qini_curve.json` instead; that harness change has not been made.

## The `exposure` trap, once more

`exposure` — whether the ad was actually rendered — is the one column most likely to be used by
mistake. It is an **outcome**, decided after randomisation by an auction, and correlated with
everything about the user. Using it as a feature, or filtering the data on it, converts a clean
randomised experiment into an observational study with an obvious confound. The drafted use case
puts it in `prepare.exclude_columns`, and [`mapping.yaml`](mapping.yaml) gives it the role
`excluded` with the reason attached.
