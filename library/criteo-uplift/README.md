# Criteo Uplift Prediction Dataset — runnable by Phase 3b, not run

**Industry** Ad-tech · **Use case** `criteo-uplift`, **planned**, see
[`configs/industries/ad_tech.yaml`](../configs/industries/ad_tech.yaml) ·
**Outcome** `conversion` · **Treatment** `treatment`

The only entry in the library with no run behind it. Until Phase 3b there were two independent
reasons; since Phase 3b (uplift modelling, see [`docs/UPLIFT.md`](../../docs/UPLIFT.md)) there is
one left, and it is not the engine: **the data cannot be fetched here**. Both facts are stated
plainly because a library of demos whose sixth entry quietly looked like the other five would be
dishonest.

| | |
|---|---|
| Upstream | <https://ailab.criteo.com/criteo-uplift-prediction-dataset/> |
| Direct link | `http://go.criteo.net/criteo-research-uplift-v2.1.csv.gz` (311 MB gzip) |
| Mirror | <https://huggingface.co/datasets/criteo/criteo-uplift> |
| Licence | **CC BY-NC-SA 4.0 — non-commercial.** See [LICENCE](LICENSE.txt) |
| Rows | 13,979,592 (v2.1) |
| Columns | 16 — 12 anonymised features, `treatment`, `visit`, `exposure`, `conversion` |
| Status in this library | **mapped, configured for the uplift problem type, not fetched, not trained** |

## Reason one, now resolved: the question is uplift, not propensity

Every other dataset here asks *who is likely to convert?*, which is the propensity question Phase 1
answers. This one exists to ask a different question: *how much does showing the ad change whether
they convert?* That is uplift.

The distinction is not academic, and it is the reason this dataset was never simply trained as a
binary classifier on `conversion`. 85 % of the rows are treated and 15 % are control; the treated
rows convert at about 0.31 % and the control rows at about 0.19 % (Criteo's published figures). A
propensity model trained on `conversion` across both groups would rank users by *how likely they
are to convert anyway*, which includes the people who would have converted with no ad at all.
Spending the ad budget on them is precisely the mistake uplift modelling exists to prevent.

Phase 3b added what was missing: the `uplift` problem type, a configured treatment column that is
never used as a feature, the AUUC metric with Qini curves, and S-, T- and X-learners. So
[`use_case.yaml`](use_case.yaml) is now written for that problem type — `problem_type: uplift`,
`uplift.treatment_column: treatment`, outcome `conversion`, `uplift.base_model: lightgbm`,
X-learner — and it validates against the engine's configuration schema (checked on 2026-09-23 by
resolving it from a copy of `library/configs/`; on a synthetic frame with the published columns the
uplift feature selection keeps exactly `f0` … `f11`). It is still **not installed** in
`library/configs/use_cases/`, and `ad_tech.yaml` still lists it as **planned**, because of reason
two.

## Reason two, still open: the data could not be fetched here

`ailab.criteo.com`, `go.criteo.net` and `huggingface.co` are **all blocked** by the egress policy
of the environment this library was built in. Every reachable alternative was tried and none serves
the file. **Re-tested on 2026-09-23** with `curl` and by starting `fetch.py` itself (both the direct
link and `--mirror`): the answers were the same as in the table below, and not one row was read.

| Host | Result (first attempt) | Result on 2026-09-23 |
|---|---|---|
| `go.criteo.net` | 403 from the egress proxy (policy denial) | 403 over `http`; the `https` tunnel refused with 403 |
| `huggingface.co` (and `cdn-lfs.huggingface.co`) | connection refused by the egress proxy | tunnel refused with 403 (`huggingface.co`) |
| `ailab.criteo.com` | — | tunnel refused with 403 |
| `criteo-uplift.s3.amazonaws.com`, `s3.amazonaws.com/criteo-uplift` | 404 — no such bucket or key | 404 (`criteo-uplift.s3.amazonaws.com`) |
| `storage.googleapis.com/criteo-cail-datasets` | 403 | 403 |
| GitHub mirrors of a small sample | none found; the repositories that use the dataset download it at runtime | not re-searched |

So there is **no `sample.csv` and no `run_report.md` numbers for this dataset**, because there is no
data to sample and nothing was run. `fetch.py` is written against the real URL and will work on a
network that allows it; what it produces is described below.

