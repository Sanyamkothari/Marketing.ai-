// Screen 1, Connect: where the customer data comes from, what the Data Doctor did with it, what it is waiting
// for you to answer, how messages go out, and how healthy the lists are. Numbers first (DESIGN.md section 6): each
// card is its title, its numbers and at most one short line; what a number counts, a safety stop, a paused
// channel's reason and what to bring are descriptions on the word they explain.
//
// It leads with the platforms (GET /v2/connectors, api/v2/connectors.py): Shopify and Klaviyo, each with one line,
// a status pill and one button. Connecting is the only human step (OAuth, or a pasted key for pilots); after it the
// card only reports, in words ("Up to date, 4 minutes ago"), and "Needs reconnect" is its only loud state. Files and
// databases still connect in the Data Doctor of the Marketing AI app (v1), and what comes next is listed, quietly.
// Every number on it comes from the API (or the sample files in mock mode); nothing is made up, and a part the
// server does not report says so in words.

import { card, clear, dash, describe, el, emptyState, errorBox, freshness, int, notice, num, pageHead, pct, pill, present, skeletonKpis, skeletonLines, table, titleInfo, toast, when, withInfo } from "../components.js";
import { SETUP_KEY } from "./onboarding.js";

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

// ---- 1. bring your data in: the platforms first, then files and databases, then what comes next --------------
// One card, one row per way in. A platform row is its name (the connector's description behind it), one line (what
// it brings, or how the connection is doing in words), a status pill and one button. "Needs reconnect" is the only
// loud state; the rest is calm. What a connection's line means, the access a key needs and the sync per stream sit
// in descriptions and in the row's details, never as raw errors.

/** What comes after Shopify and Klaviyo, shown as quiet disabled rows (nothing to click). */
export const COMING_NEXT = Object.freeze(["Stripe", "WooCommerce", "HubSpot", "Mailchimp", "WhatsApp", "Twilio", "Segment events", "Data warehouse"]);

/** The platforms the screen leads with, in order, also when the server cannot list them itself. */
export const FIRST_PLATFORMS = Object.freeze([
  { kind: "shopify", label: "Shopify", tagline: "Brings in orders, customers and products." },
  { kind: "klaviyo", label: "Klaviyo", tagline: "Sends Autonomy's messages through your own flows." },
]);

/** The pill for each connection phase the API reports (api/v2/connectors.py `ConnectionView.phase`). */
export const PHASE_PILLS = Object.freeze({
  healthy: ["Connected", "good"],
  warning: ["Connected", "good"],
  syncing: ["Syncing", "neutral"],
  backfilling: ["Syncing", "neutral"],
  stale: ["Behind", "warning"],
  needs_reconnect: ["Needs reconnect", "critical"],
  paused: ["Paused", "neutral"],
});

const KEY_LABELS = Object.freeze({ shopify: "Admin API access token", klaviyo: "Private API key" });
const KEY_WHERE = Object.freeze({
  shopify: "In your Shopify admin, open Settings, Apps, Develop apps, and create an app with this access:",
  klaviyo: "In Klaviyo, open Settings, API keys, and create a private key with this access:",
});

const platformName = (kind) => (FIRST_PLATFORMS.find((p) => p.kind === kind) || { label: String(kind || "") }).label;
const live = (p) => (Array.isArray(p.connections) ? p.connections : []).filter((c) => c.phase !== "disconnected");

/**
 * The one primary button on the screen: Reconnect when access stopped working (the only loud state); otherwise the
 * Data Doctor's "Answer" when it waits for you (that card draws it); otherwise the first platform not connected yet;
 * otherwise, with no data at all, "Open the Data Doctor".
 */
export function choosePrimary(platforms, { waiting = false, hasData = false } = {}) {
  const list = Array.isArray(platforms) ? platforms : [];
  for (const p of list) for (const c of live(p)) if (c.phase === "needs_reconnect") return `reconnect:${c.connection_id}`;
  if (waiting) return null;
  const open = list.find((p) => !live(p).length);
  if (open) return `connect:${open.kind}`;
  return hasData ? null : "files";
}

const kindOf = (key, wanted) => (key === wanted ? "primary" : "secondary");

