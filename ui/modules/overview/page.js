// The Overview page (Plan M M175, DEC-1605; `#/overview`): the first thing a marketer sees.
//
// Four parts and a checklist, in this order, all from `GET /overview` (one call, built from the sources the Results
// page and the campaign list already read; nothing is computed here):
//   1. "Needs your attention": up to five cards, each with ONE action;
//   2. "Value proven to date": one headline, a floor, with one sentence of what it means and "How this is counted";
//   3. "Live campaigns": up to five rows (short name, status, next step), each a link to its campaign;
//   4. the one primary button, "+ New campaign" (in the page header, where every page keeps its one primary button;
//      DEC-1605 records this as the one place the plan's order is not followed to the letter);
//   5. the first-visit checklist (connect or upload data, start the first campaign, add its results): ticked from real
//      state, hidden once all three are done or the person hid it.
//
// The builders are pure (`overviewHtml`, `cardAction`, `shortName`, `nextStepText`): what they draw comes from their
// arguments, so the jsdom tests call them with recorded or stubbed answers. Words follow Plan M §4.1. Numbers and days
// are `ui/format.js`'s (M172): the headline is the server's figure drawn by `figureHtml` (rupees in lakh and crore, the
// exact figure in the tooltip). A campaign's recorded name is the person's own text, marked `data-user-text` (M172).
// Anything technical goes in a `class="tech"` section (M180 hides it); this page has none: it shows no engine word.

import { crumbs, EM_DASH, emptyState, errorBox, esc, fmtInt, headActions, pageHead } from "../../dom.js";
import { day, figureHtml } from "../../format.js";
import { findNeverShown, term, USER_TEXT_ATTRIBUTE } from "../../labels.js";

/** Where the page sends a person; `places` lets the shell name a screen a branch has not merged yet. */
export const DEFAULT_PLACES = Object.freeze({
  newCampaign: "#/campaigns/new",
  campaigns: "#/campaigns",
  data: "#/data",
  reports: "#/reports",
  results: "#/results",
  approvals: "#/approvals",
});

const campaignHref = (id) => `#/campaigns/${encodeURIComponent(id)}/overview`;

// --- words and numbers (ui/format.js, M172) ---------------------------------------------------------------------

const plural = (n, one, many) => `${fmtInt(n)} ${n === 1 ? one : many}`;

/** A campaign's name for a list: the part before a dash or colon, at most 40 characters (M172 generates real short names). */
export function shortName(name) {
  const full = String(name || "").trim();
  if (!full) return EM_DASH;
  const first = full.split(/\s[-–—]\s|:\s/)[0].trim() || full;
  return first.length > 40 ? `${first.slice(0, 39).trimEnd()}…` : first;
}

// The four chips of the one status rule (Plan M DEC-1606 (b)), worded as the Campaigns pages word them
// (ui/modules/campaigns/status.js STATUS). Only campaigns not yet measured are listed here.
const STATUS = {
  draft: ["Draft", "neutral"],
  live: ["Live", "ok"],
  waiting_for_results: ["Waiting for results", "warn"],
  measured: ["Measured", "ok"],
};

/** The sentence for a campaign's next step, from the server's code and date. */
export function nextStepText(campaign) {
  const shown = day(campaign.results_date);
  const due = shown === EM_DASH ? "" : shown;
  // A Draft's next step is the Campaigns page's: how the result will be judged is not fixed yet (DEC-1606 (b)).
  if (campaign.status === "draft") return "Fix how the result will be judged, then download the contact list";
  switch (campaign.next_step) {
    case "read":
      return "See its result";
    case "wait":
      return due ? `Add its results after ${due}` : "Add its results when they are due";
    case "add_results":
      return "Add its results";
    case "measure":
      return "Measure its results";
    case "plan":
      return "Set the date its results are due";
    default:
      return EM_DASH;
  }
}

// --- 1. needs your attention ------------------------------------------------------------------------------------

