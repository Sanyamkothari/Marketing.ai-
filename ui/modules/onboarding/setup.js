// "Build from raw tables" as a way to fill Setup Step 1 (Plan A M35; prototype `predStep1()`,
// screenshots `02`, `07` and `08`).
//
// `ui/usecase.js` owns the Setup form and knows nothing about onboarding: it asks the registered
// setup source (`modules/router.js`) for a card to offer beside "Upload a prepared file", and, once
// that card is chosen, hands it an empty element to mount into. This module answers both.
//
// **Train mode** mounts the step-by-step panel (`panel.js`) for the client picked in the top bar.
// **Score mode** first finds the setup the model being scored was trained with - the version's
// training run names the dataset it read, and the dataset's manifest names the setup (the saved
// `OnboardingSpec`) - and mounts the same panel in replay mode on it, so this month's tables go
// through last month's decisions. A model trained on a prepared file has no setup, and the panel
// says so instead of guessing one.
//
// The Setup form repaints its whole `<main>` on every change, so a panel is created once per
// (mode, client, use case, model) and its element is moved into each fresh placeholder: the panel's
// working edits, its uploads in flight and its build poll all survive the repaint.

import { getRun } from "../../api.js";
import { emptyState, errorBox, esc, fmtDate, techDetails } from "../../dom.js";
import { getDataset, getDatasetReport, listOnboardingSpecs } from "./api.js";
import { currentClient } from "./clients.js";
import { onboardingPanel } from "./panel.js";

const mounted = new Map();

/** What the Setup form needs to know about the source's own state: which client it builds for. */
export function setupContext() {
  const client = currentClient();
  return { clientId: client ? client.client_id : null, clientName: client ? client.name : null };
}

/** The second Step 1 card, in the Setup form's own words for each mode. */
export function setupCard(uc, mode) {
  return mode === "score"
    ? {
        title: "Upload this month's tables",
        text: "They are matched and built exactly the way last month's were.",
      }
    : {
        title: "Build from raw tables",
        text: "Bring the tables you already have. We match their columns, build the measures and the outcome, and write the dataset.",
      };
}

/**
 * Mount the raw-tables panel into `container` (an element the Setup form just painted).
 *
 * `onDatasetReady(payload)` receives the panel's payload plus `clientId`, so the Run request names
 * the client the dataset belongs to. `modelVersion` is the version score mode would score with - its
 * setup is the one replayed.
 */
export function mountSetup(container, { uc, mode, modelVersion, onDatasetReady }) {
  injectStyles();
  const { clientId } = setupContext();
  if (!clientId) {
    container.innerHTML = `<div class="loading">Loading clients…</div>`;
    return;
  }
  const version = mode === "score" && modelVersion ? modelVersion.model_id : "";
  const key = [mode, clientId, uc.id, version].join("|");
  let entry = mounted.get(key);
  if (!entry) {
    entry = { element: document.createElement("div"), onDatasetReady };
    entry.element.className = "ob";
    mounted.set(key, entry);
    const forward = (payload) => {
      entry.onDatasetReady({ ...payload, clientId });
      showColumnsStep();
    };
    if (mode === "score") startScore(entry.element, { uc, clientId, modelVersion, onDatasetReady: forward });
    else startTrain(entry.element, { uc, clientId, onDatasetReady: forward });
  }
  entry.onDatasetReady = onDatasetReady;
  container.replaceChildren(entry.element);
}

/** After "Use this dataset" the Setup form repaints with Step 2 filled; bring that step into view. */
function showColumnsStep() {
  if (typeof document === "undefined" || typeof requestAnimationFrame !== "function") return;
  requestAnimationFrame(() => {
    const field = document.getElementById("f-pk");
    const step = field && (field.closest(".fstep") || field);
    if (step && typeof step.scrollIntoView === "function") step.scrollIntoView({ block: "center", behavior: "smooth" });
  });
}

function shell(element, title, line, extra = "") {
  element.innerHTML = `<div class="obhead"><span class="obt">${esc(title)}</span>${
    line ? `<span class="obh">${esc(line)}</span>` : ""
  }${extra}</div><div class="obbody"></div>`;
  return element.querySelector(".obbody");
}

