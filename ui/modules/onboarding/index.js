// Phase 2's entry point (PARALLEL_WORK_PROTOCOL.md §4; Plan A M35): importing this file is the only
// wiring onboarding needs, and `ui/index.html`'s PHASE-2 block is where it is imported.
//
// Onboarding draws no screen of its own, so it claims no hash route. It registers the two things the
// shared registry offers for exactly this case: a *header tool* (the client picker beside the logo on
// every screen) and a *setup source* (the "Build from raw tables" card in Setup Step 1, and the
// four-step panel it mounts). `ui/usecase.js` and `ui/dom.js` ask the registry for them; with this
// file not loaded, both render exactly as they did before.

import { registerHeaderTool, registerSetupSource } from "../router.js";
import { clientPickerHtml } from "./clients.js";
import { mountSetup, setupCard, setupContext } from "./setup.js";

registerHeaderTool({ name: "onboarding-clients", html: clientPickerHtml });

registerSetupSource({ name: "onboarding", card: setupCard, mount: mountSetup, context: setupContext });
