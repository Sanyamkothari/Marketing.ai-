// Screen 1, Connect: where the customer data comes from, what the Data Doctor did with it, what it is waiting
// for you to answer, how messages go out, and how healthy the lists are. Numbers first (DESIGN.md section 6): each
// card is its title, its numbers and at most one short line; what a number counts, a safety stop, a paused
// channel's reason and what to bring are descriptions on the word they explain. Connecting itself still happens in the
// Data Doctor of the Marketing AI app (v1); this screen says exactly where and what to bring, then shows what the
// API reports. Every number on it comes from the API (or the sample files in mock mode); nothing is made up, and
// a part the server does not report says so in words.

import { card, clear, dash, describe, el, emptyState, errorBox, freshness, int, notice, num, pageHead, pct, pill, present, skeletonKpis, skeletonLines, table, titleInfo, when, withInfo } from "../components.js";

const V1_APP = "/";
const V1_CONNECTIONS = "/#/connections";

export const SOURCE_KIND_WORDS = Object.freeze({
  file: "Uploaded file",
  shop: "Online shop",
  database: "Database",
  simulation: "Simulated sample shop",
  other: "Other source",
});

export const CHANNEL_NAMES = Object.freeze({ email: "Email", whatsapp: "WhatsApp", sms: "SMS", webhook: "Your own tools" });

/** "1 row" / "52 rows". */
const rows = (n) => `${int(n)} ${Number(n) === 1 ? "row" : "rows"}`;

/** Wait for a call and keep its answer or its error, so one failing part never blanks the screen. */
const settle = (promise) =>
  Promise.resolve(promise).then(
    (value) => ({ value, error: null }),
    (error) => ({ value: null, error }),
  );
const NOTHING = Object.freeze({ value: null, error: null });
const call = (api, name, ...args) => (typeof api[name] === "function" ? settle(api[name](...args)) : Promise.resolve(NOTHING));

// ---- 1. how to bring data in ----------------------------------------------------------------------------------
// The two ways, one under the other with a hairline between them: each is its heading and one line, with what to
// bring in the descriptions. Its first button is the screen's one primary, unless the Data Doctor is waiting for an
// answer: then "Answer in the Data Doctor" is, and this one steps back.
function howToConnect({ waiting = false } = {}) {
  const title = titleInfo("Bring your data in", "Autonomy learns from what your customers did: what they bought, when, and how they answered your messages.", { label: "About bringing your data in" });
  return card({
    title,
    level: 2,
    className: "cn-how",
    body: [
      el(
        "ol",
        { class: "cn-ways" },
        el(
          "li",
          {},
          el("h3", { class: "cn-way-title" }, withInfo(el("span", {}, "Upload your files"), "Your column names do not need to match anything. The Data Doctor works out which column is which.")),
          el(
            "p",
            {},
            "CSV, Excel, or a zip of them: your ",
            withInfo(el("span", {}, "customers"), "One row per customer, with their id, email or phone number, and whether they agreed to hear from you."),
            " and your ",
            withInfo(el("span", {}, "orders"), "One row per order, with the order id, the customer id, the date, the amount and what they bought."),
            ".",
          ),
        ),
        el(
          "li",
          {},
          el("h3", { class: "cn-way-title" }, "Or connect your database"),
          el("p", {}, withInfo(el("span", {}, "Read-only access"), "Autonomy never writes to your database."), " to PostgreSQL, Redshift, MySQL, S3 and more."),
        ),
      ),
      el("p", { class: "cn-where" }, el("strong", {}, "Where: "), "for now, both happen in the Data Doctor, inside the Marketing AI app."),
    ],
    actions: [
      el("a", { class: ["btn", waiting ? "btn-secondary" : "btn-primary"], href: V1_APP }, "Open the Data Doctor"),
      el("a", { class: "btn btn-secondary", href: V1_CONNECTIONS }, "Connect a database"),
    ],
  });
}

