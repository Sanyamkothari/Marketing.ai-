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

import { crumbs, dataTable, emptyState, esc, pageHead } from "../../dom.js";

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

const page = (inner, body) => `<main class="screen sp" data-module="simple" data-help>${pageHead(inner)}${body}</main>`;

/** The Glossary page. `entries` are `{ shown, means, technical[] }`. */
export function glossaryHtml(entries = GLOSSARY) {
  const head = `${crumbs([{ label: "Glossary" }])}<h1 class="h1">Glossary</h1><p class="desc">The words this product uses, and what each one means.</p>`;
  const words = dataTable(
    [{ label: "Word" }, { label: "What it means" }],
    entries.map((e) => [`<strong>${esc(e.shown)}</strong>`, esc(e.means)]),
    { cls: "sp-glossary" },
  );
  const technical = dataTable(
    [{ label: "Word" }, { label: "Technical names" }],
    entries.map((e) => [esc(e.shown), esc((e.technical || []).join(", "))]),
  );
  return page(
    head,
    `<section class="card" data-glossary>${words}</section>
    <section class="card sp-group"><details class="tech" data-technical><summary>Technical names</summary>${technical}</details></section>`,
  );
}

/** What's new. `entries` are `{ when, title, text }`, newest first; none gives one calm sentence. */
export function whatsNewHtml(entries) {
  const head = `${crumbs([{ label: "What's new" }])}<h1 class="h1">What's new</h1><p class="desc">The latest changes to Marketing AI, newest first.</p>`;
  const list = Array.isArray(entries) ? entries : [];
  if (!list.length) {
    return page(
      head,
      `<section class="card">${emptyState({
        title: "Nothing to show yet",
        text: "Changes to Marketing AI are listed here as they are made.",
        action: { label: "Go to Home", href: "#/", kind: "secondary" },
      })}</section>`,
    );
  }
  const items = list
    .map(
      (e) =>
        `<li class="sp-entry" data-news><span class="sp-when">${esc(e.when || "")}</span><strong>${esc(e.title || "")}</strong><span class="sp-text">${esc(
          e.text || "",
        )}</span></li>`,
    )
    .join("");
  return page(head, `<section class="card sp-group"><ul class="sp-list" data-whats-new>${items}</ul></section>`);
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
