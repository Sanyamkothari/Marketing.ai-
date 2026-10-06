// "Ask your data" (Plan I, DEC-1250 … DEC-1259): the data helper's read-only chat about the file a run
// used, as a section at the end of the run's Data page (the router's page-panel seam, DEC-1255).
//
// WHAT THE PERSON SEES
//   * A question box ("Try “Churn rate by state” or “Who are the top 10% spenders?”") and the chat,
//     in the Guided-setup chat's own bubbles. Every reply is the API's text, escaped and never reworded;
//     under it, "What the AI looked at" (`sent.js`) as in Guided setup.
//   * Under a reply whose turn grouped the rows (`rate_by`), a bar chart per grouping: one bar per group,
//     with the group's rate, mean or rows. Every number on a bar is the tool result's own (the API's
//     `charts`, read from the stored result), never a number from the reply's text. A group too small to
//     show reads "fewer than N rows" and draws no bar.
//   * With no AI service connected, the box is replaced by the same "Connect an AI service" line Guided
//     setup shows; nothing else on the page needs one.
//
// The helper here cannot suggest a setting or change anything (the API refuses it), so this panel has no
// buttons but Ask and New chat. State is kept per upload in this module, the panel redraws itself in
// place, and every event is delegated from `document`, so the page can be repainted at any time.

import { announceStatus, barTrack, errorBox, esc, fmtInt, fmtNum, fmtPct, present } from "../../dom.js";
import { canAccess } from "../router.js";
import { clearAsk, getAsk, postAsk } from "./api.js";
import { plain, sentHtml, statusHtml } from "./sent.js";
import { guidedApplies } from "./guided.js";
import { injectAgentStyles } from "./styles.js";

export const ASK_PLACEHOLDER = "Try “Churn rate by state” or “Who are the top 10% spenders?”";
const PRODUCT_AI_HREF = "#/connections/ai/product";
/** The route a question is sent to, as the access model names it. */
const ASK_ROUTE = "/uploads/{upload_id}/ask/messages";

const states = new Map();

/** The Data page of a run made from an uploaded file, in a use case that has the data helper. */
export const askApplies = (kind, uc, run) =>
  kind === "data" && !!run && !!run.upload_id && guidedApplies(uc) && run.problem_type !== "uplift";

function stateFor(uploadId) {
  let st = states.get(uploadId);
  if (!st) {
    st = { uploadId, view: null, loading: true, error: null, asking: "", draft: "", busy: false, chatError: null };
    states.set(uploadId, st);
    load(st);
  }
  return st;
}

async function load(st) {
  try {
    st.view = await getAsk(st.uploadId);
    st.error = null;
  } catch (error) {
    st.error = error;
  }
  st.loading = false;
  repaint(st);
}

function panelElement(uploadId) {
  if (typeof document === "undefined") return null;
  return [...document.querySelectorAll("[data-ask-panel]")].find((el) => el.dataset.askPanel === uploadId) || null;
}

function repaint(st) {
  const el = panelElement(st.uploadId);
  if (!el) return;
  const open = new Set([...el.querySelectorAll("details[data-ag-sent][open]")].map((d) => d.getAttribute("data-ag-sent")));
  el.outerHTML = panelHtml(st, open);
  const chat = panelElement(st.uploadId)?.querySelector(".gchat");
  if (chat) chat.scrollTop = chat.scrollHeight;
}

// --- the chart ------------------------------------------------------------------------------------

/** What a bar measures, in words, from the chart's own fields (never from the reply). */
export function chartTitle(chart) {
  const by = plain(chart.column);
  if (chart.function === "positive_rate") {
    const yes = present(chart.positive_label) ? ` “${plain(chart.positive_label)}”` : "";
    return `Share${yes} in ${plain(chart.outcome_column)}, by ${by}`;
  }
  if (chart.function === "mean") return `Average ${plain(chart.outcome_column)}, by ${by}`;
  return `Rows by ${by}`;
}

const barValue = (chart, bar) => (chart.function === "rows" ? bar.rows : bar.value);

function barText(chart, bar) {
  if (bar.suppressed) return `fewer than ${fmtInt(chart.min_group_rows)} rows`;
  const value = barValue(chart, bar);
  if (!present(value)) return "—";
  if (chart.function === "positive_rate") return fmtPct(value);
  if (chart.function === "mean") return fmtNum(value, 2);
  return fmtInt(value);
}

/** One `rate_by` result as bars, in the Model page's "What drives the score" style (inline SVG tracks). */
export function chartHtml(chart) {
  const bars = Array.isArray(chart.bars) ? chart.bars : [];
  if (!bars.length) return "";
  const shown = bars.filter((b) => !b.suppressed && Number.isFinite(barValue(chart, b)));
  const max = Math.max(0, ...shown.map((b) => Math.abs(barValue(chart, b))));
  const row = (bar) => {
    const label = plain(bar.label);
    const text = barText(chart, bar);
    const value = barValue(chart, bar);
    const width = !bar.suppressed && Number.isFinite(value) && max ? (100 * Math.abs(value)) / max : 0;
    const rows = !bar.suppressed && chart.function !== "rows" && present(bar.rows) ? `${fmtInt(bar.rows)} rows` : "";
    return `<div class="brow ask-brow${bar.suppressed ? " is-suppressed" : ""}" data-ask-bar><span class="lab">${esc(
      label,
    )}${rows ? `<span class="ask-rows">${esc(rows)}</span>` : ""}</span>${barTrack(width, `${label}: ${text}`)}<span class="pct">${esc(
      text,
    )}</span></div>`;
  };
  return `<figure class="ask-chart" data-ask-chart="${esc(plain(chart.evidence_id))}"><figcaption>${esc(
    chartTitle(chart),
  )}</figcaption><div class="bars">${bars.map(row).join("")}</div>${
    bars.some((b) => b.suppressed)
      ? `<p class="caption">Groups of fewer than ${esc(fmtInt(chart.min_group_rows))} rows show no figures, so no one person can be picked out.</p>`
      : ""
  }</figure>`;
}

