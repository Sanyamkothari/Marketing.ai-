// Plan G's entry point (PARALLEL_WORK_PROTOCOL.md §4, M75): importing this file is the only wiring
// Guided setup needs, and `ui/index.html`'s PLAN-G block is where it is imported.
//
// Guided setup draws no screen of its own and claims no hash route. It registers a *setup mode*
// (`registerSetupMode`, `modules/router.js`): `ui/usecase.js` asks for the modes that apply to a use
// case and, when there is one, draws "Guided setup (recommended)" beside "Manual setup" on the Setup
// view. With this file not loaded, the Setup view renders exactly as it did before.
//
// It also registers "Ask your data" (Plan I, DEC-1255): the same helper's read-only chat about the file a
// run used, as a *page panel* at the end of the run's Data page (`registerPagePanel`, `modules/router.js`).

import { registerPagePanel, registerSetupMode } from "../router.js";
import { askApplies, askPanelHtml } from "./ask.js";
import { guidedApplies, mountGuided } from "./guided.js";
import { dataDoctorOnNow } from "../datadoctor/switch.js";

registerSetupMode({
  name: "guided",
  label: "Guided setup (recommended)",
  applies: guidedApplies,
  mount: mountGuided,
  // With the one Data Doctor on, Manual setup is its "Expert view": what is changed there is not in the Doctor's record.
  manualNote: () => (dataDoctorOnNow() ? "Changes made here are not part of the Data Doctor's record." : ""),
});

registerPagePanel({ name: "ask", applies: askApplies, html: askPanelHtml });
