/* Plan M M176 (DEC-1606): the campaign page in each state, adding results through its Results tab, and "Refresh the
   list", against a fake API that answers with what the real app answered (`tests/integration/test_campaigns_page_ui.py`
   writes it to $M176_FIXTURES): one Phase 1 scoring run and four campaigns made from its list - a Draft (no plan),
   a Live one (plan read on 15 Jan 2027), one whose results date has passed (plan read on 15 Sep 2026) and one sent
   yesterday, whose results are not all in.

   The requests the Results tab sends are pinned to the ones the Python side sent to the real app (`expected.json`),
   where they gave a report whose every figure equals today's routes. "Refresh the list"'s two answers (the cost
   question, then the new run) are this file's own: the request it sends is what is checked. */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { $, $$, installPage, settle, until } from "../harness.mjs";
import { pageScripts } from "../simple/fake.mjs";

const FIXTURES = process.env.M176_FIXTURES;
const fixture = (name) => {
    if (!FIXTURES)
        throw new Error("M176_FIXTURES is not set: run these through tests/integration/test_campaigns_page_ui.py");
    return JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), "utf8"));
};
const ids = fixture("ids");
const expected = fixture("expected");
const RUN = ids.run;

const world = {
    views: {
        [ids.draft]: fixture("view_draft"),
        [ids.live]: fixture("view_live"),
        [ids.due]: fixture("view_due"),
        [ids.fresh]: fixture("view_fresh"),
    },
    writes: [],
    onRun: null,
};
const answered = (a) => ({ status: a.status, body: a.body });
const nameOf = (id) => Object.keys(ids).find((k) => ids[k] === id);

const server = (request) => {
    const { method, path: p, query } = request;
    const ok = (body) => ({ status: 200, body });
    if (method === "GET" && p === "/industries") return ok(fixture("industries"));
    if (method === "GET" && p.startsWith("/use-cases/")) return ok(fixture("use_case"));
    if (method === "GET" && p === "/models") return ok(fixture("models"));
    if (method === "GET" && p === "/runs") return ok(fixture("runs"));
    if (method === "GET" && p === "/datasets") return ok(fixture("datasets"));
    if (method === "GET" && p === "/auth/me") return ok(fixture("me_off"));
    if (method === "GET" && p === "/pilot/demo") return ok(fixture("demo"));
    if (method === "GET" && p === "/pilot/help") return ok(fixture("help"));
    if (method === "GET" && p === "/clients") return ok(fixture("clients"));
    if (method === "GET" && p === "/healthz") return ok(fixture("healthz"));
    if (method === "GET" && p === `/runs/${RUN}`) return ok(fixture("run"));
    if (method === "GET" && p === `/runs/${RUN}/artefacts/treat_list_summary.json`) return answered(fixture("summary"));
    if (method === "GET" && p === "/campaigns") return ok(fixture("campaigns"));
    if (method === "GET" && p === `/pilot/proof/${ids.due}` && query.format === "json")
        return answered(fixture("proof_due"));
    if (method === "POST" && p === "/runs") {
        world.writes.push({ method, path: p, body: request.body });
        return world.onRun(request.body);
    }
    const match = p.match(/^\/campaigns\/([^/]+)(\/outcomes|\/measure)?$/);
    if (match) {
        const id = decodeURIComponent(match[1]);
        const name = nameOf(id);
        if (method === "GET" && !match[2] && world.views[id]) return ok(world.views[id]);
        if (method === "POST" && match[2]) {
            world.writes.push({ method, path: p, body: request.body });
            const answer = answered(fixture(`${match[2].slice(1)}_${name}`));
            // What the record holds after each write is what the real app answered after it.
            if (match[2] === "/outcomes" && answer.status === 200) world.views[id] = answer.body;
            if (match[2] === "/measure" && answer.status === 200) world.views[id] = fixture(`view_${name}_after`);
            return answer;
        }
    }
    return null;
};

const { w } = installPage(server, { hash: "#/__start" });
for (const script of pageScripts()) await import(`../../../../../ui/${script}`);
// The upload is multipart: record the form's fields as the harness records a JSON body.
const inner = w.fetch;
w.fetch = (input, init = {}) => {
    if (init.body instanceof FormData) {
        // `ui/api.js` builds the upload with the global FormData (node's here, as the browser's in a page).
        const form = {};
        for (const [key, value] of init.body.entries()) form[key] = typeof value === "string" ? value : value.name;
        const url = new URL(typeof input === "string" ? input : input.url, w.location.href);
        if (url.pathname === "/uploads") {
            world.writes.push({ method: "POST", path: "/uploads", form });
            return Promise.resolve(
                new Response(JSON.stringify(fixture(`upload_${world.uploading}`).body), {
                    status: 201,
                    headers: { "Content-Type": "application/json" },
                }),
            );
        }
    }
    return inner(input, init);
};
const views = await import("../../../../../ui/modules/campaigns/views.js");

