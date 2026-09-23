// Behaviour of the uplift screens: fetching, polling, form events. Rendering is `views.js`'s job.
//
// One controller per screen kind, each owning a plain state object the matching view renders
// from - the shape `ui/usecase.js` uses for the Phase 1 use-case screen, so both behave alike: a
// state change calls `rerender()`, which repaints and re-binds. Every number a view shows comes
// from a response stored on that state; nothing here computes a metric of its own.

import { ApiError, cancelRun, getModels, getRun, getRuns, postRun, postUpload, scoresUrl } from "../../api.js";
import {
  getCampaignResults,
  getTreatmentCandidates,
  getUpliftArtefacts,
  postCampaignResults,
  postOpe,
  postUpliftRun,
} from "./api.js";
import { MODEL_ARTEFACTS, OUTPUT_ARTEFACTS, routes, upliftVersions } from "./views.js";

const POLL_MS = 2000;

const isUplift = (run) => run && run.problem_type === "uplift";

/** An uplift use case's runs, newest first as the API lists them. Errors leave the list empty. */
async function upliftRuns(useCaseId) {
  try {
    const { runs } = await getRuns(useCaseId);
    return (runs || []).filter(isUplift);
  } catch (error) {
    if (error instanceof ApiError) return [];
    throw error;
  }
}

/**
 * The two reports of a 409 from `POST /uplift/runs`, or `null` when the error is anything else.
 *
 * The API sends `{detail: {code, message, path}, validation, uplift_validation}`: the reports sit at
 * the TOP level, beside an error envelope that is always an object. So the top level is read first;
 * `detail` is only a fallback, for a server that nests the reports inside it (as FastAPI's
 * `HTTPException` would). Reading `detail` first dropped every real refusal: the envelope has no
 * report, so the Setup screen never showed either report or the acknowledge control.
 */
export function refusal(error) {
  if (!(error instanceof ApiError) || error.status !== 409 || !error.body) return null;
  const top = error.body;
  const nested = top.detail && typeof top.detail === "object" ? top.detail : {};
  const body = top.validation || top.uplift_validation ? top : nested;
  if (!body.validation && !body.uplift_validation) return null;
  return { validation: body.validation || null, upliftValidation: body.uplift_validation || null };
}

// --- Setup / Running / Results -----------------------------------------------------------------

const SETUP_STATE = new Map();

function setupState(useCaseId) {
  if (!SETUP_STATE.has(useCaseId)) {
    SETUP_STATE.set(useCaseId, {
      view: "setup",
      mode: "train",
      upload: null,
      uploading: false,
      uploadError: null,
      pk: "",
      target: "",
      treatment: "",
      candidates: null,
      candidatesLoading: false,
      candidatesError: null,
      models: [],
      modelVersionId: "",
      runs: [],
      validation: null,
      upliftValidation: null,
      acknowledged: [],
      submitting: false,
      submitError: null,
      runId: null,
      detail: null,
    });
  }
  return SETUP_STATE.get(useCaseId);
}

