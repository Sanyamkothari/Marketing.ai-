/* Plan M M176 (DEC-1606): the pure rules of the Campaigns list and the campaign page, in node with no page.

   - the ONE status-chip rule (Draft, Live, Waiting for results, Measured) on every case it distinguishes, and on the
     cases it shares with M175's Overview (tests/fixtures/campaigns/status_cases.json);
   - the Overview's value range, found by the Proof Pack's net value label only;
   - the line the older places draw, worded for what the Results tab holds;
   - the short name (goal and month, at most 40 characters) and the next step;
   - the evidence label in words;
   - the contact list read from the CSV a sending tool is given: who is on it, search by customer id, 25 rows a page,
     and "Why these customers?";
   - the bodies of the two writes the page sends (adding results, refreshing the list). */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import {
    STATUS,
    campaignStatus,
    clip,
    evidenceOf,
    goalName,
    GOAL_NAMES,
    monthOf,
    nextStep,
    resultsDue,
    runElsewhere,
    shortName,
} from "../../../../../ui/modules/campaigns/status.js";
import {
    ContactListReader,
    PAGE_SIZE,
    contactList,
    pageOf,
    parseCsv,
    reasonTopic,
    search,
    topReasons,
} from "../../../../../ui/modules/campaigns/audience.js";
import { outcomesPayload, refreshRequest } from "../../../../../ui/modules/campaigns/requests.js";
import { campaignLineHtml } from "../../../../../ui/modules/campaigns/links.js";
import {
    LIST_CAP,
    campaignPageHtml,
    campaignsListHtml,
    netValueRange,
} from "../../../../../ui/modules/campaigns/views.js";
import { GOAL_NAMES as LABEL_GOAL_NAMES } from "../../../../../ui/labels.js";

const base = {
    campaign_id: "c_20261001_aaaaaaaa",
    kind: "scored",
    name: "Win-back, October list",
    use_case_id: "win-back-campaign",
    run_ids: ["r_20261001_00000001"],
    treatment_start: "2026-10-01T09:00:00Z",
    outcome_window_days: 30,
    status: "live",
    outcomes: null,
    test_plan_hash: null,
    measured_at: null,
    counts: { intended_treated: 900, intended_holdout: 100 },
};
const plan = { analysis_date: "2026-11-12", outcome_column: "reactivated", positive_label: "1" };
const withPlan = { ...base, test_plan_hash: "abc" };

test("the status rule: one chip per state, from the record, its plan and its report", () => {
    // Draft: our own campaign, no plan, no results file, no result, its results date still to come.
    assert.equal(campaignStatus({ campaign: base, today: "2026-10-05" }), STATUS.draft);
    // ... but not for ever: once every customer's outcome window has closed, it waits for results.
    assert.equal(campaignStatus({ campaign: base, today: "2026-10-31" }), STATUS.waiting);
    // Live: the plan is fixed and its analysis date has not come.
    assert.equal(campaignStatus({ campaign: withPlan, plan, today: "2026-10-05" }), STATUS.live);
    assert.equal(campaignStatus({ campaign: withPlan, plan, today: "2026-11-11" }), STATUS.live);
    // Waiting for results: the analysis date has come.
    assert.equal(campaignStatus({ campaign: withPlan, plan, today: "2026-11-12" }), STATUS.waiting);
    // A results file, or an early look, before that date does not change it: the date decides (M175's rule).
    const filed = { ...withPlan, outcomes: { file_name: "oct.csv" } };
    assert.equal(campaignStatus({ campaign: filed, plan, today: "2026-10-05" }), STATUS.live);
    assert.equal(
        campaignStatus({ campaign: withPlan, plan, report: { early_look: true }, today: "2026-10-05" }),
        STATUS.live,
    );
    // A results file makes a campaign with no plan Live, not Draft.
    assert.equal(
        campaignStatus({ campaign: { ...base, outcomes: { file_name: "x.csv" } }, today: "2026-10-02" }),
        STATUS.live,
    );
    // Measured: the stored final result, whatever else is true.
    assert.equal(campaignStatus({ campaign: { ...base, status: "measured" }, today: "2026-10-05" }), STATUS.measured);
    // Without a plan, a campaign that is not ours is never a Draft; its results date is the end of its window.
    const elsewhere = { ...base, kind: "external" };
    assert.equal(campaignStatus({ campaign: elsewhere, today: "2026-10-05" }), STATUS.live);
    assert.equal(campaignStatus({ campaign: elsewhere, today: "2026-10-31" }), STATUS.waiting);
    // A plan known only by its hash (the list row before its page answered) is not a Draft either.
    assert.equal(campaignStatus({ campaign: withPlan, today: "2026-10-05" }), STATUS.live);
    // The four labels, as the plan names them.
    assert.deepEqual(
        Object.values(STATUS).map((s) => s.label),
        ["Draft", "Live", "Waiting for results", "Measured"],
    );
});

