// The "New campaign" wizard's entry point for the classic hook (Plan M §3.1, M177, DEC-1607):
// `#/campaigns/new[/<step>/...]`.
//
// The router hands a whole first hash segment to one module (`ui/modules/router.js` `resolveRoute`, first
// registered owner wins), and `campaigns` belongs to the campaign screens, so the wizard claims no route of
// its own. Its owner hands it every hash under `campaigns/new`, loading it on demand:
//
// * Plan J's campaign page (`ui/modules/decide/index.js` `renderCampaign`) imports this file;
// * the campaigns screens of Plan M M176 (`ui/modules/campaigns/index.js`) import `./wizard.js` and call its
//   `renderWizard(app, parts)` directly. That export's name is what they depend on: keep it.
//
// Whichever module owns `campaigns` must keep the hand-off, as one line:
//
//     if (isWizardRoute(parts)) return (await import("../campaigns/wizard_index.js")).renderWizard(app, parts);
//
// Loaded on demand like that, the wizard costs nothing on any other page and is drawn the same way in both
// layouts (`ui_layout` classic or campaigns): it reads neither.

export { renderWizard } from "./wizard.js";
export { WIZARD_HASH, isWizardRoute, parseRoute, stepHref } from "./wizard_rules.js";