async function open(hash) {
    w.location.hash = "#/__between";
    await settle(4);
    w.location.hash = hash;
    await settle(4);
    await until(() => $("[data-campaign-view], [data-campaigns-page]"), 3000, hash);
    await settle(4);
}

const primaries = () => $$("#app .btn.primary");
const text = (sel) => ($(sel) ? $(sel).textContent.replace(/\s+/g, " ").trim() : null);

function chooseFile(name = "october_results.csv") {
    const input = $("[data-results-file-input]");
    const csv = fs.readFileSync(path.join(FIXTURES, name), "utf8");
    Object.defineProperty(input, "files", { value: [new File([csv], name, { type: "text/csv" })], configurable: true });
}

const submit = () => $("[data-results-form]").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));

test("the list shows each campaign's state with the one rule", async () => {
    await open("#/campaigns");
    const byName = Object.fromEntries(
        $$("[data-campaigns-list] tbody tr").map((tr) => [
            tr.querySelector(".cp-sub").textContent,
            [
                tr.querySelector("a[data-campaign-link]").textContent,
                tr.querySelector("[data-status]").textContent,
                tr.querySelectorAll("td")[4].textContent,
            ],
        ]),
    );
    assert.deepEqual(byName["Win-back, July list"], ["Win back lapsed customers · May 2026", "Draft", "—"]);
    assert.deepEqual(byName["Win-back, August list"], [
        "Win back lapsed customers · May 2026",
        "Live",
        "Results due 15 Jan 2027",
    ]);
    assert.deepEqual(byName["Win-back, May list"], [
        "Win back lapsed customers · May 2026",
        "Waiting for results",
        "Results due 15 Sep 2026",
    ]);
    assert.equal(byName["Win-back, this week"][1], "Draft");
});

test("each state has its own next step and one primary button", async () => {
    await open(`#/campaigns/${ids.live}/overview`);
    assert.equal(text("[data-next-step]"), "Next step: add results on or after 15 Jan 2027.");
    assert.equal(primaries().length, 1);
    assert.equal(primaries()[0].textContent, "Download contact list");
    assert.match(primaries()[0].getAttribute("href"), new RegExp(`/runs/${RUN}/treat_list\\.csv$`));

    await open(`#/campaigns/${ids.draft}/overview`);
    assert.match(text("[data-next-step]"), /fix how the result will be judged/);
    assert.equal(primaries().length, 1);
    assert.equal(primaries()[0].getAttribute("href"), `#/campaigns/${ids.draft}/plan`);

    await open(`#/campaigns/${ids.due}/overview`);
    assert.equal(text("[data-next-step]"), "Next step: add results. They were due on 15 Sep 2026.");
    assert.equal(primaries().length, 1);
    assert.equal(primaries()[0].textContent, "Add results");
    assert.equal(primaries()[0].getAttribute("href"), `#/campaigns/${ids.due}/results`);
});

test("adding results on the Results tab sends exactly what gave today's report, then shows the result", async () => {
    await open(`#/campaigns/${ids.due}/results`);
    assert.equal(primaries().length, 1);
    assert.equal(primaries()[0].textContent, "Add results");
    // Without a file nothing is sent.
    world.writes.length = 0;
    submit();
    await settle(4);
    assert.equal(text("[data-results-error]").includes("Choose the results file first."), true);
    assert.deepEqual(world.writes, []);
    // With the file: the upload, the outcomes as the plan registered them, the measurement.
    world.uploading = "due";
    chooseFile();
    submit();
    await until(() => $("[data-results-measured]"), 3000, "the measured result");
    const uploadId = fixture("upload_due").body.upload_id;
    assert.deepEqual(world.writes, [
        { method: "POST", path: "/uploads", form: expected.upload },
        { method: "POST", path: `/campaigns/${ids.due}/outcomes`, body: { ...expected.outcomes, upload_id: uploadId } },
        { method: "POST", path: `/campaigns/${ids.due}/measure`, body: expected.measure },
    ]);
    const proof = fixture("proof_due");
    assert.equal(proof.status, 200);
    assert.equal(text("[data-headline]"), proof.body.headline);
    assert.equal(text("[data-evidence] .chip"), "Proven");
    assert.equal(primaries().length, 1);
    assert.equal(primaries()[0].getAttribute("href"), `#/pilot/proof/${ids.due}`);
    await open(`#/campaigns/${ids.due}/overview`);
    assert.equal(text("[data-overview] [data-status]"), "Measured");
});

