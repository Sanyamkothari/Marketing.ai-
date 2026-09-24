# WP4 test changes

Every assertion WP4 changed, and why. WP4 reworked the Data, Model and Output pages (`ui/pages.js`).
Each change follows a deliberate change of wording or layout: plain tile labels, file facts and the
training setup behind "Technical details", lineage behind "Details: data lineage", "Download contact
list (CSV)" as the Output page's primary action, artefacts requested by run mode, and the redirect of an
uplift run's Model and Output to the uplift module's own screens. No behavioural assertion was loosened.
Where a value moved behind a disclosure, the test now opens the disclosure and still checks the same
value. New tests are listed at the end.

| File | Test | Old assertion | New assertion | Why |
|---|---|---|---|---|
| tests/integration/test_ui.py | test_the_pages_read_what_plan_section_7_assigns_them | `{"decile_lift.json", "scoring_summary.json", "drift.json"} <= pages["output"]` | `"decile_lift.json" in pages["output"]`; `{"scoring_summary.json", "drift.json"} <= pages["output_score"]`; `"decile_lift.json" not in pages["output_score"]` | `PAGE_ARTEFACTS` is keyed by run mode (`output` for a training run, `output_score` for a scoring run). A scoring run writes no `decile_lift.json`, and asking for it caused a 404 and a failed audit event (audit C17). The new assertion is stricter. |
| tests/unit/uplift/test_phase1_pages_uplift.py | test_the_uplift_pages_read_only_registered_artefacts | `set(pages) == {"data", "model", "output"}` | `set(pages) == {"data", "data_score"}` | An uplift run's Model and Output now redirect to `#/uplift/…` (audit C14) and read nothing here. The Data list is split by mode: a training run writes no `uplift_drift.json`, and a scoring run writes no `split.json` or `uplift_validation.json`. The "registered, and never a Phase-1-only artefact" checks are unchanged. |
| tests/unit/uplift/test_phase1_pages_uplift.py | test_the_uplift_pages_read_the_uplift_artefacts_m53_names | model ⊇ {uplift_evaluation, qini_curve}; output ⊇ {scoring_summary, segments, uplift_drift}; data ⊇ {uplift_validation, uplift_drift} | `uplift_validation.json` in `data`; `uplift_drift.json` in `data_score` | Same redirect. The uplift module's own screens read those artefacts. |
| tests/unit/uplift/phase1_pages_uplift.test.mjs | an uplift run never requests an artefact it does not write | `pageArtefacts(kind, trainRun)` equals `UPLIFT_PAGE_ARTEFACTS[kind]` for data, model and output | Data for train/score equals `data`/`data_score`, and never a Phase-1-only file; model and output are `[]` for both modes | Same redirect, and requests by mode. |
| tests/unit/uplift/phase1_pages_uplift.test.mjs | data page: a failed, acknowledged randomness check… | text `TREATMENT_NOT_RANDOM error (acknowledged)` | text `Treatment not random Error (acknowledged) Who was treated can be predicted. TREATMENT_NOT_RANDOM` | The finding now leads with words (the help.yaml title once the glossary is registered). The code is still rendered, in a "Show more columns" column. |
| tests/unit/uplift/phase1_pages_uplift.test.mjs | model page: the Qini curve and AUUC…; model page without the uplift module…; model page with nothing written…; output page: segments… | the Phase 1 renderer's uplift Model and Output (AUUC, Qini points, "has not produced … yet", treat list) | replaced by "an uplift run's Model and Output redirect to the uplift module's own screens" and "an uplift run's Data page links its Model and Output tabs to the uplift screens" | `upliftModelPage` and `upliftOutputPage` were removed (the audit's C14 duplicate). The same content is on `#/uplift/<uc>/{model,output}/<run>` (uplift module, WP5), which has its own tests in `uplift_ui.test.mjs`. |
| tests/unit/uplift/phase1_pages_uplift.test.mjs | a Phase 1 run still gets the Phase 1 pages | `renderPage("model", …, {})` shows "Confusion matrix" | with an evaluation and a confusion matrix, it shows "How good is this model?" and "Confusion matrix", and no Qini | With nothing written, the Model page is now one "appears when training finishes" card instead of empty cards. |
| tests/integration/test_acceptance.py | (journey) step 5 | `key_values(page)` read straight after the page painted | `open_details(page)` first, then `key_values(page)` | The file's format/rows/columns and the training setup (Target, Algorithm) moved into "Technical details". `innerText` of a closed `<details>` is empty, so the test opens it, as a user would. The same key/value pairs are asserted. |
| tests/integration/test_acceptance.py | (journey) step 7 | link "Download all scored rows (CSV)" | link "Download contact list (CSV)" | This is now the Output page's primary action (plan top 5 #3). It is the same `/runs/{id}/scores.csv` file. |
| tests/integration/test_acceptance.py | test_the_data_page_renders_the_profile_and_the_split | tile "Features" | tile "Details used to predict" | Plain tile label. |
| tests/integration/test_acceptance.py | test_the_model_page_renders_the_trained_model | tiles "Algorithm", "Last trained", "ROC-AUC" | tiles "Model", "Trained", "Ranking quality" | Plain tile labels. ROC-AUC keeps its code in the evaluation table's "Code" column and in Technical details. Same value checks. |
| tests/integration/test_acceptance.py | test_the_output_page_of_the_training_run_renders_the_lift | tile "Lift (top decile)" | tile "Lift in the top 10%" | Plain tile label (no "decile"). |
| tests/integration/test_acceptance.py | test_the_output_page_of_the_scoring_run_renders_the_kpi | tiles "Rows scored", "Control group" | tiles "Customers scored", "Held back to measure results" | Plain tile labels (audit run-output). Same values. |
| tests/integration/test_onboarding_acceptance.py | (journey) step 4 | `.lineage` visible on arrival | clicks "Details: data lineage", then `.lineage` visible | Lineage is behind a disclosure (plan WP4). The five layer titles are still asserted. |
| tests/integration/test_onboarding_acceptance.py | (journey) step 6 | link "Download all scored rows (CSV)" | link "Download contact list (CSV)" | As above. |
| tests/integration/uplift/test_uplift_browser.py | (journey) step 4 | Phase 1 Output of the uplift scoring run: `.tab.on` "Output", then `.uentry a` "Campaign results for this run" | redirected: `main[data-module=uplift]`, `.tab.on` "Output", then the "Campaign results" tab | `#/uc/<uc>/output/<uplift run>` redirects to the uplift module's Output (audit C14), where the injected `.uentry` strip is not drawn. The run id and the campaign screen that follows are unchanged. |

