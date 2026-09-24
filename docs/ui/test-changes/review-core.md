# Core UI review fixes: test changes

The UI-only review fixes outside `ui/modules/uplift/*` and `ui/modules/production/*`: no run action
merged into the run pages' header, an exact count on the Output KPI, the client chooser on the privacy
consent / erasure / access screens, "Ranking quality" instead of "ROC-AUC" on the Results summary and
the Previous runs rows, "the model in use" instead of "champion" in the governance setting copy,
colour (not opacity) for locked steps and pending blocks, one verdict on the pilot value view, and
"Continue to Mapping" as the Sources step's primary button.

Every assertion below changed because its text changed on purpose. No assertion was loosened.

| File | Test | Old | New | Why |
|---|---|---|---|---|
| tests/unit/test_advanced_settings_schema.py | `test_labels_orders_and_bounds` (`LABELS["governance.approval_required"]`) | `Require approval before a model becomes champion` | `Require approval before a model becomes the model in use` | Plain wording: "champion" is jargon; the UI already says "the model in use" everywhere else. Display copy only, the setting key is unchanged. |
| tests/unit/test_stage_summaries.py | `test_the_eight_rendered_summaries_are_verbatim` (governance line, design section 4.7) | `Keep uploads 90 days · approval before champion` | `Keep uploads 90 days · approval before a model becomes the one in use` | Same wording change, in the stage's summary template. |
| tests/integration/test_acceptance.py (slow, not run here) | `test_the_run_reached_results_and_the_summary_carries_real_values` | `"ROC-AUC" in journey.train_summary` and `re.search(r"ROC-AUC\s+([0-9.]+)", ...)` | `"Ranking quality" in journey.train_summary` and `re.search(r"Ranking quality\s+([0-9.]+)", ...)` | The Results summary names the metric by its plain name, as the Model page does. The check that a real score between 0 and 1 is shown is unchanged. |

`docs/API.md` (generated) carries the new label too; `python -m scripts.gen_api_docs --check` passes.
