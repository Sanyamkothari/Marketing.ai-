# Criteo Uplift Prediction Dataset — reserved for Phase 3b

**Industry** Ad-tech · **Use case** `criteo-uplift`, **planned**, see
[`configs/industries/ad_tech.yaml`](../configs/industries/ad_tech.yaml) ·
**Targets** `conversion` and `treatment`

The only entry in the library with no run behind it, for two independent reasons. Both are stated
plainly here because a library of demos whose sixth entry quietly looked like the other five would
be dishonest.

| | |
|---|---|
| Upstream | <https://ailab.criteo.com/criteo-uplift-prediction-dataset/> |
| Direct link | `http://go.criteo.net/criteo-research-uplift-v2.1.csv.gz` (311 MB gzip) |
| Mirror | <https://huggingface.co/datasets/criteo/criteo-uplift> |
| Licence | **CC BY-NC-SA 4.0 — non-commercial.** See [LICENCE](LICENSE.txt) |
| Rows | 13,979,592 (v2.1) |
| Columns | 16 — 12 anonymised features, `treatment`, `visit`, `exposure`, `conversion` |
| Status in this library | **mapped, not fetched, not trained** |

## Reason one: the question is Phase 3b, not Phase 1

Every other dataset here asks *who is likely to convert?*, which is the propensity question the
engine answers today. This one exists to ask a different question: *how much does showing the ad
change whether they convert?* That is uplift, and plan §12 puts it in Phase 3 — "uplift modelling
for Win-back using the control group data".

The distinction is not academic, and it is the reason this dataset is not simply trained as a
binary classifier on `conversion`. 85 % of the rows are treated and 15 % are control; the treated
rows convert at about 0.31 % and the control rows at about 0.19 %. A propensity model trained on
`conversion` across both groups would rank users by *how likely they are to convert anyway*, which
includes the people who would have converted with no ad at all. Spending the ad budget on them is
precisely the mistake uplift modelling exists to prevent. The engine would train that model happily
and the leaderboard would look respectable, and the result would be worse than useless as advice.

So `configs/industries/ad_tech.yaml` lists `criteo-uplift` as **planned**. The use-case file is
drafted in [`use_case.yaml`](use_case.yaml) next to this README rather than installed in
`library/configs/use_cases/`, so the engine does not offer a model it cannot yet make honest.

## Reason two: the data could not be fetched here

`ailab.criteo.com`, `go.criteo.net` and `huggingface.co` are **all blocked** by the egress policy
of the environment this library was built in. Every reachable alternative was tried and none serves
the file:

| Host | Result |
|---|---|
| `go.criteo.net` | 403 from the egress proxy (policy denial) |
| `huggingface.co` (and `cdn-lfs.huggingface.co`) | connection refused by the egress proxy |
| `criteo-uplift.s3.amazonaws.com`, `s3.amazonaws.com/criteo-uplift` | 404 — no such bucket or key |
| `storage.googleapis.com/criteo-cail-datasets` | 403 |
| GitHub mirrors of a small sample | none found; the repositories that use the dataset download it at runtime |

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
observational study with an obvious confound. Criteo's own documentation flags this. Any Phase 3b
work on this file must treat `treatment` as the lever and `exposure` as a result.

Two more things a reader should know:

**Conversion is very rare.** About 0.29 % overall. At 1 M sampled rows that is roughly 2,900
positives — enough for the engine's 200-positive floor, but the metric to read is PR-AUC, and
`TARGET_IMBALANCE_SEVERE` (which fires below 1 %) would warn on every run.

**`visit` is the easier label.** About 4.7 % of rows, sixteen times more common than conversion.
Uplift work on this dataset usually reports both.

## Non-commercial licence

CC BY-NC-SA 4.0. This dataset may not be used in a commercial demo, a sales deck, a customer pilot
or a shipped product. That is why it appears in [`library/DEMO_SCRIPT.md`](../DEMO_SCRIPT.md) only
as a do-not-demo entry, and why the ad-tech industry file has nothing available under it. See [`LICENSE.txt`](LICENSE.txt).

## Files here

| File | What it is |
|---|---|
| `README.md` | this |
| `LICENSE.txt` | the licence record, and the non-commercial warning |
| `fetch.py` | the downloader and sampler. **Never executed in this environment.** |
| `mapping.yaml` | their column → standard column, with the roles and the `exposure` warning |
| `use_case.yaml` | the drafted use case, to install when Phase 3b lands |
| `run_report.md` | the report that says no run happened, and what would have to be true for one to |
