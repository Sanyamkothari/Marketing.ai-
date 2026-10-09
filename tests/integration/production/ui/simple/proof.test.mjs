/* Plan J M104: the Value Proof Pack on screen, against the real app's answers (`test_simple_ui.py` builds a
   real measured campaign with a planted harmful band and captures its pack, its page, the approval of its one
   suggestion and a refusal). What a person sees:
   * Results lists the ready packs, each with the server's own sentence and a link to it;
   * the Reports hub lists them too, with the page and its PDF;
   * a pack's page shows the server's report, the suggestion to leave the harmed band out next cycle and, while
     sign-in is off, the button to approve it; approving posts the group the server named and redraws;
   * a campaign without a final result shows the server's reason and the day, never a number. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, settle, until } from "../harness.mjs";
import { fixture, installWholePage } from "./fake.mjs";

const proofs = fixture("proofs");
const [entry] = proofs.proofs;
const pack = fixture("proof");
const world = {
  runs: fixture("runs_empty"),
  waiting: null,
  proofs,
  packs: {
    [entry.campaign_id]: { json: pack, html: fixture("proof_page").html, after: fixture("proof_approved") },
    c_20261009_waiting0: { refused: fixture("proof_refused") },
  },
};
const { w, calls } = await installWholePage({ hash: "#/results", world });

const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");

async function open(hash, ready) {
  w.location.hash = "#/";
  await settle(3);
  w.location.hash = hash;
  await until(ready, 3000, hash);
  await settle(6);
}

test("Results lists the ready packs in the server's words, each a link to its pack", async () => {
  await open("#/results", () => $("[data-proofs]"));
  const link = $(`[data-proofs] a[data-proof="${entry.campaign_id}"]`);
  assert.ok(link, "the pack is listed");
  assert.equal(link.getAttribute("href"), `#/pilot/proof/${entry.campaign_id}`);
  assert.equal(text(link), entry.name);
  const row = text(link.closest("tr"));
  assert.ok(row.includes(entry.headline), "the pack's own sentence");
  assert.ok(row.includes(entry.claim_label), "what its numbers can claim");
});

test("the Reports hub lists the pack with its page and its PDF", async () => {
  await open("#/pilot", () => $('[data-pe-card="proof"] tbody tr'));
  const actions = $$('[data-pe-card="proof"] tbody tr a').map((a) => a.getAttribute("href"));
  assert.ok(actions.includes(`#/pilot/proof/${entry.campaign_id}`));
  assert.ok(actions.some((href) => href.endsWith(`/pilot/proof/${entry.campaign_id}?format=pdf`)));
});

test("a pack's page shows the server's report and the suggestion an Analyst approves", async () => {
  await open(`#/pilot/proof/${entry.campaign_id}`, () => $("iframe.pe-frame") && $("[data-pe-proposals]"));
  assert.equal($("main h1").textContent, "Value Proof Pack");
  assert.ok(text($("main .sub")).includes(pack.campaign_name.text));
  const [proposal] = pack.proposals;
  assert.ok(text($("[data-pe-proposals]")).includes(proposal.segment.text));
  assert.ok(text($("[data-pe-proposals]")).includes(proposal.worst_case.text));
  const button = $("[data-pe-approve]");
  assert.ok(button, "sign-in is off: whoever opens it may approve");
  button.click();
  await until(() => (world.approved || []).length === 1, 3000, "the approval");
  assert.deepEqual(world.approved[0], { dimension: proposal.dimension, segment: proposal.segment.value });
  await until(() => !$("[data-pe-approve]") && /Approved by/.test(text($("[data-pe-proposals]"))), 3000, "the redraw");
  const posted = calls.filter((c) => c.method === "POST" && c.path.endsWith("/suppressions"));
  assert.equal(posted.length, 1, "approved once");
});

test("a campaign without a final result shows the server's reason, never a number", async () => {
  const refused = fixture("proof_refused");
  await open("#/pilot/proof/c_20261009_waiting0", () => $("main .notice-card") || $("main [role='status']"));
  const page = text($("main"));
  assert.ok(page.includes(refused.detail.message));
  assert.match(page, /final result/);
  assert.equal($("iframe.pe-frame"), null);
});