/** True when every source the API reports is the simulated sample shop. */
const isSimulated = (report) => {
  const sources = report && Array.isArray(report.sources) ? report.sources : [];
  return sources.length > 0 && sources.every((s) => s.kind === "simulation");
};

// ---- 2. what is connected -------------------------------------------------------------------------------------
// The screen's answer: the totals as one divided strip, then where they came from.
const CONNECTED = "What is connected";

function connectedCard(report, reportError, datasets, retry) {
  const title = CONNECTED;
  if (reportError) return card({ title, level: 2, body: [errorBox("Could not load what is connected.", reportError, { retry })] });
  if (!report) return datasetsCard(datasets);
  const sources = Array.isArray(report.sources) ? report.sources : [];
  if (!sources.length && !report.customers) {
    return card({ title, level: 2, body: [emptyState("Nothing connected yet", "Upload your files or connect a database to get started.")] });
  }
  const simulated = isSimulated(report);
  const totals = [
    ["Customers", report.customers],
    ["Orders", report.orders],
    ["Products in those orders", report.products],
  ].filter(([, v]) => v !== null && v !== undefined);
  return card({
    title,
    level: 2,
    meta: freshness(report.as_of, { label: "Checked" }) || null,
    className: "cn-connected",
    body: [
      el(
        "dl",
        { class: "kpis cn-totals", style: { "--kpi-count": String(Math.max(1, totals.length)) } },
        totals.map(([k, v]) => el("div", { class: "kpi cn-total" }, el("dt", { class: "kpi-label" }, k), el("dd", { class: "kpi-value" }, int(v)))),
      ),
      simulated ? notice("This is a simulated sample shop, not real customers.", { role: "note" }) : null,
      sourcesBlock(sources),
    ],
  });
}

const sourceName = (r) => [el("span", { class: "cn-source-kind" }, SOURCE_KIND_WORDS[r.kind] || SOURCE_KIND_WORDS.other), " ", el("span", { class: "cn-source-name" }, `(${dash(r.name)})`)];

/**
 * Where the totals came from. One source is one line (its counts are the totals above, so they are not repeated);
 * several get the table, one row each.
 */
function sourcesBlock(sources) {
  if (!sources.length) return null;
  if (sources.length === 1) {
    const [src] = sources;
    return el("p", { class: "cn-from" }, el("span", { class: "cn-from-label" }, "From "), sourceName(src), present(src.updated_at) ? el("span", { class: "cn-from-when" }, ` \u00b7 latest data ${when(src.updated_at)}`) : null);
  }
  return table({
    caption: "Where it came from",
    columns: [
      { key: "name", label: "Source", format: (v, r) => sourceName(r) },
      { key: "customers", label: "Customers", align: "right", lead: true, format: (v) => int(v) },
      { key: "orders", label: "Orders", align: "right", format: (v) => int(v) },
      { key: "updated_at", label: "Latest data", align: "right", format: (v) => when(v) },
    ],
    rows: sources,
  });
}

/** The older v1 report (GET /datasets), used only when this API has no /v2/data/status. */
function datasetsCard(datasets) {
  const title = CONNECTED;
  if (!Array.isArray(datasets)) {
    return card({
      title,
      level: 2,
      body: [emptyState("Data status is not available here", "This server does not report its data yet. Open the Data Doctor to see and manage your connected data.")],
    });
  }
  if (!datasets.length) {
    return card({ title, level: 2, body: [emptyState("Nothing connected yet", "Upload your files or connect a database to get started.")] });
  }
  return card({
    title,
    level: 2,
    body: [
      table({
        caption: "Datasets the Data Doctor has built, newest first",
        columns: [
          { key: "dataset_id", label: "Dataset" },
          { key: "use_case", label: "Built for" },
          { key: "n_entities", label: "Customers", align: "right", lead: true, format: (v) => int(v) },
          { key: "n_rows", label: "Rows", align: "right", format: (v) => int(v) },
          { key: "built_at", label: "Built", align: "right", format: (v) => when(v) },
        ],
        rows: datasets.slice(0, 10),
      }),
    ],
  });
}

