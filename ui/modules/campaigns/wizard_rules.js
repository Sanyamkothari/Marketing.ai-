// The "New campaign" wizard's rules (Plan M §3.1, M177, DEC-1607): pure functions, no DOM and no fetch,
// so node can import them and the jsdom tests can pin each one.
//
// Where a figure comes from is the point of this file. The wizard shows no number of its own: every count,
// share, change, date and amount below is read from an API answer, and a function that cannot find its
// figure returns null so the screen says why instead of guessing.

/** The five steps, in order. The URL carries the step: `#/campaigns/new/<step>/...`. */
export const STEPS = [
  { id: "goal", label: "Goal" },
  { id: "customers", label: "Customers" },
  { id: "audience", label: "Audience" },
  { id: "review", label: "Review" },
  { id: "send", label: "Send" },
];

export const WIZARD_HASH = "#/campaigns/new";

/** `#/campaigns/new/<step>/<a>/<b>`, each part encoded. */
export function stepHref(step, ...rest) {
  const tail = rest.filter((p) => p !== null && p !== undefined && p !== "").map((p) => encodeURIComponent(p));
  return [WIZARD_HASH, step, ...tail].join("/");
}

/** Whether a route (the hash split on "/") is the wizard's: `campaigns/new[/...]`. */
export const isWizardRoute = (parts) => Array.isArray(parts) && parts[0] === "campaigns" && parts[1] === "new";

/**
 * The route the hash names: `{ step, ucId, runId, campaignId }`. A step the wizard does not know, or a
 * step without the id it needs, is the Goal step: a refresh never lands on a half-drawn screen.
 */
export function parseRoute(parts) {
  const [, , raw, a, b] = (parts || []).map((p) => {
    try {
      return decodeURIComponent(p);
    } catch {
      return p;
    }
  });
  if (raw === "customers" && a) return { step: "customers", ucId: a, runId: b || null, campaignId: null };
  if ((raw === "audience" || raw === "review" || raw === "send") && a) {
    return { step: raw, ucId: null, runId: null, campaignId: a };
  }
  return { step: "goal", ucId: null, runId: null, campaignId: null };
}

// --- Step 1: goals ------------------------------------------------------------------------------------

/**
 * Plan M §4.2: the goal names a marketer sees (display only; ids, configs and APIs keep theirs), in the
 * order the plan lists them. `more: true` folds a goal under "More goals": the operational goals, which do
 * not contact customers (M6). A use case not listed here keeps its catalogue name. M172 owns the names
 * for every other page; this table is the wizard's copy until `ui/labels.js` carries them.
 */
export const GOAL_NAMES = [
  { id: "targeted-advertisement", name: "Find customers likely to buy" },
  { id: "win-back-campaign", name: "Win back lapsed customers" },
  { id: "telco-churn", name: "Keep customers likely to leave" },
  { id: "churn", name: "Keep customers likely to leave" },
  { id: "payment-propensity", name: "Get customers to pay on time" },
  { id: "rca", name: "Understand why customers leave" },
  { id: "hillstrom-email", name: "Choose the best e-mail for each customer" },
  { id: "ai-onboarding-assistant", name: "Answer customer questions (AI assistant)" },
  { id: "order-fulfillment", name: "Order Fulfillment", more: true },
  { id: "fault-prediction", name: "Fault Prediction", more: true },
];

/**
 * The goal cards, from `GET /industries`: `{ goals, more }`, each item `{ id, name, stage }`.
 *
 * Shown: every available use case of the deployment's default industry (the one Home opens), and every
 * available use case Plan M §4.2 names wherever it is listed (the demo's e-mail goal sits under another
 * industry). Planned use cases are not goals yet. Each goal appears once, named by §4.2 when listed there
 * (in §4.2's order), else by its catalogue name; the lifecycle stage is its small label.
 */
export function goalCards(industries) {
  const list = (industries && industries.industries) || [];
  const home =
    list.find((i) => i.id === (industries && industries.default_industry)) || list[0] || { stages: [] };
  const named = new Map(GOAL_NAMES.map((g, index) => [g.id, { ...g, index }]));
  const seen = new Map();
  const add = (industry, stage, uc) => {
    if (!uc || uc.status !== "available" || seen.has(uc.id)) return;
    if (industry !== home && !named.has(uc.id)) return;
    const entry = named.get(uc.id);
    seen.set(uc.id, {
      id: uc.id,
      name: entry ? entry.name : uc.name,
      stage: stage.name || "",
      more: Boolean(entry && entry.more),
      order: entry ? entry.index : GOAL_NAMES.length + seen.size,
    });
  };
  for (const stage of home.stages || []) for (const uc of stage.use_cases || []) add(home, stage, uc);
  for (const industry of list) {
    if (industry === home) continue;
    for (const stage of industry.stages || []) for (const uc of stage.use_cases || []) add(industry, stage, uc);
  }
  const all = [...seen.values()].sort((a, b) => a.order - b.order);
  const strip = ({ id, name, stage }) => ({ id, name, stage });
  return { goals: all.filter((g) => !g.more).map(strip), more: all.filter((g) => g.more).map(strip) };
}