// The cards' sentences are the server's (`engine/measurement/summary.py`) and still hold some engine words: M172 checks
// and rewords them at the source. Until then this page puts the glossary's own word for the nouns it has one for, and
// leaves out a supporting sentence that still holds an engine word after that (the card keeps its title and its one
// action). Once a sentence is clean at the source, it appears here with no change.
const PLAIN = [
  [/\buse cases?\b/gi, (m) => (/s$/i.test(m) ? "goals" : term("goal").toLowerCase())],
  [/\b(control groups?|holdouts?|hold-outs?)\b/gi, (m) => `${term("comparisonGroup").toLowerCase()}${/s$/i.test(m) ? "s" : ""}`],
  [/\bchampion\b/gi, () => term("currentModel").toLowerCase()],
  [/\bchallenger\b/gi, () => term("newCandidate").toLowerCase()],
  [/\bcausal\b/gi, () => term("evidenceProven").toLowerCase()],
];

/** A server sentence with the glossary's word for each noun it has one for; the first letter keeps its case. */
export function plainWords(sentence) {
  let out = String(sentence || "");
  for (const [pattern, word] of PLAIN) {
    out = out.replace(pattern, (m) => {
      const w = word(m);
      return m[0] === m[0].toUpperCase() && m[0] !== m[0].toLowerCase() ? `${w[0].toUpperCase()}${w.slice(1)}` : w;
    });
  }
  return out;
}

/** The sentence in plain words, or "" when it still holds an engine word the glossary has no replacement for. */
export function showable(sentence) {
  const plain = plainWords(sentence);
  return Object.keys(findNeverShown(plain)).length ? "" : plain;
}

/** The ONE action of a card: `{ label, href }`, chosen from what the card is about. */
export function cardAction(card, places = DEFAULT_PLACES) {
  if (card.kind === "challenger_ready") return { label: `Review the ${term("newCandidate").toLowerCase()}`, href: places.approvals };
  if (card.kind === "backfire" && card.campaign_id) {
    return { label: "Open the Value Proof Pack", href: `#/pilot/proof/${encodeURIComponent(card.campaign_id)}` };
  }
  if (card.campaign_id) return { label: "Open the campaign", href: campaignHref(card.campaign_id) };
  if (card.use_case_id) return { label: `Open the ${term("goal").toLowerCase()}`, href: `#/uc/${encodeURIComponent(card.use_case_id)}` };
  return { label: "See all results", href: places.results };
}

function cardHtml(card, places) {
  const action = cardAction(card, places);
  const who = card.campaign_name && card.campaign_name.text ? shortName(card.campaign_name.text) : "";
  const full = card.campaign_name && card.campaign_name.text ? card.campaign_name.text : "";
  const text = showable(card.text);
  const next = showable(card.next_step);
  return `<li class="ov-card ${esc(card.severity || "info")}" data-card="${esc(card.code)}"${
    card.campaign_id ? ` data-card-campaign="${esc(card.campaign_id)}"` : ""
  }><div class="ov-card-body"><p class="ov-card-title">${esc(plainWords(card.title))}</p>${
    who ? `<p class="ov-card-who" title="${esc(full)}" ${USER_TEXT_ATTRIBUTE}>${esc(who)}</p>` : ""
  }${text ? `<p class="ov-card-text">${esc(text)}</p>` : ""}${next ? `<p class="ov-card-next">${esc(next)}</p>` : ""}</div><a class="btn secondary sm" href="${esc(
    action.href,
  )}" data-card-action>${esc(action.label)}</a></li>`;
}

