// Which use cases have step 4, "Measure the campaign" (Plan H M83) - the browser's copy of
// `engine.uplift.measure.measure_offered`, read from the `config` block `GET /use-cases/{id}` returns.
//
//   "measure"  the actions are contacts with customers (`actions.contacts_customers`, true unless the
//              use-case YAML says false) and a control group is held back
//              (`actions.control_group_fraction > 0`), and the use case is not AI-written text;
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
  if (!(Number(config.actions.control_group_fraction) > 0)) return "none";
  return "measure";
}

/** True on a finished scoring run of a use case with step 4. */
export const measureApplies = (uc, run) =>
  !!run && run.mode === "score" && run.state === "done" && campaignStep(uc) === "measure";