test("the status rule gives every case it shares with M175's Overview (status_cases.json)", () => {
    const shared = JSON.parse(
        fs.readFileSync(new URL("../../../../fixtures/campaigns/status_cases.json", import.meta.url), "utf8"),
    );
    const KEY = { draft: "draft", live: "live", waiting_for_results: "waiting", measured: "measured" };
    assert.ok(shared.cases.length >= 10);
    for (const c of shared.cases) {
        const got = campaignStatus({ campaign: c.campaign, plan: c.plan, report: c.report, today: c.today });
        assert.equal(got.key, KEY[c.status], c.name);
        assert.equal(resultsDue(c.campaign, c.plan), c.results_date, c.name);
    }
});

test("the results date is the plan's analysis date, else the end of the outcome window, else none", () => {
    assert.equal(resultsDue(withPlan, plan), "2026-11-12");
    assert.equal(resultsDue(base, null), "2026-10-31");
    assert.equal(resultsDue({ ...base, outcome_window_days: null }, null), null);
    assert.equal(resultsDue({ ...base, outcome_window_days: 0 }, null), "2026-10-01");
});

test("a short name is the goal and the month, at most 40 characters; the long name is kept as the subtitle", () => {
    // Plan M final integration: M172's one rule (ui/format.js shortCampaignName, ui/labels.js goalShortName), the
    // same name the Results page and the Overview give a campaign.
    assert.equal(shortName(base), "Win back lapsed customers — Oct 2026");
    const hill = { ...base, use_case_id: "hillstrom-email" };
    const name = shortName(hill);
    assert.ok(name.length <= 40, name);
    assert.equal(name, "Best e-mail for each customer — Oct 2026", "the goal's short form keeps it whole");
    assert.equal(shortName({ ...base, use_case_id: "retail-win-back" }, "Retail Win-back"), "Retail Win-back — Oct 2026");
    assert.equal(GOAL_NAMES, LABEL_GOAL_NAMES, "one table of goal names: ui/labels.js");
    assert.equal(monthOf("2026-01-31T23:30:00Z"), "Jan 2026");
    assert.equal(goalName("retail-win-back", "Retail Win-back"), "Retail Win-back");
    assert.equal(goalName(null), "Campaign");
    // A campaign another tool sent keeps its own name, shortened, and is marked.
    const external = {
        ...base,
        kind: "external",
        use_case_id: null,
        name: "Diwali e-mail blast measured by the CRM team in 2026",
    };
    assert.ok(runElsewhere(external));
    assert.ok(shortName(external).length <= 40);
    assert.match(shortName(external), /…$/);
    assert.equal(clip("short", 40), "short");
});