/** A labelled text box for the connect forms; returns [field, input]. */
let fieldSeq = 0;
function textField({ name, label, help, placeholder, secret = false, value = "" }) {
  fieldSeq += 1;
  const id = `cn-f-${name}-${fieldSeq}`;
  const input = el("input", {
    id,
    name,
    type: secret ? "password" : "text",
    autocomplete: "off",
    spellcheck: "false",
    placeholder: placeholder || null,
    required: true,
  });
  if (value) input.value = value;
  const labelEl = el("label", { for: id }, label);
  return [el("div", { class: "field" }, help ? withInfo(labelEl, help) : labelEl, input), input];
}

/**
 * The inline connect form under a platform row: its fields (Shopify: the shop address), then "Continue to Shopify"
 * (OAuth) or the pasted key. A server without our app's credentials for the platform offers the key only.
 */
function connectForm(p, env, { onCancel, prefill = {} } = {}) {
  let mode = p.oauth_ready ? "oauth" : "key";
  const host = el("div", { class: "cn-p-formhost" });
  const draw = () => {
    const fields = (p.config_fields || []).map((f) =>
      textField({ name: f.name, label: f.label, help: f.help, placeholder: f.name === "shop" ? "your-shop.myshopify.com" : null, value: prefill[f.name] || "" }),
    );
    const scopes = Array.isArray(p.scopes) && p.scopes.length ? ` ${p.scopes.join(", ")}.` : "";
    const keyHelp = `${KEY_WHERE[p.kind] || `In ${p.label}, create a key with this access:`}${scopes} Autonomy keeps it encrypted and uses only that access.`;
    const key = mode === "key" ? textField({ name: "api_key", label: KEY_LABELS[p.kind] || "Access key", help: keyHelp, secret: true }) : null;
    const error = el("p", { class: "field-error", role: "alert" });
    const submit = el("button", { type: "submit", class: "btn btn-primary" }, mode === "oauth" ? `Continue to ${p.label}` : "Connect");
    const config = () => Object.fromEntries((p.config_fields || []).map((f, i) => [f.name, String(fields[i][1].value || "").trim()]));
    const form = el(
      "form",
      {
        class: "cn-p-form",
        novalidate: true,
        onsubmit: async (event) => {
          if (event && event.preventDefault) event.preventDefault();
          error.textContent = "";
          submit.disabled = true;
          try {
            if (mode === "oauth") {
              const out = await env.api.startConnect(p.kind, config());
              env.redirect(out.url);
              return;
            }
            const value = String(key[1].value || "").trim();
            if (!value) {
              error.textContent = `Paste the ${(KEY_LABELS[p.kind] || "access key").toLowerCase()} first.`;
              return;
            }
            await env.api.connectWithKey(p.kind, value, config());
            env.toast(`${p.label} is connected. The first sync has started.`, "good");
            await env.refresh();
          } catch (err) {
            error.textContent = (err && err.message) || `Could not connect ${p.label}.`;
          } finally {
            submit.disabled = false;
          }
        },
      },
      el("div", { class: "form-row" }, fields.map(([f]) => f), key ? key[0] : null),
      error,
      el(
        "div",
        { class: "form-actions" },
        submit,
        p.oauth_ready && p.key_allowed
          ? el(
              "button",
              {
                type: "button",
                class: "link-btn",
                onclick: () => {
                  mode = mode === "oauth" ? "key" : "oauth";
                  draw();
                },
              },
              mode === "oauth" ? "Use an access key instead" : `Sign in with ${p.label} instead`,
            )
          : null,
        el("button", { type: "button", class: "link-btn", onclick: onCancel }, "Cancel"),
      ),
    );
    clear(host, form);
  };
  draw();
  return host;
}

/** One quiet row of a connection's details: the stream, how fresh it is, its history, its rows. */
function streamLine(s) {
  const bits = [s.words];
  if (s.history_words && Number(s.history_progress) < 1) bits.push(s.history_words);
  if (present(s.rows_last_day)) bits.push(`${int(s.rows_last_day)} in the last day`);
  return el("li", { class: "cn-stream" }, el("span", { class: "cn-stream-name" }, s.label), el("span", { class: "cn-stream-words" }, bits.join(" · ")));
}

