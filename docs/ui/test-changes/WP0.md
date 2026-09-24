# WP0 test changes

Every assertion WP0 changed, and why. No behavioural assertion was loosened; each change follows a
deliberate change of markup or layout (the logo-only page header, the top bar, the Home-rooted
breadcrumb). New tests are listed at the end.

| File | Test | Old assertion | New assertion | Why |
|---|---|---|---|---|
| `tests/integration/test_ui.py` | `test_ui_directory_holds_exactly_the_modules_the_page_loads` (and every test reading `MODULES` / `ALL_MODULES`) | `MODULES` = api, app, availability, dom, overview, pages, settings, usecase | the same plus `chrome.js` | New module: the one top bar. It is now also held to the served-as-JS, imports-resolve, no-sample-values and em-dash checks. |
| `tests/unit/onboarding/test_setup_wiring_ui.py` | `test_dom_js_keeps_the_header_slot_and_the_router_fills_it` | `re.search(r"pageHead\(inner\) \{[^}]*headerToolHtml\(\)", dom)` (the page header draws the header tool) | `not re.search(...)` on the same pattern; `chrome.js` imports `headerToolHtml` from `./dom.js`, `topBarHtml()` calls `headerToolHtml()`, and `chrome.js` imports neither the router nor onboarding | Owner's request: the page header's right side is the logo only; the client picker moved into the top bar's context slot. The DEC-790 rule (no import back to the router) is now asserted for `chrome.js` too. |
| `tests/integration/test_ui_journey.py` | `test_a_telecom_screen_reads_exactly_as_it_did` | `back == '<a class="back" href="#/">‹&nbsp; {label}</a>'`, `crumb == '<a href="#/">{label}</a>'` | `back == '<nav class="crumbs" aria-label="Breadcrumb"><a href="#/">Home</a><span class="sep" aria-hidden="true">›</span><a href="#/">{label}</a></nav>'`, `crumb == '<a href="#/">Home</a><span class="sep" aria-hidden="true">›</span><a href="#/">{label}</a>'` | The "‹ journey" back link became the breadcrumb Home › journey › screen (plan: every breadcrumb's root is Home). Label and route still come from the industry file. |
| `tests/integration/test_ui_journey.py` | `test_another_industrys_screen_names_and_opens_that_industry` | same shapes with `href="#/industry/<id>"` | same new shapes with `href="#/industry/<id>"` | As above. |
| `tests/integration/test_ui_journey.py` | `test_with_no_industry_the_root_is_the_bare_overview` | `back == '<a class="back" href="#/">‹&nbsp; Marketing AI</a>'` | `back == '<nav class="crumbs" aria-label="Breadcrumb"><a href="#/">Home</a></nav>'` and (new) `crumb == '<a href="#/">Home</a>'` | With no journey the breadcrumb is its root alone, named Home (was "Marketing AI"). |
| `tests/integration/test_acceptance.py` | `journey` fixture (step 6, back to the use case) | `page.locator(".crumbs a").nth(1).click()` | `page.locator(".crumbs a").nth(2).click()` | The run pages' breadcrumb now starts with Home, so the use-case link is the third link (Home › journey › use case), not the second. Same target, same journey. |
| `tests/integration/test_onboarding_acceptance.py` | `journey` fixture (step 5, back to the use case) | `page.locator(".crumbs a").nth(1).click()` | `page.locator(".crumbs a").nth(2).click()` | As above. |

## New tests

| File | Tests |
|---|---|
| `tests/integration/production/ui/topbar.test.mjs` (run by `test_production_ui_js.py`) | the bar is `header#pb-bar` before `#app` with `nav[aria-label="Main"]`; an Admin sees the 6 goals, the picker in the context slot and Users under Admin; a Viewer sees 5 goals and no admin link; signed out shows the wordmark and Sign in only; the active goal follows the route (and `setActiveNav`); a menu toggles `aria-expanded` and closes on Escape with focus back on its trigger; with no access provider every goal with content is drawn. Roles come from real `GET /auth/me` fixtures. |
