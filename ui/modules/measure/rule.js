// Which use cases have step 4, "Measure the campaign" (Plan H M83) - the browser's copy of
// `engine.uplift.measure.measure_offered`, read from the `config` block `GET /use-cases/{id}` returns.
//
//   "measure"  the actions are contacts with customers (`actions.contacts_customers`, true unless the
//              use-case YAML says false) and a control group is held back
//              (the effective holdout share > 0, `holdoutFraction` below), and the use case is not
//              AI-written text;
//   "none"     an operational use case (order fulfilment, fault prediction), an AI-written-text one,
//              or one that holds nobody back: no step 4 and no campaign block at all;
//   "unknown"  a use case object without its config (an older caller): the uplift module's
//              "Campaign results" block is drawn as it always was.
//
// Decided by configuration, never by use-case id. `GET /runs/{id}/measure` answers `offered` from the
// same rule on the server, and the panel hides itself if the two ever disagree.

export function campaignStep(uc) {
  const config = uc && uc.config;
  if (!config || !config.actions) return "unknown";
  if (uc.ai_type === "generative" || config.ai_type === "generative") return "none";
  if (config.actions.contacts_customers === false) return "none";
  if (!(holdoutFraction(config.actions) > 0)) return "none";
  return "measure";
}

/** The effective holdout share (`engine.holdout.spec.effective_holdout_fraction`, Plan J M92):
 *  `actions.holdout.fraction` under a persistent scope, else `actions.control_group_fraction`. */
export function holdoutFraction(actions) {
  const holdout = actions && actions.holdout;
  if (holdout && holdout.scope && holdout.scope !== "run") return Number(holdout.fraction);
  return Number(actions && actions.control_group_fraction);
}

/** True on a finished scoring run of a use case with step 4. */
export const measureApplies = (uc, run) =>
  !!run && run.mode === "score" && run.state === "done" && campaignStep(uc) === "measure";