/** A button that asks once more before it acts ("Disconnect" -> "Confirm disconnect"). */
function confirmButton(label, confirmLabel, action) {
  let armed = false;
  const btn = el("button", { type: "button", class: "btn btn-danger btn-sm" }, label);
  btn.addEventListener("click", async () => {
    if (!armed) {
      armed = true;
      btn.textContent = confirmLabel;
      const t = setTimeout(() => {
        armed = false;
        btn.textContent = label;
      }, 6000);
      if (t && typeof t.unref === "function") t.unref();
      return;
    }
    btn.disabled = true;
    try {
      await action();
    } finally {
      btn.disabled = false;
    }
  });
  return btn;
}

/** The guided one-time step a sending tool may need (Klaviyo: its flow), filled in once the API answers. */
function setupLine(c, env) {
  const host = el("div", { class: "cn-p-setup", hidden: true });
  if (typeof env.api.getConnectorSetup !== "function" || c.status !== "active") return host;
  Promise.resolve(env.api.getConnectorSetup(c.connection_id)).then(
    (s) => {
      if (!s || !["guided", "pending"].includes(s.state)) return;
      host.hidden = false;
      if (s.state === "pending") {
        clear(host, el("p", { class: "cn-p-note" }, s.words));
        return;
      }
      const steps = el("ol", { class: "cn-steps", hidden: true }, (s.steps || []).map((t) => el("li", {}, t)));
      const toggle = el("button", { type: "button", class: "link-btn", "aria-expanded": "false" }, "Show the steps");
      toggle.addEventListener("click", () => {
        steps.hidden = !steps.hidden;
        toggle.setAttribute("aria-expanded", steps.hidden ? "false" : "true");
        toggle.textContent = steps.hidden ? "Show the steps" : "Hide the steps";
      });
      clear(host, el("p", { class: "cn-p-note" }, s.words, " ", toggle), steps);
    },
    () => {},
  );
  return host;
}

/** A platform row with a connection: the account, its health in words, and its details behind a quiet toggle. */
function connectedRow(p, c, env) {
  const [word, tone] = PHASE_PILLS[c.phase] || ["Connected", "good"];
  const loud = c.phase === "needs_reconnect";
  const panel = el("div", { class: "cn-p-panel", hidden: true });
  const problems = Array.isArray(c.problems) ? c.problems : [];
  const details = () =>
    clear(
      panel,
      el("ul", { class: "cn-streams" }, (c.streams || []).map(streamLine)),
      problems.length ? el("ul", { class: "cn-p-problems" }, problems.map((x) => el("li", {}, x.words))) : null,
      el(
        "div",
        { class: "cn-p-tools" },
        c.status === "active"
          ? el(
              "button",
              {
                type: "button",
                class: "btn btn-secondary btn-sm",
                onclick: async () => {
                  try {
                    const out = await env.api.syncConnection(c.connection_id);
                    env.toast((out && out.words) || "A sync has started.", "info");
                  } catch (err) {
                    env.toast((err && err.message) || "Could not start a sync.", "warning");
                  }
                },
              },
              "Sync now",
            )
          : null,
        confirmButton("Disconnect", "Confirm disconnect", async () => {
          try {
            await env.api.disconnectConnection(c.connection_id);
            env.toast(`${p.label} is disconnected.`, "info");
            await env.refresh();
          } catch (err) {
            env.toast((err && err.message) || `Could not disconnect ${p.label}.`, "warning");
          }
        }),
      ),
    );
  let open = false;
  const detailsBtn = el("button", { type: "button", class: "btn btn-ghost btn-sm", "aria-expanded": "false" }, "Details");
  detailsBtn.addEventListener("click", () => {
    open = !open;
    if (open) details();
    panel.hidden = !open;
    detailsBtn.setAttribute("aria-expanded", open ? "true" : "false");
  });
  const actions = [detailsBtn];
  if (loud) {
    const key = `reconnect:${c.connection_id}`;
    actions.unshift(
      el(
        "button",
        {
          type: "button",
          class: ["btn", `btn-${kindOf(key, env.primary)}`],
          onclick: () => {
            open = false;
            detailsBtn.setAttribute("aria-expanded", "false");
            panel.hidden = false;
            clear(panel, connectForm(p, env, { prefill: { shop: c.external_id || "" }, onCancel: () => (panel.hidden = true) }));
          },
        },
        "Reconnect",
      ),
    );
  }
  return el(
    "li",
    { class: ["cn-platform", loud && "is-loud"], dataset: { kind: p.kind, phase: c.phase } },
    el(
      "div",
      { class: "cn-p-main" },
      el("h3", { class: "cn-p-name" }, withInfo(el("span", {}, p.label), p.description)),
      el("p", { class: "cn-p-line" }, el("span", { class: "cn-p-account" }, c.label), " · ", withInfo(el("span", { class: "cn-p-words" }, c.words), c.detail)),
    ),
    el("div", { class: "cn-p-status" }, pill(word, tone)),
    el("div", { class: "cn-p-actions" }, actions),
    p.sends ? setupLine(c, env) : null,
    panel,
  );
}

