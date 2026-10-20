/* Operator mode: power tools for us, acting as the growth engineer for an early client
   (plan-v2.md section 9).

   Three tools on one page:
     - the raw JSON of the goal, its brief, its content library, its proof and its approval batches,
       exactly as the API sent them;
     - a decisions table with filters (customer, kind, group, channel, words in the reason) and the
       raw JSON of any row;
     - a customer lookup by id: state, decisions and events, as a table and as raw JSON.

   It shows the API's own values (ids, probabilities, expected net, policy version) with no
   rounding into new numbers, because this is where we check what the agent really did. */

import {
  DASH,
  call,
  channelLabel,
  count,
  describeAction,
  describeEvent,
  ensureStyles,
  errText,
  groupLabel,
  h,
  money,
  newestFirst,
  notFound,
  notify,
  num,
  present,
  resolveGoal,
  timeline,
  when,
} from "./live.js";

const LIMITS = [50, 200, 500];

/**
 * The client-side filters of the decisions table. `customer_id` and `limit` are sent to the API;
 * these narrow what came back. Every filter is optional ("" means any).
 */
export function filterDecisions(decisions, { kind = "", group = "", channel = "", text = "" } = {}) {
  const needle = text.trim().toLowerCase();
  return (decisions || [])
    .filter((d) => !kind || d.kind === kind)
    .filter((d) => !group || d.group === group)
    .filter((d) => !channel || (d.action && d.action.channel === channel))
    .filter(
      (d) =>
        !needle ||
        String(d.reason || "").toLowerCase().includes(needle) ||
        String(d.customer_id).toLowerCase().includes(needle) ||
        String(d.decision_id).toLowerCase().includes(needle),
    )
    .sort(newestFirst((d) => d.made_at));
}

/** Pretty JSON for the viewer. `undefined` (never sent) reads as the em dash. */
export const pretty = (value) => (value === undefined ? DASH : JSON.stringify(value, null, 2));

/** A collapsible raw-JSON block with its own copy button. `meta` is a short grey note on its row (a count). */
function jsonBlock(title, value, ctx, announcer, open = false, meta = null) {
  const text = pretty(value);
  const copy = h("button", { type: "button", class: "btn btn-ghost btn-sm" }, "Copy JSON");
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      notify(ctx, `${title} copied.`, "good", announcer);
    } catch (e) {
      notify(ctx, `Couldn't copy: ${errText(e)}`, "critical", announcer);
    }
  });
  return h(
    "details",
    { class: "op-json", open },
    h("summary", null, h("span", null, title), present(meta) && h("span", { class: "meta" }, meta)),
    h("div", { class: "op-json-body" }, h("div", { class: "op-json-actions" }, copy), h("pre", { class: "op-pre", tabindex: "0" }, text)),
  );
}

function field(label, control, extra = "") {
  return h("label", { class: `field op-field ${extra}`.trim() }, h("span", { class: "op-label" }, label), control);
}

function select(options, value = "") {
  const el = h("select", null, ...options.map(([v, text]) => h("option", { value: v }, text)));
  el.value = value;
  return el;
}

/** Grey blocks at the size of what is loading (nothing shifts when it lands), with a screen-reader word. */
function loadingBlock(lines = 3) {
  return h(
    "div",
    { class: "skeleton-stack op-loading", role: "status" },
    h("span", { class: "visually-hidden" }, "Loading…"),
    ...Array.from({ length: lines }, (_, i) => h("span", { class: "skeleton", "aria-hidden": "true", style: i === lines - 1 ? "width: 60%" : "width: 100%" })),
  );
}

/** The block that failed: the API's words and a Retry for that block only. */
function failedBox(what, error, retry) {
  return h(
    "div",
    { class: "error-box", role: "alert" },
    h("p", { class: "empty-title" }, `Couldn't load ${what}: ${errText(error)}`),
    typeof retry === "function" && h("div", { class: "error-actions" }, h("button", { type: "button", class: "link-btn", onclick: retry }, "Retry")),
  );
}

function emptyBox(sentence) {
  return h("div", { class: "empty", role: "status" }, h("p", null, sentence));
}

