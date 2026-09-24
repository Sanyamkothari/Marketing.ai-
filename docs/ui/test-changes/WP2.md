# WP2 test changes (use-case Setup, running and results)

Only text that changed on purpose (plan WP2, docs/UI_AUDIT.md 5.2 and 5.3). No behavioural assertion was loosened: every
value, default and flow assertion is unchanged.

| File | Test | Old assertion | New assertion | Why |
|---|---|---|---|---|
| `tests/integration/production/ui/usecase/score_model.test.mjs` | right after training, Score mode starts on the model just trained, not the champion | `assert.match(select().selectedOptions[0].textContent, /ROC-AUC/)` | `assert.match(select().selectedOptions[0].textContent, / · trained /)` | The Score mode option is now "<model> · trained <date> · in use" (plan WP2: the metric left the option text). The value assertion `select().value === ids.justTrained` is unchanged. |
| `tests/integration/production/ui/usecase/score_model.test.mjs` | same test | `assert.match(champion.textContent, /· Champion$/, ...)` | `assert.match(champion.textContent, /· in use$/, ...)` | "Champion" is jargon; the in-use model is tagged "in use" (help.yaml `terms.champion`: "the model currently approved for use"). |
| `tests/integration/test_api_config.py` | test_use_case_body_validates_and_carries_the_setup_screen | `body.target.label == "Target column"` | `body.target.label == "What to predict (outcome column)"` | Plain copy in `configs/engine.yaml` `ui.target_label` (plan WP2). |
| `tests/integration/test_api_config.py` | test_model_choices_start_with_automl_and_use_catalog_labels | `(first.value, first.label) == ("__automl__", "AutoML (recommended)")` | `(first.value, first.label) == ("__automl__", "Best model, picked automatically (recommended)")` | Plain copy in `configs/engine.yaml` `catalog.automl_choice.label` (plan WP2). The sentinel value is unchanged. |

Unchanged and still passing: `tests/integration/test_acceptance.py` (".summary" still carries "Training complete",
"ROC-AUC <score>", "Scoring complete" and "N rows scored"; `#f-again`, `.flow .block`, `#prog`, `#f-pk`, `#f-target`,
"Run training", "Score new data", "Score data" kept), `tests/integration/test_inactive_settings_ui.py` (the folded
`<div class="ss">Coming later` summary now sits in the planned-settings section, same markup).