test("the next step names the date to come back, in the page's own date format", () => {
    const day = (d) => `<${d}>`;
    assert.match(nextStep({ status: STATUS.draft, campaign: base }, day), /fix how the result will be judged/);
    assert.equal(
        nextStep({ status: STATUS.live, campaign: withPlan, plan }, day),
        "Next step: add results on or after <2026-11-12>.",
    );
    assert.match(nextStep({ status: STATUS.waiting, campaign: withPlan, plan }, day), /They were due on <2026-11-12>/);
    // Before the results date, a file that is in and an early look are said, with the date that decides.
    const filed = { ...withPlan, outcomes: { file_name: "oct.csv" } };
    assert.equal(
        nextStep({ status: STATUS.live, campaign: filed, plan }, day),
        "The results file is in. The result can be read on or after <2026-11-12>.",
    );
    assert.match(
        nextStep({ status: STATUS.live, campaign: filed, plan, report: { early_look: true } }, day),
        /^An early look is in\. The result is final on <2026-11-12>/,
    );
    assert.equal(
        nextStep({ status: STATUS.waiting, campaign: filed, plan }, day),
        "The results file is in. Read the result on the Results tab.",
    );
    // A campaign with no plan whose window closed: its due date is the window's end.
    assert.equal(
        nextStep({ status: STATUS.waiting, campaign: base }, day),
        "Next step: add results. They were due on <2026-10-31>.",
    );
    assert.match(
        nextStep({ status: STATUS.measured, campaign: { ...base, measured_at: "2026-11-13T10:00:00Z" } }, day),
        /<2026-11-13>/,
    );
});

// --- the value range and the older places' line ---------------------------------------------------------

const pack = (claim, lines) => ({ claim, sections: [{ key: "net_value", status: "measured", lines }] });
const money = (text) => ({ text });
// The range is the two server figures (drawn by ui/format.js on the page); the test reads their texts.
const texts = (range) => range && { low: range.low.text, high: range.high.text };

test("the value range is the Proof Pack's net value line, found by its label, never by its position", () => {
    const before = [
        { label: "Value of what the campaign changed", low: money("₹1"), high: money("₹2") },
        { label: "Cost of the contacts", low: null, high: null },
        { label: "Cost of the offers", low: null, high: null },
    ];
    const net = { label: "Net value", low: money("₹3"), high: money("₹4") };
    assert.deepEqual(texts(netValueRange(pack("proven", [...before, net]))), { low: "₹3", high: "₹4" });
    // A Pack whose claim is stated_random words the line conditionally (engine/pilot/proof.py `_conditional`).
    const conditional = { ...net, label: "If the groups were random as you said: net value" };
    const lastWithout = { label: "Cost of one offer taken, as entered", low: null, high: null };
    assert.deepEqual(texts(netValueRange(pack("stated_random", [...before, conditional, lastWithout]))), {
        low: "₹3",
        high: "₹4",
    });
    // A later line with a range of its own is never shown as the net value.
    const later = { label: "Value per customer contacted", low: money("₹7"), high: money("₹8") };
    assert.deepEqual(texts(netValueRange(pack("proven", [...before, net, later]))), { low: "₹3", high: "₹4" });
    // No net value line: no range, rather than another line's.
    assert.equal(netValueRange(pack("proven", [...before, later])), null);
    assert.equal(netValueRange(pack("proven", [])), null);
    assert.equal(netValueRange(null), null);
});

test("the older places' line says what the campaign's Results tab holds", () => {
    const line = (c) => {
        const html = campaignLineHtml(c);
        const m = /href="([^"]+)">([^<]+)</.exec(html);
        return m ? [m[1], m[2]] : null;
    };
    assert.deepEqual(line(base), ["#/campaigns/c_20261001_aaaaaaaa/results", "Add results from the campaign page ›"]);
    assert.deepEqual(line({ ...base, status: "measured" }), [
        "#/campaigns/c_20261001_aaaaaaaa/results",
        "See the result on the campaign page ›",
    ]);
    // Another tool's campaign has no results to add on its Results tab: no line.
    assert.equal(line({ ...base, kind: "external", use_case_id: null }), null);
    assert.equal(campaignLineHtml(null), "");
});