/** A table head: 12px grey sentence-case labels; `num` columns right-aligned. */
function thead(cols) {
  return h("thead", null, h("tr", null, ...cols.map(([label, num]) => h("th", { scope: "col", class: num ? "num" : null }, label))));
}

/** A cell that keeps its header as a label when the table folds into two-line rows on a phone. */
function td(label, cls, ...kids) {
  return h("td", { class: cls || null, "data-label": label || null }, ...kids);
}

/** The goal's own fields, raw, as label and value pairs. */
function goalFacts(goal) {
  const pairs = [
    ["Status", goal?.status],
    ["Autonomy", goal?.autonomy],
    ["Currency", goal?.currency],
    ["Budget", goal?.budget, "tabular"],
  ];
  return h(
    "dl",
    { class: "op-facts" },
    ...pairs.map(([label, value, cls]) => h("div", null, h("dt", null, label), h("dd", { class: cls || null }, present(value) ? String(value) : DASH))),
  );
}

function card(title, meta, ...kids) {
  return h(
    "section",
    { class: "card span-12" },
    h("div", { class: "card-head" }, h("h2", { class: "card-title" }, title), meta),
    ...kids,
  );
}

export async function render(root, ctx) {
  ensureStyles();
  const api = ctx.api;
  const announcer = h("p", { class: "ap-sr", role: "status", "aria-live": "polite" });
  const page = h("div", { class: "ap-page ap-operator" });
  root.replaceChildren(page);
  page.append(
    h(
      "div",
      { class: "page-head" },
      h("div", null,
        h("h1", { class: "page-title" }, "Operator"),
        h("p", { class: "page-sub" }, "Raw data and power tools for the people running the agent. Everything here is exactly what the API returned.")),
    ),
  );
  const grid = h("div", { class: "grid op-grid" });
  page.append(grid);

  // ---- goal picker + raw JSON
  const goalInput = h("input", { class: "mono", id: "ap-op-goal", autocomplete: "off", spellcheck: "false" });
  const goalForm = h("form", { class: "op-filters" }, field("Goal id", goalInput, "op-field-wide"), h("button", { type: "submit", class: "btn btn-secondary" }, "Load"));
  const goalOut = h("div", { class: "op-out" }, loadingBlock(3));
  grid.append(card("Goal, brief and content", h("span", { class: "card-meta" }, "Raw JSON, as the API sent it"), goalForm, goalOut));

  // ---- decisions
  const custFilter = h("input", { class: "mono", autocomplete: "off", spellcheck: "false", placeholder: "any" });
  const limitSel = select(LIMITS.map((n) => [String(n), String(n)]), "200");
  const kindSel = select([["", "Any"], ["act", "Act"], ["nothing", "Left alone"]]);
  const groupSel = select([["", "Any"], ["agent", "Agent"], ["baseline", "Simple rule"], ["holdout", "Holdout"]]);
  const channelSel = select([["", "Any"], ...["email", "whatsapp", "sms", "push", "web", "webhook"].map((c) => [c, channelLabel(c)])]);
  const textFilter = h("input", { type: "search", autocomplete: "off", placeholder: "words in the reason, or an id" });
  const decForm = h(
    "form",
    { class: "op-filters" },
    field("Customer id", custFilter),
    field("How many", limitSel, "op-field-narrow"),
    field("Kind", kindSel, "op-field-narrow"),
    field("Group", groupSel, "op-field-narrow"),
    field("Channel", channelSel, "op-field-narrow"),
    field("Search", textFilter, "op-field-wide"),
    h("button", { type: "submit", class: "btn btn-primary" }, "Fetch"),
  );
  const decCount = h("span", { class: "card-meta tabular", "aria-live": "polite" });
  const decTable = h("div", { class: "op-out" });
  const decDetail = h("div", { class: "op-detail" });
  grid.append(card("Decisions", decCount, decForm, decTable, decDetail));

  // ---- customer lookup
  const lookupInput = h("input", { class: "mono", autocomplete: "off", spellcheck: "false", required: true, placeholder: "cust_…" });
  const lookupForm = h("form", { class: "op-filters" }, field("Customer id", lookupInput, "op-field-wide"), h("button", { type: "submit", class: "btn btn-secondary" }, "Look up"));
  const lookupOut = h("div", { class: "op-out" });
  grid.append(card("Customer lookup", h("span", { class: "card-meta" }, "State, decisions and events for one customer"), lookupForm, lookupOut));
  page.append(announcer);

  let bundle = null;
  let goalId = present(ctx.goalId) ? ctx.goalId : null;
  let decisions = [];

  const brief = () => bundle?.brief || null;
  const currency = () => bundle?.goal?.currency;

  async function loadGoal(id) {
    goalOut.replaceChildren(loadingBlock(3));
    try {
      bundle = await resolveGoal({ ...ctx, goalId: id });
    } catch (e) {
      bundle = null;
      goalOut.replaceChildren(failedBox(present(id) ? `goal ${id}` : "a goal", e, () => loadGoal(id)));
      return;
    }
    if (!bundle) {
      goalOut.replaceChildren(emptyBox("No goals yet."));
      return;
    }
    goalId = bundle.goal?.goal_id ?? id;
    goalInput.value = goalId || "";
    // Batches: null when this API build has no batch route; an error here never hides the rest.
    let batches;
    let batchNote = null;
    try {
      batches = typeof api?.listBatches === "function" ? await api.listBatches(goalId) : null;
      if (batches === null) batchNote = "This API does not list batches yet.";
    } catch (e) {
      batches = undefined;
      batchNote = `Couldn't load batches: ${errText(e)}`;
    }
    goalOut.replaceChildren(
      ...[
        goalFacts(bundle.goal),
        h(
          "div",
          { class: "op-json-list" },
          ...[
            jsonBlock("Goal", bundle.goal, ctx, announcer),
            jsonBlock("Brief", bundle.brief, ctx, announcer),
            jsonBlock("Content", bundle.content, ctx, announcer, false, `${(bundle.content || []).length} items`),
            jsonBlock("Proof", bundle.proof, ctx, announcer),
            Array.isArray(batches) ? jsonBlock("Batches", batches, ctx, announcer, false, `${batches.length} batches`) : null,
          ].filter(Boolean),
        ),
        batchNote ? h("p", { class: "meta op-note" }, batchNote) : null,
      ].filter(Boolean),
    );
  }

  function paintDecisions() {
    const rows = filterDecisions(decisions, { kind: kindSel.value, group: groupSel.value, channel: channelSel.value, text: textFilter.value });
    decCount.textContent = `Showing ${count(rows.length)} of ${count(decisions.length)} fetched`;
    if (!rows.length) {
      decTable.replaceChildren(emptyBox("No decisions match."));
      return;
    }
    const cols = [["Made"], ["Customer"], ["Kind"], ["Group"], ["Action"], ["Prob.", true], ["Exp. net", true], ["Attention price", true], ["Policy"], ["Reason"], [h("span", { class: "visually-hidden" }, "Raw")]];
    const table = h(
      "table",
      { class: "table-stack op-table" },
      thead(cols),
      h("tbody", null, ...rows.map((d) => {
        const who = h("button", { type: "button", class: "link-btn mono op-id" }, d.customer_id);
        who.addEventListener("click", () => {
          lookupInput.value = d.customer_id;
          lookup(d.customer_id);
        });
        const view = h("button", { type: "button", class: "btn btn-ghost btn-sm", "aria-label": `Show JSON of ${d.decision_id}` }, "JSON");
        view.addEventListener("click", () => {
          decDetail.replaceChildren(h("div", { class: "op-json-list" }, jsonBlock(`Decision ${d.decision_id}`, d, ctx, announcer, true)));
        });
        return h(
          "tr",
          null,
          td(null, "op-nowrap", when(d.made_at)),
          td("Customer", "op-nowrap", who),
          td("Kind", "op-nowrap", d.kind === "act" ? "Act" : "Left alone"),
          td("Group", "op-nowrap", groupLabel(d.group)),
          td("Action", "op-nowrap", describeAction(d, brief())),
          td("Prob.", "num", num(d.probability, 3)),
          td("Exp. net", "num is-key", money(d.expected_net, currency())),
          td("Attention price", "num", money(d.attention_price, currency())),
          td("Policy", "mono op-nowrap", d.policy_version ?? DASH),
          td("Reason", "op-reason", d.reason || DASH, (d.blocked_by || []).length > 0 && h("span", { class: "meta" }, ` [ruled out: ${d.blocked_by.join(", ")}]`)),
          td(null, "op-raw", view),
        );
      })),
    );
    decTable.replaceChildren(h("div", { class: "table-wrap op-table-wrap" }, table));
  }

  async function loadDecisions() {
    decTable.replaceChildren(loadingBlock(6));
    decDetail.replaceChildren();
    const query = { limit: Number(limitSel.value) };
    if (present(goalId)) query.goal_id = goalId;
    if (custFilter.value.trim()) query.customer_id = custFilter.value.trim();
    try {
      decisions = (await call(api, "listDecisions", query)) || [];
    } catch (e) {
      decisions = [];
      decCount.textContent = "";
      decTable.replaceChildren(failedBox("decisions", e, loadDecisions));
      return;
    }
    paintDecisions();
    decDetail.replaceChildren(h("div", { class: "op-json-list" }, jsonBlock("All fetched decisions", decisions, ctx, announcer, false, `${decisions.length} decisions`)));
  }

  async function lookup(id) {
    const cid = String(id || "").trim();
    if (!cid) {
      lookupOut.replaceChildren(h("p", { class: "field-error", role: "alert" }, "Type a customer id first."));
      return;
    }
    lookupOut.replaceChildren(loadingBlock(3));
    let found;
    try {
      found = await call(api, "getCustomer", cid);
    } catch (e) {
      lookupOut.replaceChildren(notFound(e) ? emptyBox(`No customer ${cid}.`) : failedBox(`customer ${cid}`, e, () => lookup(cid)));
      return;
    }
    if (!found || !found.state) {
      lookupOut.replaceChildren(emptyBox(`No customer ${cid}.`));
      return;
    }
    const rows = timeline(found).map(({ at, kind, item }) =>
      h(
        "tr",
        null,
        td(null, "op-nowrap", when(at)),
        td("Type", "op-nowrap", kind === "event" ? "Event" : "Decision"),
        td("What", null, kind === "event" ? describeEvent(item, currency()) : describeAction(item, brief())),
        td("Decision", "mono op-nowrap", kind === "event" ? item.decision_id ?? DASH : item.decision_id),
        td("Why / source", "op-reason", kind === "decision" ? item.reason || DASH : item.source ?? DASH),
      ),
    );
    lookupOut.replaceChildren(
      h("p", { class: "op-summary tabular" }, `${found.state.customer_id} · ${groupLabel(found.state.group)} · ${count(found.state.purchases)} purchases · ${money(found.state.revenue, currency())} revenue · state version ${found.state.version ?? DASH}`),
      rows.length
        ? h("div", { class: "table-wrap op-table-wrap" }, h("table", { class: "table-stack op-table" },
            thead([["When"], ["Type"], ["What"], ["Decision"], ["Why / source"]]),
            h("tbody", null, ...rows)))
        : emptyBox("No events or decisions."),
      h(
        "div",
        { class: "op-json-list" },
        jsonBlock("State", found.state, ctx, announcer),
        jsonBlock("Decisions", found.decisions, ctx, announcer, false, `${(found.decisions || []).length} decisions`),
        jsonBlock("Events", found.events, ctx, announcer, false, `${(found.events || []).length} events`),
      ),
    );
  }

  goalForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    await loadGoal(goalInput.value.trim() || null);
    await loadDecisions();
  });
  decForm.addEventListener("submit", (e) => {
    e.preventDefault();
    loadDecisions();
  });
  for (const el of [kindSel, groupSel, channelSel]) el.addEventListener("change", paintDecisions);
  textFilter.addEventListener("input", paintDecisions);
  lookupForm.addEventListener("submit", (e) => {
    e.preventDefault();
    lookup(lookupInput.value);
  });

  await loadGoal(goalId);
  await loadDecisions();
}
