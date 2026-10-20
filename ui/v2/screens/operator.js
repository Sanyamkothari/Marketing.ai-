/* Operator mode: power tools for us, acting as the growth engineer for an early client
   (plan-v2.md section 9).

   Three tools on one page:
     - the raw JSON of the goal, its brief, its content library and its proof, exactly as the API sent it;
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
  empty,
  ensureStyles,
  errText,
  failed,
  groupLabel,
  h,
  money,
  newestFirst,
  notFound,
  notify,
  num,
  pageHead,
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

/** A collapsible raw-JSON block with its own copy button. */
function jsonBlock(title, value, ctx, announcer, open = false) {
  const text = pretty(value);
  const copy = h("button", { type: "button", class: "ap-btn ap-btn-quiet ap-btn-sm" }, "Copy JSON");
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      notify(ctx, `${title} copied.`, "good", announcer);
    } catch (e) {
      notify(ctx, `Couldn't copy: ${errText(e)}`, "critical", announcer);
    }
  });
  return h("details", { class: "ap-json", open }, h("summary", null, title), h("div", { class: "ap-actions" }, copy), h("pre", { class: "ap-pre", tabindex: "0" }, text));
}

function field(label, control) {
  return h("label", { class: "ap-field" }, h("span", { class: "ap-field-label" }, label), control);
}

function select(options, value = "") {
  const el = h("select", { class: "ap-input" }, ...options.map(([v, text]) => h("option", { value: v }, text)));
  el.value = value;
  return el;
}

