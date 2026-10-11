/* Plan M M176 (DEC-1606): the pure rules of the Campaigns list and the campaign page, in node with no page.

   - the ONE status-chip rule (Draft, Live, Waiting for results, Measured) on every case it distinguishes;
   - the short name (goal and month, at most 40 characters) and the next step;
   - the evidence label in words;
   - the contact list read from the CSV a sending tool is given: who is on it, search by customer id, 25 rows a page,
     and "Why these customers?";
   - the bodies of the two writes the page sends (adding results, refreshing the list). */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
    STATUS,
    campaignStatus,
    clip,
    evidenceOf,
    goalName,
    monthOf,
    nextStep,
    resultsDue,
    runElsewhere,
    shortName,
} from "../../../../../ui/modules/campaigns/status.js";
import {
    PAGE_SIZE,
    contactList,
    pageOf,
    parseCsv,
    reasonTopic,
    search,
    topReasons,
} from "../../../../../ui/modules/campaigns/audience.js";
import { outcomesPayload, refreshRequest } from "../../../../../ui/modules/campaigns/requests.js";

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
    // Draft: our own campaign, no plan, no results file, no result.
    assert.equal(campaignStatus({ campaign: base, today: "2026-10-05" }), STATUS.draft);
    // Live: the plan is fixed and its analysis date has not come.
    assert.equal(campaignStatus({ campaign: withPlan, plan, today: "2026-10-05" }), STATUS.live);
    assert.equal(campaignStatus({ campaign: withPlan, plan, today: "2026-11-11" }), STATUS.live);
    // Waiting for results: the analysis date has come ...
    assert.equal(campaignStatus({ campaign: withPlan, plan, today: "2026-11-12" }), STATUS.waiting);
    // ... or a results file is in, or an early look was read (never a result).
    const filed = { ...withPlan, outcomes: { file_name: "oct.csv" } };
    assert.equal(campaignStatus({ campaign: filed, plan, today: "2026-10-05" }), STATUS.waiting);
    assert.equal(
        campaignStatus({ campaign: withPlan, plan, report: { early_look: true }, today: "2026-10-05" }),
        STATUS.waiting,
    );
    // A results file makes a campaign with no plan Waiting, not Draft.
    assert.equal(
        campaignStatus({ campaign: { ...base, outcomes: { file_name: "x.csv" } }, today: "2026-10-02" }),
        STATUS.waiting,
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

test("the results date is the plan's analysis date, else the end of the outcome window, else none", () => {
    assert.equal(resultsDue(withPlan, plan), "2026-11-12");
    assert.equal(resultsDue(base, null), "2026-10-31");
    assert.equal(resultsDue({ ...base, outcome_window_days: null }, null), null);
    assert.equal(resultsDue({ ...base, outcome_window_days: 0 }, null), "2026-10-01");
});

test("a short name is the goal and the month, at most 40 characters; the long name is kept as the subtitle", () => {
    assert.equal(shortName(base), "Win back lapsed customers · Oct 2026");
    const hill = { ...base, use_case_id: "hillstrom-email" };
    const name = shortName(hill);
    assert.ok(name.length <= 40, name);
    assert.match(name, /^Choose the best e-mail for… · Oct 2026$/);
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
    assert.match(
        nextStep({ status: STATUS.waiting, campaign: withPlan, plan, report: { early_look: true } }, day),
        /final on <2026-11-12>/,
    );
    assert.match(
        nextStep({ status: STATUS.measured, campaign: { ...base, measured_at: "2026-11-13T10:00:00Z" } }, day),
        /<2026-11-13>/,
    );
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