test("a list as long as GET /campaigns answers says it shows the newest campaigns, not every one", () => {
    const entry = (i) => ({
        campaign: { ...base, campaign_id: `c_20261001_${String(i).padStart(8, "0")}` },
        view: null,
        proof: null,
        goal: "Win back lapsed customers",
        status: STATUS.live,
    });
    const few = campaignsListHtml({ entries: [entry(1), entry(2)] });
    assert.match(few, /Every campaign: where it stands/);
    assert.match(few, /All campaigns/);
    assert.ok(!few.includes("data-campaigns-capped"));
    assert.equal(LIST_CAP, 100, "api/routes/campaigns.py CAMPAIGNS_SHOWN");
    const full = campaignsListHtml({ entries: Array.from({ length: LIST_CAP }, (_, i) => entry(i)) });
    assert.match(full, /The newest 100 campaigns: where each stands/);
    assert.match(full, /<h3>The newest 100 campaigns/);
    assert.ok(full.includes("data-campaigns-capped"));
    assert.ok(!/Every campaign|All campaigns/.test(full));
});

test("the results box takes the two file types its sentence names", () => {
    const html = campaignPageHtml({
        campaign: withPlan,
        view: { campaign: withPlan, plan },
        plan,
        status: STATUS.waiting,
        goal: "Win back lapsed customers",
        tab: "results",
        results: {},
        refresh: {},
        canAdd: true,
        canRefresh: false,
    });
    assert.match(html, /Upload a CSV or Parquet file/);
    assert.match(html, /accept="\.csv,\.parquet"/);
});

test("the evidence label is said in words, never the technical label", () => {
    assert.equal(evidenceOf("proven").label, "Proven");
    assert.equal(evidenceOf("stated_random").label, "Estimated");
    assert.equal(evidenceOf("descriptive").label, "Not yet proven");
    assert.equal(evidenceOf("anything else").label, "Not yet proven");
});

// --- the contact list ---------------------------------------------------------------------------------------

const HEAD =
    "customer_id,use_case,model_version,band,treat,holdout,explore,suppression_reason,offer,channel,contactable_channels,runner_up_offer,runner_up_net_value,offer_reason,control_group,net_value,expected_gross_value,reason_1,reason_2,reason_3";
function csv(rows) {
    return `${HEAD}\n${rows.join("\n")}\n`;
}
const row = (id, treat, band, reason) =>
    `${id},win-back-campaign,m_1,${band},${treat},0,0,,Discount 10%,email,email,,,,0,,,${reason},,`;

test("parsing follows RFC 4180: quoted commas, doubled quotes, CRLF", () => {
    assert.deepEqual(parseCsv('a,b\r\n"x, y","say ""hi"""\r\n'), [
        ["a", "b"],
        ["x, y", 'say "hi"'],
    ]);
});

test("the contact list is the rows marked to contact, in file order, with priority, offer, channel and top reason", () => {
    const text = csv([
        row("C1", 1, "High", "recency ↑ (2)"),
        row("C2", 0, "High", "recency ↑ (3)"),
        row("C3", 1, "Medium", '"Monthly spend of 1,200 pushes the score up"'),
    ]);
    const list = contactList(text, "customer_id");
    assert.equal(list.total, 3);
    assert.equal(list.grouping, "band");
    assert.deepEqual(
        list.rows.map((r) => [r.key, r.priority, r.offer, r.channel, r.reason]),
        [
            ["C1", "High", "Discount 10%", "email", "recency ↑ (2)"],
            ["C3", "Medium", "Discount 10%", "email", "Monthly spend of 1,200 pushes the score up"],
        ],
    );
    // A campaign-effect list has groups, not priorities: said in words, and called a group.
    const effect = contactList(
        "customer_id,segment,treat,reason_1\nA,persuadable,1,x\nB,sleeping_dog,1,y\n",
        "customer_id",
    );
    assert.equal(effect.grouping, "segment");
    assert.deepEqual(
        effect.rows.map((r) => r.priority),
        ["Responds to contact", "Put off by contact"],
    );
    // Several key columns are joined.
    const two = contactList("store,customer_id,treat\nS1,7,1\n", ["store", "customer_id"]);
    assert.equal(two.rows[0].key, "S1 · 7");
});

