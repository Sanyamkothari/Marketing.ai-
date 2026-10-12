// What this environment can do, asked once, so a screen whose feature is not available here shows one
// calm notice instead of a broken or empty screen (DEC-954).
//
// Today that is one feature: text written by an AI service. The assistant, root-cause notes and
// campaign copy call a language model, and there is no stand-in: with no AI service connected they
// cannot answer, so their screens are replaced by the notice below, in every mode. Whether one is
// connected comes from `GET /ai-service`: these screens write what the customer receives, so they depend
// on the Deliverable AI slot (`slots.deliverable.connected`; a slot that uses the Product AI's service
// counts as connected). The Guided setup chat depends on the Product AI and says so itself. When that call fails or the route is absent, nothing is claimed
// either way (unknown is not "not connected"), and the server, which answers 409 `AI_NOT_CONNECTED`
// when a call needs a model it does not have, stays the one that decides.
//
// The notice is role-aware (v1 UI): whoever may connect an AI service gets the one button that goes
// there; everyone else is told who can, and gets the way back to the journey. This file may only
// import the shared modules (`tests/integration/test_ui.py`), so it does not read the session itself:
// the generative module registers the question with `registerAiServiceAccess`.

import { API_BASE } from "./api.js";
import { esc, journeyBack, noticeCard, pageHead, typeChip } from "./dom.js";

let demo = null;
let demoAnswer;

/** `GET /pilot/demo`, once per page load; null when the call fails or the route is absent. */
export function demoStatus() {
  if (!demo) {
    demo = fetch(`${API_BASE}/pilot/demo`)
      .then((response) => (response.ok ? response.json() : null))
      .catch(() => null)
      .then((status) => {
        demoAnswer = status;
        return status;
      });
  }
  return demo;
}

// --- is an AI service connected? ---------------------------------------------------------------------

/** Fired on `window` by the AI service screen after a save or a disconnect. */
export const AI_SERVICE_EVENT = "marketing-ai:ai-service-changed";
const AI_STALE_MS = 15000;

let ai = null; // the pending or settled answer of `GET /ai-service`
let aiAnswer; // undefined until the first answer; then the state, or null when it could not be read
let aiAt = 0;

/** `GET /ai-service` (`{ slots, providers }`), reused for a few seconds; null when the call fails. Never rejects. */
export function aiServiceStatus({ fresh = false } = {}) {
  if (!ai || fresh || Date.now() - aiAt > AI_STALE_MS) {
    aiAt = Date.now();
    ai = fetch(`${API_BASE}/ai-service`)
      .then((response) => (response.ok ? response.json() : null))
      .catch(() => null)
      .then((state) => {
        aiAnswer = state && typeof state === "object" && state.slots ? state : null;
        return aiAnswer;
      });
  }
  return ai;
}

/** The AI service screen calls this after it changed what is saved: the next question asks again. */
export function forgetAiServiceStatus() {
  ai = null;
  aiAnswer = undefined;
  aiAt = 0;
  // Ask again straight away, so a screen opened next finds the new answer settled, and tell listeners
  // once it has arrived (a repaint before then would still see "no answer yet").
  const announce = () => {
    if (typeof window !== "undefined" && typeof Event === "function") window.dispatchEvent(new Event(AI_SERVICE_EVENT));
  };
  aiServiceStatus({ fresh: true }).then(announce, announce);
}

/** The Deliverable AI's state out of an `aiServiceStatus()` answer, or null. */
const deliverable = (status) => (status && status.slots && status.slots.deliverable) || null;

/** Whether this use case writes text with an AI service (assistant, root causes or copy). */
export function usesAiService(uc) {
  const kind = uc && uc.config && uc.config.generative && uc.config.generative.kind;
  return Boolean(kind) && kind !== "none";
}

/** True when this screen should show the notice instead: an AI feature, and no AI service connected. */
export async function needsAiNotice(uc) {
  if (!usesAiService(uc)) return false;
  const slot = deliverable(await aiServiceStatus());
  return Boolean(slot) && slot.connected === false;
}

