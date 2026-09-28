// Plan H's Connections module (M80): the page at `#/connections`. Imported by `ui/index.html`'s PLAN-G
// block (Plan H shares Plan G's blocks); importing this file is all the wiring. The top bar's
// "Connections" item (M82, `ui/chrome.js`) links here, and Settings sends the AI service here too.
// Guided setup's "Pick from a connection" (`picker.js`) is imported by `modules/agent/guided.js`.

import { registerModule } from "../router.js";
import { renderConnections } from "./page.js";

registerModule({
  name: "connections",
  routes: ["connections"],
  render: renderConnections,
});
