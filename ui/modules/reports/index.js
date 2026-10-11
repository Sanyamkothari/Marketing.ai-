// Reports (Plan M, M178, DEC-1608): `#/reports[/<tab>]`, one place for everything that shows what campaigns earned.
//
//   Value proof            the Value Proof Packs (GET /pilot/proof), each with its claim in words
//   Programme summary      the value proven to date and its rules (GET /campaigns/summary), the block Results also draws
//   Check a past campaign  today's audit form, in three steps (POST /uploads, /campaigns/audit, /campaigns/programme)
//   Model reports          today's `#/pilot` lists, in a closed technical section
//
// It adds a route and takes nothing away: `#/results`, `#/audit` and `#/pilot` still draw what they drew, and every
// report they reach is reachable here. The module imports nothing of the layout switch (`ui/layout.js`), so it works
// in the classic and the campaigns layout alike. Registration is the whole wiring (`ui/index.html`).

import { errorBox } from "../../dom.js";
import { getIndustries, listProofs, postAudit, postProgramme, postUpload, readSummary } from "./api.js";
import { setActiveNav, canAccess, registerModule } from "../router.js";
import { auditBody, fileOf, programmeBody } from "../decide/audit.js";
import { injectStyles as injectSimpleStyles } from "../simple/pages.js";
import { checkHtml } from "./check.js";
import { loadModelReports, modelReportsHtml } from "./models.js";
import { injectStyles, reportsPageHtml, tabOf } from "./page.js";
import { programmeHtml } from "./programme.js";
import { valueProofHtml } from "./value_proof.js";

export const ROUTES = ["reports"];

const hashParts = () => window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);

const loading = `<div class="skel-screen" aria-busy="true"><div class="skel-list"><span class="skel skel-row"></span><span class="skel skel-row"></span><span class="skel skel-row"></span></div><span class="sr">Loading…</span></div>`;

let paintSeq = 0;
let lastTab = null;

const emptyFiles = () => ({ assignment: null, outcomes: null, contact: null, programme: null });
const check = { files: emptyFiles(), busy: false, error: null, programmeBusy: false, programmeError: null, useCase: null };

async function settle(promise) {
  try {
    return { value: await promise, error: null };
  } catch (error) {
    return { value: null, error };
  }
}

/** The tab's body, once its answers are in. */
async function bodyOf(tab) {
  if (tab === "check") return checkHtml({ can: canAccess, state: check });
  if (tab === "programme") {
    // A summary that cannot be read is an error with a retry, never "nothing is proven yet" (that is a 200 with no totals).
    const answer = await settle(readSummary());
    return answer.error ? errorBox(answer.error, { retry: true }) : programmeHtml(answer.value);
  }
  if (tab === "models") return modelReportsHtml(await loadModelReports());
  const answer = await settle(listProofs());
  return answer.error ? errorBox(answer.error, { retry: true }) : valueProofHtml(answer.value);
}

async function render(app, parts) {
  injectStyles();
  injectSimpleStyles();
  const tab = tabOf(parts[1]);
  const seq = (paintSeq += 1);
  if (tab === "check" && lastTab !== "check") Object.assign(check, { files: emptyFiles(), busy: false, error: null, programmeBusy: false, programmeError: null });
  lastTab = tab;
  setActiveNav("reports"); // "reports" is the campaigns layout's item; the classic top bar maps it to Results (ui/chrome.js OLD_GOALS)
  document.title = "Reports · Marketing AI";
  app.innerHTML = reportsPageHtml({ tab, bodyHtml: loading });
  const html = await bodyOf(tab);
  if (seq !== paintSeq || hashParts()[0] !== "reports") return;
  const body = app.querySelector("[data-reports-body]");
  if (body) body.innerHTML = html;
}

registerModule({ name: "reports", routes: ROUTES, render });

// --- Check a past campaign: the files, the two forms ------------------------------------------------------------