/** A platform row with nothing connected: what it brings, and Connect (inline form or straight to its sign-in). */
function connectRow(p, env, { gone = null, unavailable = false } = {}) {
  const panel = el("div", { class: "cn-p-panel", hidden: true });
  const key = `connect:${p.kind}`;
  const button = el("button", { type: "button", class: ["btn", `btn-${unavailable ? "secondary" : kindOf(key, env.primary)}`], disabled: unavailable }, "Connect");
  button.addEventListener("click", async () => {
    const fields = Array.isArray(p.config_fields) ? p.config_fields : [];
    if (p.oauth_ready && !fields.length) {
      // Nothing to type: straight to the platform's own sign-in and consent screen.
      button.disabled = true;
      try {
        const out = await env.api.startConnect(p.kind, {});
        env.redirect(out.url);
      } catch (err) {
        env.toast((err && err.message) || `Could not start connecting ${p.label}.`, "warning");
      } finally {
        button.disabled = false;
      }
      return;
    }
    button.hidden = true;
    panel.hidden = false;
    clear(
      panel,
      connectForm(p, env, {
        onCancel: () => {
          panel.hidden = true;
          button.hidden = false;
        },
      }),
    );
  });
  const line = unavailable ? "Not available on this server yet." : gone ? gone.words : p.tagline;
  return el(
    "li",
    { class: "cn-platform", dataset: { kind: p.kind } },
    el(
      "div",
      { class: "cn-p-main" },
      el("h3", { class: "cn-p-name" }, p.description ? withInfo(el("span", {}, p.label), p.description) : p.label),
      el("p", { class: "cn-p-line" }, gone && gone.detail ? withInfo(el("span", {}, line), gone.detail) : line),
    ),
    el("div", { class: "cn-p-status" }, unavailable ? null : pill("Not connected", "neutral")),
    el("div", { class: "cn-p-actions" }, button),
    panel,
  );
}

/** The files and database rows: the existing flow, in the Data Doctor of the Marketing AI app (v1). */
function fileRows(env) {
  return [
    el(
      "li",
      { class: "cn-platform cn-way" },
      el(
        "div",
        { class: "cn-p-main" },
        el("h3", { class: "cn-p-name cn-way-title" }, withInfo(el("span", {}, "Upload your files"), "Your column names do not need to match anything. The Data Doctor works out which column is which.")),
        el(
          "p",
          { class: "cn-p-line" },
          "CSV, Excel, or a zip of them: your ",
          withInfo(el("span", {}, "customers"), "One row per customer, with their id, email or phone number, and whether they agreed to hear from you."),
          " and your ",
          withInfo(el("span", {}, "orders"), "One row per order, with the order id, the customer id, the date, the amount and what they bought."),
          ".",
        ),
      ),
      el("div", { class: "cn-p-status" }),
      el("div", { class: "cn-p-actions" }, el("a", { class: ["btn", `btn-${kindOf("files", env.primary)}`], href: V1_APP }, "Open the Data Doctor")),
    ),
    el(
      "li",
      { class: "cn-platform cn-way" },
      el(
        "div",
        { class: "cn-p-main" },
        el("h3", { class: "cn-p-name cn-way-title" }, "Or connect your database"),
        el("p", { class: "cn-p-line" }, withInfo(el("span", {}, "Read-only access"), "Autonomy never writes to your database."), " to PostgreSQL, Redshift, MySQL, S3 and more."),
      ),
      el("div", { class: "cn-p-status" }),
      el("div", { class: "cn-p-actions" }, el("a", { class: "btn btn-secondary", href: V1_CONNECTIONS }, "Connect a database")),
    ),
  ];
}

