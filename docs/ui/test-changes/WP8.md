# WP8 test changes (pilot module: Reports, Build data kit, tour, help popover, feedback)

Only assertions whose text or layout changed on purpose were changed; no behavioural assertion was loosened.
Everything else in these files is additions (more coverage).

## Changed

| File | Test | Old | New | Why |
|---|---|---|---|---|
| tests/integration/pilot/test_pilot_acceptance.py | test_the_pilot_screen_lists_the_reports_and_shows_one_in_place | `page.goto(".../ui/#/pilot")` then wait for "Data request kit" | `page.goto(".../ui/#/pilot/kit")` then wait for "Data request kit" | The kit moved from the `#/pilot` hub to the new Build data page (`#/pilot/kit`), as the navigation plan says. `#/pilot` is now Reports. The readiness frame assertion (`iframe.pe-frame`, "Not ready: ...") is unchanged. |
| tests/integration/pilot/test_pilot_acceptance.py | test_a_warning_pill_painted_by_any_screen_gets_its_explanation | `page.goto(".../ui/#/pilot")` then wait for "Data request kit" | `page.goto(".../ui/#/pilot/kit")` then wait for "Data request kit" | Same move: the ready signal lives on Build data now. |
| tests/integration/pilot/test_pilot_acceptance.py | test_feedback_from_the_screen_is_recorded | `page.locator("#pe-fb-btn").click()` | `page.locator("#pb-bar [data-menu='tn-help']").click()` then `page.locator("#pe-fb-btn").click()` | The floating Feedback button is gone. "Send feedback" (still `#pe-fb-btn`) is an entry in the top bar's Help menu, so the menu is opened first. The recorded text, the masking and the stored file checks are unchanged. |

## Added (no existing assertion touched)

| File | Test | Addition | Why |
|---|---|---|---|
| tests/integration/pilot/test_pilot_acceptance.py | test_a_warning_pill_painted_by_any_screen_gets_its_explanation | Also inserts `<div data-code="PII_DETECTED"><b>Personal details</b></div>` and waits for its `.pe-q` | Screens now show the plain title and keep the code in `data-code` (audit C5); help.js must match on `data-code` too. |
| tests/unit/pilot/test_pilot_ui.py | API_PREFIXES | Adds `/clients` and `/auth` | api.js now calls `GET /clients` (rows labelled by client) and `GET /auth/me` (the feedback export offered only when allowed); both are checked to exist. |
| tests/unit/pilot/test_pilot_ui.py | UNTYPED_JSON | `GET /pilot/readiness/{dataset_id}` and `GET /pilot/results` map to `ReportDocument` | The report viewers read the report's `format=json` document (title, facts, verdict state) for their header; these routes declare JSON without a schema. |
| tests/unit/pilot/test_pilot_ui.py | FIELD_READS | 37 new rows: runs `client_id`/`use_case_id`, datasets `client_id`/`target`, model `approved_at`/`created_at`, `/clients`, `/industries`, demo `client_name`/`broken_client_id`/`campaigns`, ROI `status`/`use_case_id`/`results_available_on`/`outcome_is_good`/`benefit_label`/`benefit.low`/`net_value`/`summary`, report `facts`/`blocks[].state`/`client_name`, `/auth/me` `permissions[]` | Every new field the screens read is pinned to its route's schema. |