export function createSetupController(uc, rerender) {
  const s = setupState(uc.id);
  let timer = null;

  const stop = () => {
    if (timer) clearInterval(timer);
    timer = null;
  };

  async function refreshLists() {
    s.runs = await upliftRuns(uc.id);
    try {
      const models = await getModels(uc.id);
      s.models = upliftVersions(models.versions);
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      s.models = [];
    }
    if (!s.models.some((v) => v.version.model_id === s.modelVersionId)) {
      const champion = s.models.find((v) => v.is_champion) || s.models[0];
      s.modelVersionId = champion ? champion.version.model_id : "";
    }
  }

  function showSetup() {
    stop();
    s.view = "setup";
    s.runId = null;
    s.detail = null;
  }

  async function loadRun(runId) {
    s.runId = runId;
    s.detail = await getRun(runId);
    const state = s.detail.run.state;
    s.view = state === "pending" || state === "running" ? "running" : "results";
  }

  function poll() {
    stop();
    timer = setInterval(async () => {
      if (!s.runId) return stop();
      try {
        s.detail = await getRun(s.runId);
      } catch {
        stop();
        return;
      }
      const state = s.detail.run.state;
      if (state === "pending" || state === "running") {
        rerender();
        return;
      }
      stop();
      s.view = "results";
      await refreshLists();
      rerender();
    }, POLL_MS);
  }

  async function loadCandidates() {
    s.candidates = null;
    s.candidatesError = null;
    if (s.mode !== "train" || !s.upload) return;
    s.candidatesLoading = true;
    rerender();
    try {
      s.candidates = await getTreatmentCandidates(s.upload.upload_id, uc.id);
      const detected = s.candidates && s.candidates.detected;
      if (detected && !s.treatment) s.treatment = detected;
      if (s.target && s.target === s.treatment) s.target = "";
    } catch (error) {
      s.candidatesError = error;
    }
    s.candidatesLoading = false;
  }

  async function upload(file) {
    Object.assign(s, {
      uploading: true,
      uploadError: null,
      upload: null,
      pk: "",
      target: "",
      treatment: "",
      candidates: null,
      validation: null,
      upliftValidation: null,
      acknowledged: [],
      submitError: null,
    });
    rerender();
    try {
      s.upload = await postUpload(file, uc.id, s.mode);
      const profile = s.upload.profile;
      s.pk = (profile.primary_key_candidates || [])[0] || "";
      s.target = s.mode === "train" ? profile.target_candidate || "" : "";
      s.uploading = false;
      await loadCandidates();
    } catch (error) {
      s.uploadError = error;
    }
    s.uploading = false;
    rerender();
  }

  async function submit() {
    s.submitting = true;
    s.submitError = null;
    rerender();
    try {
      let created;
      if (s.mode === "train") {
        const payload = {
          use_case: uc.id,
          upload_id: s.upload.upload_id,
          primary_key: s.pk,
          target: s.target,
          treatment_column: s.treatment,
        };
        if (s.acknowledged.length) payload.overrides = { validation: { acknowledged: s.acknowledged.slice() } };
        created = await postUpliftRun(payload);
      } else {
        // A scoring run of an uplift model goes through Phase 1's `POST /runs`: the model version
        // it names is an uplift one, and that is what makes the run write segments and a treat list.
        created = await postRun({
          use_case: uc.id,
          mode: "score",
          upload_id: s.upload.upload_id,
          primary_key: s.pk,
          model_version_id: s.modelVersionId,
          overrides: {},
        });
      }
      s.submitting = false;
      s.validation = null;
      s.upliftValidation = null;
      window.location.hash = routes.run(uc.id, created.run_id);
    } catch (error) {
      s.submitting = false;
      const refused = refusal(error);
      if (refused) Object.assign(s, refused);
      else s.submitError = error;
      rerender();
    }
  }

  function bind(root) {
    const on = (id, event, fn) => {
      const el = root.querySelector(`#${id}`);
      if (el) el.addEventListener(event, fn);
    };
    root.querySelectorAll("[data-umode]").forEach((button) =>
      button.addEventListener("click", () => {
        if (s.mode === button.dataset.umode) return;
        Object.assign(s, {
          mode: button.dataset.umode,
          upload: null,
          pk: "",
          target: "",
          treatment: "",
          candidates: null,
          validation: null,
          upliftValidation: null,
          acknowledged: [],
          submitError: null,
          uploadError: null,
        });
        rerender();
      }),
    );
    on("u-file", "change", (event) => {
      const file = event.target.files[0];
      if (file) upload(file);
    });
    on("u-pk", "change", (event) => {
      s.pk = event.target.value;
      if (s.target === s.pk) s.target = "";
      if (s.treatment === s.pk) s.treatment = "";
      rerender();
    });
    on("u-target", "change", (event) => {
      s.target = event.target.value;
      rerender();
    });
    on("u-treatment", "change", (event) => {
      s.treatment = event.target.value;
      if (s.target === s.treatment) s.target = "";
      s.validation = null;
      s.upliftValidation = null;
      s.acknowledged = [];
      rerender();
    });
    on("u-model", "change", (event) => {
      s.modelVersionId = event.target.value;
    });
    root.querySelectorAll("[data-uack]").forEach((box) =>
      box.addEventListener("change", () => {
        const token = box.dataset.uack;
        s.acknowledged = box.checked
          ? [...new Set([...s.acknowledged, token])]
          : s.acknowledged.filter((t) => t !== token);
        rerender();
      }),
    );
    on("u-setup", "submit", (event) => {
      event.preventDefault();
      if (s.submitting) return;
      submit();
    });
    on("u-cancel", "click", async () => {
      if (!s.runId) return;
      try {
        await cancelRun(s.runId);
        s.detail = await getRun(s.runId);
      } catch (error) {
        s.submitError = error;
      }
      rerender();
    });
  }

  return { state: s, bind, refreshLists, loadRun, showSetup, poll, stop };
}

// --- Model page ----------------------------------------------------------------------------------

