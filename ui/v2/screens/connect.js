// Screen 1, Connect: where the customer data comes from, what the Data Doctor did with it, what it is waiting
// for you to answer, how messages go out, and how healthy the lists are. Connecting itself still happens in the
// Data Doctor of the Marketing AI app (v1); this screen says exactly where and what to bring, then shows what the
// API reports. Every number on it comes from the API (or the sample files in mock mode); nothing is made up, and
// a part the server does not report says so in words.

import { card, clear, dash, el, emptyState, errorBox, int, loading, num, pct, pill, table, when } from "../components.js";

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
function howToConnect() {
  return card({
    title: "Bring your data in",
    level: 2,
    className: "cn-how",
    body: [
      el("p", {}, "Autonomy learns from what your customers did: what they bought, when, and how they answered your messages. Bring that history in one of two ways."),
      el(
        "ol",
        { class: "cn-ways" },
        el(
          "li",
          {},
          el("h3", { class: "cn-way-title" }, "Upload your files"),
          el("p", {}, "CSV, Excel, or a zip of them. Two files are enough:"),
          el(
            "ul",
            { class: "plain-list cn-files" },
            el("li", {}, el("strong", {}, "Customers: "), "one row per customer, with their id, email or phone number, and whether they agreed to hear from you."),
            el("li", {}, el("strong", {}, "Orders: "), "one row per order, with the order id, the customer id, the date, the amount and what they bought."),
          ),
          el("p", { class: "muted small" }, "Your column names do not need to match anything. The Data Doctor works out which column is which."),
        ),
        el(
          "li",
          {},
          el("h3", { class: "cn-way-title" }, "Or connect your database"),
          el("p", {}, "PostgreSQL, Redshift, MySQL, files on S3 and more. Read-only access is enough: Autonomy never writes to your database."),
        ),
      ),
      el(
        "p",
        { class: "cn-where" },
        el("strong", {}, "Where: "),
        "for now, both happen in the Data Doctor, inside the Marketing AI app. \u201cWhat is connected\u201d shows the data Autonomy is working from right now.",
      ),
    ],
    actions: [
      el("a", { class: "btn btn-primary", href: V1_APP }, "Open the Data Doctor"),
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
function connectedCard(report, reportError, datasets) {
  const title = "What is connected";
  if (reportError) return card({ title, level: 2, body: [errorBox("Could not load what is connected.", reportError)] });
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
    className: "cn-connected",
    body: [
      el("dl", { class: "cn-totals" }, totals.map(([k, v]) => el("div", { class: "cn-total" }, el("dt", {}, k), el("dd", {}, int(v))))),
      simulated ? el("p", { class: "cn-note", role: "note" }, "This is a simulated sample shop, not real customers. Connect your own data to see your business here.") : null,
      sources.length
        ? table({
            caption: "Where it came from",
            columns: [
              {
                key: "name",
                label: "Source",
                format: (v, r) => [el("span", { class: "cn-source-kind" }, SOURCE_KIND_WORDS[r.kind] || SOURCE_KIND_WORDS.other), " ", el("span", { class: "muted small" }, `(${dash(v)})`)],
              },
              { key: "customers", label: "Customers", align: "right", format: (v) => int(v) },
              { key: "orders", label: "Orders", align: "right", format: (v) => int(v) },
              { key: "updated_at", label: "Latest data", format: (v) => when(v) },
            ],
            rows: sources,
          })
        : null,
      el("p", { class: "muted small" }, `Checked ${when(report.as_of)}.`),
    ],
  });
}

/** The older v1 report (GET /datasets), used only when this API has no /v2/data/status. */
function datasetsCard(datasets) {
  const title = "What is connected";
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
          { key: "n_entities", label: "Customers", align: "right", format: (v) => int(v) },
          { key: "n_rows", label: "Rows", align: "right", format: (v) => int(v) },
          { key: "built_at", label: "Built", format: (v) => when(v) },
        ],
        rows: datasets.slice(0, 10),
      }),
    ],
  });
}

// ---- 3. what the Data Doctor fixed ----------------------------------------------------------------------------
function fixedCard(report) {
  const title = "What the Data Doctor fixed";
  if (!report) return null;
  if (!report.doctor_connected) {
    return card({
      title,
      level: 2,
      className: "cn-fixed",
      body: [
        el("p", {}, "The Data Doctor reads files the way real exports come: a title row above the column names, a \u201cTotal\u201d row at the end, \u201cN/A\u201d in empty cells, dates it cannot read, duplicate or missing ids, and personal details that should be hidden."),
        emptyState("Its report is not linked to Autonomy yet", "So its fixes are not shown here. Open the Data Doctor to see what it found in your files."),
      ],
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
            fixes.map((f) => el("li", {}, el("span", { class: "cn-fix-what" }, f.what), el("span", { class: "cn-fix-rows" }, rows(f.rows)))),
          )
        : el("p", {}, "Nothing needed fixing."),
      el("h3", { class: "cn-sub" }, "What it could not use"),
      problems.length ? el("ul", { class: "plain-list cn-problems" }, problems.map((p) => el("li", {}, p))) : el("p", { class: "muted" }, "Nothing: every row could be used."),
    ],
  });
}

