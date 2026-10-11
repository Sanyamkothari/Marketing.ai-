// Plan K M139 (DEC-1406): "Draft a plan" under the Results page's warning cards - no new top-bar item (Plan H).
//
// The button appears only where the server offers the planner: `/auth/me` (read once at boot by the production
// module) lists `POST /agentic/plans` - the route is declared only while `agentic.enabled` is on, so with the
// switch off this module sends no request at all - and `canAccess("POST", "/agentic/plans")` holds (an Analyst).
// Clicking it asks the server to draft (`POST /agentic/plans`); the drafted plan is drawn
// under the card by `view.js` from the server's answer. Approving goes through the same route an Approver
// uses anywhere (`POST /agentic/proposals/{id}/approve`); nothing is sent from here.

import { API_BASE } from "../../api.js";
import { ensureMe } from "../production/session.js";
import { canAccess } from "../router.js";
import { cardRequest, draftButtonHtml, plannerOffered, planHtml, refusalHtml } from "./view.js";

async function call(path, body) {
  let response;
  try {
    response = await fetch(API_BASE + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  } catch {
    return { ok: false, code: "NETWORK_ERROR", message: `Could not reach the API at ${API_BASE}.` };
  }
  let parsed = null;
  try {
    parsed = await response.json();
  } catch {
    parsed = null;
  }
  if (response.ok) return { ok: true, body: parsed };
  const detail = (parsed && parsed.detail) || {};
  return { ok: false, code: detail.code || `HTTP_${response.status}`, message: detail.message || "The planner could not draft a plan." };
}

// Whether this deployment offers the planner at all, from the `/auth/me` answer already in hand (`ensureMe`
// reuses the boot call): no extra request, and none to a route that is not mounted.
async function offeredHere() {
  try {
    return plannerOffered(await ensureMe());
  } catch {
    return false;
  }
}

async function decorate(root) {
  if (!canAccess("POST", "/agentic/plans") || !root.querySelector("[data-attention] [data-card]")) return;
  if (!(await offeredHere())) return;
  for (const entry of root.querySelectorAll("[data-attention] [data-card]")) {
    if (entry.querySelector("[data-draft-plan]")) continue;
    const card = {
      code: entry.getAttribute("data-card"),
      campaign_id: entry.getAttribute("data-card-campaign"),
      use_case_id: entry.getAttribute("data-card-use-case"),
    };
    const html = draftButtonHtml(card);
    if (html) entry.insertAdjacentHTML("beforeend", html);
  }
}

async function onClick(event) {
  // Duck-typed, not `instanceof Element`: the page's own tests run the modules in jsdom, where `Element` is not a
  // global of the module's realm; anything with `closest` is an element here.
  const target = event.target && typeof event.target.closest === "function" ? event.target : null;
  if (!target) return;
  const draft = target.closest("[data-draft-plan]");
  const approve = target.closest("[data-plan-approve]");
  const reject = target.closest("[data-plan-reject]");
  if (draft) {
    draft.disabled = true;
    const entry = draft.closest("[data-card]");
    const answer = await call(
      "/agentic/plans",
      cardRequest(
        draft.getAttribute("data-draft-plan"),
        draft.getAttribute("data-plan-campaign"),
        draft.getAttribute("data-plan-use-case"),
      ),
    );
    const html = answer.ok
      ? planHtml(answer.body, { canApprove: canAccess("POST", "/agentic/proposals/{proposal_id}/approve") })
      : refusalHtml(answer.code, answer.message);
    if (entry) entry.insertAdjacentHTML("beforeend", html);
    draft.disabled = false;
  } else if (approve || reject) {
    const button = approve || reject;
    const id = button.getAttribute(approve ? "data-plan-approve" : "data-plan-reject");
    button.disabled = true;
    const answer = await call(`/agentic/proposals/${encodeURIComponent(id)}/${approve ? "approve" : "reject"}`, {});
    const section = button.closest("[data-proposal]");
    const words = answer.ok
      ? approve
        ? "Approved: the list is built, the campaign opened and its test plan fixed."
        : "Rejected: nothing will be sent."
      : answer.message;
    if (section) section.insertAdjacentHTML("beforeend", refusalHtml(answer.ok ? "OK" : answer.code, words));
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("click", (event) => {
    onClick(event);
  });
  const observer = new MutationObserver(() => decorate(document));
  observer.observe(document.body || document.documentElement, { childList: true, subtree: true });
  decorate(document);
}
