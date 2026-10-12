// The Overview page (Plan M M175, DEC-1605; `#/overview`): the first thing a marketer sees.
//
// Four parts and a checklist, all from `GET /overview` (one call, built from the sources the Results page and the
// campaign list already read; nothing is computed here):
//   1. "Needs your attention": up to five attention items, each with ONE action;
//   2. "Value proven to date": one headline, a floor, drawn as the page's hero figure, with one sentence of what it
//      means and "How this is counted";
//   3. "Live campaigns": up to five compact rows (short name, next step, status chip), each a link to its campaign;
//   4. the one primary button, "+ New campaign" (in the page header, where every page keeps its one primary button;
//      DEC-1605 records this as the one place the plan's order is not followed to the letter);
//   5. the first-visit checklist (connect or upload data, start the first campaign, add its results): ticked from real
//      state, hidden once all three are done or the person hid it. While it shows it leads the page, as a progress list.
//
// The look (Plan M M186, DEC-1616) is ui/theme.css's and ui/components.js's: attention items (`attentionItem`), status
// chips (`statusChip`, the one mapping), empty states with an icon (`emptyState`), buttons (`button`) and the value
// range's track (`.vrange`). The page adds only its own grid and the hero, in one sheet (`STYLE_ID`, scoped to `.ovp`).
//
// The builders are pure (`overviewHtml`, `cardAction`, `shortName`, `nextStepText`): what they draw comes from their
// arguments, so the jsdom tests call them with recorded or stubbed answers. Words follow Plan M §4.1. Numbers and days
// are `ui/format.js`'s (M172): the headline is the server's figure drawn by `figureHtml` (rupees in lakh and crore, the
// exact figure in the tooltip). A campaign's recorded name is the person's own text, marked `data-user-text` (M172).
// Anything technical goes in a `class="tech"` section (M180 hides it); this page has none: it shows no engine word.

import { attentionItem, button, emptyState, icon, statusChip } from "../../components.js";
import { EM_DASH, errorBox, esc, fmtInt, headActions, pageHead } from "../../dom.js";
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

/** A card's section head: its title (an H2) and, on the right, what belongs beside it. */
const sectionHead = (id, title, aside = "") =>
  `<div class="ovp-head"><h2 class="ov-h" id="${esc(id)}">${esc(title)}</h2>${aside}</div>`;

/** The server's two severities, as the look's tones: a warning stays a warning, anything else is information. */
const toneOf = (severity) => (severity === "warning" ? "warning" : "info");

function cardHtml(card, places) {
  const action = cardAction(card, places);
  const full = card.campaign_name && card.campaign_name.text ? card.campaign_name.text : "";
  const who = full ? shortName(full) : "";
  // One line: the card's sentence, and its "what to do" sentence when that one is in plain words too.
  const line = [showable(card.text), showable(card.next_step)].filter(Boolean).join(" ");
  const attrs = [
    `role="listitem"`,
    `data-card="${esc(card.code)}"`,
    card.campaign_id ? `data-card-campaign="${esc(card.campaign_id)}"` : "",
  ]
    .filter(Boolean)
    .join(" ");
  return attentionItem({
    tone: toneOf(card.severity),
    title: plainWords(card.title),
    text: line,
    meta: who,
    metaAttrs: who ? `${USER_TEXT_ATTRIBUTE} title="${esc(full)}"` : "",
    action: { label: action.label, href: action.href, size: "sm", attrs: "data-card-action" },
    attrs,
  });
}

