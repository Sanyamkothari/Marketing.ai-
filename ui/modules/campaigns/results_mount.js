// Wires the Review page's tabs (`results_tabs.js` draws them): switching shows one panel; the parts that need requests are
// loaded once for this campaign, from the training and scoring runs' own artefacts, and drawn by `ui/pages.js`.

import { ApiError, contactListNow, getArtefacts, getRun, getUseCase, loadContactList, scoresUrl } from "../../api.js";
import { emptyState, errorBox } from "../../dom.js";
import { repaintContactList } from "../../contact_list.js";
import { bindPage, columnDescriptions, isUplift, pageArtefacts, renderPage } from "../../pages.js";
import { getScreen } from "../datadoctor/api.js";
import { doctorChanges, doctorChangesHtml } from "./doctor_changes.js";
import { visit } from "./results_tabs.js";

const asError = (error) => errorBox(error instanceof ApiError || (error && error.message) ? error : new Error("This part could not be loaded."));

/** The training and scoring runs' pages, as markup: `{ data, model, outputTrain, outputScore }`. Each is its own answer, so one that fails says so alone. */
async function loadParts({ useCaseId, trainRunId, scoreRunId, caseId }) {
  const uc = await getUseCase(useCaseId);
  const scoreArt = {};
  const draw = async (kind, runId, extra = {}) => {
    const { run } = await getRun(runId);
    // An effect model's Model and Output are the uplift module's own screens.
    if (isUplift(run) && kind !== "data") {
      const href = `#/uplift/${encodeURIComponent(uc.id)}/${kind}/${encodeURIComponent(run.run_id)}`;
      return `<section class="card">${emptyState({ title: "This is an effect model", text: "Its results are shown with the other effect models.", action: { label: "Open its page", href, kind: "secondary" } })}</section>`;
    }
    const art = await getArtefacts(run.run_id, pageArtefacts(kind, run));
    if (runId === scoreRunId) scoreArt.summary = art["scoring_summary.json"] || null;
    return renderPage(kind, uc, run, art, scoresUrl(run.run_id), { embedded: true, ...extra });
  };
  const safe = (promise) => promise.catch(asError);
  const doctor = caseId
    ? getScreen(caseId).then(
        (screen) => ({ screen }),
        () => ({ screen: null }),
      )
    : Promise.resolve(null);
  const [found, run] = await Promise.all([doctor, trainRunId ? getRun(trainRunId) : null]);
  const outcome = run && run.run && run.run.target ? { name: run.run.target, text: (uc.target && uc.target.definition) || "" } : null;
  // A reader without the Analyst role gets no file preview in the screen: the training table's column names then come
  // from the training run's own prepare.json (names only).
  const noPreview = Boolean(found && found.screen && trainRunId && !(found.screen.view && found.screen.view.preview));
  const prepare = noPreview ? (await getArtefacts(trainRunId, ["prepare.json"]).catch(() => ({})))["prepare.json"] : null;
  const doctorHtml = found
    ? doctorChangesHtml(
        found.screen
          ? doctorChanges(found.screen, {
              entity: String(uc.entity || "customer").toLowerCase(),
              describe: (name) => columnDescriptions(uc).get(name),
              outcome,
              trainingColumns: (prepare && prepare.feature_columns) || [],
            })
          : null,
        caseId,
      )
    : "";
  const score = scoreRunId ? contactListNow(scoreRunId) : null;
  const [data, model, outputTrain, outputScore] = await Promise.all([
    trainRunId ? safe(draw("data", trainRunId, { doctorHtml })) : "",
    trainRunId ? safe(draw("model", trainRunId)) : "",
    trainRunId ? safe(draw("output", trainRunId)) : "",
    scoreRunId ? safe(draw("output", scoreRunId, { contactList: score })) : "",
  ]);
  return { data, model, outputTrain, outputScore, scoring: scoreArt.summary || null };
}

/**
 * Wire the tabs: switching shows one panel; the parts that need requests are loaded once for this campaign. `ctx` is
 * `{ useCaseId, trainRunId, scoreRunId, caseId }`. Safe to call again after a redraw: the answer is kept.
 */
export function mountResultsTabs(root, ctx) {
  const tabs = root && root.querySelector("[data-cw-tabs]");
  if (!tabs) return;
  const key = [ctx.useCaseId, ctx.trainRunId, ctx.scoreRunId, ctx.caseId].join("|");
  if (visit.key !== key) Object.assign(visit, { key, loaded: null, tab: "data" });
  const select = (id) => {
    visit.tab = id;
    for (const b of tabs.querySelectorAll("[data-cw-tab]")) {
      const on = b.getAttribute("data-cw-tab") === id;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", String(on));
    }
    for (const panel of tabs.querySelectorAll("[data-cw-panel]")) panel.hidden = panel.getAttribute("data-cw-panel") !== id;
  };
  for (const b of tabs.querySelectorAll("[data-cw-tab]")) b.addEventListener("click", () => select(b.getAttribute("data-cw-tab")));
  select(visit.tab);
  if (!visit.loaded) visit.loaded = loadParts(ctx);
  const loading = visit.loaded;
  loading.then(
    (parts) => {
      if (!tabs.isConnected) return;
      const fill = (id, html, kind) => {
        const el = tabs.querySelector(`[data-cw-slot="${id}"]`);
        if (!el) return;
        el.innerHTML = html;
        if (kind) bindPage(kind, el);
      };
      fill("data", parts.data, "data");
      fill("model", parts.model, "model");
      fill("output-train", parts.outputTrain, "output");
      fill("output-score", parts.outputScore, "output");
      if (ctx.scoreRunId) {
        loadContactList(ctx.scoreRunId).then((facts) => repaintContactList(tabs, { runId: ctx.scoreRunId, facts, scoring: parts.scoring, scoresHref: scoresUrl(ctx.scoreRunId) }));
      }
    },
    (error) => {
      visit.loaded = null; // asked again on the next draw
      if (!tabs.isConnected) return;
      for (const el of tabs.querySelectorAll("[data-cw-slot]")) el.innerHTML = asError(error);
    },
  );
}
