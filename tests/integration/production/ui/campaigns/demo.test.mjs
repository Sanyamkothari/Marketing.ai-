/* Plan M M176 (DEC-1606): the Campaigns list and every tab of both Hillstrom campaigns' pages, drawn as the browser
   draws them (app.js and every module in ui/index.html's order) against the API answers recorded from the validated
   Hillstrom demo (tests/fixtures/ui_demo/api.json, shared with the visible-text check).

   What a person sees: the list (short names, goal, status chip, contacted, result), and on each campaign one H1, one
   sentence and one primary button per tab; the three tiles; the contact list 25 rows at a time with a search box and
   one download; "Why these customers?"; the Proof Pack's headline with the evidence label in words; the technical
   links. The old campaign page still draws in the classic layout, and the older places that add results point here. */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { $, $$, settle } from "../harness.mjs";
import { installDemoPage, loadModules, recorded } from "../glossary/replay.mjs";

const C_CONVERSION = "c_20261011_c9276210";
const C_SPEND = "c_20261011_5ee8a5d4";
const SCORE_UPLIFT = "r_20261010_11100003";
const SCORE_RISK = "r_20261010_11100004";
const UC = "hillstrom-email";

const { w, state, missing } = installDemoPage({ hash: "#/__start" });
await loadModules();
const campaigns = await import("../../../../../ui/modules/campaigns/index.js");

async function untilStable(ms = 10000) {
    const started = Date.now();
    let last = null;
    let same = 0;
    while (Date.now() - started < ms) {
        await new Promise((resolve) => setTimeout(resolve, 20));
        const now = `${$("#app")?.innerHTML.length}:${w.location.hash}`;
        if (state.inFlight === 0 && now === last) same += 1;
        else same = 0;
        last = now;
        if (same >= 5) return;
    }
}

async function visit(hash) {
    w.location.hash = "#/__between";
    await untilStable();
    missing.clear();
    w.location.hash = hash;
    await settle(2);
    await untilStable();
    assert.deepEqual(
        [...missing].filter((k) => !k.includes("/__")),
        [],
        `${hash} asked for answers that are not recorded`,
    );
}

const text = (sel) => ($(sel) ? $(sel).textContent.replace(/\s+/g, " ").trim() : null);
const primaries = () => $$("#app .btn.primary");
const view = (id) => recorded[`GET /campaigns/${id}`].body;
const proof = (id) => recorded[`GET /pilot/proof/${id}?format=json`].body;

test("the list: both campaigns, each with a short name, its goal, a status chip, how many were contacted and the result", async () => {
    await visit("#/campaigns");
    assert.equal($$("#app h1").length, 1);
    assert.equal(text("#app h1"), "Campaigns");
    assert.equal(primaries().length, 1, "one primary button");
    assert.equal(primaries()[0].getAttribute("href"), "#/campaigns/new");
    assert.equal(primaries()[0].textContent, "+ New campaign");
    const rows = $$("[data-campaigns-list] tbody tr");
    const listed = recorded["GET /campaigns"].body.campaigns;
    assert.equal(rows.length, listed.length);
    rows.forEach((tr, i) => {
        const campaign = listed[i];
        const cells = [...tr.querySelectorAll("td")];
        const link = tr.querySelector("a[data-campaign-link]");
        assert.equal(link.getAttribute("href"), `#/campaigns/${campaign.campaign_id}/overview`);
        assert.ok(link.textContent.length <= 40, link.textContent);
        assert.equal(link.textContent, "Choose the best e-mail for… · Oct 2026");
        assert.equal(tr.querySelector(".cp-sub").textContent, campaign.name, "the long recorded name is the subtitle");
        assert.equal(cells[1].textContent, "Choose the best e-mail for each customer");
        assert.equal(tr.querySelector("[data-status]").getAttribute("data-status"), "measured");
        assert.equal(cells[3].textContent, "12,862");
        assert.equal(tr.querySelector("[data-result]").textContent, proof(campaign.campaign_id).headline);
    });
});