test("the list is read in one pass, in pieces cut anywhere, exactly as the whole text is", () => {
    const text =
        "\ufeffcustomer_id,treat,band,offer,channel,reason_1,reason_2,extra\r\n" +
        'C1,1,High,"Discount, 10%",email,"say ""hi""",,x\r\n' +
        "\r\n" +
        'C2,0,Low,,,,,"multi\nline"\n' +
        'C3,1,Medium,Plain,sms,"recency ↑ (2)","history ↓ (3)",\n' +
        "C4,1,High,,,,";
    const whole = contactList(text, "customer_id");
    assert.equal(whole.total, 4);
    assert.deepEqual(
        whole.rows.map((r) => [r.key, r.offer, r.reason, r.reasons.length]),
        [
            ["C1", "Discount, 10%", 'say "hi"', 1],
            ["C3", "Plain", "recency ↑ (2)", 2],
            ["C4", "", "", 0],
        ],
    );
    for (let size = 1; size <= 7; size += 1) {
        const reader = new ContactListReader("customer_id");
        for (let i = 0; i < text.length; i += size) reader.push(text.slice(i, i + size));
        assert.deepEqual(reader.end(), whole, `pieces of ${size}`);
    }
    // Only the cells the list shows are kept: never the whole row, never the whole file.
    const reader = new ContactListReader("customer_id");
    reader.push(text);
    assert.ok(reader.cells.length <= 8 && !("table" in reader));
});

test("a million-row list is read in linear time, without a table of the file", () => {
    // The Hillstrom demo's 20 columns; four customers in ten to contact, some fields quoted.
    const rows = 1_000_000;
    const pieces = [HEAD];
    for (let i = 0; i < rows; i += 1) {
        pieces.push(
            `${i},hillstrom-email,m_2,High,${i % 10 < 4 ? 1 : 0},,,,Womens E-Mail,email,email,Mens E-Mail,8.92,` +
                `${i % 3 ? "" : '"Every offer, compared"'},0,19.82,,history ↓ (142.44),recency ↑ (10),zip_code = Urban`,
        );
    }
    const text = `${pieces.join("\n")}\n`;
    pieces.length = 0;
    // What this machine takes, now, for the least any reader must do: split every line into its cells, natively.
    let started = performance.now();
    let cells = 0;
    for (const line of text.split("\n")) cells += line.split(",").length;
    const native = performance.now() - started;
    assert.ok(cells > rows * 20);
    started = performance.now();
    const list = contactList(text, "customer_id");
    const elapsed = performance.now() - started;
    assert.equal(list.total, rows);
    assert.equal(list.rows.length, 400_000);
    assert.equal(list.rows[1].key, "1");
    assert.equal(list.rows[0].reason, "history ↓ (142.44)");
    // Measured against that native split, so a busy machine slows both: this reader takes about 1.5 times as long;
    // the first one (a table of every cell of the file, then the rows) took about 5 times as long, and 7 seconds.
    assert.ok(
        elapsed < 3 * native,
        `${Math.round(elapsed)} ms for ${rows} rows; a native split of every line took ${Math.round(native)} ms`,
    );
});