/** The display name of a goal, by §4.2, else the use case's own name. */
export function goalName(uc) {
  const entry = GOAL_NAMES.find((g) => g.id === (uc && uc.id));
  return entry ? entry.name : (uc && uc.name) || "";
}

/**
 * Whether a goal runs as a campaign here: its actions contact customers and hold a comparison group back
 * (`campaignStep`, the measure module's copy of the server's rule), and Guided setup applies to it. Any
 * other goal opens its classic page (Plan M M6).
 */
export const runsAsCampaign = (uc, step, hasGuided) => step === "measure" && Boolean(hasGuided);

// --- Step 2: the models behind the list ---------------------------------------------------------------

/** The current model of a goal (`GET /models?use_case=`), or null on its first campaign. */
export function currentModel(models) {
  const versions = (models && models.versions) || [];
  const found = versions.find((v) => v.is_champion || (v.version && v.version.status === "champion"));
  return found ? found.version : null;
}

/** One version by id, or null. */
export function versionById(models, modelId) {
  const versions = (models && models.versions) || [];
  const found = versions.find((v) => v.version && v.version.model_id === modelId);
  return found ? found.version : null;
}

/** The share of a run's stages that are done, 0 to 1, from `GET /runs/{id}`'s status; null without stages. */
export function progressOf(detail) {
  const stages = (detail && detail.status && detail.status.stages) || [];
  if (!stages.length) return null;
  return stages.filter((s) => s.state === "done" || s.state === "skipped").length / stages.length;
}

/** The run's state: "pending" | "running" | "done" | "failed" | "cancelled" (or whatever the API says). */
export const runState = (detail) => (detail && detail.run && detail.run.state) || (detail && detail.status && detail.status.state) || "";

// --- Steps 3 and 4: the audience and the test ----------------------------------------------------------

/**
 * The rate expected without the campaign, for the test size: the scoring answer's `score_mean` - the
 * model's average predicted chance across everyone on the list - only when the list's model answers yes or
 * no (a chance), as the Output page's size check does (`ui/power.js` `powerInputs`). Null otherwise: the
 * planner then says why it cannot size the test.
 */
export function baseRateOf(run, summary) {
  if (!run || run.problem_type !== "binary_classification") return null;
  const mean = summary && summary.score_mean;
  return typeof mean === "number" && mean > 0 && mean < 1 ? mean : null;
}

export const BASE_RATE_SOURCE = "the model's average predicted chance across everyone on the list";

/** The planner's point at the campaign's own split (`current_index`), or null. */
export function ownPoint(preview) {
  if (!preview || !Array.isArray(preview.points)) return null;
  const index = preview.current_index;
  return index === null || index === undefined ? null : preview.points[index] || null;
}

/** "2,560" with separators. */
export const fmtCount = (n) => Number(n).toLocaleString("en-US", { maximumFractionDigits: 0 });

/** A share as a percentage with at most one decimal and no trailing ".0": "10%", "49.8%". */
export const fmtShare = (fraction) => `${(Math.round(Number(fraction) * 1000) / 10).toLocaleString("en-US", { maximumFractionDigits: 1 })}%`;

/** Percentage points with one decimal: "0.6". */
export const fmtPoints = (pp) => Number(pp).toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });

/**
 * The plain-language test size (Plan M §3.1): who is held back for comparison, and the smallest change that
 * comparison is sure to see - "Enough to see a 0.6-point change." Every figure is the planner's point at the
 * campaign's own split; with no figure, the planner's own reason is the second sentence.
 */
export function testSizeLines(preview) {
  if (!preview) return null;
  const point = ownPoint(preview);
  if (!point) {
    return { group: null, size: preview.reason || null, ok: false };
  }
  const group = `${fmtCount(point.n_control)} customers (${fmtShare(point.holdout_share)}) are not contacted. They are the comparison group, so the result can be measured.`;
  if (typeof point.mde_pp === "number") {
    return { group, size: `Enough to see a ${fmtPoints(point.mde_pp)}-point change.`, ok: true };
  }
  return { group, size: point.reason || preview.reason || null, ok: false };
}

