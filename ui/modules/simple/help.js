// The Help pages (Plan M M173): the Glossary (`#/help/glossary`) and What's new (`#/help/whats-new`),
// both opened from the Help (?) menu in the top bar.
//
// The Glossary is Plan M §4.1 as a page: the word the product shows, what it means, and - folded under
// "Technical names", which the visible-text check leaves out - the engine's own names for it, so an
// analyst can match a screen to a report. `GLOSSARY` is a static list for now; once `ui/labels.js`
// (M170) is on main the integrator replaces it with that module's entries (the shape is the same:
// `shown`, `means`, `technical`).
//
// What's new is `whats-new.json` beside this file: a short list of the latest changes, newest first,
// each with a day, a title and one sentence. It is read with one `fetch` and drawn as it is; a file that
// cannot be read gives one calm sentence, never an error screen.
//
// Both builders are pure (`glossaryHtml`, `whatsNewHtml`): what they draw comes from their arguments.

import { emptyState, pageHeader } from "../../components.js"; // Plan M M188 (DEC-1618): the look of Help
import { announceStatus, crumbs, dataTable, esc } from "../../dom.js";

/** Plan M §4.1, one entry per concept: what a marketer sees, what it means, and the engine's names. */
export const GLOSSARY = Object.freeze([
  {
    shown: "Goal",
    means: "What you want a campaign to achieve, such as winning back customers who have lapsed.",
    technical: ["use case"],
  },
  {
    shown: "Contact list",
    means: "The one file of customers to contact, with each customer's priority, offer and channel. Send it to your sending tool.",
    technical: ["treat list", "scored list", "treat = 1", "holdout = 1", "explore = 1"],
  },
  {
    shown: "Refresh the list",
    means: "Make a new contact list from today's customer data.",
    technical: ["run", "train", "score", "scoring run"],
  },
  {
    shown: "Comparison group",
    means: "Customers who are deliberately not contacted, so you can measure what the campaign really changed.",
    technical: ["holdout", "control group", "explore slice"],
  },
  {
    shown: "The effect of contacting them",
    means: "How much a contact changes what a customer does, as opposed to what they would have done anyway.",
    technical: ["uplift", "campaign-effect model", "X-learner", "S-learner", "T-learner", "AUUC", "Qini"],
  },
  {
    shown: "Likelihood",
    means: "How likely a customer is to buy, respond or leave, whether or not you contact them.",
    technical: ["propensity", "score", "probability"],
  },
  {
    shown: "Priority (High, Medium, Low)",
    means: "How early a customer should be contacted.",
    technical: ["band"],
  },
  {
    shown: "Current model and New candidate",
    means: "The model in use now, and a newly trained one that is waiting for approval.",
    technical: ["champion", "challenger"],
  },
  {
    shown: "Proven, Estimated and Not yet proven",
    means:
      "Proven means the result was measured against a randomly chosen comparison group. Estimated means it is the model's prediction. Not yet proven means there are not enough results to say.",
    technical: ["causal", "evidence label"],
  },
  {
    shown: "What drives it",
    means: "The main reasons behind a customer's priority, in plain words.",
    technical: ["SHAP", "feature importance"],
  },
  {
    shown: "Customers have changed since the model was trained",
    means: "A warning that today's customers look different from the ones the model learned from, so its choices may be less reliable.",
    technical: ["drift", "PSI"],
  },
  {
    shown: "One message per customer",
    means: "When a customer qualifies for several campaigns, they receive only one of them.",
    technical: ["arbitration"],
  },
  {
    shown: "The fixes you approved",
    means: "The data corrections you accepted, applied the same way every time.",
    technical: ["recipe"],
  },
]);

const page = (head, body) => `<main class="screen sp st" data-module="simple" data-help>${head}${body}</main>`;

/** The magnifier of the search box (a line icon on the look's 24px grid; it is decoration, the box has its own label). */
const SEARCH_ICON =
  '<svg class="ds-ico" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" focusable="false" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>';