/**
 * The same answer without waiting, for code that must decide synchronously (a run action, a Home
 * card tag): `true` only once `aiServiceStatus()` has answered that nothing is connected. Before that
 * answer it is `false`, so nothing is tagged or hidden on a guess.
 */
export function needsAiNoticeNow(uc) {
  if (!usesAiService(uc)) return false;
  const slot = deliverable(aiAnswer);
  return Boolean(slot) && slot.connected === false;
}

/** Whether this use case's AI writing can be offered here: it writes text, and no notice replaces it. */
export function aiWritingAvailableNow(uc) {
  return usesAiService(uc) && aiAnswer !== undefined && !needsAiNoticeNow(uc);
}

// --- who may connect one -----------------------------------------------------------------------------

let canConnect = null;

/**
 * `fn()` answers whether the person looking may connect an AI service (the generative module asks the
 * session whether this role may test the connection). With none registered, the button is offered,
 * as the server stays the one that refuses.
 */
export function registerAiServiceAccess(fn) {
  canConnect = typeof fn === "function" ? fn : null;
}

function mayConnect() {
  if (!canConnect) return true;
  try {
    return canConnect() !== false;
  } catch {
    return true;
  }
}

export const AI_NOTICE_TITLE = "Needs AI service connection";
export const CONNECT_HREF = "#/connections/ai/deliverable";

/** The notice card alone: a title, two sentences and one button that depends on the role. */
export function aiNoticeCard(uc) {
  if (campaignLayoutNow()) return aiNoticeCardCampaigns(uc); // Plan M M192 (DEC-1622)
  const back = journeyBack(uc);
  const admin = mayConnect();
  return noticeCard({
    title: AI_NOTICE_TITLE,
    text: [
      "This screen writes text for your customer with the Deliverable AI, and none is connected yet.",
      admin
        ? "Everything else works without it. Connect an AI service to turn this on, or let it use the Product AI's."
        : "Everything else works without it. Ask your administrator to connect an AI service.",
    ],
    action: admin
      ? { label: "Connect the Deliverable AI", href: CONNECT_HREF, kind: "primary" }
      : { label: `Back to ${back.label}`, href: back.href, kind: "secondary" },
    attrs: "data-ai-notice-card",
  });
}

const TYPE_LABEL = { predictive: "Predictive AI", generative: "Generative AI", hybrid: "Hybrid" };

/** The one notice, under the screen's usual header, with a way back. */
export function aiNoticeHtml(uc, backHtml) {
  const chip =
    uc.stars && uc.marker
      ? `<div class="chips">${typeChip({ ...uc, label: TYPE_LABEL[uc.ai_type] || uc.label || "" })}</div>`
      : "";
  return `<main class="screen" data-ai-notice>${pageHead(
    `${backHtml}<h1 class="h1">${esc(uc.name)}</h1>${uc.description ? `<p class="desc">${esc(uc.description)}</p>` : ""}${chip}`,
  )}${aiNoticeCard(uc)}</main>`;
}

// --- Plan M M192 (DEC-1622): the AI service in one click -------------------------------------------------------
//
// Settings › AI service (`#/settings/ai`, drawn by `modules/simple/ai_service.js`) is the one place a person finds the
// two AI services, in plain words, with a direct Connect / Change button to each key form. In the campaigns layout a
// feature that answers 409 `AI_NOT_CONNECTED` says so in one sentence that links there (`aiNotConnectedHtml`), and the
// notice above sends there too; the classic layout keeps its own words and links. The layout is read from
// `<html data-ui-layout>` (`ui/layout.js` sets it only in the campaigns layout), so this file still imports only
// `api.js` and `dom.js`.

/** The Settings › AI service page. */
export const AI_SETTINGS_HREF = "#/settings/ai";

/** The first sentence a feature shows when no AI service is connected (campaigns layout). */
export const AI_NOT_CONNECTED_SENTENCE = "No AI service is connected.";