/**
 * The date a campaign's results can be read: its treatment start plus its outcome window, as the server
 * checks it (`api/routes/campaigns.py` `_resolved`: the UTC date of `treatment_start + window days`). The
 * start's own date when the campaign has no window.
 */
export function resultsDate(campaign) {
  if (!campaign || !campaign.treatment_start) return null;
  const start = Date.parse(campaign.treatment_start);
  if (!Number.isFinite(start)) return null;
  const days = Number(campaign.outcome_window_days) || 0;
  return new Date(start + days * 86400000).toISOString().slice(0, 10);
}

/** "12 Nov 2026" for an ISO date. */
export function fmtDay(iso) {
  if (!iso) return "";
  const date = new Date(`${String(iso).slice(0, 10)}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

/**
 * The body of `POST /campaigns/{id}/plan`, from the planner's own numbers: the smallest change at the
 * campaign's split (`mde_pp`) and the rate it was worked out on (`base_rate`), the use case's outcome in its
 * own words (`target.definition`) and column, and the results date above. A number the planner did not give
 * is left out, never filled in: the server then records the plan with its note saying what is missing.
 */
export function planBody({ campaign, preview, uc, outcomeColumn }) {
  const target = (uc && uc.target) || {};
  const point = ownPoint(preview);
  const metric = String(target.definition || outcomeColumn || target.column || "").slice(0, 200);
  const body = {
    metric,
    outcome_column: outcomeColumn || target.column,
    analysis_date: resultsDate(campaign),
    expectation: "",
  };
  if (target.positive_label !== null && target.positive_label !== undefined) body.positive_label = String(target.positive_label);
  if (point && typeof point.mde_pp === "number" && point.mde_pp > 0 && point.mde_pp <= 100) body.mde_pp = point.mde_pp;
  if (preview && typeof preview.base_rate === "number") {
    body.base_rate = preview.base_rate;
    if (preview.base_rate_source === "request") body.base_rate_source = BASE_RATE_SOURCE;
  }
  return body;
}

/** Rupees, whole (dom.js's `fmtMoney`; ui/format.js replaces it with lakh and crore when it lands). */
export const fmtRupees = (n) =>
  new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(Number(n));

/**
 * The expected value of the contact list, from `treat_list_summary.json`: the net value the list adds
 * (incremental, a list ranked by value, Plan J M97) or, failing that, the expected value of the customers on it
 * (not all of it because of the campaign). Null when the run has neither: then nothing is shown.
 */
export function expectedValue(summary) {
  if (!summary) return null;
  if (typeof summary.net_value_total === "number") {
    return { amount: summary.net_value_total, text: "more than not contacting them (estimated)", kind: "net" };
  }
  if (typeof summary.expected_gross_value_total === "number") {
    return {
      amount: summary.expected_gross_value_total,
      text: "expected from the customers on the list; not all of it is because of the campaign",
      kind: "gross",
    };
  }
  return null;
}

/**
 * The cost line, from `GET /use-cases/{id}/cost-estimate?mode=score` (Plan J M108): rupees when the server
 * gave them, else dollars at list price; on a deployment that runs on its own machine, that nothing is
 * billed; else that it cannot be estimated (the server's reason goes under technical details).
 */
export function costLine(estimate) {
  if (!estimate) return null;
  if (typeof estimate.inr === "number") return { text: `About ${fmtRupees(estimate.inr)} to prepare this list, at list price.`, known: true };
  if (typeof estimate.estimated_usd === "number") {
    const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(
      estimate.estimated_usd,
    );
    return { text: `About ${usd} to prepare this list, at list price.`, known: true };
  }
  if (estimate.backend === "local") return { text: "No cloud charge: this deployment works on its own machine.", known: true };
  return { text: "The cost cannot be estimated.", known: false };
}

/**
 * The compliance result (Plan K M134), from `compliance_decisions.json`, only when the switch is on: the treat
 * list summary then carries `compliance_rule_version`. `{ allowed, withheld, rerouted }` customer counts.
 */
export function complianceResult(summary) {
  if (!summary) return null;
  const allowed = Number(summary.customers) - Number(summary.withheld_rows);
  return {
    checked: Number(summary.customers),
    allowed: Number.isFinite(allowed) ? allowed : null,
    withheld: Number(summary.withheld_rows),
    rerouted: Number(summary.rerouted_rows) || 0,
  };
}

/**
 * The step a campaign record allows: a campaign with a test plan is past Review (only Send is left), one
 * without is a Draft (Audience or Review). `wanted` is the step the URL asked for.
 */
export function stepForCampaign(view, wanted) {
  const planned = Boolean(view && (view.plan || (view.campaign && view.campaign.test_plan_hash)));
  if (planned) return "send";
  return wanted === "send" ? "review" : wanted;
}
