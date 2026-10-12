/* Plan M, M188 (DEC-1618): the look of Reports, in jsdom - what the markup must say for the CSS (ui/theme.css and the
   module's own) to draw it, and for a screen reader to read it. The words and the requests are the older tests' business
   (tabs.test.mjs, audit_equal.test.mjs); this file holds the look's contracts:

   - the header is components.js's: one H1, one sentence, the page's one primary action inside it;
   - the tabs are underline tabs (role=tablist / tab / aria-selected, a roving tabindex), never pills;
   - an evidence label is a status chip: an icon hidden from screen readers and the word, in the tone of the mapping;
   - the net value is a value range: a bar hidden from screen readers and, always beside it, the server's words;
   - the programme summary has a KPI row of two to four tiles above the card Results draws;
   - the check form is a clean three-step form: drop zones, a label above every field tied to it, help text under it. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, settle } from "../harness.mjs";
import { installReports, open, recorded, text } from "./fake.mjs";

const proofs = recorded["GET /pilot/proof"].body.proofs;
const [spend, conversion] = proofs;

const { w, overlay } = await installReports({ hash: "#/reports" });
const { splitHeadline } = await import("../../../../../ui/modules/reports/value_proof.js");
const { programmeKpis } = await import("../../../../../ui/modules/reports/programme.js");

const proofRows = () => $$("[data-proofs] tbody tr");
const proofsReady = () => proofRows().length > 0 || $("[data-proofs-empty]");

test("the header is the look's: one H1, one sentence and the one primary action inside it", async () => {
  await open(w, "#/reports", proofsReady);
  const header = $("main > .page-header");
  assert.ok(header, "components.js pageHeader");
  assert.equal(header.querySelectorAll("h1").length, 1);
  assert.equal(header.querySelectorAll(".desc").length, 1);
  const primary = header.querySelectorAll(".ph-actions .btn.primary");
  assert.equal(primary.length, 1, "the newest pack is the page's one primary action, on the right of the title");
  assert.equal(text(primary[0]), "Open the newest Value Proof Pack");
  assert.equal($$("main .btn.primary").length, 1, "and the only one on the page");
  assert.equal($(".breadcrumb, .crumbs"), null, "Reports is a top-level place: no breadcrumb");
});

test("the tabs are underline tabs with the tab roles, not pills", async () => {
  const list = $('main [role="tablist"]');
  assert.ok(list && list.classList.contains("ds-tabs"), "components.js tabs");
  assert.equal(list.getAttribute("aria-label"), "Reports");
  const tabs = [...list.querySelectorAll('[role="tab"]')];
  assert.deepEqual(tabs.map(text), ["Value proof", "Programme summary", "Check a past campaign", "Model reports"]);
  assert.deepEqual(tabs.map((t) => t.getAttribute("aria-selected")), ["true", "false", "false", "false"]);
  assert.deepEqual(tabs.map((t) => t.getAttribute("tabindex")), ["0", "-1", "-1", "-1"], "a roving tabindex");
  assert.ok(tabs.every((t) => t.getAttribute("href")?.startsWith("#/reports/")), "each tab is still a route");
  assert.equal($(".tabs-bar .tab, nav.tabs a.tab"), null, "no pill tabs");
  assert.equal($('[data-reports-tab="models"]').hasAttribute("data-technical"), true, "the technical tab stays marked for the switch");
});

test("the evidence label is a status chip: an icon that screen readers skip, and the word, in the mapping's tone", async () => {
  overlay["GET /pilot/proof"] = {
    status: 200,
    body: {
      proofs: [
        { ...spend, campaign_id: "c_a", name: "Spring win-back", claim_label: "Random by your statement, not verified" },
        { ...spend, campaign_id: "c_b", name: "Summer offer", claim_label: "Descriptive only" },
        { ...spend, campaign_id: "c_d", name: "Winter offer", claim_label: "Causal: the customers held back were chosen at random" },
      ],
    },
  };
  await open(w, "#/reports/value-proof", () => proofRows().length === 3);
  const chip = (id) => $(`[data-proof-campaign="${id}"]`).closest("tr").querySelector("[data-claim] .badge");
  const seen = {
    c_a: ["info", "Estimated", "estimated"],
    c_b: ["neutral", "Not yet proven", "not_yet_proven"],
    c_d: ["good", "Proven", "proven"],
  };
  for (const [id, [tone, word, status]] of Object.entries(seen)) {
    const el = chip(id);
    assert.ok(el && el.matches(".badge[data-status]"), `${id}: statusChip("evidence", …), a .badge[data-status]`);
    assert.equal(el.dataset.status, status, `${id}: the evidence key of the mapping`);
    assert.equal(el.dataset.tone, tone, `${id}: the tone of the mapping`);
    assert.ok(el.closest("td").querySelector(".td-sub"), `${id}: the one-line reason is the cell's td-sub line`);
    assert.equal(text(el), word);
    const icon = el.querySelector("svg");
    assert.ok(icon && icon.getAttribute("aria-hidden") === "true", `${id}: an icon, hidden from screen readers: the word carries the meaning`);
  }
  assert.equal($("[data-proofs] .pill, [data-proofs] .chip"), null, "the old pills are gone");
  delete overlay["GET /pilot/proof"];
});

test("the net value is a value range: the bar is decoration, the server's words are beside it", async () => {
  await open(w, "#/reports/value-proof", () => proofRows().length === proofs.length);
  const row = proofRows().find((tr) => tr.querySelector(`[data-proof-campaign="${conversion.campaign_id}"]`));
  const range = row.querySelector("[data-value-range]");
  assert.ok(range, "a value range in the row");
  assert.equal(range.querySelector(".vr-track").getAttribute("aria-hidden"), "true");
  assert.match(text(range.querySelector(".vr-text")), /^Net value ₹88,187 to ₹4,18,481 \(4\.18 lakh\)\.$/, "the server's net value sentence, unchanged");
  assert.ok(range.querySelector(".vr-bar"), "the range as a bar");
  assert.equal(range.querySelector(".vr-dot"), null, "no estimate is in the list, so none is drawn");
  assert.ok(range.querySelector(".vr-zero"), "the range starts near zero: a zero tick");
  assert.equal(text(row.querySelector('td[data-label="What it shows"]')), conversion.headline, "and the whole headline still reads as the server wrote it");
  const actions = row.querySelectorAll(".rp-acts .btn");
  assert.deepEqual([...actions].map(text), ["Open", "PDF"]);
  assert.ok(actions[0].classList.contains("secondary") && actions[1].classList.contains("tertiary"));
});

test("splitHeadline: the effect and the net value's two figures; a headline with no net value stays whole", () => {
  assert.deepEqual(splitHeadline(conversion.headline), {
    effect: "Extra outcomes because of the campaign: +26 (likely +9 to +43).",
    net: "Net value ₹88,187 to ₹4,18,481 (4.18 lakh).",
    low: 88187,
    high: 418481,
  });
  const minus = splitHeadline("Extra amount: -10 (likely -30 to +5). Net value -₹5,000 to ₹12,500.");
  assert.equal(minus.low, -5000);
  assert.equal(minus.high, 12500);
  const none = splitHeadline("Descriptive only: the groups were not chosen at random.");
  assert.deepEqual(none, { effect: "Descriptive only: the groups were not chosen at random.", net: "", low: null, high: null });
  assert.deepEqual(splitHeadline(""), { effect: "", net: "", low: null, high: null });
});

test("a pack with no net value draws its sentence as plain text, never an empty bar", async () => {
  overlay["GET /pilot/proof"] = {
    status: 200,
    body: { proofs: [{ ...spend, campaign_id: "c_n", name: "Autumn", claim_label: "Descriptive only", headline: "Descriptive only: the groups were not chosen at random." }] },
  };
  await open(w, "#/reports/value-proof", () => proofRows().length === 1);
  assert.equal($("[data-proofs] [data-value-range]"), null);
  assert.match(text($("[data-proofs] tbody")), /Descriptive only: the groups were not chosen at random\./);
  delete overlay["GET /pilot/proof"];
});

test("no pack yet: an empty state with a 40px icon, one sentence and one action", async () => {
  overlay["GET /pilot/proof"] = { status: 200, body: { proofs: [] } };
  await open(w, "#/reports", () => $("[data-proofs-empty]"));
  const empty = $("[data-proofs-empty] .empty-state");
  assert.ok(empty && empty.querySelector(".es-icon svg"), "an icon");
  assert.equal(empty.querySelectorAll("a.btn, button.btn").length, 1, "one action");
  assert.equal($("main .btn.primary"), null, "nothing to open: no primary button in the header either");
  delete overlay["GET /pilot/proof"];
});

test("programme summary: a KPI row of tiles above the card, rupees first; the card is the one Results draws", async () => {
  await open(w, "#/reports/programme", () => $("[data-proven]"));
  const row = $("[data-programme-kpis]");
  assert.ok(row && row.classList.contains("stat-row"), "components.js kpiRow");
  const tiles = [...row.querySelectorAll(".stat")];
  assert.ok(tiles.length >= 2 && tiles.length <= 4, "two to four tiles");
  assert.equal(tiles[0].dataset.totalTile, "rupees", "the rupees total first");
  assert.equal(text(tiles[0].querySelector(".stat-label")), "Net value proven");
  assert.equal(text(tiles[0].querySelector(".stat-value")), "₹81,034");
  assert.match(text(tiles[0].querySelector(".stat-caption")), /A floor, not an estimate\. From 1 campaign\./);
  assert.equal(row.nextElementSibling, $("[data-proven]"), "the card follows the row");
  assert.ok(!$("[data-proven]").contains(row), "and the row is not inside it: the card's markup is the one Results draws");
});

test("programmeKpis: at most four tiles, nothing for a summary with no totals", () => {
  const total = (unit, n) => ({ unit, unit_column: { text: unit }, total: { value: n, text: String(n), format: "count" }, campaigns: [{}, {}] });
  const html = programmeKpis({ totals: ["a", "b", "c", "d", "e", "rupees"].map((u, i) => total(u, i + 1)) });
  const holder = w.document.createElement("div");
  holder.innerHTML = html;
  assert.equal(holder.querySelectorAll(".stat").length, 4);
  assert.equal(holder.querySelector(".stat").dataset.totalTile, "rupees");
  assert.match(holder.textContent, /From 2 campaigns\./);
  assert.equal(programmeKpis({ totals: [] }), "");
  assert.equal(programmeKpis(null), "");
});

test("check a past campaign: three drop zones, a label above every field tied to it, help text under it", async () => {
  await open(w, "#/reports/check", () => $("[data-rp-check-form]"));
  const zones = $$(".rp-drop");
  assert.equal(zones.length, 4, "three files and the programme's");
  for (const zone of zones) {
    assert.equal(zone.tagName, "LABEL", "the whole zone is the file input's label");
    const input = zone.querySelector('input[type="file"]');
    assert.ok(input && input.hasAttribute("data-rp-file"), "a real file input inside the zone");
    assert.equal(zone.querySelector(".rp-drop-ico svg").getAttribute("aria-hidden"), "true");
    assert.match(text(zone), /Choose a file or drop it here/);
  }
  const basis = $$("[data-rp-basis] .rp-choice");
  assert.equal(basis.length, 2, "how the groups were chosen: two choice cards");
  assert.ok(basis.every((c) => c.querySelector('input[type="radio"]')), "each a real radio");
  assert.equal($("[data-rp-basis] input:checked"), null, "never assumed");
  // The name field: its label is `for` the control, and the control is 40px-class (.input).
  const name = $("#rp-f-name");
  assert.ok(name && name.classList.contains("input"));
  assert.equal($('label[for="rp-f-name"]').textContent, "A name for this campaign (optional)");
  assert.equal($$(".rp-step").length, 3);
  assert.equal($$("main .btn.primary").length, 1, "one primary button: the check");
});

test("check a past campaign: with a file chosen, every column question is labelled above and its help is described by it", async () => {
  const { chooseFile, uploaded } = await import("./fake.mjs");
  overlay["POST /uploads"] = uploaded("u_a", "groups.csv", ["customer_id", "arm", "sent"]);
  await open(w, "#/reports/check", () => $("[data-rp-check-form]")); // a fresh form
  chooseFile(w, $('[data-rp-file="assignment"]'), "groups.csv"); // choosing the file draws the form again
  const until = Date.now() + 3000;
  while (!$("[data-file-name]") && Date.now() < until) await settle(2);
  assert.match(text($("[data-file-name]")), /groups\.csv · /, "the file's name and rows once chosen");
  assert.ok($(".rp-drop.has-file"), "the zone shows it is filled");
  for (const field of $$(".rp-field")) {
    const control = field.querySelector("input, select");
    const label = field.querySelector("label");
    assert.ok(control && label, "a label and a control");
    assert.equal(label.getAttribute("for"), control.id, `${control.name}: the label is for the control`);
    const help = field.querySelector(".rp-hint");
    if (help) assert.equal(control.getAttribute("aria-describedby"), help.id, `${control.name}: the help text describes it`);
  }
  assert.ok($("#rp-f-control_value").getAttribute("aria-describedby"), "the held-back value has its help text");
  delete overlay["POST /uploads"];
});

test("model reports: the practice dataset's problem is a status chip with an icon, not a grey pill", async () => {
  overlay["GET /datasets"] = { status: 200, body: { datasets: [] } };
  overlay["GET /pilot/demo"] = { status: 200, body: { manifest: { broken_dataset_id: "d_practice", use_case_id: "hillstrom-email", campaigns: [] } } };
  await open(w, "#/reports/models", () => $('[data-models-part="readiness"] tbody tr'));
  const chip = $('[data-models-part="readiness"] .badge');
  assert.ok(chip, "the practice dataset is listed with a chip");
  assert.equal(chip.dataset.tone, "warning");
  assert.match(text(chip), /Practice data with problems/);
  assert.equal(chip.querySelector("svg").getAttribute("aria-hidden"), "true");
  assert.equal($("[data-rp-models] .chip.neutral"), null, "no old chip");
  const opens = $$("[data-rp-models] [data-report-open]");
  assert.ok(opens.length >= 1);
  for (const open of opens) assert.ok(open.classList.contains("secondary") && open.classList.contains("sm"), "report buttons are the small secondary kind");
  delete overlay["GET /datasets"];
  delete overlay["GET /pilot/demo"];
});
