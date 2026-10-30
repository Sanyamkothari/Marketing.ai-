// The Review page's three tabs, Data | Model | Output: the pages a person has always had for a trained campaign, drawn
// inside the Review page by `ui/pages.js` (`renderPage` with `embedded`), not copied. Data and Model are the training
// run's pages; Output has both parts of the campaign on one tab, the training run's (how well the top of the list is
// picked out, tenth by tenth) and the scoring run's (who to contact, who not, and why). The Data tab adds one card,
// "What the Data Doctor changed", for a run whose file came through a Data Doctor case.
//
// This file is pure: the tab bar and the parts that need no request (the Output tab's tenths table and its contact
// part). `results_mount.js` fills the rest from the training and scoring runs' own artefacts, once, and keeps the answer,
// so a redraw of the Review page (a busy button) does not ask again.

import { esc } from "../../dom.js";
import { contactPartHtml, decileTableHtml } from "./summary_views.js";

export const TABS = Object.freeze([
  { id: "data", label: "Data" },
  { id: "model", label: "Model" },
  { id: "output", label: "Output" },
]);

const LOADING = '<p class="cw-p cw-note" data-cw-loading>Loading…</p>';
const slot = (id) => `<div data-cw-slot="${id}">${LOADING}</div>`;

/** The tab and the folds that are open: kept for the visit, so a redraw of the page does not close them (`results_mount.js`). */
export const visit = { tab: "data", key: "", loaded: null, open: new Set() };

/**
 * The tabs with every part that needs no request drawn. `list` is the contact list's summary, `campaign` the campaign
 * record, `top` the first customers (`getResultsSummary`), `story` the model's lift and importance files, `numbers` the
 * campaign's own numbers as markup (the tiles and tables it will be judged by), shown under the Output tab's fold.
 */
export function resultsTabsHtml({ list = null, campaign = null, top = null, story = null, numbers = "" } = {}) {
  const goal = campaign && campaign.use_case_id;
  const bar = TABS.map(
    (t) =>
      `<button type="button" role="tab" class="tab${t.id === visit.tab ? " on" : ""}" id="cw-tab-${t.id}" aria-controls="cw-panel-${t.id}" aria-selected="${t.id === visit.tab}" data-cw-tab="${t.id}">${esc(t.label)}</button>`,
  ).join("");
  const panel = (id, inner) =>
    `<div role="tabpanel" id="cw-panel-${id}" aria-labelledby="cw-tab-${id}" data-cw-panel="${id}"${id === visit.tab ? "" : " hidden"}>${inner}</div>`;
  const deciles = decileTableHtml(story && story.lift, goal);
  const output = `<h3 class="cw-h3 cw-sum-h">How well the top of the list is picked out</h3><p class="cw-p cw-note">Measured on customers the model had not seen, before anyone is contacted.</p>${slot(
    "output-train",
  )}${deciles ? `<section class="card cw-card"><div class="cw-card-body">${deciles}</div></section>` : ""}<h3 class="cw-h3 cw-sum-h">Who to contact</h3><section class="card cw-card" data-cw-contact><div class="cw-card-body">${contactPartHtml(
    { list, campaign, top },
  )}<p class="cw-p cw-note" data-cw-download-note>The contact list is the file you download when you approve above.</p></div></section>${slot("output-score")}${
    numbers ? `<details class="cw-more" data-cw-numbers><summary>The numbers this campaign will be judged by</summary><div class="cw-more-body">${numbers}</div></details>` : ""
  }`;
  return `<section class="cw-tabs" data-cw-tabs><div class="tabs-bar"><div class="tabs" role="tablist" aria-label="What was learned from and made">${bar}</div></div>${panel(
    "data",
    slot("data"),
  )}${panel("model", slot("model"))}${panel("output", output)}</section>`;
}
