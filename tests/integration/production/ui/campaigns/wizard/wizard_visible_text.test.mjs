/* Plan M M177 (DEC-1607): the visible-text check (M170) on the "New campaign" wizard, with an EMPTY allow-list.

   Each step is drawn as the browser draws it - app.js and every module of ui/index.html, Guided setup included -
   against the API answers recorded from the validated Hillstrom demo (tests/fixtures/ui_demo/api.json), and the
   text a person can read (M170's collector: technical sections and hidden elements left out) must hold no glossary
   "never shown" word at all. The states no recorded answer can reach (learning in progress, waiting for an approval,
   a refusal) are drawn by the wizard's own builders and checked the same way.

   One part of step 2 is not the wizard's: Guided setup (Plan G/L), embedded unchanged as Plan M asks. Its words are
   checked where it is the page (the `setup-guided-*` pages of visible_text.test.mjs, with their baselines, which M172
   empties), so here its container is left out and everything around it is checked. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, settle } from "../../harness.mjs";
import { violations, describe } from "../../glossary/check.mjs";
import { visibleText } from "../../glossary/collect.mjs";
import { installDemoPage, loadModules } from "../../glossary/replay.mjs";
import {
  customersHtml,
  goalHtml,
  pageHtml,
  reviewHtml,
  sendHtml,
} from "../../../../../../ui/modules/campaigns/wizard_views.js";
import { LIST_HOLD } from "../../../../../../ui/modules/campaigns/views.js";
import { runPageHtml } from "../../../../../../ui/modules/campaigns/run_views.js";
import { startSentenceHtml } from "../../../../../../ui/modules/campaigns/run_rules.js";

const UC = "hillstrom-email";
const CAMPAIGN = "c_20261011_c9276210";

/** Answers the recording does not hold, which the wizard asks for and does without (the cost line is left out). */
const EXPECTED_UNRECORDED = new Set([`GET /use-cases/${UC}/cost-estimate?mode=score`]);
/** The demo's campaigns were not made by a campaign run, so Review's look-up of the run behind the list finds none, and the demo's model has no lift, importance or effect-evaluation file (the summary leaves those parts out). */
const isRunLookup = (key) => key.startsWith("GET /campaign-runs?") || /^GET \/runs\/[^/]+\/artefacts\/(decile_lift|feature_importance|uplift_evaluation)\.json$/.test(key);

const { w, state, missing } = installDemoPage({ hash: "#/__start" });
await loadModules();

// The demo's campaigns are replays that already have their results (a report, status `measured`), and the wizard
// sends a campaign with results to its own page. So each step is drawn on the same campaign as the wizard meets it:
// just sent (no report yet, `live`) for Send, and a Draft (no plan either) for Audience and Review.
let draft = false;
const recordedFetch = w.fetch;
w.fetch = async (input, init) => {
  const response = await recordedFetch(input, init);
  const url = new URL(typeof input === "string" ? input : input.url, w.location.href);
  if (url.pathname !== `/campaigns/${CAMPAIGN}`) return response;
  const body = await response.json();
  body.report = null;
  body.campaign = { ...body.campaign, status: "live" };
  if (draft) {
    body.plan = null;
    body.campaign = { ...body.campaign, test_plan_hash: null, test_plan_version: null };
  }
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
};

async function untilStable(ms = 10000) {
  const started = Date.now();
  let last = null;
  let same = 0;
  while (Date.now() - started < ms) {
    await new Promise((resolve) => setTimeout(resolve, 30));
    const now = `${$("#app")?.innerHTML.length}:${document.body.innerHTML.length}`;
    if (state.inFlight === 0 && now === last) same += 1;
    else same = 0;
    last = now;
    if (same >= 6) return;
  }
}

async function visit(hash) {
  w.location.hash = "#/__between";
  await untilStable();
  w.location.hash = hash;
  await settle(2);
  await untilStable();
}

/** The page's visible text, without Guided setup's own container (checked on its own page). */
function wizardText() {
  const copy = document.body.cloneNode(true);
  copy.querySelectorAll("[data-cw-guided]").forEach((el) => el.remove());
  return visibleText(copy);
}

const STEPS = [
  { id: "wizard-goal", hash: "#/campaigns/new", selector: "[data-cw-goal-card]" },
  { id: "wizard-customers", hash: `#/campaigns/new/customers/${UC}`, selector: "[data-cw-customers]", guided: true },
  { id: "wizard-audience", hash: `#/campaigns/new/audience/${CAMPAIGN}`, selector: "[data-cw-audience]", draft: true },
  { id: "wizard-review", hash: `#/campaigns/new/review/${CAMPAIGN}`, selector: "[data-cw-review]", draft: true },
  { id: "wizard-send", hash: `#/campaigns/new/send/${CAMPAIGN}`, selector: "[data-cw-send]" },
];

for (const step of STEPS) {
  test(`${step.id}: no engine word outside technical details (empty allow-list)`, async () => {
    missing.clear();
    draft = Boolean(step.draft);
    await visit(step.hash);
    assert.ok($(step.selector), `${step.id} did not draw: ${$("#app").textContent.slice(0, 300)}`);
    if (step.guided) assert.ok($("[data-cw-guided] #ag-file"), "Guided setup itself is mounted, unchanged");
    const unrecorded = [...missing].filter((k) => !k.includes("/__") && !EXPECTED_UNRECORDED.has(k) && !isRunLookup(k));
    assert.deepEqual(unrecorded, [], `${step.id} asked for answers that are not recorded`);
    const text = wizardText();
    const v = violations(text, {});
    assert.ok(v.ok, describe(step.id, v));
  });
}

