// "Ask your data" (Plan I, DEC-1255): the data helper's read-only chat about the file a run used, as a
// *page panel* at the end of the run's Data page (`registerPagePanel`, `modules/router.js`). Importing this
// file is the only wiring it needs; `ui/index.html` imports it.

import { registerPagePanel } from "../router.js";
import { askApplies, askPanelHtml } from "./ask.js";

registerPagePanel({ name: "ask", applies: askApplies, html: askPanelHtml });
