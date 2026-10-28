// The Data Doctor's place in a goal's Setup view and the campaign wizard's Customers step. It registers a
// *setup mode* (`registerSetupMode`, `modules/router.js`): `ui/usecase.js` and the wizard ask for the modes that apply
// to a goal and mount it in the two places the Setup form paints. The screen is the one Data Doctor
// (`screen.js`), opened on the goal's case; nothing here asks for an AI service, so the rules run without one.
//
// Importing this file is the only wiring it needs; `ui/index.html` imports it.

import { registerSetupMode } from "../router.js";
import { mountDataDoctor } from "./screen.js";

/** Which goals offer it: the ones that train a model on a table (Plan G G10, DEC-1003). */
export function dataDoctorApplies(uc) {
  return Boolean(uc && uc.trainable_in_phase_1 && uc.advanced_settings && (uc.advanced_settings.stages || []).length);
}

/** The wizard's Customers step (inside its card `data-cw-customers`) or a goal's Setup tab. */
const placeOf = (slots) =>
  slots.main && typeof slots.main.closest === "function" && slots.main.closest("[data-cw-customers]") ? "wizard" : "setup";

/** Mount the screen into the two elements the Setup form just painted. `host` is the Setup form's door back. */
export function mountDataDoctorSetup(slots, host) {
  return mountDataDoctor(slots, {
    place: placeOf(slots),
    uc: host.uc,
    mode: host.mode,
    modelVersionId: host.modelVersionId, // a goal that has a model uses it: no learning time to choose
    approved: host.mayRun === false ? null : host.approved,
    manual: host.manual, // "Expert view" opens Manual setup in place
  });
}

registerSetupMode({
  name: "datadoctor",
  label: "Data Doctor (recommended)",
  applies: dataDoctorApplies,
  mount: mountDataDoctorSetup,
  manualNote: () => "Changes made here are not part of the Data Doctor's record.",
});