function startTrain(element, { uc, clientId, onDatasetReady }) {
  const body = shell(element, "Build a dataset from raw tables", "One step at a time; you can go back to any step before building.");
  onboardingPanel(body, { clientId, useCaseId: uc.id, onDatasetReady, entity: uc.entity || null });
}

async function startScore(element, { uc, clientId, modelVersion, onDatasetReady }) {
  let body = shell(element, "This month's tables", "Finding the setup this model was trained with…");
  let found;
  try {
    found = await savedRecipe(modelVersion, clientId, uc.id);
  } catch (error) {
    found = { error };
  }
  if (!found.spec) {
    body = shell(element, "This month's tables", "");
    body.innerHTML = found.error
      ? errorBox(found.error, { title: "The setup this model was trained with could not be found." })
      : emptyState({ title: found.title, text: found.reason });
    return;
  }
  body = shell(
    element,
    "This month's tables",
    `Using the setup from ${fmtDate(found.spec.created_at)}`,
    `<div class="ob-headtech">${techDetails([["Setup id", found.spec.spec_id]], "Details")}</div>`,
  );
  onboardingPanel(body, {
    clientId,
    useCaseId: uc.id,
    onDatasetReady,
    entity: uc.entity || null,
    replay: { spec: found.spec, trainingKeys: found.trainingKeys },
  });
}

/**
 * The setup `version` was trained with, or the reason there is none to replay.
 *
 * Three lookups, each a document the API already serves: the version names its training run, the
 * run names the dataset it read (`run.json`'s `dataset_id`), and the dataset's manifest names the
 * setup. Every refusal is worded for the person holding the Score button. The training build's own
 * report is read too (best effort), so this month's build shows only the warnings that differ.
 */
async function savedRecipe(version, clientId, useCaseId) {
  if (!version) {
    return {
      title: "No trained model yet",
      reason: "Train a model from this client's tables first: scoring reads this month's tables the way that model's were read.",
    };
  }
  const detail = await getRun(version.run_id);
  const datasetId = detail.run.dataset_id;
  if (!datasetId) {
    return {
      title: "This model was trained from a prepared file",
      reason: "Score it with a prepared file instead, or choose a model trained from raw tables.",
    };
  }
  const { manifest } = await getDataset(datasetId);
  if (!manifest) {
    return {
      title: "The setup cannot be found",
      reason: "The dataset this model was trained on has no record of how it was built, so its setup cannot be replayed.",
    };
  }
  if (manifest.client_id !== clientId) {
    return {
      title: "This model belongs to another client",
      reason: "It was trained on another client's tables. Switch client in the top bar (Client ▾) to score with its setup.",
    };
  }
  const { specs } = await listOnboardingSpecs(clientId, useCaseId);
  const spec = specs.find((candidate) => candidate.spec_id === manifest.spec_id);
  if (!spec) {
    return {
      title: "The setup is no longer saved",
      reason: "The setup this model was trained with is no longer saved for this client. Train a new model first.",
    };
  }
  let trainingKeys = null;
  try {
    const report = await getDatasetReport(datasetId);
    trainingKeys = new Set((report.checks || []).map((check) => `${check.code}|${check.column || ""}`));
  } catch {
    trainingKeys = null; // no training report: every warning of this month's build is shown
  }
  return { spec, trainingKeys };
}

// --- the panel's own styles -------------------------------------------------------------------------
// Injected once, like the other modules' `styles.js`: every rule reads the tokens `index.html`
// defines (light, dark and the theme override apply), and every class is scoped under `.ob`.