// ---- 3. what the Data Doctor fixed ----------------------------------------------------------------------------
const FIXED = "What the Data Doctor fixed";
const READS =
  "The Data Doctor reads files the way real exports come: a title row above the column names, a \u201cTotal\u201d row at the end, \u201cN/A\u201d in empty cells, dates it cannot read, duplicate or missing ids, and personal details that should be hidden.";

function fixedCard(report) {
  if (!report) return null;
  const title = titleInfo(FIXED, READS, { label: "About what the Data Doctor fixes" });
  if (!report.doctor_connected) {
    return card({
      title,
      level: 2,
      className: "cn-fixed",
      body: [emptyState("Its report is not linked to Autonomy yet", "Open the Data Doctor to see what it found in your files.")],
      actions: [el("a", { class: "btn btn-secondary", href: V1_APP }, "See it in the Data Doctor")],
    });
  }
  const fixes = Array.isArray(report.fixes) ? report.fixes : [];
  const problems = Array.isArray(report.problems) ? report.problems : [];
  return card({
    title,
    level: 2,
    className: "cn-fixed",
    body: [
      fixes.length
        ? el(
            "ul",
            { class: "cn-fix-list" },
            fixes.map((f) => el("li", {}, el("span", { class: "cn-fix-what" }, f.what), el("span", { class: "cn-fix-rows num" }, rows(f.rows)))),
          )
        : el("p", {}, "Nothing needed fixing."),
      el("h3", { class: "cn-sub" }, "What it could not use"),
      problems.length ? el("ul", { class: "cn-problems" }, problems.map((p) => el("li", {}, p))) : el("p", { class: "muted" }, "Nothing: every row could be used."),
    ],
  });
}

// ---- 4. it asks before changing anything ----------------------------------------------------------------------
// The promise is the one line; how it asks (and where its questions wait until its report is linked) is its description.
function questionsCard(report) {
  if (!report) return null;
  const linked = Boolean(report.doctor_connected);
  const questions = linked && Array.isArray(report.questions) ? report.questions : [];
  const how = "When the Data Doctor wants to change your data, it shows you the change and how many rows it touches, then waits for your yes.";
  const promise = el(
    "p",
    { class: "cn-promise" },
    withInfo(el("strong", {}, "It asks before changing anything."), linked ? how : `${how} Its questions show here once its report is linked; until then they wait for you in the Data Doctor.`),
  );
  let body;
  if (!linked || !questions.length) body = [promise];
  else
    body = [
      promise,
      el(
        "ul",
        { class: "cn-questions" },
        questions.map((q) => el("li", { class: "cn-question" }, el("p", { class: "cn-question-text" }, q.text), el("p", { class: "cn-question-meta" }, `Would change ${rows(q.rows_affected)}.`))),
      ),
    ];
  return card({
    title: questions.length ? `Waiting for your answer (${int(questions.length)})` : "Data Doctor questions",
    meta: linked && !questions.length ? "None waiting" : null,
    level: 2,
    className: questions.length ? "cn-asks cn-asks-open" : "cn-asks",
    body,
    actions: questions.length ? [el("a", { class: "btn btn-primary", href: V1_APP }, "Answer in the Data Doctor")] : [],
  });
}

// ---- 5. how messages go out -----------------------------------------------------------------------------------
// A channel is its name and its state; a paused one's reason is the pill's description.
function channelStatus(c) {
  if (c.paused) return withInfo(pill("Paused", "warning"), c.paused_reason || "Paused by a safety check.");
  if (c.ready) return pill("Ready", "good");
  return pill("Not set up yet", "neutral");
}