export function createModelController(runId, rerender) {
  const s = { run: null, art: null, ope: { report: null, topSharePct: "", submitting: false, error: null } };

  async function load() {
    const [detail, art] = await Promise.all([getRun(runId), getUpliftArtefacts(runId, MODEL_ARTEFACTS)]);
    s.run = detail.run;
    s.art = art;
    s.ope.report = art["ope_report.json"];
  }

  function bind(root) {
    const form = root.querySelector("#u-ope");
    if (!form) return;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = root.querySelector("#u-ope-share");
      const pct = Number(input && input.value);
      s.ope.topSharePct = input ? input.value : "";
      if (!(pct > 0 && pct <= 100)) {
        s.ope.error = new ApiError(0, "INVALID_INPUT", "Enter a share between 1 and 100 percent.", null);
        rerender();
        return;
      }
      s.ope.submitting = true;
      s.ope.error = null;
      rerender();
      try {
        s.ope.report = await postOpe(runId, { top_share: pct / 100 });
      } catch (error) {
        s.ope.error = error;
      }
      s.ope.submitting = false;
      rerender();
    });
  }

  return { state: s, load, bind };
}

// --- Output page ---------------------------------------------------------------------------------

export function createOutputController(uc, runId) {
  const s = { run: null, art: null, scoreRuns: [], scoresHref: scoresUrl(runId) };

  async function load() {
    const [detail, art, runs] = await Promise.all([
      getRun(runId),
      getUpliftArtefacts(runId, OUTPUT_ARTEFACTS),
      upliftRuns(uc.id),
    ]);
    s.run = detail.run;
    s.art = art;
    s.scoreRuns = runs.filter((r) => r.mode === "score");
  }

  return { state: s, load, bind: () => {} };
}

// --- Campaign results page -----------------------------------------------------------------------

export function createCampaignController(uc, runId, rerender) {
  const s = {
    run: null,
    report: null,
    loadError: null,
    upload: null,
    uploading: false,
    uploadError: null,
    form: {},
    submitting: false,
    submitError: null,
  };

  async function load() {
    const detail = await getRun(runId);
    s.run = detail.run;
    if (s.run.mode !== "score") return;
    try {
      s.report = await getCampaignResults(runId);
    } catch (error) {
      s.loadError = error;
    }
  }

  async function upload(file) {
    Object.assign(s, { uploading: true, uploadError: null, upload: null, form: {}, submitError: null });
    rerender();
    try {
      // Outcomes are rows to join onto a scoring run, not training data, so they are profiled as a
      // scoring upload: nothing about them is validated as a target.
      s.upload = await postUpload(file, uc.id, "score");
    } catch (error) {
      s.uploadError = error;
    }
    s.uploading = false;
    rerender();
  }

  /** The request body: only the fields the user set, so the server's own defaults apply to the rest. */
  function payload() {
    const f = s.form;
    const body = { upload_id: s.upload.upload_id, outcome_column: f.outcome_column };
    if (f.positive_label) body.positive_label = f.positive_label;
    if (f.outcome_window_days) body.outcome_window_days = Number(f.outcome_window_days);
    if (f.treatment_date_column) body.treatment_date_column = f.treatment_date_column;
    // A date picked on screen means "judged at the end of that day", in UTC like every stamp the
    // engine writes; the API needs an aware datetime, not a bare date.
    if (f.as_of) body.as_of = `${f.as_of}T23:59:59Z`;
    return body;
  }

  function bind(root) {
    const field = (id, key, event = "change") => {
      const el = root.querySelector(`#${id}`);
      if (!el) return;
      el.addEventListener(event, () => {
        s.form[key] = el.value;
        if (key === "outcome_column") rerender();
      });
    };
    const file = root.querySelector("#u-camp-file");
    if (file) {
      file.addEventListener("change", (event) => {
        const chosen = event.target.files[0];
        if (chosen) upload(chosen);
      });
    }
    field("u-camp-outcome", "outcome_column");
    field("u-camp-date", "treatment_date_column");
    field("u-camp-window", "outcome_window_days", "input");
    field("u-camp-label", "positive_label", "input");
    field("u-camp-asof", "as_of");
    const form = root.querySelector("#u-camp");
    if (form) {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!s.upload || !s.form.outcome_column || s.submitting) return;
        s.submitting = true;
        s.submitError = null;
        rerender();
        try {
          s.report = await postCampaignResults(runId, payload());
          s.loadError = null;
        } catch (error) {
          s.submitError = error;
        }
        s.submitting = false;
        rerender();
      });
    }
  }

  return { state: s, load, bind };
}