/** Draw the check tab again, keeping what was typed (choosing a file draws the form again). */
function drawCheck() {
  const body = document.querySelector("[data-reports-body]");
  if (!body || hashParts()[0] !== "reports" || tabOf(hashParts()[1]) !== "check") return;
  const kept = {};
  const ticks = {};
  for (const form of document.querySelectorAll("[data-rp-check-form], [data-rp-programme-form]")) {
    for (const [key, value] of new FormData(form).entries()) {
      if (typeof value === "string") kept[key] = value;
    }
  }
  // FormData leaves an unticked box out, so its state is read from the box: otherwise an unticked "More of the outcome is
  // better" would come back ticked by its default, and the audit would be sent the other way round.
  for (const box of document.querySelectorAll("[data-rp-check-form] input[type=checkbox], [data-rp-programme-form] input[type=checkbox]")) {
    ticks[box.name] = box.checked;
  }
  const open = Boolean(document.querySelector("[data-rp-programme-card][open]"));
  body.innerHTML = checkHtml({ can: canAccess, state: check });
  if (open) body.querySelector("[data-rp-programme-card]")?.setAttribute("open", "");
  for (const [key, value] of Object.entries(kept)) {
    const input = body.querySelector(`[name="${key}"]`);
    if (!input || input.type === "checkbox") continue;
    if (input.type === "radio") {
      const chosen = body.querySelector(`[name="${key}"][value="${value}"]`);
      if (chosen) chosen.checked = true;
    } else {
      input.value = value;
    }
  }
  for (const [key, on] of Object.entries(ticks)) {
    const box = body.querySelector(`input[type=checkbox][name="${key}"]`);
    if (box) box.checked = on;
  }
}

/** The goal an uploaded file is filed under: the first one open today (the file is not about it). Same rule as `#/audit`. */
async function uploadUseCase() {
  if (check.useCase) return check.useCase;
  const payload = await getIndustries();
  for (const industry of (payload && payload.industries) || []) {
    for (const stage of industry.stages || []) {
      const found = (stage.use_cases || []).find((u) => !u.status || u.status === "available");
      if (found) {
        check.useCase = found.id;
        return found.id;
      }
    }
  }
  throw new Error("No goal is set up yet, so a file cannot be uploaded.");
}

document.addEventListener("change", async (event) => {
  const input = event.target && event.target.closest ? event.target.closest("[data-rp-file]") : null;
  if (!input || !input.files || !input.files[0]) return;
  const kind = input.dataset.rpFile;
  const failed = kind === "programme" ? "programmeError" : "error";
  try {
    check.files[kind] = fileOf(await postUpload(input.files[0], await uploadUseCase(), "score"));
    check[failed] = null;
  } catch (error) {
    check[failed] = error;
  }
  drawCheck();
});

document.addEventListener("submit", async (event) => {
  const checkForm = event.target && event.target.closest ? event.target.closest("[data-rp-check-form]") : null;
  const programmeForm = event.target && event.target.closest ? event.target.closest("[data-rp-programme-form]") : null;
  const form = checkForm || programmeForm;
  if (!form) return;
  event.preventDefault();
  const values = Object.fromEntries(new FormData(form).entries());
  const busy = checkForm ? "busy" : "programmeBusy";
  const failed = checkForm ? "error" : "programmeError";
  check[busy] = true;
  check[failed] = null;
  drawCheck();
  try {
    const view = checkForm
      ? await postAudit(auditBody(values, check.files))
      : await postProgramme(programmeBody(values, check.files));
    check.files = emptyFiles();
    check[busy] = false;
    // The answer is the campaign's page, where the server's label says what its numbers can claim.
    window.location.hash = `#/campaigns/${encodeURIComponent(view.campaign.campaign_id)}`;
    return;
  } catch (error) {
    // `CAMPAIGN_NOT_MATURED` says the day to come back; the files stay chosen so the person can try again.
    check[failed] = error;
  }
  check[busy] = false;
  drawCheck();
});
