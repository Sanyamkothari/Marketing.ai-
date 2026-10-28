// The Guided setup tab's place (Plan G §5.1, M75): the wizard's Customers step and a goal's Setup tab. Both are the one
// Data Doctor screen (Plan O M247, DEC-1817), mounted into the two elements the Setup form just painted
// (`registerSetupMode`, `modules/router.js`). `host` is the Setup form's door back: `{ uc, mode, mayRun,
// approved(fill), manual() }`.

import { mountDataDoctor } from "../datadoctor/screen.js";

/** Which use cases offer Guided setup: the ones that train a model on a table (Plan G G10, DEC-1003). */
export function guidedApplies(uc) {
  return Boolean(uc && uc.trainable_in_phase_1 && uc.advanced_settings && (uc.advanced_settings.stages || []).length);
}

/** The wizard's Customers step (inside its card `data-cw-customers`) or a goal's Setup tab. */
const placeOf = (slots) =>
  slots.main && typeof slots.main.closest === "function" && slots.main.closest("[data-cw-customers]") ? "wizard" : "setup";

/** Mount the Data Doctor screen into the Setup form's two elements. */
export function mountGuided(slots, host) {
  return mountDataDoctor(slots, {
    place: placeOf(slots),
    uc: host.uc,
    mode: host.mode,
    modelVersionId: host.modelVersionId, // a goal that has a model uses it: no learning time to choose
    approved: host.mayRun === false ? null : host.approved,
    manual: host.manual, // "Expert view" opens Manual setup in place
  });
}
