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