function channelsCard(channels, error, retry) {
  const title = "How messages go out";
  if (error) return card({ title, level: 2, body: [errorBox("Could not load the channels.", error, { retry })] });
  if (!Array.isArray(channels)) {
    return card({ title, level: 2, body: [emptyState("Channel status is not available here", "This server does not report its channels yet.")] });
  }
  const line = (c) => el("li", { class: "cn-channel" }, el("div", { class: "cn-channel-head" }, el("span", { class: "cn-channel-name" }, CHANNEL_NAMES[c.channel] || c.channel), channelStatus(c)));
  const block = (mode, heading, words) => {
    const list = channels.filter((c) => c.mode === mode);
    if (!list.length) return null;
    return el("div", { class: "cn-channel-group" }, el("h3", { class: "cn-sub" }, withInfo(el("span", {}, heading), words)), el("ul", { class: "cn-channel-list" }, list.map(line)));
  };
  return card({
    title,
    level: 2,
    className: "cn-channels",
    body: [
      el(
        "div",
        { class: "cn-channel-groups" },
        block("direct", "Sent by Autonomy", "Email and WhatsApp: Autonomy sends these itself."),
        block("handoff", "By hand-off", "SMS and your own tools: Autonomy decides who gets what, and hands each message to your tool to send."),
      ),
    ],
  });
}

// ---- 6. sending health ----------------------------------------------------------------------------------------
export const HEALTH_LINES = Object.freeze([
  { key: "delivery_rate", label: "Messages delivered", fmt: (v) => pct(v, 1), words: "Out of the messages sent, how many reached the inbox or phone.", rule: "delivery_rate_below" },
  { key: "complaint_rate", label: "Spam complaints", fmt: (v) => pct(v, 2), words: "People who marked a message as spam, out of the messages delivered.", rule: "complaint_rate" },
  { key: "unsubscribe_rate", label: "Unsubscribes", fmt: (v) => pct(v, 2), words: "People who asked to stop hearing from you, out of the messages delivered.", rule: "unsubscribe_multiple" },
  { key: "block_rate", label: "Blocked", fmt: (v) => pct(v, 2), words: "People who blocked your messages (on WhatsApp, for example), out of the messages delivered.", rule: null },
  {
    key: "contacts_per_customer_week",
    label: "Messages per customer per week",
    fmt: (v) => num(v, 2),
    words: "On average, how many messages each customer got in a week. Lower is kinder.",
    rule: null,
  },
]);

const HEALTH = "Sending health: less spam, healthier lists";

/**
 * Each line is its label and its number. What the number counts, and the goal's safety stop for it, are the label's
 * description; the box sits at the end of the line, so the label and the number still read as one.
 */
function healthCard(health, limits, simulated) {
  if (!health) return null;
  const asOf = when(health.as_of);
  const rules = new Map(((limits && limits.rules) || []).map((r) => [r.metric, r]));
  const selfPauses = (health.alerts || []).filter((a) => a.auto_paused);
  const about = [
    "Every message has a cost: annoyance, unsubscribes, spam complaints. Autonomy weighs that cost before each message, so doing nothing often wins.",
    simulated ? "These are the simulated sample shop's lists, not real customers." : "These are your lists.",
    rules.size ? "The safety stops are the ones for the goal you have open." : "",
  ]
    .filter(Boolean)
    .join(" ");
  return card({
    title: titleInfo(HEALTH, about, { label: "About sending health" }),
    level: 2,
    meta: `As of ${asOf}`,
    className: "cn-health",
    body: [
      el(
        "ul",
        { class: "cn-health-lines" },
        HEALTH_LINES.map((line) => {
          const rule = line.rule ? rules.get(line.rule) : null;
          const label = el("span", { class: "cn-health-label" }, line.label);
          const tip = el("span", {}, line.words, rule && rule.words ? [" ", el("strong", {}, "Safety stop: "), rule.words] : null);
          describe(label, tip);
          return el(
            "li",
            { class: "cn-health-line" },
            el("div", { class: "cn-health-head" }, label, el("span", { class: "cn-health-value num" }, dash(health[line.key], line.fmt))),
            tip,
          );
        }),
      ),
      selfPauses.length
        ? el(
            "div",
            { class: "cn-self-paused", role: "note" },
            el("h3", { class: "cn-sub" }, "It paused itself"),
            el(
              "ul",
              { class: "cn-paused-list" },
              selfPauses.map((a) => el("li", {}, a.message, " ", el("span", { class: "cn-when" }, `(${when(a.at)})`))),
            ),
          )
        : null,
    ],
  });
}