for (const [name, id] of [
    ["conversion", C_CONVERSION],
    ["spend", C_SPEND],
]) {
    test(`${name}: Overview shows where it stands, the next step, three tiles, what was sent and one primary button`, async () => {
        await visit(`#/campaigns/${id}/overview`);
        assert.equal($$("#app h1").length, 1);
        assert.equal(text("#app h1"), "Choose the best e-mail for… · Oct 2026");
        assert.equal(
            text("#app .desc"),
            "Choose the best e-mail for each customer: sent on 11 Oct 2026 to 12,862 customers.",
        );
        assert.equal(text("[data-long-name]"), view(id).campaign.name);
        assert.deepEqual(
            $$(".tabs a").map((a) => a.getAttribute("data-tab")),
            ["overview", "audience", "results", "technical"],
        );
        assert.ok(
            $('.tabs a[data-tab="technical"]').hasAttribute("data-technical"),
            "the Technical tab is marked for M180",
        );
        assert.equal(text("[data-overview] [data-status]"), "Measured");
        assert.equal(text("[data-next-step]"), "Measured on 11 Oct 2026. The full report is ready.");
        assert.equal(primaries().length, 1);
        assert.equal(primaries()[0].textContent, "Open the full report");
        assert.equal(primaries()[0].getAttribute("href"), `#/pilot/proof/${id}`);
        assert.ok($("[data-refresh-ask]"), "Refresh the list is a secondary action");
        assert.equal(text("[data-tile-contacted] .v"), "12,862 customers");
        assert.equal(text("[data-tile-comparison] .l"), "Comparison group (not contacted)");
        assert.equal(text("[data-tile-comparison] .v"), "12,737 customers");
        const net = proof(id)
            .sections.find((s) => s.key === "net_value")
            .lines.find((l) => l.label === "Net value");
        assert.equal(text("[data-tile-value] .v"), `${net.low.text} to ${net.high.text}`);
        const sent = $$("[data-what-was-sent] .cp-kv-row").map((r) => [...r.children].map((c) => c.textContent));
        assert.deepEqual(sent, [
            ["Sent on", "11 Oct 2026"],
            ["Offer: Mens E-Mail", "7,597 customers"],
            ["Offer: Womens E-Mail", "5,265 customers"],
            ["Contact list", "12,862 customers"],
        ]);
    });

    test(`${name}: Audience shows the contact list 25 rows at a time, searchable, with one download and why`, async () => {
        await visit(`#/campaigns/${id}/audience`);
        assert.equal(text("[data-audience-lead]"), "12,862 customers to contact, out of 32,000 customers on the list.");
        assert.equal(primaries().length, 1);
        assert.equal(primaries()[0].textContent, "Download contact list");
        assert.match(primaries()[0].getAttribute("href"), new RegExp(`/runs/${SCORE_UPLIFT}/treat_list\\.csv$`));
        assert.deepEqual(
            $$("[data-audience-table] thead th").map((th) => th.textContent),
            ["Customer", "Group", "Offer", "Channel", "Top reason"],
        );
        const firstKeys = () => $$("[data-audience-table] tbody tr").map((tr) => tr.querySelector("td").textContent);
        assert.equal(firstKeys().length, 25);
        assert.equal(firstKeys()[0], "1");
        assert.match(text("[data-page-of]"), /^Showing 1 to 25 of 12,862 customers · page 1 of 515$/);
        // Next, then Previous: the table alone is repainted.
        const before = firstKeys();
        $('[data-audience-table] [data-page="2"]').click();
        assert.match(text("[data-page-of]"), /^Showing 26 to 50 of 12,862 customers · page 2 of 515$/);
        assert.notDeepEqual(firstKeys(), before);
        $('[data-audience-table] [data-page="1"]').click();
        assert.deepEqual(firstKeys(), before);
        // Search by customer id: every row shown contains it, and the box keeps its value.
        const box = $("[data-audience-search]");
        box.value = "1077";
        box.dispatchEvent(new w.Event("input", { bubbles: true }));
        const found = firstKeys();
        assert.ok(found.length > 0 && found.every((k) => k.includes("1077")), found.join(","));
        assert.equal($("[data-audience-search]"), box, "the search box is not redrawn while typing");
        box.value = "no such customer";
        box.dispatchEvent(new w.Event("input", { bubbles: true }));
        assert.ok($("[data-audience-none]"));
        box.value = "";
        box.dispatchEvent(new w.Event("input", { bubbles: true }));
        assert.equal(firstKeys().length, 25);
        // Why these customers: three reasons, each with how many customers it is the strongest reason for.
        const reasons = $$("[data-why] [data-reason]");
        assert.equal(reasons.length, 3);
        for (const li of reasons)
            assert.match(li.textContent, /^Their .+ for [\d,]+ of 12,862 customers \(\d+\.\d%\)$/);
    });

    test(`${name}: Results shows the Proof Pack's headline, the evidence in words and the full report one click away`, async () => {
        await visit(`#/campaigns/${id}/results`);
        assert.equal(text("[data-headline]"), proof(id).headline);
        assert.equal(text("[data-evidence] .chip"), "Proven");
        assert.ok(!/Causal/.test(text("[data-results-measured]")), "the technical label is not shown");
        assert.equal(primaries().length, 1);
        assert.equal(primaries()[0].getAttribute("href"), `#/pilot/proof/${id}`);
        assert.equal($("[data-results-form]"), null, "a measured campaign offers no upload");
    });

    test(`${name}: Technical details is a closed section linking today's pages and Plan J's campaign page`, async () => {
        await visit(`#/campaigns/${id}/technical`);
        const section = $("[data-technical-tab]");
        assert.ok(section.hasAttribute("data-technical"));
        assert.equal(section.querySelector("details").open, false);
        const hrefs = [...section.querySelectorAll("a")].map((a) => a.getAttribute("href"));
        assert.deepEqual(hrefs, [
            `#/campaigns/${id}/plan`,
            `#/uc/${UC}/data/${SCORE_UPLIFT}`,
            `#/uc/${UC}/model/${SCORE_UPLIFT}`,
            `#/uc/${UC}/output/${SCORE_UPLIFT}`,
        ]);
        assert.equal(primaries().length, 0);
    });
}

