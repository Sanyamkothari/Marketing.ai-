# WP3 test changes (Build from raw tables)

Every assertion changed on purpose by WP3, and why. No behavioural assertion was loosened.

| File | Test | Old | New | Why |
|---|---|---|---|---|
| `tests/integration/test_onboarding_acceptance.py` | `test_every_raw_table_got_its_proposed_role` | `journey.roles == ["Activity", "Bills", "Complaints", "Entity"]` | `journey.roles == ROLE_LABELS` (`["Activity", "Bills", "Complaints", "Subscriber table"]`) | The Role column reads in plain words: the entity role is now "<use case's own noun> table" (`uc.entity`, "subscriber" for telco churn) instead of the internal "Entity". Same four roles, same confirm flow. |
| `tests/integration/test_onboarding_acceptance.py` | `test_next_months_tables_replayed_the_recipe_without_a_mapping_to_review` | `journey.score_roles == ["Activity", "Bills", "Complaints", "Entity"]` | `journey.score_roles == ROLE_LABELS` | Same wording change in score mode's third cell (`td:nth-child(3)`, kept). "replayed exactly as it was" is unchanged. |
| `tests/unit/test_onboarding_ui.py` | `UI_READS` | `BuildReport` without `rows_out`; `DatasetManifest` without `snapshot_dates` | adds `BuildReport.rows_out` and `DatasetManifest.snapshot_dates` | The build verdict line ("Dataset ready: 3,300 rows across 11 month-ends, …") reads these two fields; the table is pinned from both ends, so new reads are listed. Additive. |
| `tests/unit/test_onboarding_ui.py` | new: `test_an_event_tables_maps_to_options_are_named_not_objects`, `test_a_stale_no_entity_source_check_is_hidden_once_an_entity_table_exists`, `test_the_sources_step_reads_as_plain_words_with_one_number_locale` | - | Node renders `mappingStep`/`sourcesStep` from the schema the real router serves | Regression tests for the reported bugs: "[object Object]" options, stale NO_ENTITY_SOURCE on saved cards, en-US grouping, Remove behind the row menu. Additive. |

Unchanged on purpose: every `data-act` the acceptance journey drives (`pick-files`, `confirm-role`,
`set-role`, `accept-all`, `build`, `use-dataset`), `details[data-step]` and its `open` attribute,
`.kv .k` "Saved", `[data-leak-check]` text, the `data_split .ss` line and the client picker ids.
`tests/unit/onboarding/test_setup_wiring_ui.py` needed no change (payload keys unchanged).
