// The bodies of the two writes the campaign page sends (Plan M M176, DEC-1606), as pure functions of what the API
// already answered, so a node test can pin them and the integration test can replay them against the real app.

/**
 * The body of `POST /campaigns/{id}/outcomes` for an uploaded results file: the upload, and the outcome as the
 * test plan registered it (its column and the value that counts, and its covariate with the column dating it).
 * Without a plan the server finds the outcome column in the file, as Plan J's own callers leave it to.
 */
export function outcomesPayload(uploadId, plan = null, covariateDateColumn = "") {
  const body = { upload_id: uploadId };
  if (!plan) return body;
  body.outcome_column = plan.outcome_column;
  if (plan.positive_label !== null && plan.positive_label !== undefined) body.positive_label = plan.positive_label;
  if (plan.covariate_column && plan.covariate_source !== "run_score") {
    body.covariate_column = plan.covariate_column;
    body.covariate_date_column = String(covariateDateColumn || "").trim() || `${plan.covariate_column}_date`;
  }
  return body;
}

/**
 * The body of `POST /runs` that refreshes a list: the scoring run's own customers (its upload, or its built
 * dataset), key and settings, in score mode, with no model named so the current approved model scores them.
 */
export function refreshRequest(run, { confirmCost = false } = {}) {
  const body = {
    use_case: run.use_case_id,
    mode: "score",
    primary_key: run.primary_key,
    overrides: run.overrides || {},
  };
  if (run.dataset_id) {
    body.dataset_id = run.dataset_id;
    if (run.client_id) body.client_id = run.client_id;
  } else {
    body.upload_id = run.upload_id;
  }
  if (confirmCost) body.confirm_cost = true;
  return body;
}