function attentionHtml(overview, places) {
  const cards = overview.cards || [];
  let body;
  if (!overview.summary_checked) {
    body = `<p class="ov-note" data-unchecked>Some figures could not be checked against their records, so this list is not shown. Your campaigns below are unaffected.</p>`;
  } else if (!cards.length) {
    body = emptyState({
      title: "Nothing needs your attention",
      text: "When a campaign needs a decision, it appears here with the one thing to do.",
    });
  } else {
    const more = Math.max(0, (overview.cards_total || cards.length) - cards.length);
    body = `<ul class="ov-cards">${cards.map((card) => cardHtml(card, places)).join("")}</ul>${
      more
        ? `<p class="ov-more" data-more-cards>${esc(plural(more, "more item needs", "more items need"))} your attention. <a href="${esc(
            places.results,
          )}">See all</a></p>`
        : ""
    }`;
  }
  return `<section class="card ov-attention" data-attention aria-labelledby="ov-h-attention"><h2 class="ov-h" id="ov-h-attention">Needs your attention</h2>${body}</section>`;
}

// --- 2. value proven to date ---------------------------------------------------------------------------------------

/** What a total that is not in rupees is counted in, when the server's words hold an engine word: its kind alone. */
const UNIT_FALLBACK = { outcomes: "extra outcomes", prevented: "outcomes prevented", amount: "in total" };

/** The unit after the number, in plain words: the server's own, else the kind of unit alone. Rupees carry their own sign. */
export function unitText(headline) {
  if (headline.unit === "rupees") return "";
  const words = showable(headline.unit_words);
  return words || UNIT_FALLBACK[String(headline.unit).split(":")[0]] || "";
}

function valueHtml(overview, places) {
  const headline = overview.headline;
  const link = `<a class="ov-link" href="${esc(places.reports)}" data-how-counted>How this is counted</a>`;
  let body;
  if (!overview.summary_checked) {
    body = `<p class="ov-note">This figure is not shown because it could not be checked against its records.</p>`;
  } else if (!headline) {
    // No total can mean two things: nothing has results yet, or every campaign with results is set apart or not final.
    const hasResults = Boolean(overview.checklist && overview.checklist.results);
    const why = hasResults
      ? "None of your measured campaigns is counted as proven yet. How this is counted explains why."
      : "Nothing is proven yet. Once a campaign's results are added, the least it earned appears here.";
    body = `<p class="ov-amount" data-value-empty>${EM_DASH}</p><p class="ov-meaning" data-value-meaning>${esc(why)}</p>`;
  } else {
    const rupees = headline.unit === "rupees";
    const fallback = rupees
      ? "A floor, not a forecast: the least your measured campaigns earned, after the cost of contacts and offers."
      : "A floor, not a forecast: the least your measured campaigns achieved, added up.";
    const sentence = `${headline.label.charAt(0).toUpperCase()}${headline.label.slice(1)}.`;
    const meaning = rupees ? fallback : showable(sentence) || fallback;
    const unit = unitText(headline);
    const counted = headline.campaigns_counted
      ? ` <span class="ov-based">Based on ${esc(plural(headline.campaigns_counted, "measured campaign", "measured campaigns"))}.</span>`
      : "";
    body = `<p class="ov-amount" data-value-amount title="${esc(headline.total.text)}"><span class="ov-atleast">At least</span> ${figureHtml(
      headline.total,
    )}${unit ? ` <span class="ov-unit">${esc(unit)}</span>` : ""}</p><p class="ov-meaning" data-value-meaning>${esc(meaning)}${counted}</p>`;
  }
  return `<section class="card ov-value" data-value aria-labelledby="ov-h-value"><h2 class="ov-h" id="ov-h-value">Value proven to date</h2><div class="ov-value-body">${body}${link}</div></section>`;
}

// --- 3. live campaigns -------------------------------------------------------------------------------------------

function liveHtml(overview, places) {
  const rows = overview.campaigns || [];
  let body;
  if (!rows.length) {
    body = overview.checklist && overview.checklist.campaign
      ? emptyState({
          title: "No campaign is live right now",
          text: "Campaigns that are measured are on the Campaigns page.",
          action: { label: "See all campaigns", href: places.campaigns, kind: "secondary" },
        })
      : emptyState({
          title: "You have no campaigns yet",
          text: "Choose + New campaign to plan your first one.",
        });
  } else {
    const items = rows
      .map((campaign) => {
        const [label, tone] = STATUS[campaign.status] || [String(campaign.status), "neutral"];
        return `<li class="ov-row" data-live-row="${esc(campaign.campaign_id)}"><a class="ov-row-name" href="${esc(
          campaignHref(campaign.campaign_id),
        )}" title="${esc(campaign.name)}"><span ${USER_TEXT_ATTRIBUTE}>${esc(shortName(campaign.name))}</span></a><span class="pill ${esc(tone)}">${esc(
          label,
        )}</span><span class="ov-row-next">${esc(nextStepText(campaign))}</span></li>`;
      })
      .join("");
    const more = Math.max(0, (overview.campaigns_total || rows.length) - rows.length);
    body = `<ul class="ov-rows">${items}</ul>${
      more
        ? `<p class="ov-more" data-more-campaigns><a href="${esc(places.campaigns)}">${esc(plural(more, "more live campaign", "more live campaigns"))}</a></p>`
        : ""
    }`;
  }
  return `<section class="card ov-live" data-live aria-labelledby="ov-h-live"><h2 class="ov-h" id="ov-h-live">Live campaigns</h2>${body}</section>`;
}

// --- 5. the first-visit checklist ------------------------------------------------------------------------------

/** The three items, with whether each is done (read from the server's real state) and where it leads. */
export function checklistItems(checklist, places = DEFAULT_PLACES) {
  const c = checklist || {};
  return [
    { id: "connected", label: "Connect or upload your data", done: Boolean(c.connected), href: places.data },
    { id: "campaign", label: "Start your first campaign", done: Boolean(c.campaign), href: places.newCampaign },
    { id: "results", label: "Add its results", done: Boolean(c.results), href: places.campaigns },
  ];
}

/** Whether the checklist is drawn: not when all three are done, and not when the person hid it. */
export const showChecklist = (checklist, hidden) => !hidden && checklistItems(checklist).some((item) => !item.done);

function checklistHtml(overview, places) {
  const items = checklistItems(overview.checklist, places)
    .map(
      (item) =>
        `<li class="ov-check${item.done ? " done" : ""}" data-check="${esc(item.id)}" data-done="${item.done}"><span class="ov-tick" aria-hidden="true">${
          item.done ? "✓" : "○"
        }</span>${
          item.done
            ? `<span class="ov-check-label">${esc(item.label)}</span><span class="sr">Done</span>`
            : `<a class="ov-check-label" href="${esc(item.href)}">${esc(item.label)}</a><span class="sr">To do</span>`
        }</li>`,
    )
    .join("");
  return `<section class="card ov-checklist" data-checklist aria-labelledby="ov-h-check"><div class="ov-check-head"><h2 class="ov-h" id="ov-h-check">Getting started</h2><button type="button" class="btn quiet sm" data-checklist-hide>Hide this list</button></div><ol class="ov-checks">${items}</ol></section>`;
}

// --- the page ---------------------------------------------------------------------------------------------------------

/**
 * The Overview. `overview` is `GET /overview`'s answer; `error` its failure; `places` where its links lead;
 * `checklistHidden` whether the person hid the first-visit checklist.
 */
export function overviewHtml({ overview = null, error = null, places = DEFAULT_PLACES, checklistHidden = false } = {}) {
  const head = pageHead(
    `${crumbs([{ label: "Overview" }])}<h1 class="h1">Overview</h1><p class="desc">What needs your attention, what your campaigns have earned, and what to do next.</p>${headActions(
      { primary: { label: "+ New campaign", href: places.newCampaign, attrs: "data-new-campaign" } },
    )}`,
  );
  let body;
  if (error) {
    body = errorBox(error, { retry: true });
  } else if (!overview) {
    body = `<p class="loading" role="status">Loading…</p>`;
  } else {
    body = `<div class="ov-grid">${attentionHtml(overview, places)}${valueHtml(overview, places)}${liveHtml(overview, places)}${
      showChecklist(overview.checklist, checklistHidden) ? checklistHtml(overview, places) : ""
    }</div>`;
  }
  return `<main class="screen ov" data-module="overview">${head}${body}</main>`;
}

// --- styles: tokens only, so both themes apply ---------------------------------------------------------------------

const CSS = `
.ov .ov-grid{display:grid;gap:16px;grid-template-columns:minmax(0,3fr) minmax(0,2fr);grid-template-areas:"attention value" "attention live" "check check";grid-template-rows:auto 1fr auto;align-items:start}
.ov .ov-attention{grid-area:attention}.ov .ov-value{grid-area:value}.ov .ov-live{grid-area:live}.ov .ov-checklist{grid-area:check}
@media (max-width:900px){.ov .ov-grid{grid-template-columns:minmax(0,1fr);grid-template-areas:"attention" "value" "live" "check"}}
.ov .ov-h{margin:0;padding:14px 20px;font-size:15px;font-weight:600;border-bottom:1px solid var(--line)}
.ov .ov-cards,.ov .ov-rows,.ov .ov-checks{list-style:none;margin:0;padding:0}
.ov .ov-card{display:flex;gap:16px;align-items:flex-start;justify-content:space-between;padding:12px 20px;border-top:1px solid var(--line);border-left:3px solid transparent}
.ov .ov-card:first-child{border-top:0}
.ov .ov-card.warning{border-left-color:var(--bad,#b3261e)}
.ov .ov-card-body{min-width:0}
.ov .ov-card-body p{margin:0}
.ov .ov-card-title{font-size:14px;font-weight:600;color:var(--ink)}
.ov .ov-card-who{font-size:12px;color:var(--muted);margin-top:2px}
.ov .ov-card-text{font-size:13px;color:var(--ink2);line-height:1.45;margin-top:4px}
.ov .ov-card-next{font-size:13px;font-style:italic;color:var(--ink2);margin-top:4px}
.ov .ov-card .btn{flex:none}
.ov .ov-more,.ov .ov-note{margin:0;padding:10px 20px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}
.ov .ov-more a,.ov .ov-link,.ov .ov-row-name,.ov .ov-check-label[href]{color:var(--brand-blue);font-weight:600;text-decoration:none}
.ov .ov-more a:hover,.ov .ov-link:hover,.ov .ov-row-name:hover,.ov .ov-check-label[href]:hover{text-decoration:underline}
.ov .ov-value-body{padding:14px 20px 16px;display:flex;flex-direction:column;gap:6px}
.ov .ov-amount{margin:0;font-size:30px;font-weight:600;line-height:1.15;font-feature-settings:"tnum";color:var(--ink)}
.ov .ov-atleast,.ov .ov-unit{font-size:13px;font-weight:500;color:var(--muted)}
.ov .ov-atleast{margin-right:4px}
.ov .ov-meaning{margin:0;font-size:13px;color:var(--ink2);line-height:1.45}
.ov .ov-based{color:var(--muted)}
.ov .ov-link{font-size:13px}
.ov .ov-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 12px;align-items:center;padding:10px 20px;border-top:1px solid var(--line)}
.ov .ov-row:first-child{border-top:0}
.ov .ov-row-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ov .ov-row-next{grid-column:1 / -1;font-size:13px;color:var(--ink2)}
.ov .ov-check-head{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);padding-right:12px}
.ov .ov-check-head .ov-h{border-bottom:0}
.ov .ov-checks{display:flex;flex-wrap:wrap;gap:8px 32px;padding:12px 20px}
.ov .ov-check{display:flex;gap:8px;align-items:center;font-size:14px}
.ov .ov-tick{width:20px;height:20px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:13px;color:var(--muted);border:1px solid var(--line2)}
.ov .ov-check.done .ov-tick{background:var(--ok-t);color:var(--ok);border-color:var(--ok)}
.ov .ov-check.done .ov-check-label{color:var(--muted)}
.ov .empty-state{padding:20px}
`;

export function injectStyles(doc = document) {
  if (!doc || !doc.head || doc.getElementById("ov-styles")) return;
  const style = doc.createElement("style");
  style.id = "ov-styles";
  style.textContent = CSS;
  doc.head.appendChild(style);
}