test("Plan J's campaign page: a bare id still draws it in the classic layout, and .../plan in both", async () => {
    await visit(`#/campaigns/${C_CONVERSION}`);
    assert.equal(w.location.hash, `#/campaigns/${C_CONVERSION}`);
    assert.ok($(`[data-campaign-page="${C_CONVERSION}"]`), "Plan J's page");
    assert.match($("#app").textContent, /Plan the test/);
    // Its result card points to the one place to add results.
    assert.equal(
        $("[data-campaign-result] [data-campaign-results-line] a").getAttribute("href"),
        `#/campaigns/${C_CONVERSION}/results`,
    );
    await visit(`#/campaigns/${C_CONVERSION}/plan`);
    assert.ok($(`[data-campaign-page="${C_CONVERSION}"]`));
});

test("in the campaigns layout a bare campaign id opens the new page's Overview", async () => {
    campaigns.setLayoutForTests(true);
    try {
        await visit(`#/campaigns/${C_SPEND}`);
        assert.equal(w.location.hash, `#/campaigns/${C_SPEND}/overview`);
        assert.ok($(`[data-campaign-view="${C_SPEND}"]`));
        await visit(`#/campaigns/${C_SPEND}/plan`);
        assert.ok($(`[data-campaign-page="${C_SPEND}"]`), "Plan J's page is still reachable");
    } finally {
        campaigns.setLayoutForTests(false);
    }
});

test("the older places that add results point to the campaign's Results tab when the list is a campaign", async () => {
    // The uplift scoring run's list became both campaigns; the newest one is linked.
    await visit(`#/uc/${UC}/run/${SCORE_UPLIFT}`);
    const newest = recorded[`GET /campaigns?run_id=${SCORE_UPLIFT}`].body.campaigns[0].campaign_id;
    const line = $("[data-measure-panel] [data-campaign-results-line] a");
    assert.ok(line, "the Measure panel draws the line");
    assert.equal(line.textContent, "Add results from the campaign page ›");
    assert.equal(line.getAttribute("href"), `#/campaigns/${newest}/results`);
    // The risk run's list is no campaign: no line, rather than a line to nowhere.
    for (const hash of [
        `#/uc/${UC}/run/${SCORE_RISK}`,
        `#/campaign/${UC}/${SCORE_RISK}`,
        `#/monitoring/runs/${SCORE_RISK}`,
    ]) {
        await visit(hash);
        assert.ok($(`[data-campaign-results-for="${SCORE_RISK}"]`), `${hash} asks`);
        assert.equal($("[data-campaign-results-line]"), null, hash);
    }
});

test("+ New campaign opens M177's wizard when it is on this build, and says what to do when it is not", async () => {
    await visit("#/campaigns/new");
    const wizard = new URL("../../../../../ui/modules/campaigns/wizard.js", import.meta.url);
    if (fs.existsSync(wizard)) {
        assert.equal($("[data-new-campaign-pending]"), null);
    } else {
        assert.ok($("[data-new-campaign-pending]"));
        assert.equal(primaries().length, 1);
        assert.equal(primaries()[0].getAttribute("href"), "#/");
    }
});
