// Plan G's entry point (PARALLEL_WORK_PROTOCOL.md §4, M75): importing this file is the only wiring
// Guided setup needs, and `ui/index.html`'s PLAN-G block is where it is imported.
//
// Guided setup draws no screen of its own and claims no hash route. It registers a *setup mode*
// (`registerSetupMode`, `modules/router.js`): `ui/usecase.js` asks for the modes that apply to a use
// case and, when there is one, draws "Guided setup (recommended)" beside "Manual setup" on the Setup
// view. With this file not loaded, the Setup view renders exactly as it did before.

import { registerSetupMode } from "../router.js";
import { guidedApplies, mountGuided } from "./guided.js";

registerSetupMode({
  name: "guided",
  label: "Guided setup (recommended)",
  applies: guidedApplies,
  mount: mountGuided,
});