// ---- 4. it asks before changing anything ----------------------------------------------------------------------
function questionsCard(report) {
  if (!report) return null;
  const linked = Boolean(report.doctor_connected);
  const questions = linked && Array.isArray(report.questions) ? report.questions : [];
  const promise = el(
    "p",
    { class: "cn-promise" },
    el("strong", {}, "It asks before changing anything. "),
    "When the Data Doctor wants to change your data, it shows you the change and how many rows it touches, then waits for your yes.",
  );
  let body;
  if (!linked) body = [promise, el("p", { class: "muted" }, "Its questions will show here once its report is linked. Until then, they wait for you in the Data Doctor.")];
  else if (!questions.length) body = [promise, el("p", {}, "Nothing is waiting for you right now.")];
  else
    body = [
      promise,
      el(
        "ul",
        { class: "cn-questions" },
        questions.map((q) =>
          el(
            "li",
            { class: "cn-question" },
            el("p", { class: "cn-question-text" }, q.text),
            el("p", { class: "muted small" }, `Would change ${rows(q.rows_affected)}. Nothing changes until you answer.`),
          ),
        ),
      ),
    ];
  return card({
    title: linked ? `Waiting for your answer (${int(questions.length)})` : "Before anything changes",
    level: 2,
    className: questions.length ? "cn-asks cn-asks-open" : "cn-asks",
    body,
    actions: questions.length ? [el("a", { class: "btn btn-primary", href: V1_APP }, "Answer in the Data Doctor")] : [],
  });
}

// ---- 5. how messages go out -----------------------------------------------------------------------------------
function channelStatus(c) {
  if (c.paused) return pill("Paused", "warning");
  if (c.ready) return pill("Ready", "good");
  return pill("Not set up yet", "neutral");
}

function channelsCard(channels, error) {
  const title = "How messages go out";
  if (error) return card({ title, level: 2, body: [errorBox("Could not load the channels.", error)] });
  if (!Array.isArray(channels)) {
    return card({ title, level: 2, body: [emptyState("Channel status is not available here", "This server does not report its channels yet.")] });
  }
  const line = (c) =>
    el(
      "li",
      { class: "cn-channel" },
      el("div", { class: "cn-channel-head" }, el("span", { class: "cn-channel-name" }, CHANNEL_NAMES[c.channel] || c.channel), channelStatus(c)),
      c.paused ? el("p", { class: "cn-channel-why small" }, c.paused_reason || "Paused by a safety check.") : null,
    );
  const block = (mode, heading, words) => {
    const list = channels.filter((c) => c.mode === mode);
    if (!list.length) return null;
    return el(
      "div",
      { class: "cn-channel-group" },
      el("h3", { class: "cn-sub" }, heading),
      el("p", { class: "muted small" }, words),
      el("ul", { class: "cn-channel-list" }, list.map(line)),
    );
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

function healthCard(health, limits, simulated) {
  if (!health) return null;
  const rules = new Map(((limits && limits.rules) || []).map((r) => [r.metric, r]));
  const selfPauses = (health.alerts || []).filter((a) => a.auto_paused);
  return card({
    title: "Sending health: less spam, healthier lists",
    level: 2,
    className: "cn-health",
    body: [
      el(
        "p",
        {},
        `Every message has a cost: annoyance, unsubscribes, spam complaints. Autonomy weighs that cost before each message, so doing nothing often wins. ${simulated ? "Here is how the simulated sample shop's lists are holding up (not real customers)." : "Here is how your lists are holding up."}`,
      ),
      el(
        "ul",
        { class: "cn-health-lines" },
        HEALTH_LINES.map((line) => {
          const rule = line.rule ? rules.get(line.rule) : null;
          return el(
            "li",
            { class: "cn-health-line" },
            el("div", { class: "cn-health-head" }, el("span", { class: "cn-health-label" }, line.label), el("strong", { class: "cn-health-value" }, dash(health[line.key], line.fmt))),
            el("p", { class: "muted small" }, line.words),
            rule && rule.words ? el("p", { class: "cn-health-limit small" }, el("strong", {}, "Safety stop: "), rule.words) : null,
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
              { class: "plain-list" },
              selfPauses.map((a) => el("li", {}, a.message, " ", el("span", { class: "muted small" }, `(${when(a.at)})`))),
            ),
          )
        : null,
      el("p", { class: "muted small" }, `As of ${when(health.as_of)}.${rules.size ? " The safety stops shown are the ones for the goal you have open." : ""}`),
    ],
  });
}

// ---- the screen -----------------------------------------------------------------------------------------------
const head = () => [el("h1", {}, "Connect"), el("p", { class: "lede" }, "Bring your customer data in, and see what Autonomy is working from.")];

export async function render(root, ctx) {
  clear(root, ...head(), loading());
  const api = ctx.api;
  const [report, channels, health, limits] = await Promise.all([
    call(api, "getDataReport"),
    call(api, "listChannels"),
    call(api, "getHealth"),
    ctx.goalId ? call(api, "getHealthLimits", ctx.goalId) : Promise.resolve(NOTHING),
  ]);
  // Only an API without /v2/data/status falls back to the older v1 datasets list.
  const datasets = !report.value && !report.error ? (await call(api, "getDataStatus")).value : null;
  clear(
    root,
    ...head(),
    el("div", { class: "grid-2 cn-top" }, howToConnect(), connectedCard(report.value, report.error, datasets)),
    report.value ? el("div", { class: "grid-2 cn-doctor" }, fixedCard(report.value), questionsCard(report.value)) : null,
    channelsCard(channels.value, channels.error),
    healthCard(health.value, limits.value, isSimulated(report.value)),
    el("p", { class: "cn-next" }, el("a", { href: "#/goal" }, "Next: say what you want to achieve →")),
  );
}
