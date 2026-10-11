// Plan M's Data module (M179, DEC-1609): the page at `#/data` and `#/data/check`. Importing this file is all the
// wiring; `ui/index.html` loads it in its PLAN-M block.
//
// It registers a route that nothing links to yet: the sidebar that does (M174) and the redirect from `#/connections`
// are owned elsewhere. `#/connections`, `#/connections/<id>` and `#/doctor` keep working unchanged.

import { announceModulesChanged, registerModule } from "../router.js";
import { renderData } from "./page.js";

registerModule({
  name: "data",
  routes: ["data"],
  render: renderData,
});

// `app.js` draws the first screen before this module is loaded: a page opened straight at `#/data` is drawn again
// now that the route has an owner.
announceModulesChanged();