/** True in the campaigns layout (the five-place sidebar), read from the mark `ui/layout.js` puts on `<html>`. */
export function campaignLayoutNow(doc = typeof document === "undefined" ? null : document) {
  return Boolean(doc && doc.documentElement && doc.documentElement.getAttribute("data-ui-layout") === "campaigns");
}

/**
 * "No AI service is connected. Connect one in Settings › AI service." with the place as a link, for a person who may
 * connect one (`canConnect`: `PUT /ai-service/{slot}`); anyone else is told to ask an admin, with no link to a form
 * they could not save. Without `canConnect` the question registered with `registerAiServiceAccess` answers it. `tag`
 * is the element ("p" by default), `attrs` extra attribute markup the caller wrote.
 */
export function aiNotConnectedHtml({ canConnect = undefined, tag = "p", attrs = "" } = {}) {
  const el = /^[a-z][a-z0-9]*$/.test(tag) ? tag : "p";
  const may = canConnect === undefined ? mayConnect() : Boolean(canConnect);
  const rest = may
    ? `Connect one in <a href="${AI_SETTINGS_HREF}" data-ai-settings-link>Settings › AI service</a>.`
    : "Ask an admin to connect one in Settings › AI service.";
  return `<${el} data-ai-not-connected${attrs ? ` ${attrs}` : ""}>${esc(AI_NOT_CONNECTED_SENTENCE)} ${rest}</${el}>`;
}

/** The sentence for an error a call answered, or "" when it is not `AI_NOT_CONNECTED` or the layout is classic. */
export function aiNotConnectedFor(error, options = {}) {
  if (!error || error.code !== "AI_NOT_CONNECTED" || !campaignLayoutNow()) return "";
  return aiNotConnectedHtml(options);
}

/** The notice card in the campaigns layout: the same title, the one sentence, and the way to Settings › AI service. */
function aiNoticeCardCampaigns(uc) {
  const admin = mayConnect();
  const back = journeyBack(uc);
  return noticeCard({
    title: AI_NOTICE_TITLE,
    text: [
      admin
        ? `${AI_NOT_CONNECTED_SENTENCE} Connect one in Settings › AI service.`
        : `${AI_NOT_CONNECTED_SENTENCE} Ask an admin to connect one in Settings › AI service.`,
      "Everything else works without it.",
    ],
    action: admin
      ? { label: "Open Settings › AI service", href: AI_SETTINGS_HREF, kind: "primary" }
      : { label: `Back to ${back.label}`, href: back.href, kind: "secondary" },
    attrs: "data-ai-notice-card data-ai-not-connected",
  });
}

// Where a key form was opened from. Settings › AI service marks its buttons `data-ai-from-settings="<slot>"`; a click on
// one remembers the slot, and the key form (`modules/connections/ai.js`) asks `openedFromAiSettings(slot)` for its
// breadcrumb: Settings › AI service › ... instead of Connections › .... Any other address forgets it, so the form
// reached from Connections, a bookmark or a reload keeps the Connections breadcrumb.
let aiFrom = null;
let aiFromWired = false;

/** Listen once, on `doc`, for the clicks and the address changes above. */
export function trackAiSettingsOrigin(doc = typeof document === "undefined" ? null : document) {
  if (aiFromWired || !doc || typeof doc.addEventListener !== "function") return;
  aiFromWired = true;
  doc.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      const link = target && typeof target.closest === "function" ? target.closest("[data-ai-from-settings]") : null;
      if (link) aiFrom = link.getAttribute("data-ai-from-settings");
    },
    true,
  );
  const view = doc.defaultView;
  if (view && typeof view.addEventListener === "function") {
    view.addEventListener("hashchange", () => {
      if (aiFrom && view.location.hash.replace(/\/+$/, "") !== `#/connections/ai/${aiFrom}`) aiFrom = null;
    });
  }
}

/** Whether the key form for `slot` was opened from Settings › AI service (and the address has not moved since). */
export function openedFromAiSettings(slot) {
  if (!aiFrom || aiFrom !== slot || typeof window === "undefined") return false;
  return window.location.hash.replace(/\/+$/, "") === `#/connections/ai/${slot}`;
}
