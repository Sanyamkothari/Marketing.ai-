// Phase 4b's first half, loaded before any screen draws (M46): the bearer token on every call, the
// user bar, role-aware controls and signed-in downloads.
//
// Imported for its side effects from the Phase 4b block of `ui/modules/router.js`, not from
// `index.html`, and that is the whole reason this file exists apart from `index.js` (DEC-790):
// `app.js` imports `router.js`, so everything here runs *before* `app.js`'s first `render()` - the
// fetch wrapper is in place when the very first `GET /industries` leaves. A `<script>` tag in
// `index.html` runs after `app.js`, which is too late for that one call.
//
// It must not import `router.js` itself: `router.js` is still evaluating when this file runs (it is
// this file's importer), so its `registered` list is not initialised yet and `registerModule` would
// throw. The screens that *do* register a route - sign-in, users, audit log - are therefore in
// `index.js`, which `index.html` loads once `router.js` is complete.

import { installDownloads } from "./downloads.js";
import { installGates } from "./gate.js";
import { installFetch, loadMe } from "./session.js";
import { injectProductionStyles } from "./styles.js";
import { mountUserBar } from "./userbar.js";

injectProductionStyles();
installFetch(window);
installGates(document); // before the download listener, so a gated link is swallowed, not fetched
installDownloads(document);
mountUserBar(document);
loadMe();
