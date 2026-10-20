// Screen 1, Connect: where the customer data comes from. v1's Data Doctor does the connecting (files or a
// database, mapped to purchases, visits and contacts); until v2 has its own step this screen explains it, links to
// the v1 app, and shows what is already connected when that is known. Nothing is shown that was not reported.

import { card, clear, dash, el, emptyState, int, loading, pct, table, when } from "../components.js";

const V1_APP = "/";

function explainer() {
  return card({
    title: "Connect your customer data",
    level: 2,
    body: [
      el("p", {}, "Autonomy learns from what your customers did: what they bought, when, and how they responded to messages. It needs that history before it can act."),
      el(
        "ol",
        { class: "steps" },
        el("li", {}, el("strong", {}, "Files: "), "upload exports of orders, customers and contacts (CSV or Excel)."),
        el("li", {}, el("strong", {}, "A database: "), "point it at your store's database with read-only access."),
        el("li", {}, el("strong", {}, "Checks: "), "the Data Doctor maps your columns to purchases, visits and contacts, and tells you in plain words what it could not use."),
      ),
      el("p", { class: "muted" }, "Connecting happens in the current Marketing AI app for now; Autonomy reads the same data."),
    ],
    actions: [el("a", { class: "btn btn-primary", href: V1_APP }, "Open the Data Doctor")],
  });
}

function datasetsCard(datasets) {
  if (datasets === null) {
    return card({
      title: "What is connected",
      level: 2,
      body: [emptyState("Data status is not available here", "Open the Data Doctor to see and manage your connected data.")],
    });
  }
  if (!datasets.length) {
    return card({
      title: "What is connected",
      level: 2,
      body: [emptyState("Nothing connected yet", "Connect files or a database to get started.")],
    });
  }
  return card({
    title: "What is connected",
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

function healthCard(health) {
  if (!health) return null;
  const rows = [
    ["Messages delivered", pct(health.delivery_rate, 1)],
    ["Complaints", pct(health.complaint_rate, 2)],
    ["Unsubscribes", pct(health.unsubscribe_rate, 2)],
    ["Blocked", pct(health.block_rate, 2)],
    ["Contacts per customer per week", dash(health.contacts_per_customer_week)],
  ];
  return card({
    title: "Sending health",
    level: 2,
    body: [
      el("p", { class: "muted" }, `As of ${when(health.as_of)}. Complaints are counted against messages delivered.`),
      el("dl", { class: "facts" }, rows.map(([k, v]) => [el("dt", {}, k), el("dd", {}, v)])),
    ],
  });
}

export async function render(root, ctx) {
  clear(root, el("h1", {}, "Connect"), el("p", { class: "lede" }, "Bring your customer data in once. Autonomy keeps it up to date from there."), loading());
  const [datasets, health] = await Promise.all([
    ctx.api.getDataStatus ? ctx.api.getDataStatus().catch(() => null) : Promise.resolve(null),
    ctx.api.getHealth().catch(() => null),
  ]);
  clear(
    root,
    el("h1", {}, "Connect"),
    el("p", { class: "lede" }, "Bring your customer data in once. Autonomy keeps it up to date from there."),
    el("div", { class: "grid-2" }, explainer(), datasetsCard(datasets)),
    healthCard(health),
    el("p", {}, el("a", { href: "#/goal" }, "Next: say what you want to achieve →")),
  );
}
