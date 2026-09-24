// What this environment can do, asked once, so a screen whose feature is not available here shows one
// calm notice instead of a broken, empty or placeholder screen (DEC-954).
//
// Today that is one feature: text written by an AI service. The assistant, root-cause notes and
// campaign copy call an LLM. Without a connected AI service they run on the deterministic fake, which a
// developer wants (it is how the tests run) but a demo visitor must never mistake for real output.
// So in demo mode, when the use case's resolved `generative.llm.backend` is `fake`, those screens are
// replaced by the notice below. Outside demo mode nothing changes: the fake backend keeps its
// watermark on every generative screen.
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

/** Whether this use case writes text with an AI service (assistant, root causes or copy). */
export function usesAiService(uc) {
  const kind = uc && uc.config && uc.config.generative && uc.config.generative.kind;
  return Boolean(kind) && kind !== "none";
}

/** Whether the AI service the use case would call is a real one (anything but the fake backend). */
export function aiServiceConnected(uc) {
  const llm = uc && uc.config && uc.config.generative && uc.config.generative.llm;
  return !llm || llm.backend !== "fake";
}

/** True when this screen should show the notice instead: demo mode, an AI feature, no AI service. */
export async function needsAiNotice(uc) {
  if (!usesAiService(uc) || aiServiceConnected(uc)) return false;
  const status = await demoStatus();
  return Boolean(status && status.demo_mode);
}

/**
 * The same answer without waiting, for code that must decide synchronously (a run action, a Home
 * card tag): `true` only once `demoStatus()` has answered and the notice applies. Before that answer
 * it is `false`, so nothing is tagged or hidden on a guess.
 */
export function needsAiNoticeNow(uc) {
  if (!usesAiService(uc) || aiServiceConnected(uc)) return false;
  return Boolean(demoAnswer && demoAnswer.demo_mode);
}

/** Whether this use case's AI writing can be offered here: it writes text, and no notice replaces it. */
export function aiWritingAvailableNow(uc) {
  return usesAiService(uc) && demoAnswer !== undefined && !needsAiNoticeNow(uc);
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

export const AI_NOTICE_TITLE = "AI writing is switched off in this demo";
export const CONNECT_HREF = "#/generative/connection";

/** The notice card alone: a title, two sentences and one button that depends on the role. */
export function aiNoticeCard(uc) {
  const back = journeyBack(uc);
  const admin = mayConnect();
  return noticeCard({
    title: AI_NOTICE_TITLE,
    text: [
      "This screen writes text with an AI service, and this demo is not connected to one. It is switched off rather than showing sample text.",
      admin
        ? "Everything else in the demo works without it. Connect your company's AI service to turn it on."
        : "Everything else in the demo works without it. Ask your administrator to connect an AI service.",
    ],
    action: admin
      ? { label: "Connect an AI service", href: CONNECT_HREF, kind: "primary" }
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