const comingNext = () =>
  el(
    "li",
    { class: "cn-platform cn-soon-row", "aria-disabled": "true" },
    el("div", { class: "cn-p-main" }, el("h3", { class: "cn-p-name" }, "Coming next"), el("ul", { class: "cn-soon" }, COMING_NEXT.map((n) => el("li", { class: "cn-soon-item" }, n)))),
  );

const TOOLS = "Bring your data in";

/**
 * The card. `overview` is GET /v2/connectors (null: this server has no connector routes; undefined: loading),
 * `error` its failure. Platforms the server lists come first in FIRST_PLATFORMS order, then any others it lists.
 */
function toolsCard(overview, error, env) {
  const title = titleInfo(TOOLS, "Autonomy learns from what your customers did: what they bought, when, and how they answered your messages. Connect your shop and your email tool once; after that it keeps them in sync on its own.", { label: "About bringing your data in" });
  const listed = overview && Array.isArray(overview.platforms) ? overview.platforms : null;
  const order = (k) => {
    const i = FIRST_PLATFORMS.findIndex((p) => p.kind === k);
    return i === -1 ? FIRST_PLATFORMS.length : i;
  };
  let rows;
  if (overview === undefined) {
    rows = FIRST_PLATFORMS.map((p) =>
      el("li", { class: "cn-platform" }, el("div", { class: "cn-p-main" }, el("h3", { class: "cn-p-name" }, p.label), el("p", { class: "cn-p-line" }, p.tagline)), el("div", { class: "cn-p-status" }), el("div", { class: "cn-p-actions" }, skeletonLines(1))),
    );
  } else if (listed) {
    rows = [...listed]
      .sort((a, b) => order(a.kind) - order(b.kind))
      .flatMap((p) => {
        const on = live(p);
        if (on.length) return on.map((c) => connectedRow(p, c, env));
        const gone = (p.connections || []).find((c) => c.phase === "disconnected") || null;
        return [connectRow(p, env, { gone })];
      });
  } else {
    rows = FIRST_PLATFORMS.map((p) => connectRow({ ...p, description: null }, env, { unavailable: true }));
  }
  // Whenever the runtime holds decisions, say so, also when the only connection that holds them is a disconnected
  // one (its row then reads as "Connect" again, and nothing else on the screen would explain the wait).
  const held = overview && overview.can_act === false && Array.isArray(overview.on_hold) && overview.on_hold.length > 0 && listed;
  return card({
    title,
    level: 2,
    className: "cn-tools",
    body: [
      held ? notice(withInfo(el("span", {}, "Autonomy waits to decide until your data is ready."), overview.on_hold.join(" ")), { role: "note" }) : null,
      error ? errorBox("Could not load the connected platforms.", error, { retry: env.refresh }) : null,
      el("ul", { class: "cn-platforms" }, rows, fileRows(env), comingNext()),
      el("p", { class: "cn-where" }, el("strong", {}, "Where: "), "files and databases go in through the Data Doctor (Marketing AI app)."),
    ],
  });
}

/** After the platform's sign-in, the callback sends the browser to `#/connect?connected=shopify` (or
 * `?connect_error=...`): say so once, then tidy the address. True when there was an outcome to say. */
function connectOutcome(say) {
  if (typeof location === "undefined" || !location.hash || !location.hash.includes("?")) return false;
  const params = new URLSearchParams(location.hash.split("?")[1]);
  const kind = params.get("connected") || params.get("kind");
  if (params.get("connected")) say(`${platformName(kind)} is connected. The first sync has started.`, "good");
  else if (params.get("connect_error")) say(params.get("connect_error"), "critical");
  else return false;
  if (typeof history !== "undefined" && history.replaceState) history.replaceState(null, "", "#/connect");
  return true;
}