test("the states no recording reaches carry no engine word either", () => {
  const uc = { id: UC, name: "E-mail Offer Choice" };
  const holder = document.createElement("div");
  const bodies = {
    "goal-no-role": goalHtml({ canStart: false }),
    "goal-none": goalHtml({ goals: [], more: [] }),
    "customers-first": customersHtml({ uc, first: true }),
    "customers-again": customersHtml({ uc, first: false, busy: "Starting the contact list…" }),
    "customers-no-guided": customersHtml({ uc, first: true, hasGuided: false }),
    "customers-no-role": customersHtml({ uc, first: true, canStart: false }),
  };
  for (const [id, body] of Object.entries(bodies)) {
    holder.innerHTML = pageHtml("customers", body);
    holder.querySelectorAll("[data-cw-guided]").forEach((el) => el.remove());
    const v = violations(visibleText(holder), {});
    assert.ok(v.ok, describe(id, v));
  }
  // The campaign progress page in each of its states, and the sentence above Start.
  const run = (state, extra = {}, error = null) => ({
    record: {
      campaign_run_id: "cr_x",
      state,
      use_case_id: UC,
      campaign_id: state === "done" ? CAMPAIGN : null,
      score_run_id: null,
      error,
      steps: [
        { key: "train", title: "Learning from your customers", state: state === "done" ? "done" : "running" },
        { key: "score", title: "Scoring your customers", state: state === "done" ? "done" : "pending" },
      ],
      ...extra,
    },
    pct: 40,
    eta_seconds: state === "running" ? 600 : null,
    summary: error ? error.message : "Learning from your customers: about 10 min left.",
  });
  const stopped = { code: "RUN_INTERRUPTED", message: "Learning from your customers stopped because the server restarted. Start the campaign again." };
  for (const [id, html] of Object.entries({
    "run-running": runPageHtml(run("running")),
    "run-done": runPageHtml(run("done")),
    "run-stopped": runPageHtml(run("stopped", {}, stopped)),
    "run-failed": runPageHtml(run("failed", {}, { code: "VALIDATION_FAILED", message: "The data did not pass the checks. Open the Data Doctor and check it again." })),
    "start-sentence": `<p>${startSentenceHtml({ table: "customers.csv", customers: 1200, speed: "balanced" })}</p>`,
    "start-sentence-reuse": `<p>${startSentenceHtml({ table: "customers.csv", customers: null, reuse: true })}</p>`,
  })) {
    holder.innerHTML = html;
    const v = violations(visibleText(holder), {});
    assert.ok(v.ok, describe(id, v));
  }
  // M181 part C: the Review screen's states for the model behind a list, and Send after its download started.
  const campaign = { campaign_id: CAMPAIGN, use_case_id: UC, run_ids: ["r_x"], status: "live" };
  const list = { treat_rows: 1200 };
  const review = (model, extra = {}) => reviewHtml({ campaign, list, preview: null, cost: null, compliance: null, resultsOn: null, model, ...extra });
  const steps = {
    "review-approved": ["review", review({ state: "approved", canApprove: true, own: false })],
    "review-busy": ["review", review({ state: "approved", canApprove: true, own: false }, { busy: "Approving…" })],
    "review-new-model": ["review", review({ state: "pending", canApprove: true, own: false })],
    "review-new-model-busy": ["review", review({ state: "pending", canApprove: true, own: false }, { busy: "Approving the new model…" })],
    "review-new-model-held": ["review", review({ state: "pending", canApprove: false, own: false })],
    "review-new-model-own": ["review", review({ state: "pending", canApprove: true, own: true })],
    "review-model-refused": ["review", review({ state: "refused", canApprove: true, own: false })],
    // M181 part C review: the registry did not answer (fails closed), and a new model that can no longer be approved.
    "review-model-unknown": [
      "review",
      review({ state: "unknown", canApprove: true, own: false, error: { code: "STORE_BUSY", message: "The registry is busy." } }),
    ],
    "review-model-superseded": ["review", review({ state: "superseded", canApprove: true, own: false })],
    "review-no-role": ["review", review({ state: "pending", canApprove: true, own: false }, { canApprove: false })],
    "send-downloaded": ["send", sendHtml({ campaign, plan: null, downloadHref: "/runs/r_x/treat_list.csv", resultsOn: null, downloaded: true })],
  };
  for (const held of ["pending", "refused", "superseded", "unknown"]) {
    steps[`send-held-${held}`] = ["send", sendHtml({ campaign, plan: null, downloadHref: "/runs/r_x/treat_list.csv", resultsOn: null, held })];
  }
  for (const [id, [step, body]] of Object.entries(steps)) {
    holder.innerHTML = pageHtml(step, body);
    const v = violations(visibleText(holder), {});
    assert.ok(v.ok, describe(id, v));
  }
  // The campaign's own page says the same of a held list (M176's views, `LIST_HOLD`), in the same words.
  for (const [held, words] of Object.entries(LIST_HOLD)) {
    holder.innerHTML = `<p>${words}</p>`;
    const v = violations(visibleText(holder), {});
    assert.ok(v.ok, describe(`campaign-page-held-${held}`, v));
  }
});

test("the check would catch an engine word on a wizard step (planted)", async () => {
  draft = false;
  await visit("#/campaigns/new");
  const planted = document.createElement("p");
  planted.textContent = "Download the treat list";
  $("[data-cw-goal-card]").appendChild(planted);
  assert.equal(violations(wizardText(), {}).ok, false);
  planted.remove();
});
