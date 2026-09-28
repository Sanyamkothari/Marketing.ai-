// Plan H's Connections module (M80): the page at `#/connections`. Imported by `ui/index.html`'s PLAN-G
// block (Plan H shares Plan G's blocks); importing this file is all the wiring. The top bar's
// "Connections" item (M82, `ui/chrome.js`) links here, and Settings sends the AI service here too.
// Guided setup's "Pick from a connection" (`picker.js`) is imported by `modules/agent/guided.js`;
// Manual setup's is the upload source registered below (`source.js`, UI audit §8.4 item 10).

import { registerModule, registerUploadSource } from "../router.js";
import { renderConnections } from "./page.js";
import { connectionEntry, connectionPanel } from "./source.js";

registerModule({
  name: "connections",
  routes: ["connections"],
  render: renderConnections,
});

registerUploadSource({ name: "connections", entry: connectionEntry, panel: connectionPanel });