test("results not all in yet: the day to come back, never a number, and the file stays in", async () => {
    await open(`#/campaigns/${ids.fresh}/results`);
    world.writes.length = 0;
    world.uploading = "fresh";
    chooseFile();
    submit();
    await until(() => $("[data-results-notice]"), 3000, "the notice");
    const refusal = fixture("measure_fresh");
    assert.equal(text("[data-results-notice]"), refusal.body.detail.message);
    assert.equal($("[data-results-measured]"), null);
    assert.match(text("[data-results-file]"), /A results file is in: october_results\.csv/);
    assert.deepEqual(
        world.writes.map((c) => `${c.method} ${c.path}`),
        ["POST /uploads", `POST /campaigns/${ids.fresh}/outcomes`, `POST /campaigns/${ids.fresh}/measure`],
    );
    // "Read the result" asks again with the file already in: one more measurement, no upload.
    world.writes.length = 0;
    $("[data-results-read]").click();
    await settle(8);
    assert.deepEqual(
        world.writes.map((c) => `${c.method} ${c.path}`),
        [`POST /campaigns/${ids.fresh}/measure`],
    );
    assert.equal(text("[data-results-notice]"), refusal.body.detail.message);
    await open(`#/campaigns/${ids.fresh}/overview`);
    assert.equal(text("[data-overview] [data-status]"), "Waiting for results");
    assert.equal(text("[data-next-step]"), "The results file is in. Read the result on the Results tab.");
});

test("Refresh the list asks first, says what the cost check said, and starts a scoring of the same customers", async () => {
    const run = fixture("run").run;
    const answers = [
        {
            status: 409,
            body: {
                detail: {
                    code: "RUN_COST_NEEDS_CONFIRMATION",
                    message: "This run could cost more than the cap. Start it anyway?",
                    path: null,
                },
            },
        },
        { status: 202, body: { run_id: "r_20261011_99999999" } },
    ];
    world.onRun = () => answers.shift();
    await open(`#/campaigns/${ids.live}/overview`);
    world.writes.length = 0;
    $("[data-refresh-ask]").click();
    await settle(2);
    assert.ok($("[data-refresh-confirm]"), "a confirmation first");
    assert.deepEqual(world.writes, [], "nothing is sent before the person says yes");
    $("[data-refresh-no]").click();
    await settle(2);
    assert.equal($("[data-refresh-confirm]"), null);
    $("[data-refresh-ask]").click();
    await settle(2);
    $("[data-refresh-yes]").click();
    await until(() => $("[data-refresh-cost]"), 3000, "the cost question");
    assert.equal(text("[data-refresh-cost]"), "This run could cost more than the cap. Start it anyway?");
    assert.equal(text("[data-refresh-yes]"), "Start it anyway");
    $("[data-refresh-yes]").click();
    await until(() => $("[data-refresh-started]"), 3000, "the started notice");
    const body = {
        use_case: run.use_case_id,
        mode: "score",
        primary_key: run.primary_key,
        overrides: run.overrides || {},
        upload_id: run.upload_id,
    };
    assert.deepEqual(world.writes, [
        { method: "POST", path: "/runs", body },
        { method: "POST", path: "/runs", body: { ...body, confirm_cost: true } },
    ]);
    assert.equal($("[data-refresh-started] a").getAttribute("href"), `#/uc/${run.use_case_id}/run/r_20261011_99999999`);
});

test("a person who may not add results is told so, and sees no upload", () => {
    const view = fixture("view_due");
    const status = { key: "waiting", label: "Waiting for results", tone: "warn" };
    const html = views.campaignPageHtml({
        campaign: view.campaign,
        view,
        plan: view.plan,
        status,
        goal: "Win back lapsed customers",
        tab: "results",
        results: {},
        refresh: {},
        canAdd: false,
        canRefresh: false,
    });
    const holder = document.createElement("div");
    holder.innerHTML = html;
    assert.ok(holder.querySelector("[data-results-refused]"));
    assert.equal(holder.querySelector("[data-results-form]"), null);
    assert.equal(holder.querySelectorAll(".btn.primary").length, 0);
});
