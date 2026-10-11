// The "New campaign" wizard's entry point (Plan M §3.1, M177, DEC-1607): `#/campaigns/new[/<step>/...]`.
//
// The router hands a whole first hash segment to one module (`ui/modules/router.js` `resolveRoute`), and
// `campaigns` belongs to the campaign screens: Plan J's campaign page today (`ui/modules/decide/`), the
// campaigns list and campaign page of Plan M M176 next. So the wizard does not claim a route of its own. The
// owner of `campaigns` hands it every hash under `campaigns/new` - the tiny registration hook is this file:
//
//     if (isWizardRoute(parts)) return (await import("../campaigns/wizard_index.js")).renderWizard(app, parts);
//
// Loaded on demand like that, the wizard costs nothing on any other page and is drawn the same way in both
// layouts (`ui_layout` classic or campaigns): it reads neither.

export { renderWizard } from "./wizard.js";
export { WIZARD_HASH, isWizardRoute, parseRoute, stepHref } from "./wizard_rules.js";

/** The link "+ New campaign" opens (Overview, the campaigns list). */
export const NEW_CAMPAIGN_HREF = "#/campaigns/new";
