/* Plan J M94: campaigns beside the runs on Results, and a campaign's page - the plan the server froze
   (its values only), the form before one exists, an early look with no verdict, a final verdict. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, settle, until } from "../harness.mjs";
import { fixture, installWholePage } from "./fake.mjs";

const ids = fixture("ids");
const world = {
  campaigns: fixture("campaigns_empty"),
  views: { [ids.fresh]: fixture("fresh"), [ids.dated]: fixture("early") },
  plans: { [ids.fresh]: fixture("fresh_plans"), [ids.dated]: fixture("dated_plans") },
  posted: [],
  onPlan: null,
  onMeasure: null,
};
const { w } = await installWholePage({ hash: "#/results", world });

const open = async (hash) => {
  w.location.hash = "#/";
  await settle(3);
  w.location.hash = hash;
};

const pct = (fraction) => `${Number((fraction * 100).toFixed(1))}%`;

test("with no campaign, Results is drawn exactly as before", async () => {
  await until(() => $("[data-results]"), 3000, "the runs");
  await settle(3);
  assert.equal($("[data-campaigns]"), null, "no empty campaigns card");
});

test("Results lists the campaigns beside the runs, newest first, each linking to its page", async () => {
  world.campaigns = fixture("campaigns");
  await open("#/results");
  await until(() => $("[data-campaigns]"), 3000, "the campaigns list");
  assert.ok($("[data-results]"), "the runs are still there");
  const links = $$("[data-campaigns] a[data-campaign]");
  assert.deepEqual(
    links.map((a) => a.textContent),
    world.campaigns.campaigns.map((c) => c.name),
  );
  assert.deepEqual(
    links.map((a) => a.getAttribute("href")),
    world.campaigns.campaigns.map((c) => `#/campaigns/${c.campaign_id}`),
  );
  const cells = [...$$("[data-campaigns] tbody tr")[0].querySelectorAll("td")].map((td) => td.textContent.trim());
  assert.equal(cells[2], "Measured", "the June campaign was measured");
  assert.equal(cells[3], world.campaigns.campaigns[0].counts.intended.toLocaleString("en-US"));
});

test("before a plan exists, an Analyst gets the form and the server's answer is what is drawn", async () => {
  await open(`#/campaigns/${ids.fresh}`);
  await until(() => $("[data-plan-form]"), 3000, "the plan form");
  assert.match($("[data-campaign-result]").textContent, /not been measured yet/);
  assert.equal($("[data-plan-hash]"), null, "no number of ours before the server answers");
  assert.equal($("[data-plan-card] input[name=outcome_column]").value, "reactivated_90d", "the outcomes file's column");
  world.onPlan = (id, body) => {
    world.posted.push({ id, body });
    world.views[ids.fresh] = fixture("fresh_after");
    world.plans[ids.fresh] = fixture("fresh_plans_after");
    return { status: 201, body: fixture("fresh_plan_registered") };
  };
  const form = $("[data-plan-form]");
  form.elements.namedItem("metric").value = "came back within 90 days";
  form.elements.namedItem("analysis_date").value = "2026-09-15";
  form.elements.namedItem("mde_pp").value = "2";
  form.elements.namedItem("base_rate_pct").value = "10";
  form.dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => $("[data-plan-hash]"), 3000, "the registered plan");
  assert.deepEqual(world.posted, [
    {
      id: ids.fresh,
      body: {
        metric: "came back within 90 days",
        outcome_column: "reactivated_90d",
        analysis_date: "2026-09-15",
        expectation: "",
        mde_pp: 2,
        base_rate: 0.1,
      },
    },
  ]);
  const plan = fixture("fresh_plan_registered");
  assert.equal($("[data-plan-hash] .mono").textContent, plan.plan_hash.slice(0, 12));
  assert.match($("[data-plan-holdout]").textContent, new RegExp(pct(plan.holdout_fraction).replace(".", "\\.")));
  assert.match($("[data-plan-holdout]").textContent, new RegExp(`${plan.n_holdout.toLocaleString("en-US")} of`));
  assert.match($("[data-plan-power]").textContent, new RegExp(`${Math.round(plan.achieved_power * 100)}%`));
  assert.equal($("[data-plan-form]"), null, "the form is gone once the plan is fixed");
});

test("an early look is labelled and carries no verdict", async () => {
  await open(`#/campaigns/${ids.dated}`);
  await until(() => $("[data-early-look]"), 3000, "the early look");
  assert.equal($("[data-verdict]"), null, "no final verdict on an early look");
  assert.match($("[data-campaign-result]").textContent, /Not a final result/);
  assert.match($("[data-campaign-result]").textContent, /Early look, before the planned analysis date/);
  assert.ok($('[data-plan-warning="PLAN_UNDERPOWERED"]'), "the underpowered plan says so, as a warning");
});

test("measuring on the analysis date draws the server's verdict", async () => {
  world.onMeasure = () => {
    world.views[ids.dated] = fixture("final");
    return { status: 200, body: fixture("final") };
  };
  $("[data-measure]").click();
  await until(() => $("[data-verdict]"), 3000, "the verdict");
  assert.equal($("[data-early-look]"), null);
  assert.equal($("[data-verdict]").textContent, fixture("final").verdict.headline);
});

test("a measurement refused while outcome windows are open shows the server's sentence and no number", async () => {
  world.onMeasure = () => ({
    status: 409,
    body: {
      detail: { code: "CAMPAIGN_NOT_MATURED", message: "Some customers are still inside the outcome window: measure again on or after 2026-07-30.", path: null },
      results_available_on: "2026-07-30",
    },
  });
  $("[data-measure]").click();
  await until(() => $("[data-measure-error]"), 3000, "the refusal");
  assert.match($("[data-measure-error]").textContent, /2026-07-30/);
});