const CSS = `
.ob>.obhead .ob-headtech{flex-basis:100%}
.ob>.obhead .ob-headtech details.tech{margin:0}
.ob .ob-body{padding:8px 16px 24px;display:flex;flex-direction:column;gap:12px;background:var(--surface)}
.ob .ob-step>summary .sn{color:var(--ok)}
.ob .ob-step.done>summary .sn{background:var(--ok-t);border-color:transparent}
.ob .ob-step[open]>summary .ob-edit{visibility:hidden}
.ob .ob-edit{color:var(--brand-blue);font-weight:500}
.ob .ob-toolbar{gap:12px}
.ob .ob-body>.btn,.ob .ob-sub>.btn,.ob .ob-sub>.ob-upload{align-self:flex-start}
.ob .ob-toolbar .fhint{margin:0}
.ob .ob-actions{gap:12px}
.ob .ob-upload{position:relative}
.ob .ob-upload:focus-within{outline:2px solid var(--brand-blue);outline-offset:2px}
.ob .empty-state{padding:16px 0}
.ob .ob-sel{min-width:220px;width:100%;max-width:360px}
.ob .tbl-wrap .control.sel{height:36px}
.ob .ob-role{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.ob .ob-role-name{font-weight:500;color:var(--ink)}
.ob .ob-tag{font-size:12px;color:var(--muted)}
.ob details.ob-change{display:inline-block}
.ob details.ob-change>summary{cursor:pointer;list-style:none;display:inline-flex;align-items:center;min-height:24px;font-size:12px;font-weight:500;color:var(--brand-blue)}
.ob details.ob-change>summary::-webkit-details-marker{display:none}
.ob details.ob-change[open]{display:flex;flex-direction:column;gap:8px;flex-basis:100%;padding-top:4px}
.ob details.ob-change .fhint{margin:0}
.ob .ob-rowmenu .menu-pop{min-width:220px}
.ob .dialog.ob-dialog{max-width:520px;display:flex;flex-direction:column;gap:12px}
.ob .dialog.ob-wide{max-width:none}
.ob .ob-dialog p{margin:0;font-size:13px;color:var(--ink2)}
.ob .ob-dialog .ob-dialog-t{font-size:14px;font-weight:600;color:var(--ink)}
.ob .ob-fields{border:0;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:12px}
.ob .control input:not([type]){appearance:none;-webkit-appearance:none;width:100%;height:100%;border:0;background:transparent;color:inherit;font:inherit;padding:0 12px;outline:none}
.ob .ob-fields .field{width:240px}
.ob .ob-fields .field.xs{width:140px}
.ob .ob-fields .fhint{margin:0}
.ob .ob-note{border:1px solid var(--line);border-left:3px solid var(--warn);border-radius:8px;padding:12px 16px;background:var(--soft);font-size:13px;color:var(--ink2)}
.ob .ob-note p{margin:0}
.ob .ob-note .ob-note-t{font-weight:600;color:var(--ink);margin-bottom:4px}
.ob .ob-muted{color:var(--muted)}
.ob .ob-samples{color:var(--ink2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:inline-block;max-width:320px;vertical-align:bottom}
.ob .ob-colname{font-weight:500;color:var(--ink)}
.ob .ob-mapto{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.ob .ob-vmap{display:flex;align-items:center;gap:8px;padding:4px 0}
.ob .ob-vmap .control{min-width:140px}
.ob .card.ob-map{margin:0}
.ob .card.ob-map.flag{border-color:var(--warn)}
.ob .ob-map-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;padding:12px 16px}
.ob .ob-map-t{flex:1 1 240px;min-width:0}
.ob .ob-map-t h3{margin:0;padding:0;border:0;font-size:14px;background:none}
.ob .ob-map-sum{display:block;font-size:12px;color:var(--muted);margin-top:2px}
.ob .ob-map-head .kv.ob-map-state{padding:0;border:0;gap:4px;align-items:center}
.ob .ob-ok{color:var(--ok);font-weight:600}
.ob .ob-map-body{border-top:1px solid var(--line)}
.ob .ob-card-foot{padding:12px 16px}
.ob .ob-card-foot .fhint{margin:0}
.ob .ob-map-body .vlist{margin:12px 16px}
.ob details.ob-fold{border:1px solid var(--line);border-radius:8px;background:var(--surface)}
.ob details.ob-fold>summary{cursor:pointer;list-style:none;display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:40px;padding:8px 16px;font-size:13px;font-weight:500;color:var(--ink)}
.ob details.ob-fold>summary::-webkit-details-marker{display:none}
.ob details.ob-fold>summary::after{content:"";flex:none;width:7px;height:7px;border-right:1.5px solid var(--muted);border-bottom:1.5px solid var(--muted);transform:rotate(45deg);transition:transform .15s}
.ob details.ob-fold[open]>summary::after{transform:rotate(225deg)}
.ob details.ob-fold[open]>summary{border-bottom:1px solid var(--line)}
.ob details.ob-fold>*:not(summary){margin:12px 16px}
.ob details.ob-fold .card{margin:12px 16px}
.ob .ob-link{margin-left:auto;color:var(--brand-blue);font-weight:500;font-size:12px}
.ob .ob-sub{display:flex;flex-direction:column;gap:8px;padding-top:12px;border-top:1px solid var(--line)}
.ob .ob-sub:first-child{border-top:0;padding-top:0}
.ob .ob-sub-t{margin:0;font-size:12px;font-weight:600;color:var(--ink2)}
.ob .ob-line{margin:0;font-size:14px;color:var(--ink);line-height:1.9}
.ob .control.ob-num{display:inline-flex;width:88px;height:32px;vertical-align:middle}
.ob .ob-fgroup{padding:4px 0}
.ob .ob-fgroup-t{margin:8px 0 4px;font-size:12px;font-weight:600;color:var(--muted)}
.ob .check.ob-feature{height:auto;align-items:flex-start;padding:8px 0;gap:12px}
.ob .ob-ftext{display:flex;flex-direction:column;gap:2px;flex:1;min-width:0}
.ob .ob-fdesc{color:var(--ink)}
.ob .ob-fname{font-size:12px;color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.ob .ob-flist{margin:0;padding:0 0 0 16px;display:flex;flex-direction:column;gap:8px;font-size:13px}
.ob .ob-flist li{display:flex;flex-direction:column;gap:2px}
.ob .ob-preview{display:flex;flex-direction:column;gap:12px}
.ob .ob-lead-s{margin:0;font-size:14px;font-weight:500;color:var(--ink)}
.ob .ob-verdict{display:flex;flex-direction:column;gap:12px;padding:16px 20px;border:1px solid var(--line);border-left:3px solid var(--ok);border-radius:8px;background:var(--soft)}
.ob .ob-verdict.bad{border-left-color:var(--bad)}
.ob .ob-verdict .ob-lead{margin:0;font-size:20px;font-weight:600;color:var(--ink);line-height:1.35}
.ob .ob-folded{display:flex;align-items:center;gap:12px;padding:12px 16px;font-size:14px;font-weight:500;color:var(--ink)}
.ob .vlist{margin-top:0}
.ob .vitem details.tech{margin-top:4px;font-size:12px}
.ob details.ob-fold .card>.empty,.ob details.ob-fold .card>.fhint{padding:12px 20px;margin:0}
@media (max-width:700px){
  .ob .ob-body{padding:8px 12px 20px}
  .ob .ob-sel{min-width:0;max-width:none}
  .ob .ob-samples{max-width:180px}
  .ob .ob-fields .field,.ob .ob-fields .field.xs{width:100%}
  .ob .ob-verdict .ob-lead{font-size:17px}
  .ob .brow{grid-template-columns:120px 1fr 48px}
  .ob .tbl-stack td:has(.control),.ob .tbl-stack td:has(.ob-role){flex-direction:column;align-items:stretch;text-align:left}
  .ob .ob-map-head .btn{white-space:normal;height:auto;min-height:32px;padding:8px 12px;text-align:left}
  .ob .ob-toolbar .btn,.ob .ob-actions .btn{flex:1 1 auto;white-space:normal;height:auto;min-height:40px;padding:8px 16px;text-align:center}
}
`;

let injected = false;

function injectStyles() {
  if (injected || typeof document === "undefined") return;
  injected = true;
  const style = document.createElement("style");
  style.id = "ob-styles";
  style.textContent = CSS;
  document.head.appendChild(style);
}
