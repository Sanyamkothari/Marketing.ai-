// Plan H's Connections module (M80): the page at `#/connections` and the way to reach it. Imported by
// `ui/index.html`'s PLAN-G block (Plan H shares Plan G's blocks); importing this file is all the wiring.
//
// Until the four-page navigation (M82) links `#/connections` from the top bar itself, the page is
// reached from the top bar's Admin menu, through the router's `registerNavSlot` seam - so no shared
// screen file is edited for it. Guided setup's "Pick from a connection" (`picker.js`) is imported by
// `modules/agent/guided.js` directly.

import { registerModule, registerNavSlot } from "../router.js";
import { renderConnections } from "./page.js";

registerModule({
  name: "connections",
  routes: ["connections"],
  render: renderConnections,
});

registerNavSlot("admin", {
  html: () => `<a href="#/connections">Connections</a>`,
});