test("search by customer id, then 25 rows at a time", () => {
    const rows = Array.from({ length: 60 }, (_, i) => ({ key: `CUST${String(i + 1).padStart(3, "0")}` }));
    assert.equal(PAGE_SIZE, 25);
    const first = pageOf(rows, 1);
    assert.deepEqual(
        [first.page, first.pages, first.from, first.to, first.total, first.rows.length],
        [1, 3, 1, 25, 60, 25],
    );
    const last = pageOf(rows, 3);
    assert.deepEqual([last.from, last.to, last.rows.length], [51, 60, 10]);
    assert.equal(pageOf(rows, 99).page, 3, "a page past the end is the last page");
    assert.equal(pageOf(rows, 0).page, 1, "a page before the start is the first");
    assert.deepEqual(pageOf([], 1), { rows: [], page: 1, pages: 1, from: 0, to: 0, total: 0 });
    assert.deepEqual(
        search(rows, " cust05 ").map((r) => r.key),
        ["CUST050", "CUST051", "CUST052", "CUST053", "CUST054", "CUST055", "CUST056", "CUST057", "CUST058", "CUST059"],
    );
    assert.equal(search(rows, "").length, 60);
    assert.equal(search(rows, "nobody").length, 0);
});

test("why these customers: the three most common strongest reasons, counted by what they are about", () => {
    assert.deepEqual(reasonTopic("history ↓ (142.44)"), {
        id: "history|↓",
        text: "Their history lowers their priority",
    });
    assert.deepEqual(reasonTopic("zip_code = Urban"), { id: "zip_code=Urban", text: "Their zip code is Urban" });
    assert.equal(
        reasonTopic("Monthly spend of 1,200 pushes the score up").text,
        "Monthly spend of … pushes the score up",
    );
    assert.equal(reasonTopic(""), null);
    const rows = [
        "history ↑ (10)",
        "history ↑ (20)",
        "history ↑ (30)",
        "zip_code = Urban",
        "zip_code = Urban",
        "recency ↓ (4)",
        "Monthly spend of 1,200 pushes the score up",
        "Monthly spend of 300 pushes the score up",
        "",
    ].map((reason) => ({ reason }));
    const top = topReasons(rows);
    assert.deepEqual(
        top.map((r) => [r.text, r.customers]),
        [
            ["Their history raises their priority", 3],
            ["Monthly spend of … pushes the score up", 2],
            ["Their zip code is Urban", 2],
        ],
    );
    assert.equal(top[0].share, 3 / 9);
});

// --- the two writes -------------------------------------------------------------------------------------------

test("adding results sends the upload and the outcome as the plan registered it", () => {
    assert.deepEqual(outcomesPayload("u_1"), { upload_id: "u_1" });
    assert.deepEqual(outcomesPayload("u_1", plan), {
        upload_id: "u_1",
        outcome_column: "reactivated",
        positive_label: "1",
    });
    const amount = { outcome_column: "spend", positive_label: null, covariate_column: "history" };
    assert.deepEqual(outcomesPayload("u_2", amount), {
        upload_id: "u_2",
        outcome_column: "spend",
        covariate_column: "history",
        covariate_date_column: "history_date",
    });
    assert.equal(outcomesPayload("u_2", amount, " measured_on ").covariate_date_column, "measured_on");
    // A plan adjusted by the list's own prediction needs no column from the file.
    assert.deepEqual(outcomesPayload("u_3", { ...amount, covariate_source: "run_score" }), {
        upload_id: "u_3",
        outcome_column: "spend",
    });
});

test("refreshing the list scores the same customers again, with no model named (the current approved one)", () => {
    const run = { use_case_id: "win-back-campaign", primary_key: "customer_id", upload_id: "u_9", overrides: { a: 1 } };
    assert.deepEqual(refreshRequest(run), {
        use_case: "win-back-campaign",
        mode: "score",
        primary_key: "customer_id",
        overrides: { a: 1 },
        upload_id: "u_9",
    });
    const built = { use_case_id: "x", primary_key: ["a", "b"], dataset_id: "d_1", client_id: "c_1", upload_id: null };
    assert.deepEqual(refreshRequest(built, { confirmCost: true }), {
        use_case: "x",
        mode: "score",
        primary_key: ["a", "b"],
        overrides: {},
        dataset_id: "d_1",
        client_id: "c_1",
        confirm_cost: true,
    });
    assert.ok(!("model_version_id" in refreshRequest(run)));
});