/** The Glossary page. `entries` are `{ shown, means, technical[] }`. */
export function glossaryHtml(entries = GLOSSARY) {
  const head = pageHeader({
    title: "Glossary",
    subtitle: "The words this product uses, and what each one means.",
    crumbs: crumbs([{ label: "Glossary" }]),
  });
  const words = dataTable(
    [{ label: "Word" }, { label: "What it means" }],
    entries.map((e) => [`<strong>${esc(e.shown)}</strong>`, esc(e.means)]),
    { cls: "sp-glossary" },
  );
  const technical = dataTable(
    [{ label: "Word" }, { label: "Technical names" }],
    entries.map((e) => [esc(e.shown), esc((e.technical || []).join(", "))]),
  );
  const search = `<label class="gl-search">${SEARCH_ICON}<span class="sr">Search the glossary</span><input type="search" data-glossary-filter placeholder="Search the glossary" autocomplete="off"></label>`;
  return page(
    head,
    `<section class="card" data-glossary>${search}${words}<p class="gl-none" data-glossary-none hidden>No word matches your search.</p></section>
    <section class="card sp-group" data-technical><details class="tech" data-technical><summary>Technical names</summary>${technical}</details></section>`,
  );
}

/**
 * Show only the glossary's rows whose word or meaning holds what was typed (case does not matter; the engine's names are not
 * searched, they are not shown); an empty search shows
 * them all. Says how many are left to screen readers. Nothing is removed: the rows are only hidden, so the page still holds them.
 */
export function filterGlossary(root, query) {
  const needle = String(query || "").trim().toLowerCase();
  const rows = [...root.querySelectorAll("tbody tr")];
  let shown = 0;
  for (const row of rows) {
    const match = !needle || row.textContent.toLowerCase().includes(needle);
    row.hidden = !match;
    if (match) shown += 1;
  }
  const none = root.querySelector("[data-glossary-none]");
  if (none) none.hidden = shown > 0;
  return shown;
}

// One delegated listener, as the other pages of this module do: the Help page is drawn by innerHTML, so nothing is bound to it.
if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
  document.addEventListener("input", (event) => {
    const input = event.target;
    if (!input || typeof input.matches !== "function" || !input.matches("[data-glossary-filter]")) return;
    const root = input.closest("[data-glossary]");
    if (!root) return;
    const shown = filterGlossary(root, input.value);
    announceStatus(shown === 1 ? "1 word" : `${shown} words`);
  });
}

/** What's new. `entries` are `{ when, title, text }`, newest first; none gives one calm sentence. */
export function whatsNewHtml(entries) {
  const head = pageHeader({
    title: "What's new",
    subtitle: "The latest changes to Marketing AI, newest first.",
    crumbs: crumbs([{ label: "What's new" }]),
  });
  const list = Array.isArray(entries) ? entries : [];
  if (!list.length) {
    return page(
      head,
      `<section class="card">${emptyState({
        icon: "inbox",
        title: "Nothing to show yet",
        text: "Changes to Marketing AI are listed here as they are made.",
        action: { label: "Go to Home", href: "#/", kind: "secondary" },
      })}</section>`,
    );
  }
  const items = list
    .map(
      (e) =>
        `<li class="sp-entry st-entry" data-news><span class="sp-when">${esc(e.when || "")}</span><strong>${esc(e.title || "")}</strong><span class="sp-text">${esc(
          e.text || "",
        )}</span></li>`,
    )
    .join("");
  return page(head, `<section class="card sp-group"><ul class="sp-list st-news" data-whats-new>${items}</ul></section>`);
}

/** The entries of `whats-new.json`, or `[]` when it cannot be read. */
export async function loadWhatsNew() {
  try {
    const response = await fetch(new URL("./whats-new.json", import.meta.url).href);
    if (!response.ok) return [];
    const body = await response.json();
    return Array.isArray(body && body.entries) ? body.entries : [];
  } catch {
    return [];
  }
}

/** The Help menu's two entries, as the top bar's `help` slot draws them. */
export const glossaryEntry = () => `<a href="#/help/glossary">Glossary</a>`;
export const whatsNewEntry = () => `<a href="#/help/whats-new">What's new</a>`;