// --- the panel ------------------------------------------------------------------------------------

function messagesHtml(st, open) {
  const view = st.view || {};
  const session = view.session;
  const charts = Array.isArray(view.charts) ? view.charts : [];
  const lines = ((session && session.transcript) || []).map((m, i) => {
    if (m.role === "user") return `<div class="gmsg user">${esc(m.text)}</div>`;
    const drawn = charts.filter((c) => c.message_index === i).map(chartHtml).join("");
    return `<div class="gmsg bot">${esc(m.text)}${drawn}${sentHtml(m.sent, { id: `ask-${i}`, open: open.has(`ask-${i}`) })}</div>`;
  });
  if (st.asking) {
    lines.push(`<div class="gmsg user">${esc(st.asking)}</div><div class="gmsg bot"><span class="loading">Looking it up…</span></div>`);
  }
  return lines.join("");
}

export function panelHtml(st, open = new Set()) {
  const id = esc(st.uploadId);
  const head = `<section class="card ag-chat ask" data-ask-panel="${id}" aria-labelledby="ask-h-${id}"><h3 id="ask-h-${id}">Ask your data</h3>`;
  if (!st.view && st.loading) return `${head}<p class="muted ask-lead" role="status">Loading…</p></section>`;
  if (!st.view) {
    // A use case without the helper answers 409: the page simply has no such section.
    if (st.error && st.error.status === 409) return `<section data-ask-panel="${id}" hidden></section>`;
    return `${head}<div class="card-body">${errorBox(st.error, { title: "Ask your data could not be loaded." })}</div></section>`;
  }
  const chat = st.view.chat || {};
  const lead = `<p class="ask-lead">Ask a question about the file this run used. The helper looks the answer up in the data and cannot change anything.</p>`;
  const messages = messagesHtml(st, open);
  const log = messages ? `<div class="gchat" data-ask-chat>${messages}</div>` : "";
  if (chat.available === false) {
    return `${head}${lead}${log}<div class="ag-nochat" data-ask-nochat role="status"><p><a href="${PRODUCT_AI_HREF}">Connect an AI service</a> to chat with the helper.</p></div></section>`;
  }
  if (!canAccess("POST", ASK_ROUTE)) {
    // A Viewer reads the chat; asking spends the AI service's budget, which is an Analyst's (DEC-1253).
    return `${head}${lead}${log}<p class="ask-lead muted" data-ask-readonly>Only an Analyst can ask questions here.</p></section>`;
  }
  const again = st.view.session
    ? `<button type="button" class="btn quiet" data-ask-clear="${id}"${st.busy ? " disabled" : ""}>New chat</button>`
    : "";
  return `${head}${lead}${log}${st.chatError ? `<div class="card-body">${errorBox(st.chatError)}</div>` : ""}<form class="gaskrow" data-ask-form="${id}"><div class="control"><input type="text" data-ask-input="${id}" aria-label="Ask about your data" placeholder="${esc(
    ASK_PLACEHOLDER,
  )}" value="${esc(st.draft)}" autocomplete="off" maxlength="1000"${st.busy ? " disabled" : ""}></div><button type="submit" class="btn secondary"${
    st.busy ? " disabled" : ""
  }>Ask</button>${again}</form>${statusHtml(chat)}</section>`;
}

// --- events (delegated once) ------------------------------------------------------------------------

async function ask(st, text) {
  st.busy = true;
  st.asking = text;
  st.draft = "";
  st.chatError = null;
  repaint(st);
  try {
    st.view = await postAsk(st.uploadId, text);
    const transcript = (st.view.session && st.view.session.transcript) || [];
    const last = transcript[transcript.length - 1];
    if (last) announceStatus(last.text);
  } catch (error) {
    st.chatError = error;
    st.draft = text;
  }
  st.busy = false;
  st.asking = "";
  repaint(st);
}

async function clear(st) {
  st.busy = true;
  st.chatError = null;
  repaint(st);
  try {
    st.view = await clearAsk(st.uploadId);
  } catch (error) {
    st.chatError = error;
  }
  st.busy = false;
  repaint(st);
}

function bindOnce() {
  if (typeof document === "undefined" || bindOnce.done) return;
  bindOnce.done = true;
  document.addEventListener("submit", (event) => {
    const form = event.target && event.target.closest ? event.target.closest("[data-ask-form]") : null;
    if (!form) return;
    event.preventDefault();
    const st = states.get(form.dataset.askForm);
    const input = form.querySelector("[data-ask-input]");
    const text = input ? input.value.trim() : "";
    if (st && text && !st.busy) ask(st, text);
  });
  document.addEventListener("input", (event) => {
    const input = event.target && event.target.closest ? event.target.closest("[data-ask-input]") : null;
    const st = input ? states.get(input.dataset.askInput) : null;
    if (st) st.draft = input.value;
  });
  document.addEventListener("click", (event) => {
    const button = event.target && event.target.closest ? event.target.closest("[data-ask-clear]") : null;
    const st = button ? states.get(button.dataset.askClear) : null;
    if (st && !st.busy) clear(st);
  });
}

/** The panel's markup for a run's page; registering is `index.js`'s. */
export function askPanelHtml(kind, uc, run) {
  injectAgentStyles();
  bindOnce();
  return panelHtml(stateFor(run.upload_id));
}

/** For tests: forget every upload's chat (a fresh page). */
export function resetAskState() {
  states.clear();
}