function attentionHtml(overview, places) {
  const cards = overview.cards || [];
  let body;
  if (!overview.summary_checked) {
    body = `<p class="ov-note" data-unchecked>Some figures could not be checked against their records, so this list is not shown. Your campaigns below are unaffected.</p>`;
  } else if (!cards.length) {
    body = emptyState({
      icon: "checkCircle",
      title: "Nothing needs your attention",
      text: "When a campaign needs a decision, it appears here with the one thing to do.",
    });
  } else {
    const more = Math.max(0, (overview.cards_total || cards.length) - cards.length);
    body = `<div class="ov-cards" role="list">${cards.map((card) => cardHtml(card, places)).join("")}</div>${
      more
        ? `<p class="ov-more" data-more-cards>${esc(plural(more, "more item needs", "more items need"))} your attention. <a href="${esc(
            places.results,
          )}">See all</a></p>`
        : ""
    }`;
  }
  return `<section class="card ov-attention" data-attention aria-labelledby="ov-h-attention">${sectionHead(
    "ov-h-attention",
    "Needs your attention",
  )}${body}</section>`;
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

/**
 * The hero: "At least" over the figure at 48px. It has no value range bar (M186, DEC-1616 (c)): the headline is a
 * floor, the sum of each measured campaign's lower bound (`engine/measurement/overview.py`), and `GET /overview` sends
 * no upper end, so a bar would invent a scale. The ranges are drawn where the server gives both ends: the Campaigns
 * list and the campaign page (the Value Proof Pack's net value line).
 */
function valueHtml(overview, places) {
  const headline = overview.headline;
  const link = `<a class="ov-link" href="${esc(places.reports)}" data-how-counted>How this is counted${icon("arrowRight", { size: 14 })}</a>`;
  let body;
  if (!overview.summary_checked) {
    body = `<div class="ov-value-body"><p class="ov-note ovp-inline">This figure is not shown because it could not be checked against its records.</p></div>`;
  } else if (!headline) {
    // No total can mean two things: nothing has results yet, or every campaign with results is set apart or not final.
    // No number either way: not a zero, and not a dash standing in for one.
    const hasResults = Boolean(overview.checklist && overview.checklist.results);
    body = hasResults
      ? emptyState({
          icon: "report",
          title: "None of your measured campaigns is counted as proven yet",
          text: "How this is counted explains why.",
          attrs: "data-value-empty",
        })
      : emptyState({
          icon: "report",
          title: "Nothing is proven yet",
          text: "Once a campaign's results are added, the least it earned appears here.",
          attrs: "data-value-empty",
        });
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
    body = `<div class="ov-value-body"><p class="ov-amount" data-value-amount title="${esc(headline.total.text)}"><span class="ov-atleast">At least</span> <span class="ovp-figure"><span class="hero-figure">${figureHtml(
      headline.total,
    )}</span>${unit ? ` <span class="ov-unit">${esc(unit)}</span>` : ""}</span></p><p class="ov-meaning" data-value-meaning>${esc(
      meaning,
    )}${counted}</p></div>`;
  }
  return `<section class="card ov-value" data-value aria-labelledby="ov-h-value">${sectionHead(
    "ov-h-value",
    "Value proven to date",
    link,
  )}${body}</section>`;
}

// --- 3. live campaigns -------------------------------------------------------------------------------------------

function liveHtml(overview, places) {
  const rows = overview.campaigns || [];
  let body;
  if (!rows.length) {
    body =
      overview.checklist && overview.checklist.campaign
        ? emptyState({
            icon: "campaign",
            title: "No campaign is live right now",
            text: "Campaigns that are measured are on the Campaigns page.",
            action: { label: "See all campaigns", href: places.campaigns, kind: "secondary", size: "sm" },
          })
        : emptyState({
            icon: "campaign",
            title: "You have no campaigns yet",
            text: "Choose + New campaign to plan your first one.",
          });
  } else {
    const items = rows
      .map(
        (campaign) =>
          `<li class="ovp-row" data-live-row="${esc(campaign.campaign_id)}"><div class="ovp-row-main"><a class="ov-row-name" href="${esc(
            campaignHref(campaign.campaign_id),
          )}" title="${esc(campaign.name)}"><span ${USER_TEXT_ATTRIBUTE}>${esc(shortName(campaign.name))}</span></a><span class="ov-row-next">${esc(
            nextStepText(campaign),
          )}</span></div>${statusChip("campaign", campaign.status)}</li>`,
      )
      .join("");
    const more = Math.max(0, (overview.campaigns_total || rows.length) - rows.length);
    body = `<ul class="ov-rows">${items}</ul>${
      more
        ? `<p class="ov-more" data-more-campaigns><a href="${esc(places.campaigns)}">${esc(plural(more, "more live campaign", "more live campaigns"))}</a></p>`
        : ""
    }`;
  }
  return `<section class="card ov-live" data-live aria-labelledby="ov-h-live">${sectionHead("ov-h-live", "Live campaigns")}${body}</section>`;
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
  const all = checklistItems(overview.checklist, places);
  const done = all.filter((item) => item.done).length;
  const next = all.findIndex((item) => !item.done);
  const items = all
    .map((item, n) => {
      const state = item.done ? " done" : n === next ? " is-next" : "";
      const tick = item.done ? icon("check", { size: 14 }) : String(n + 1);
      return `<li class="ov-check${state}" data-check="${esc(item.id)}" data-done="${item.done}"><span class="ov-tick" aria-hidden="true">${tick}</span>${
        item.done
          ? `<span class="ov-check-label">${esc(item.label)}</span><span class="sr">Done</span>`
          : `<a class="ov-check-label" href="${esc(item.href)}">${esc(item.label)}</a><span class="sr">To do</span>`
      }</li>`;
    })
    .join("");
  const hide = button({ label: "Hide this list", kind: "tertiary", size: "sm", attrs: "data-checklist-hide" });
  const share = Math.round((done / all.length) * 100);
  return `<section class="card ov-checklist" data-checklist aria-labelledby="ov-h-check">${sectionHead(
    "ov-h-check",
    "Getting started",
    `<span class="ovp-check-aside"><span class="ovp-check-count" data-check-count>${done} of ${all.length} done</span>${hide}</span>`,
  )}<div class="ovp-progress" aria-hidden="true"><span style="width:${share}%"></span></div><ol class="ov-checks">${items}</ol></section>`;
}

// --- the page ---------------------------------------------------------------------------------------------------------

/**
 * The Overview. `overview` is `GET /overview`'s answer; `error` its failure; `places` where its links lead;
 * `checklistHidden` whether the person hid the first-visit checklist.
 */
export function overviewHtml({ overview = null, error = null, places = DEFAULT_PLACES, checklistHidden = false } = {}) {
  // A top-level place: no breadcrumb (visual_common §3). The one primary action sits right of the title.
  const head = pageHead(
    `<h1 class="h1">Overview</h1><p class="desc">What needs your attention, what your campaigns have earned, and what to do next.</p>${headActions(
      { primary: { label: "+ New campaign", href: places.newCampaign, attrs: "data-new-campaign" } },
    )}`,
  );
  let body;
  if (error) {
    body = errorBox(error, { retry: true });
  } else if (!overview) {
    body = `<p class="loading" role="status">Loading…</p>`;
  } else {
    // The checklist leads while it shows (a first visit's guide); attention, value and live keep the plan's order.
    const check = showChecklist(overview.checklist, checklistHidden) ? checklistHtml(overview, places) : "";
    body = `<div class="ov-grid ovp-grid${check ? " has-check" : ""}">${check}${attentionHtml(overview, places)}${valueHtml(
      overview,
      places,
    )}${liveHtml(overview, places)}</div>`;
  }
  return `<main class="screen ovp" data-module="overview">${head}${body}</main>`;
}

// --- styles: the page's grid and hero; everything else is ui/theme.css ----------------------------------------------
// Scoped to `.ovp` so Home's `.ov` rules never reach this page. Text colours are tokens on the card's white or the soft
// panel, which the contrast test checks.

const CSS = `
.ovp .ovp-grid{display:grid;gap:20px;grid-template-columns:minmax(0,1fr) minmax(0,1fr);grid-template-areas:"attention attention" "value live";align-items:stretch}
.ovp .ovp-grid.has-check{grid-template-areas:"check check" "attention attention" "value live"}
.ovp .ov-attention{grid-area:attention}.ovp .ov-value{grid-area:value;align-self:start}.ovp .ov-live{grid-area:live}.ovp .ov-checklist{grid-area:check}
.ovp .card{overflow:hidden}
.ovp .ovp-head{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:56px;padding:12px 20px;border-bottom:1px solid var(--line)}
.ovp .ov-h{margin:0;font-size:var(--fs-card,15px);line-height:var(--lh-card,22px);font-weight:600;letter-spacing:-.005em;color:var(--ink)}
.ovp .ov-cards,.ovp .ov-rows,.ovp .ov-checks{list-style:none;margin:0;padding:0}
.ovp .ov-cards>.attn+.attn{border-top:1px solid var(--line)}
.ovp .ov-cards .attn-text{max-width:80ch}
.ovp .ov-more,.ovp .ov-note{margin:0;padding:12px 20px;border-top:1px solid var(--line);font-size:13px;line-height:20px;color:var(--muted)}
.ovp .ov-note.ovp-inline{padding:0;border:0}
.ovp .ov-more a{color:var(--brand-blue);font-weight:600;text-decoration:none}
.ovp .ov-more a:hover,.ovp .ov-link:hover{text-decoration:underline;text-underline-offset:3px}
.ovp .ov-value-body{display:flex;flex-direction:column;align-items:flex-start;gap:12px;padding:20px}
.ovp .ov-amount{margin:0;display:flex;flex-direction:column;gap:2px;color:var(--ink)}
.ovp .ov-atleast{font-size:13px;line-height:20px;font-weight:500;color:var(--muted)}
.ovp .ovp-figure{display:flex;align-items:baseline;flex-wrap:wrap;gap:4px 10px}
.ovp .hero-figure{font-size:48px;line-height:56px;font-weight:600;letter-spacing:-.03em;font-variant-numeric:proportional-nums;color:var(--ink)}
.ovp .ov-unit{font-size:15px;line-height:22px;font-weight:500;color:var(--ink2)}
.ovp .ov-meaning{margin:0;font-size:13px;line-height:20px;color:var(--ink2);max-width:52ch}
.ovp .ov-based{color:var(--muted)}
.ovp .ov-link{display:inline-flex;align-items:center;gap:6px;min-height:24px;font-size:13px;font-weight:600;color:var(--brand-blue);text-decoration:none}
.ovp .ovp-row{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:56px;padding:10px 20px;border-top:1px solid var(--line);transition:background-color var(--dur,140ms) var(--ease,ease)}
.ovp .ovp-row:first-child{border-top:0}
.ovp .ovp-row:hover{background:var(--soft)}
.ovp .ovp-row-main{display:flex;flex-direction:column;min-width:0}
.ovp .ov-row-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600;color:var(--ink);text-decoration:none}
.ovp .ov-row-name:hover{color:var(--brand-blue);text-decoration:underline;text-underline-offset:3px}
.ovp .ov-row-next{font-size:13px;line-height:20px;color:var(--muted)}
.ovp .ovp-row .badge{flex:none}
.ovp .ovp-check-aside{display:flex;align-items:center;gap:16px}
.ovp .ovp-check-count{font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.ovp .ovp-progress{height:4px;background:var(--neutral-t)}
.ovp .ovp-progress>span{display:block;height:100%;background:var(--brand-blue);border-radius:0 2px 2px 0}
.ovp .ov-checks{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;padding:16px 20px}
.ovp .ov-check{display:flex;align-items:center;gap:12px;min-height:52px;padding:8px 14px;border-radius:var(--r,8px);background:var(--soft);font-size:14px}
.ovp .ov-tick{flex:none;display:inline-grid;place-items:center;width:28px;height:28px;border-radius:50%;border:2px solid var(--line2);background:var(--surface);color:var(--ink2);font-size:13px;font-weight:600;font-variant-numeric:tabular-nums}
.ovp .ov-check.is-next .ov-tick{border-color:var(--brand-blue);color:var(--brand-blue);box-shadow:0 0 0 3px var(--brand-100)}
.ovp .ov-check.done .ov-tick{border-color:var(--brand-blue);background:var(--brand-blue);color:#FFFFFF}
.ovp .ov-check-label{font-weight:600;color:var(--ink)}
.ovp a.ov-check-label{text-decoration:none}
.ovp a.ov-check-label:hover{color:var(--brand-blue);text-decoration:underline;text-underline-offset:3px}
.ovp .ov-check.done .ov-check-label{font-weight:500;color:var(--ink2)}
@media (max-width:700px),(pointer:coarse){
  .ovp .btn.sm,.ovp .btn.tertiary{min-height:40px}
  .ovp .btn.sm:not(.tertiary){height:40px}
  .ovp .ov-link,.ovp .ov-more a,.ovp a.ov-check-label{display:inline-flex;align-items:center;min-height:40px}
}
@media (max-width:900px){
  .ovp .ovp-grid,.ovp .ovp-grid.has-check{grid-template-columns:minmax(0,1fr);grid-template-areas:none;gap:16px}
  .ovp .ovp-grid>*{grid-area:auto}
}
@media (max-width:700px){
  .ovp .ov-checks{grid-template-columns:minmax(0,1fr);gap:8px;padding:12px 16px 16px}
  .ovp .ovp-head,.ovp .ov-value-body,.ovp .ov-more,.ovp .ov-note{padding-left:16px;padding-right:16px}
  .ovp .ovp-row{padding:10px 16px}
  .ovp .ov-cards>.attn{padding-left:16px;padding-right:16px}
}
`;

// Its own id: the classic Home (ui/overview.js) injects a sheet called "ov-styles" (scoped to `.ov`) too, and when app.js
// drew Home before this module registered, the Overview page used to find that id and skip its own rules (M185 review).
// M186 also scopes these rules to `.ovp`, so none of Home's `.ov` rules reaches this page either.
export const STYLE_ID = "ov-page-styles";

export function injectStyles(doc = document) {
  if (!doc || !doc.head || doc.getElementById(STYLE_ID)) return;
  const style = doc.createElement("style");
  style.id = STYLE_ID;
  style.textContent = CSS;
  doc.head.appendChild(style);
}
