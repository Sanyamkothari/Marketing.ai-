# WP1 test changes

Every assertion WP1 changed, and why. No behavioural assertion was loosened: the one change follows a
deliberate change of wording on the sign-in screen. New tests are listed after it, and the assertions
other packages own that this package's changes affect are listed last, for the integrator.

| File | Test | Old assertion | New assertion | Why |
|---|---|---|---|---|
| `tests/integration/production/ui/off.test.mjs` | `the sign-in route says there is nothing to sign in to` | `assert.match($("#app").textContent, /Access control is off on this deployment/)` | `assert.match($("#app").textContent, /Sign-in is turned off on this installation/)` | Plain wording (audit 5.5 "signin"): "Sign-in is turned off on this installation. You can use everything without an account.", with "Go to Home" and the environment variable under "For administrators". The test still checks the same state: no `#pb-signin` form is drawn (unchanged assertion). |

Kept unchanged on purpose, and still passing: `off.test.mjs` "the bar says access control is off"
(`/Access control is off/` is now the text of the "Sign-in off" chip's toggletip, still inside
`#pb-bar`; the Users link is in the Admin menu, still inside `#pb-bar`); `session.test.mjs` "a
production deployment with sign-in off is named in the bar" (`userBarHtml` is still exported and still
says "Sign-in is not configured" with the server's sentence); `deeplink.test.mjs` (the signed-in reload
lands on its route as before); `flow.test.mjs` (owned by WP7; its `#pb-bar` links, "Signed in as",
"Not signed in", `#pb-signin`, `#pb-username`, `#pb-password`, `#pb-signout` and the
`.apierr` text "The username or password is not right." are all kept).

## New tests

| File | Tests |
|---|---|
| `tests/integration/production/ui/session.test.mjs` | `the user menu names the person and one role, never the implied Viewer, with Sign out set apart` (display name, one role label from `mainRole`, Change password, `#pb-signout` last after a separator); `sign-in off is a neutral chip whose toggletip says access control is off; signed out is Sign in` (neutral chip, toggletip text, no Sign out; signed out: a Sign in link to `#/signin/<where it was>` whose accessible name starts "Not signed in.") |

## Assertions in other packages' files that WP1's changes affect (not edited here)

| File (owner) | Test | Current assertion | Needed | Why |
|---|---|---|---|---|
| `tests/integration/test_acceptance.py:339` (WP2) | `journey` fixture, step 1 | `page.get_by_role("heading", name="Marketing AI")` | `page.get_by_role("heading", name="Customer Lifecycle")` | Home's H1 is now the journey label (plan WP1: "the H1 is the journey label"); the breadcrumb reads "Home" and the product name is the top bar's wordmark (a link, not a heading). |
| `tests/integration/test_onboarding_acceptance.py:342` (WP3) | `journey` fixture, step 1 | `page.get_by_role("heading", name="Marketing AI")` | `page.get_by_role("heading", name="Customer Lifecycle")` | As above. Lines 343-350 (`#f-client`, "+ New client", `#f-client-name`, `#f-client-add`) pass unchanged: Home counts as a per-client screen, the ids and the option label are kept, and "Add client" is the form's submit button. |
| `tests/integration/production/ui/ops/monitoring.test.mjs:80` (WP7) | the Analyst's schedule list | `#pb-bar a[href="#/monitoring/schedules"]` | unchanged (passes) | Kept by a "Schedules" entry WP1 adds to the Models menu (the "models" slot), beside "Model health". If WP7 retargets this to `#/monitoring/alerts` (Model health), the entry can go. |