## New tests

In `tests/unit/uplift/phase1_pages_uplift.test.mjs` (run by `test_phase1_pages_uplift.py::test_node_suite_passes`):

- a Phase 1 run reads by its mode: a scoring run never asks for `decile_lift.json` or `split.json`.
- an uplift run's Model and Output redirect (a `data-redirect` and a link); its Data page's tabs point at the uplift screens, with Campaign results on a scoring run.
- scoring Output: one primary "Download contact list (CSV)"; KPI with "High 851 + Medium 4"; "Customers scored" and "Held back to measure results (10%)"; the drift notice by its help.yaml title; readable sample columns and reasons; no "decile", "Lift", `.json` or "has not produced" on screen.
- training Output: one lift tile with the sentence; value labels on D1 to D3 only; primary "Score new customers with this model".
- Model: the ROC-AUC verdict sentence; `THRESHOLD_FALLBACK` by its title outside Technical details (and kept inside); the calm baseline warning; plain tiles; top 8 drivers with "Show all"; no Fairness card unless evaluated; one primary.
- Model of a scoring run: a link to the training run's Model page.
- Data: glance tiles, lineage behind "Details: data lineage", grouped pipeline steps, features sorted by missing values, no label source for a built dataset, no unknown date range, file facts only in Technical details.
