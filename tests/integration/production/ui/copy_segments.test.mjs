/* Campaign copy written per score band, per main reason or per uplift segment (DEC-1240 … DEC-1247).
   The screen offers "Write one message per" before anything is generated - uplift segment only on an
   uplift scoring run - sends the choice as `overrides.segment_by` (nothing extra per band), and labels
   every template card with the group it was written for and how many customers that group holds. The
   batches below are shaped as `generate_campaign_copy` writes `copy_batch.json`; each request the
   controller makes is checked for what it sent. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { installPage, settle } from "./harness.mjs";

let batch = null;
let run = { run_id: "r1", created_at: "2026-09-01T00:00:00Z", problem_type: "binary_classification" };
const { w, calls } = installPage((request) => {
  if (request.method === "GET" && request.path === "/runs/r1") return { status: 200, body: { run } };
  if (request.method === "POST" && request.path === "/runs/r1/campaign-copy") {
    return { status: 202, body: { run_id: "r1", job_id: "j1" } };
  }
  if (request.path === "/runs/r1/artefacts/copy_batch.json" && batch) return { status: 200, body: batch };
  return null;
});

const copy = await import("../../../../ui/modules/generative/copy.js");
const uc = { id: "win-back-campaign", name: "Win-back Campaign", marker: "G" };

const template = (id, extra) => ({
  template_id: id,
  band: "High",
  channel: "sms",
  variant: "A",
  subject: null,
  text: "Hi {{first_name}}. Reply STOP to opt out",
  fields_used: ["first_name"],
  status: "pending_review",
  judge_scores: [],
  guardrails: [],
  block_reason: null,
  attempts: 1,
  approved_by: null,
  approved_at: null,
  ...extra,
});
const base = {
  run_id: "r1",
  batch_id: "cb_r1",
  holdout: { control_rows: 5, suppressed_rows: 3, out_of_band_rows: 0 },
  require_human_review: true,
  messages_rendered: 10,
  messages_blocked: 0,
  prompt_versions: {},
  prompt_hashes: {},
  created_at: "2026-09-01T00:00:00Z",
};

/** Mount a fresh controller for r1 and draw it into a holder, re-drawing whenever it asks. */
async function mount() {
  const holder = w.document.createElement("div");
  w.document.body.appendChild(holder);
  let controller;
  const draw = () => {
    holder.innerHTML = copy.copyHtml(uc, controller.state);
    controller.bind(holder);
  };
  controller = copy.createCopyController(uc, "r1", draw);
  await controller.load();
  draw();
  return { holder, controller };
}

const perButtons = (holder) => [...holder.querySelectorAll("[data-copy-per]")].map((b) => b.dataset.copyPer);

test("a run that is not an uplift run offers score band and main reason, and per band sends nothing extra", async () => {
  batch = null;
  const { holder, controller } = await mount();
  assert.deepEqual(perButtons(holder), ["band", "top_reason"]);
  assert.match(holder.textContent, /Write one message per/);
  assert.equal(holder.querySelector("[data-copy-per].on").dataset.copyPer, "band");

  holder.querySelector("#g-generate-copy").click();
  await settle(6);
  const post = calls.filter((c) => c.method === "POST" && c.path === "/runs/r1/campaign-copy").pop();
  assert.deepEqual(post.body, { overrides: {} });
  controller.stop();
});

test("choosing main reason sends segment_by: top_reason", async () => {
  batch = null;
  const { holder, controller } = await mount();
  holder.querySelector('[data-copy-per="top_reason"]').click();
  assert.equal(holder.querySelector("[data-copy-per].on").dataset.copyPer, "top_reason");
  assert.match(holder.querySelector(".gper .seg-help").textContent, /main reason/);
  holder.querySelector("#g-generate-copy").click();
  await settle(6);
  const post = calls.filter((c) => c.method === "POST" && c.path === "/runs/r1/campaign-copy").pop();
  assert.deepEqual(post.body, { overrides: { segment_by: "top_reason" } });
  controller.stop();
});