/** True while the five-click setup on Getting started is under way (it set the flag before the sign-in round trip). */
function settingUp() {
  try {
    return typeof sessionStorage !== "undefined" && sessionStorage.getItem(SETUP_KEY) === "1";
  } catch {
    return false;
  }
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
function questionsCard(report, { primary = true } = {}) {
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
    actions: questions.length ? [el("a", { class: ["btn", primary ? "btn-primary" : "btn-secondary"], href: V1_APP }, "Answer in the Data Doctor")] : [],
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
// One question: what is Autonomy working from, and is anything waiting for me? "Bring your data in" answers it first,
// across the page: each platform's connection in words, then files and databases, then what comes next. Under it, two
// columns, 7 + 5: what the data holds, what the Data Doctor fixed and how the lists hold up on the left; what waits for
// you and how messages go out on the right.
const head = () => pageHead({ title: "Connect", sub: "What Autonomy is working from, and how to bring your data in." });

/** While the API answers: the real frame with grey blocks where the numbers go, so nothing jumps when they land. */
function skeletonScreen(env) {
  return [
    head(),
    el("div", { class: "cn-tools-host", "aria-busy": "true" }, toolsCard(undefined, null, env)),
    el(
      "div",
      { class: "grid cn-grid", "aria-busy": "true" },
      el(
        "div",
        { class: "span-7 stack cn-col" },
        card({ title: CONNECTED, level: 2, body: [skeletonKpis(["Customers", "Orders", "Products in those orders"]), skeletonLines(3)] }),
        card({ title: HEALTH, level: 2, body: [skeletonLines(5)] }),
      ),
      el("div", { class: "span-5 stack cn-col" }, card({ title: "How messages go out", level: 2, body: [skeletonLines(4)] })),
    ),
  ];
}

/** Put a card in its phone place (`cn-o1`...): on a desktop the two columns keep their own order. */
const at = (node, n) => (node && node.classList.add(`cn-o${n}`), node);
const column = (cls, nodes) => el("div", { class: [cls, "stack cn-col"] }, nodes.filter(Boolean));

export async function render(root, ctx) {
  const api = ctx.api;
  const say = typeof ctx.toast === "function" ? ctx.toast : toast;
  const tools = el("div", { class: "cn-tools-host" });
  const env = {
    api,
    primary: null,
    toast: say,
    redirect: typeof ctx.redirect === "function" ? ctx.redirect : (url) => window.location.assign(url),
    // After a connect, a disconnect or a retry only the platforms card is drawn again.
    refresh: async () => {
      const again = await call(api, "listConnectors");
      env.primary = choosePrimary(again.value && again.value.platforms, env.state);
      clear(tools, toolsCard(again.value, again.error, env));
    },
    state: { waiting: false, hasData: false },
  };
  clear(root, ...skeletonScreen(env));
  // Back from a platform's sign-in in the middle of the setup: straight on to the next step, no extra click.
  if (connectOutcome(say) && settingUp() && typeof ctx.navigate === "function") {
    ctx.navigate("start");
    return;
  }
  const retry = () => render(root, ctx);
  const [report, channels, health, limits, platforms] = await Promise.all([
    call(api, "getDataReport"),
    call(api, "listChannels"),
    call(api, "getHealth"),
    ctx.goalId ? call(api, "getHealthLimits", ctx.goalId) : Promise.resolve(NOTHING),
    call(api, "listConnectors"),
  ]);
  // Only an API without /v2/data/status falls back to the older v1 datasets list.
  const datasets = !report.value && !report.error ? (await call(api, "getDataStatus")).value : null;
  const r = report.value;
  const waiting = Boolean(r && r.doctor_connected && Array.isArray(r.questions) && r.questions.length);
  const hasData = Boolean(r && (Number(r.customers) || (Array.isArray(r.sources) && r.sources.length))) || (Array.isArray(datasets) && datasets.length > 0);
  env.state = { waiting, hasData };
  env.primary = choosePrimary(platforms.value && platforms.value.platforms, env.state);
  // A reconnect outranks the Data Doctor's questions for the one primary button.
  const reconnecting = Boolean(env.primary && env.primary.startsWith("reconnect:"));
  clear(tools, toolsCard(platforms.value, platforms.error, env));
  const connected = at(connectedCard(r, report.error, datasets, retry), 1);
  const asks = at(r ? questionsCard(r, { primary: !reconnecting }) : null, 2);
  const fixed = at(r ? fixedCard(r) : null, 3);
  const healthBlock = at(healthCard(health.value, limits.value, isSimulated(r)), 4);
  const channelBlock = at(channelsCard(channels.value, channels.error, retry), 5);
  clear(
    root,
    head(),
    tools,
    el("div", { class: "grid cn-grid" }, column("span-7", [connected, fixed, healthBlock]), column("span-5", [asks, channelBlock])),
    el("p", { class: "cn-next" }, el("a", { href: "#/goal" }, "Next: say what you want to achieve \u2192")),
  );
}