## What `fetch.py` would produce

It downloads the gzip and streams it in 500,000-row chunks, keeping the same proportion of each
chunk so that the 13.98 M rows are never resident at once and the 85/15 treated/control ratio
survives. It takes at most **1,000,000 rows** as the brief asks, adds the primary key the file
lacks, and writes `data/prepared.csv` plus a 5,000-row `sample.csv`.

The one derived column is `impression_id`: the file ships no key, and the data contract needs one.
It is the row number in the published order, exactly as `client_id` is for the bank dataset.

## The columns

| Column | What it is |
|---|---|
| `f0` … `f11` | twelve anonymised, dense float features. Criteo publishes no meaning for them. |
| `treatment` | 1 when the user was in the group eligible to be shown the ad, 0 when held out. Randomised. |
| `exposure` | 1 when the ad was actually shown. **A treated user may not be exposed.** |
| `visit` | 1 when the user visited the advertiser's site. |
| `conversion` | 1 when the user converted. The outcome. |

## The quirk that decides the whole analysis

**`exposure` is an outcome, not a treatment.** `treatment` is randomised, so comparing treated and
control groups is a valid experiment. `exposure` is what happened afterwards — whether an ad slot
was actually won and rendered — and it is correlated with everything about the user. Conditioning
on it, or using it as a feature, breaks the randomisation and turns a clean experiment into an
observational study with an obvious confound. Criteo's own documentation flags this. The use case
treats `treatment` as the lever (`uplift.treatment_column`) and `exposure` as a result
(`prepare.exclude_columns`).

Two more things a reader should know:

**Conversion is very rare.** About 0.29 % overall. At 1 M sampled rows that is roughly 2,900
positives, and about 285 of them in the 15 % control arm — above the uplift check's floor of 50
positives per arm (`TREATMENT_ARM_TOO_SMALL`), but thin: expect wide AUUC intervals.
`TARGET_IMBALANCE_SEVERE` (which fires below 1 %) would warn on every run. These counts are
arithmetic on Criteo's published rates, not counts this library made.

**`visit` is the easier label.** About 4.7 % of rows, sixteen times more common than conversion.
Uplift work on this dataset usually reports both.

## How to run it once the data is reachable

1. Check the licence (below). Then `python library/criteo-uplift/fetch.py` on a network that allows
   `go.criteo.net` (or `--mirror` for Hugging Face).
2. Replace the invented `examples` in `use_case.yaml` with values from `data/prepared.csv`, copy it
   to `library/configs/use_cases/criteo_uplift.yaml`, flip `ad_tech.yaml`'s entry to `available`,
   and update `library/tests/test_criteo_uplift.py`, which pins today's not-installed state.
3. Start an uplift run from the uplift screens (`#/uplift`) or `POST /uplift/runs` with
   `treatment_column: treatment` and target `conversion` — not from Phase 1's Setup, which does not
   offer uplift (DEC-608). `library/run_engine.py` reads Phase 1's artefacts and cannot report an
   uplift run as it stands.
4. Expect `TREATMENT_NOT_RANDOM` to pass (the assignment is randomised) and
   `OUTCOME_WINDOW_IMMATURE` and `FEATURE_AFTER_TREATMENT` to be skipped (the file has no dates).
   The plan B acceptance test asks for an AUUC interval above zero on this sample; nobody knows yet
   whether it will be, and a run report would have to say whichever it is.

## Non-commercial licence

CC BY-NC-SA 4.0. This dataset may not be used in a commercial demo, a sales deck, a customer pilot
or a shipped product. That is why it appears in [`library/DEMO_SCRIPT.md`](../DEMO_SCRIPT.md) only
as a do-not-demo entry, and why the ad-tech industry file has nothing available under it. See [`LICENSE.txt`](LICENSE.txt).

## Files here

| File | What it is |
|---|---|
| `README.md` | this |
| `LICENSE.txt` | the licence record, and the non-commercial warning |
| `fetch.py` | the downloader and sampler. **Never run to completion in this environment**; started on 2026-09-23 and refused before any row was read. |
| `mapping.yaml` | their column → standard column, with the roles and the `exposure` warning |
| `use_case.yaml` | the uplift use case (`problem_type: uplift`), valid for the engine, not installed |
| `run_report.md` | the report that says no run happened, and what would have to be true for one to |