test("an uplift scoring run also offers uplift segment, and sends it", async () => {
  batch = null;
  run = { ...run, problem_type: "uplift" };
  const { holder, controller } = await mount();
  assert.deepEqual(perButtons(holder), ["band", "top_reason", "uplift_segment"]);
  holder.querySelector('[data-copy-per="uplift_segment"]').click();
  holder.querySelector("#g-generate-copy").click();
  await settle(6);
  const post = calls.filter((c) => c.method === "POST" && c.path === "/runs/r1/campaign-copy").pop();
  assert.deepEqual(post.body, { overrides: { segment_by: "uplift_segment" } });
  controller.stop();
  run = { ...run, problem_type: "binary_classification" };
});

test("a batch written per main reason labels each card with its segment and size, largest first", async () => {
  batch = {
    ...base,
    audience: { rows: 30, per_band: { High: 12, Medium: 18 } },
    segment_by: "top_reason",
    segments: [
      { segment: "reason_tenure", label: "Main reason: tenure", rows: 20, share_pct: 66.7, per_band: {}, reasons: [], written: true },
      { segment: "other_reasons", label: "Other reasons", rows: 10, share_pct: 33.3, per_band: {}, reasons: [], written: true },
    ],
    templates: [
      template("other_reasons-sms-A", { band: "other_reasons", segment: "other_reasons" }),
      template("reason_tenure-sms-A", { band: "reason_tenure", segment: "reason_tenure" }),
    ],
  };
  const { holder, controller } = await mount();
  const sections = [...holder.querySelectorAll("[data-group]")].map((s) => s.dataset.group);
  assert.deepEqual(sections, ["reason_tenure", "other_reasons"]);
  const card = holder.querySelector('[data-template="reason_tenure-sms-A"] .gtpl-seg');
  assert.equal(card.textContent, "Main reason: tenure · 20 customers");
  assert.equal(holder.querySelector('[data-template="other_reasons-sms-A"] .gtpl-seg').textContent, "Other reasons · 10 customers");
  assert.match(holder.querySelector(".gverdict-sub").textContent, /2 groups × 1 channel/);
  assert.equal(holder.querySelector("[data-copy-per]"), null, "the chooser is for before generating");
  controller.stop();
});

test("a batch written per band labels each card with its band and the band's size", async () => {
  batch = {
    ...base,
    audience: { rows: 30, per_band: { High: 12, Medium: 18 } },
    templates: [template("High-sms-A"), template("Medium-sms-A", { band: "Medium" })],
  };
  const { holder, controller } = await mount();
  assert.equal(holder.querySelector('[data-template="High-sms-A"] .gtpl-seg').textContent, "High band · 12 customers");
  assert.equal(holder.querySelector('[data-template="Medium-sms-A"] .gtpl-seg').textContent, "Medium band · 18 customers");
  assert.match(holder.querySelector(".gverdict-sub").textContent, /2 bands × 1 channel/);
  controller.stop();
});

test("a batch written per uplift segment shows the persuadables' cards and says why nobody else gets one", async () => {
  batch = {
    ...base,
    holdout: { ...base.holdout, not_persuadable_rows: 40 },
    audience: { rows: 15, per_band: {} },
    segment_by: "uplift_segment",
    segments: [
      { segment: "persuadable", label: "Persuadables", rows: 15, share_pct: 27.3, per_band: {}, reasons: [], written: true },
      {
        segment: "sleeping_dog",
        label: "Sleeping dogs",
        rows: 40,
        share_pct: 72.7,
        per_band: {},
        reasons: [],
        written: false,
        skipped_reason: "Never treat (contact makes it worse), so no message is written for them.",
      },
    ],
    templates: [template("persuadable-sms-A", { band: "persuadable", segment: "persuadable" })],
  };
  const { holder, controller } = await mount();
  assert.deepEqual([...holder.querySelectorAll("[data-group]")].map((s) => s.dataset.group), ["persuadable"]);
  assert.equal(holder.querySelector(".gtpl-seg").textContent, "Persuadables · 15 customers");
  const skipped = holder.querySelector('[data-skipped="sleeping_dog"]');
  assert.match(skipped.textContent, /Sleeping dogs · 40 customers: Never treat/);
  assert.match(holder.textContent, /writes nothing for 40 who are not persuadables/);
  controller.stop();
});
