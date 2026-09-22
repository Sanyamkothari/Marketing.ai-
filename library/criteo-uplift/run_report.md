# Criteo Uplift — run report

**No run happened. No model was trained. No number in this library came from this dataset.**

Every other `run_report.md` in `library/` quotes figures a real engine run produced. This one has
none, and inventing them would be the single worst thing this library could do. What follows is
what was attempted, what stopped it, and what has to be true before a run is possible.

| | |
|---|---|
| Dataset | Criteo Uplift Prediction Dataset v2.1 |
| Use case | `criteo-uplift` — **planned**, drafted in [`use_case.yaml`](use_case.yaml), not installed |
| Run attempted | **no** |
| Data fetched | **no** |
| Reported metrics | **none** |

## Why no run: the brief says so

The brief reserves this dataset for Phase 3b uplift and says *"prepare the config and a mapped
sample, do not run training"*. The config is prepared ([`use_case.yaml`](use_case.yaml)) and the
mapping is written ([`mapping.yaml`](mapping.yaml)).

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

Independently of the above, the file could not be obtained. Every reachable route was tried:

| Host | Attempted URL | Result |
|---|---|---|
| `go.criteo.net` | `http://go.criteo.net/criteo-research-uplift-v2.1.csv.gz` | **403** from the egress proxy — organisation policy denial |
| `huggingface.co` | `.../datasets/criteo/criteo-uplift/resolve/main/criteo-research-uplift-v2.1.csv.gz` | connection refused by the egress proxy |
| `huggingface.co` | `/api/datasets/criteo/criteo-uplift` | connection refused |
| `cdn-lfs.huggingface.co` | root | connection refused |
| `criteo-uplift.s3.amazonaws.com` | `/criteo-uplift-v2.1.csv.gz`, `/criteo-uplift.csv.gz` | **404** — no such key |
| `s3.amazonaws.com` | `/criteo-uplift/criteo-uplift-v2.1.csv.gz` | **404** |
| `storage.googleapis.com` | `/criteo-cail-datasets/criteo-uplift-v2.1.csv.gz` | **403** |
| GitHub | searched for a committed sample in the repositories that use the dataset | none found; they all download it at runtime |

Per the environment's own guidance, an egress policy denial is reported, not routed around.

Consequences, stated so nobody has to infer them:

* there is **no `sample.csv`** in this directory, because there is no data to sample from;
* there is **no pytest** in `library/tests/` for this dataset, for the same reason;
* [`fetch.py`](fetch.py) has **never been executed**. It is written against the published schema
  and carries a `verify()` step that fails loudly on the first real download if the schema has
  moved;
* the `examples` values in [`use_case.yaml`](use_case.yaml) are invented placeholders in the right
  shape, labelled as such in that file. No real value from this dataset appears anywhere in this
  library.

## What a run would need

Two things, in this order.

**1. The data.** Run `python library/criteo-uplift/fetch.py` on a network that allows
`go.criteo.net` or `huggingface.co`. It streams the 311 MB gzip, samples at most 1,000,000 rows
(the brief's ceiling) proportionally so the 85/15 treated/control ratio survives, adds the
`impression_id` key the file lacks, and writes `data/prepared.csv` and a 5,000-row `sample.csv`.
Check the licence first: **CC BY-NC-SA 4.0, non-commercial**.

**2. Phase 3b.** Three things the current schema cannot express, each already half-decided
elsewhere in the repository:

| Missing | Why it matters here | Where it is already noted |
|---|---|---|
| A `treatment` column **role** | The template roles are `primary_key \| time \| feature \| target \| consent \| contact`. A randomised lever is none of them. Declaring it a feature — which the draft is forced to do — is what turns the experiment into a correlation. | `engine/config.py`, `ColumnRole` |
| An uplift **metric** | `binary_classification` scores separation of converters from non-converters. Qini and the uplift curve score what the ad *changed*. | DEC-009 — "Uplift (Qini)" was cut from the metric list in Phase 1 |
| Uplift **model families** | A T-learner can be assembled from families the engine already has, but no recipe field says "fit one model per treatment arm and subtract". | DEC-009 — "Uplift forest" was cut from the candidate pool |

None of these is filed as a cross-branch request, because none of them is a small change and all
three are already scheduled: plan §12 puts uplift in Phase 3. This report exists to make sure that
when Phase 3b starts, the mapping, the licence position and the `exposure` trap are already
written down.

## The `exposure` trap, once more

`exposure` — whether the ad was actually rendered — is the one column most likely to be used by
mistake. It is an **outcome**, decided after randomisation by an auction, and correlated with
everything about the user. Using it as a feature, or filtering the data on it, converts a clean
randomised experiment into an observational study with an obvious confound. The drafted use case
puts it in `prepare.exclude_columns`, and [`mapping.yaml`](mapping.yaml) gives it the role
`excluded` with the reason attached.