// ---- the screen -----------------------------------------------------------------------------------------------
// One question: what is Autonomy working from, and is anything waiting for me? "What is connected" answers it, at
// the top of the wider column (7 of 12); what the Data Doctor is asking, the channels and how to bring data in sit in
// the narrower one beside it.
const head = () => pageHead({ title: "Connect", sub: "What Autonomy is working from, and how to bring your data in." });

/** While the API answers: the real frame with grey blocks where the numbers go, so nothing jumps when they land. */
function skeletonScreen() {
  return [
    head(),
    el(
      "div",
      { class: "grid cn-grid", "aria-busy": "true" },
      el(
        "div",
        { class: "span-7 stack cn-col" },
        card({ title: CONNECTED, level: 2, body: [skeletonKpis(["Customers", "Orders", "Products in those orders"]), skeletonLines(3)] }),
        card({ title: HEALTH, level: 2, body: [skeletonLines(5)] }),
      ),
      el(
        "div",
        { class: "span-5 stack cn-col" },
        card({ title: "How messages go out", level: 2, body: [skeletonLines(4)] }),
        howToConnect(),
      ),
    ),
  ];
}

/** Put a card in its phone place (`cn-o1`...): on a desktop the two columns keep their own order. */
const at = (node, n) => (node && node.classList.add(`cn-o${n}`), node);
const column = (cls, nodes) => el("div", { class: [cls, "stack cn-col"] }, nodes.filter(Boolean));

export async function render(root, ctx) {
  clear(root, ...skeletonScreen());
  const api = ctx.api;
  const retry = () => render(root, ctx);
  const [report, channels, health, limits] = await Promise.all([
    call(api, "getDataReport"),
    call(api, "listChannels"),
    call(api, "getHealth"),
    ctx.goalId ? call(api, "getHealthLimits", ctx.goalId) : Promise.resolve(NOTHING),
  ]);
  // Only an API without /v2/data/status falls back to the older v1 datasets list.
  const datasets = !report.value && !report.error ? (await call(api, "getDataStatus")).value : null;
  const r = report.value;
  const waiting = Boolean(r && r.doctor_connected && Array.isArray(r.questions) && r.questions.length);
  const hasData = Boolean(r && (Number(r.customers) || (Array.isArray(r.sources) && r.sources.length))) || (Array.isArray(datasets) && datasets.length > 0);
  // Two columns, 7 + 5. The left one answers the screen's question (what it works from, what was fixed, how the
  // lists hold up); the right one holds what waits for you and how to bring more in. With nothing connected yet,
  // "Bring your data in" is the next action, so it goes to the top of its column (and second on a phone).
  const connected = at(connectedCard(r, report.error, datasets, retry), 1);
  const how = at(howToConnect({ waiting }), hasData ? 6 : 2);
  const asks = at(r ? questionsCard(r) : null, hasData ? 2 : 3);
  const fixed = at(r ? fixedCard(r) : null, hasData ? 3 : 4);
  const healthBlock = at(healthCard(health.value, limits.value, isSimulated(r)), hasData ? 4 : 6);
  const channelBlock = at(channelsCard(channels.value, channels.error, retry), 5);
  clear(
    root,
    head(),
    el(
      "div",
      { class: "grid cn-grid" },
      column("span-7", [connected, fixed, healthBlock]),
      column("span-5", hasData ? [asks, channelBlock, how] : [how, asks, channelBlock]),
    ),
    el("p", { class: "cn-next" }, el("a", { href: "#/goal" }, "Next: say what you want to achieve \u2192")),
  );
}