export async function render(root, ctx) {
  ensureStyles();
  const api = ctx.api;
  const announcer = h("p", { class: "ap-sr", role: "status", "aria-live": "polite" });
  const page = h("div", { class: "ap-page ap-operator" });
  root.replaceChildren(page);
  page.append(pageHead("Operator", "Raw data and power tools for the people running the agent. Everything here is exactly what the API returned."));

  // ---- goal picker + raw JSON
  const goalInput = h("input", { class: "ap-input ap-mono", id: "ap-op-goal", autocomplete: "off", spellcheck: "false" });
  const goalForm = h("form", { class: "ap-filters" }, field("Goal id", goalInput), h("button", { type: "submit", class: "ap-btn" }, "Load"));
  const goalOut = h("div", null, h("p", { class: "ap-muted" }, "Loading…"));
  page.append(h("section", { class: "ap-card" }, h("h2", { class: "ap-h2" }, "Goal, brief and content"), goalForm, goalOut));

  // ---- decisions
  const custFilter = h("input", { class: "ap-input ap-mono", autocomplete: "off", spellcheck: "false", placeholder: "any" });
  const limitSel = select(LIMITS.map((n) => [String(n), String(n)]), "200");
  const kindSel = select([["", "Any"], ["act", "Act"], ["nothing", "Left alone"]]);
  const groupSel = select([["", "Any"], ["agent", "Agent"], ["baseline", "Simple rule"], ["holdout", "Holdout"]]);
  const channelSel = select([["", "Any"], ...["email", "whatsapp", "sms", "push", "web", "webhook"].map((c) => [c, channelLabel(c)])]);
  const textFilter = h("input", { class: "ap-input", type: "search", autocomplete: "off", placeholder: "words in the reason, or an id" });
  const decForm = h(
    "form",
    { class: "ap-filters" },
    field("Customer id", custFilter),
    field("How many", limitSel),
    field("Kind", kindSel),
    field("Group", groupSel),
    field("Channel", channelSel),
    field("Search", textFilter),
    h("button", { type: "submit", class: "ap-btn" }, "Fetch"),
  );
  const decCount = h("p", { class: "ap-muted ap-small", "aria-live": "polite" });
  const decTable = h("div", { class: "ap-table-wrap" });
  const decDetail = h("div");
  page.append(h("section", { class: "ap-card" }, h("h2", { class: "ap-h2" }, "Decisions"), decForm, decCount, decTable, decDetail));

  // ---- customer lookup
  const lookupInput = h("input", { class: "ap-input ap-mono", autocomplete: "off", spellcheck: "false", required: true, placeholder: "cust_…" });
  const lookupForm = h("form", { class: "ap-filters" }, field("Customer id", lookupInput), h("button", { type: "submit", class: "ap-btn" }, "Look up"));
  const lookupOut = h("div");
  page.append(h("section", { class: "ap-card" }, h("h2", { class: "ap-h2" }, "Customer lookup"), lookupForm, lookupOut), announcer);

  let bundle = null;
  let goalId = present(ctx.goalId) ? ctx.goalId : null;
  let decisions = [];

  const brief = () => bundle?.brief || null;
  const currency = () => bundle?.goal?.currency;

  async function loadGoal(id) {
    goalOut.replaceChildren(h("p", { class: "ap-muted" }, "Loading…"));
    try {
      bundle = await resolveGoal({ ...ctx, goalId: id });
    } catch (e) {
      bundle = null;
      goalOut.replaceChildren(failed(present(id) ? `goal ${id}` : "a goal", e));
      return;
    }
    if (!bundle) {
      goalOut.replaceChildren(empty("No goals yet."));
      return;
    }
    goalId = bundle.goal?.goal_id ?? id;
    goalInput.value = goalId || "";
    goalOut.replaceChildren(
      h("p", { class: "ap-small" }, `Status ${bundle.goal?.status ?? DASH} · autonomy ${bundle.goal?.autonomy ?? DASH} · currency ${bundle.goal?.currency ?? DASH}`),
      jsonBlock("Goal", bundle.goal, ctx, announcer, true),
      jsonBlock("Brief", bundle.brief, ctx, announcer),
      jsonBlock(`Content (${(bundle.content || []).length})`, bundle.content, ctx, announcer),
      jsonBlock("Proof", bundle.proof, ctx, announcer),
    );
  }

  function paintDecisions() {
    const rows = filterDecisions(decisions, { kind: kindSel.value, group: groupSel.value, channel: channelSel.value, text: textFilter.value });
    decCount.textContent = `Showing ${count(rows.length)} of ${count(decisions.length)} fetched`;
    if (!rows.length) {
      decTable.replaceChildren(empty("No decisions match."));
      return;
    }
    const head = ["Made", "Customer", "Kind", "Group", "Action", "Prob.", "Exp. net", "Attention price", "Policy", "Reason", ""];
    const table = h(
      "table",
      { class: "ap-table" },
      h("thead", null, h("tr", null, ...head.map((t) => h("th", { scope: "col" }, t)))),
      h("tbody", null, ...rows.map((d) => {
        const who = h("button", { type: "button", class: "ap-link ap-mono" }, d.customer_id);
        who.addEventListener("click", () => {
          lookupInput.value = d.customer_id;
          lookup(d.customer_id);
        });
        const view = h("button", { type: "button", class: "ap-btn ap-btn-quiet ap-btn-sm", "aria-label": `Show JSON of ${d.decision_id}` }, "JSON");
        view.addEventListener("click", () => {
          decDetail.replaceChildren(jsonBlock(`Decision ${d.decision_id}`, d, ctx, announcer, true));
        });
        return h(
          "tr",
          null,
          h("td", { class: "ap-nowrap" }, when(d.made_at)),
          h("td", null, who),
          h("td", null, d.kind === "act" ? "Act" : "Left alone"),
          h("td", null, groupLabel(d.group)),
          h("td", null, describeAction(d, brief())),
          h("td", { class: "ap-num" }, num(d.probability, 3)),
          h("td", { class: "ap-num" }, money(d.expected_net, currency())),
          h("td", { class: "ap-num" }, money(d.attention_price, currency())),
          h("td", { class: "ap-mono" }, d.policy_version ?? DASH),
          h("td", { class: "ap-reason" }, d.reason || DASH, (d.blocked_by || []).length > 0 && h("span", { class: "ap-muted ap-small" }, ` [ruled out: ${d.blocked_by.join(", ")}]`)),
          h("td", null, view),
        );
      })),
    );
    decTable.replaceChildren(table);
  }

  async function loadDecisions() {
    decTable.replaceChildren(h("p", { class: "ap-muted" }, "Loading…"));
    decDetail.replaceChildren();
    const query = { limit: Number(limitSel.value) };
    if (present(goalId)) query.goal_id = goalId;
    if (custFilter.value.trim()) query.customer_id = custFilter.value.trim();
    try {
      decisions = (await call(api, "listDecisions", query)) || [];
    } catch (e) {
      decisions = [];
      decCount.textContent = "";
      decTable.replaceChildren(failed("decisions", e));
      return;
    }
    paintDecisions();
    decDetail.replaceChildren(jsonBlock(`All fetched decisions (${decisions.length})`, decisions, ctx, announcer));
  }

  async function lookup(id) {
    const cid = String(id || "").trim();
    if (!cid) {
      lookupOut.replaceChildren(h("p", { class: "ap-error", role: "alert" }, "Type a customer id first."));
      return;
    }
    lookupOut.replaceChildren(h("p", { class: "ap-muted" }, "Loading…"));
    let found;
    try {
      found = await call(api, "getCustomer", cid);
    } catch (e) {
      lookupOut.replaceChildren(notFound(e) ? empty(`No customer ${cid}.`) : failed(`customer ${cid}`, e));
      return;
    }
    if (!found || !found.state) {
      lookupOut.replaceChildren(empty(`No customer ${cid}.`));
      return;
    }
    const rows = timeline(found).map(({ at, kind, item }) =>
      h(
        "tr",
        null,
        h("td", { class: "ap-nowrap" }, when(at)),
        h("td", null, kind === "event" ? "Event" : "Decision"),
        h("td", null, kind === "event" ? describeEvent(item, currency()) : describeAction(item, brief())),
        h("td", { class: "ap-mono" }, kind === "event" ? item.decision_id ?? DASH : item.decision_id),
        h("td", { class: "ap-reason" }, kind === "decision" ? item.reason || DASH : item.source ?? DASH),
      ),
    );
    lookupOut.replaceChildren(
      h("p", { class: "ap-small" }, `${found.state.customer_id} · ${groupLabel(found.state.group)} · ${count(found.state.purchases)} purchases · ${money(found.state.revenue, currency())} revenue · state version ${found.state.version ?? DASH}`),
      rows.length
        ? h("div", { class: "ap-table-wrap" }, h("table", { class: "ap-table" },
            h("thead", null, h("tr", null, ...["When", "Type", "What", "Decision", "Why / source"].map((t) => h("th", { scope: "col" }, t)))),
            h("tbody", null, ...rows)))
        : empty("No events or decisions."),
      jsonBlock("State", found.state, ctx, announcer),
      jsonBlock(`Decisions (${(found.decisions || []).length})`, found.decisions, ctx, announcer),
      jsonBlock(`Events (${(found.events || []).length})`, found.events, ctx, announcer),
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
