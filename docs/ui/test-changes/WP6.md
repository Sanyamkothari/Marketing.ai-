# WP6 test changes

WP6 (AI screens: the switched-off notice, assistant, root-cause notes, campaign copy, AI service
connection) changed no test assertion. The tests that pin these screens pass unchanged:

| File | What it checks | Status |
|---|---|---|
| `tests/integration/test_ui.py` | connection screen: radio-only inputs, no credential words in code, at least 15 `esc(` calls, no unescaped API values, badge links to `#/generative/connection`, `availability.js` free of prototype sample values | unchanged, passing |
| `tests/integration/test_ui_journey.py` | `assistant.js`, `rca.js`, `copy.js` name no journey and use `backLink(uc)` / `journeyCrumb(uc)` | unchanged, passing |
| `tests/integration/production/test_production_ui.py` | every gated id (`#g-*`, `#c-*`, `data-approve`, `data-regen`, `name="c-source"`) is still written out in a screen's source | unchanged, passing |
| `tests/integration/production/ui/gate.test.mjs`, `flow.test.mjs`, `off.test.mjs` | gating of the copy and connection controls; a Viewer's disabled "Test connection" with its reason | unchanged, passing |

No test was added. The integrator's browser journeys (`scripts/ui_screens.mjs` shots `ai-notice`,
`ai-connection`, `ai-assistant`, `ai-rca`, `ai-copy`) were not run here (slow).

| File | Test | Old | New | Why |
|---|---|---|---|---|
| (none) | | | | |
