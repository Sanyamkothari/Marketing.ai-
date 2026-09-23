// "Build from raw tables" as a way to fill Setup Step 1 (Plan A M35; prototype `predStep1()`,
// screenshots `02`, `07` and `08`).
//
// `ui/usecase.js` owns the Setup form and knows nothing about onboarding: it asks the registered
// setup source (`modules/router.js`) for a card to offer beside "Upload a prepared file", and, once
// that card is chosen, hands it an empty element to mount into. This module answers both.
//
// **Train mode** mounts the four-step panel (`panel.js`) for the client picked in the header.
// **Score mode** first finds the recipe the model being scored was trained with - the version's
// training run names the dataset it read, and the dataset's manifest names the recipe - and mounts
// the same panel in replay mode on it, so this month's tables go through last month's decisions.
// A model trained on a prepared file has no recipe, and the panel says so instead of guessing one.
//
// The Setup form repaints its whole `<main>` on every change, so a panel is created once per
// (mode, client, use case, model) and its element is moved into each fresh placeholder: the panel's
// working edits, its uploads in flight and its build poll all survive the repaint.

import { getRun } from "../../api.js";
import { errorBox, esc, fmtStamp } from "../../dom.js";
import { getDataset, listOnboardingSpecs } from "./api.js";
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
        text: "The saved recipe maps and builds them exactly as last time.",
      }
    : {
        title: "Build from raw tables",
        text: "Bring the tables you already have. We map them, build the features and the label, and write the dataset.",
      };
}

/**
 * Mount the raw-tables panel into `container` (an element the Setup form just painted).
 *
 * `onDatasetReady(payload)` receives the panel's payload plus `clientId`, so the Run request names
 * the client the dataset belongs to. `modelVersion` is the version score mode would score with - its
 * recipe is the one replayed.
 */
export function mountSetup(container, { uc, mode, modelVersion, onDatasetReady }) {
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
    const forward = (payload) => entry.onDatasetReady({ ...payload, clientId });
    if (mode === "score") startScore(entry.element, { uc, clientId, modelVersion, onDatasetReady: forward });
    else startTrain(entry.element, { uc, clientId, onDatasetReady: forward });
  }
  entry.onDatasetReady = onDatasetReady;
  container.replaceChildren(entry.element);
}

function shell(element, title, hint) {
  element.innerHTML = `<div class="obhead"><span class="obt">${esc(title)}</span><span class="obh">${esc(
    hint,
  )}</span></div><div class="obbody"></div>`;
  return element.querySelector(".obbody");
}

function startTrain(element, { uc, clientId, onDatasetReady }) {
  const body = shell(element, "Build a dataset from raw tables", "Four steps. You can go back to any of them before building.");
  onboardingPanel(body, { clientId, useCaseId: uc.id, onDatasetReady });
}

async function startScore(element, { uc, clientId, modelVersion, onDatasetReady }) {
  let body = shell(element, "Build a dataset from raw tables", "Finding the recipe this model was trained with…");
  let found;
  try {
    found = await savedRecipe(modelVersion, clientId, uc.id);
  } catch (error) {
    found = { error };
  }
  if (!found.spec) {
    body = shell(element, "Build a dataset from raw tables", "No saved recipe to replay.");
    body.innerHTML = found.error
      ? errorBox(found.error)
      : `<div class="empty">${esc(found.reason)}</div>`;
    return;
  }
  body = shell(
    element,
    "Build a dataset from raw tables",
    `Saved recipe ${found.spec.spec_id}, saved ${fmtStamp(found.spec.created_at)}.`,
  );
  onboardingPanel(body, { clientId, useCaseId: uc.id, onDatasetReady, replay: { spec: found.spec } });
}

/**
 * The recipe `version` was trained with, or the reason there is none to replay.
 *
 * Three lookups, each a document the API already serves: the version names its training run, the
 * run names the dataset it read (`run.json`'s `dataset_id`), and the dataset's manifest names the
 * recipe. Every refusal is worded for the person holding the Score button.
 */
async function savedRecipe(version, clientId, useCaseId) {
  if (!version) return { reason: "Train a model first: scoring replays the recipe its training data was built with." };
  const detail = await getRun(version.run_id);
  const datasetId = detail.run.dataset_id;
  if (!datasetId) {
    return {
      reason:
        "The selected model was trained on a prepared file, so there is no saved recipe to replay. " +
        "Upload a prepared file instead, or choose a model trained from raw tables.",
    };
  }
  const { manifest } = await getDataset(datasetId);
  if (!manifest) {
    return { reason: "The dataset this model was trained on has no manifest, so its recipe cannot be found." };
  }
  if (manifest.client_id !== clientId) {
    return {
      reason:
        "The selected model was trained on another client's tables. Choose that client in the header " +
        "to score with its recipe.",
    };
  }
  const { specs } = await listOnboardingSpecs(clientId, useCaseId);
  const spec = specs.find((candidate) => candidate.spec_id === manifest.spec_id);
  if (!spec) return { reason: "The recipe this model was trained with is no longer saved for this client." };
  return { spec };
}
