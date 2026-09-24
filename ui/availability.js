// What this environment can do, asked once, so a screen whose feature is not available here shows one
// calm notice instead of a broken, empty or placeholder screen (DEC-954).
//
// Today that is one feature: text written by an AI service. The assistant, root-cause notes and
// campaign copy call an LLM. Without a connected AI service they run on the deterministic fake, which a
// developer wants (it is how the tests run) but a demo visitor must never mistake for real output.
// So in demo mode, when the use case's resolved `generative.llm.backend` is `fake`, those screens are
// replaced by the notice below. Outside demo mode nothing changes: the fake backend keeps its
// watermark on every generative screen.

import { API_BASE } from "./api.js";
import { esc, pageHead } from "./dom.js";

let demo = null;

/** `GET /pilot/demo`, once per page load; null when the call fails or the route is absent. */
export function demoStatus() {
  if (!demo) {
    demo = fetch(`${API_BASE}/pilot/demo`)
      .then((response) => (response.ok ? response.json() : null))
      .catch(() => null);
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

/** The one notice, under the screen's usual header, with a way back. */
export function aiNoticeHtml(uc, backHtml) {
  return `<main class="screen" data-ai-notice>${pageHead(
    `${backHtml}<h1 class="h1">${esc(uc.name)}</h1>${uc.description ? `<p class="desc">${esc(uc.description)}</p>` : ""}`,
  )}<section class="card notice-card" role="status"><h3>Needs AI service connection</h3><p>This feature writes text with an AI service. This demo is not connected to one, so the screen is switched off rather than showing placeholder text.</p><p class="muted">Everything else in the demo works without it. To turn it on, an administrator connects the platform to its AI service (see <b>docs/GENERATIVE.md</b>).</p></section></main>`;
}
