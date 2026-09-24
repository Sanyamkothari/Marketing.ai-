// Behaviour of the uplift screens: fetching, polling, form events. Rendering is `views.js`'s job.
//
// One controller per screen kind, each owning a plain state object the matching view renders
// from - the shape `ui/usecase.js` uses for the Phase 1 use-case screen, so both behave alike: a
// state change calls `rerender()`, which repaints and re-binds. Every number a view shows comes
// from a response stored on that state; nothing here computes a metric of its own.

import {
  ApiError,
  cancelRun,
  getArtefact,
  getModels,
  getRun,
  getRuns,
  postRun,
  postUpload,
  scoresUrl,
} from "../../api.js";
// The campaign's value view and the demo manifest are the pilot module's endpoints; its calls are
// reused rather than repeated, so the Campaign results page reads the value view's own words and sign.
import { getDemo, getRoi } from "../pilot/api.js";
import {
  getCampaignResults,
  getTreatmentCandidates,
  getUpliftArtefact,
  getUpliftArtefacts,
  postCampaignResults,
  postOpe,
  postUpliftRun,
} from "./api.js";
import {
  DEFAULT_TOP_SHARE_PCT,
  MODEL_ARTEFACTS,
  OUTPUT_ARTEFACTS,
  routes,
  upliftUnavailable,
  upliftVersions,
} from "./views.js";

const POLL_MS = 2000;

const isUplift = (run) => run && run.problem_type === "uplift";

/** Artefacts written on request after a run finished (`POST .../uplift/ope`), so never in its record. */
const ON_REQUEST = new Set(["ope_report.json", "incrementality_report.json"]);

/**
 * The artefacts of `names` worth asking for. `run.json`'s `artefacts` map lists every file the run's
 * own flow wrote, so a name absent from it would only come back `404` - which the screen already
 * renders as "—", but which the browser also logs as a console error on every visit (a scoring run
 * writes no `uplift_validation.json`, for one). Files written on request are always asked for, and a
 * record without the map (an older run) gets every name asked for, as before.
 */
function producedBy(run, names) {
  const listed = run && run.artefacts;
  if (!listed || typeof listed !== "object") return names;
  return names.filter((name) => ON_REQUEST.has(name) || Object.prototype.hasOwnProperty.call(listed, name));
}

/** `names` keyed by file name: fetched when the run produced them, `null` otherwise. */
async function artefactsOf(runId, run, names) {
  const fetched = await getUpliftArtefacts(runId, producedBy(run, names));
  return Object.fromEntries(names.map((name) => [name, name in fetched ? fetched[name] : null]));
}

/** One artefact of a run when its record lists it (or lists nothing); `null` otherwise or on a failure. */
async function optional(run, name, fetch) {
  if (producedBy(run, [name]).length === 0) return null;
  try {
    return await fetch();
  } catch (error) {
    if (error instanceof ApiError) return null;
    throw error;
  }
}

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
      evaluation: null,
      scoresHref: null,
      confirmCancel: false,
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

  const FRESH = {
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
  };

  function showSetup(mode = null) {
    stop();
    s.view = "setup";
    s.runId = null;
    s.detail = null;
    s.confirmCancel = false;
    // `#/uplift/<uc>/score` ("Score customers with this model") opens Setup in score mode.
    if (mode && mode !== s.mode) Object.assign(s, { ...FRESH, mode });
  }

  /** What the Results headline quotes: the evaluation of a finished training run. */
  async function loadFinished() {
    const run = s.detail && s.detail.run;
    s.evaluation = null;
    s.scoresHref = run && run.mode === "score" ? scoresUrl(run.run_id) : null;
    if (!run || run.state !== "done" || run.mode !== "train") return;
    s.evaluation = await optional(run, "uplift_evaluation.json", () =>
      getUpliftArtefact(run.run_id, "uplift_evaluation.json"),
    );
  }

  async function loadRun(runId) {
    s.runId = runId;
    s.confirmCancel = false;
    s.detail = await getRun(runId);
    const state = s.detail.run.state;
    s.view = state === "pending" || state === "running" ? "running" : "results";
    if (s.view === "results") await loadFinished();
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
      s.confirmCancel = false;
      await refreshLists();
      await loadFinished();
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
        Object.assign(s, { ...FRESH, mode: button.dataset.umode });
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
    // Two steps: "Cancel run" asks, "Yes, cancel run" (the same id) cancels.
    on("u-cancel", "click", async () => {
      if (!s.runId) return;
      if (!s.confirmCancel) {
        s.confirmCancel = true;
        rerender();
        return;
      }
      s.confirmCancel = false;
      try {
        await cancelRun(s.runId);
        s.detail = await getRun(s.runId);
      } catch (error) {
        s.submitError = error;
      }
      rerender();
    });
    on("u-cancel-keep", "click", () => {
      s.confirmCancel = false;
      rerender();
    });
  }

  return { state: s, bind, refreshLists, loadRun, showSetup, poll, stop };
}

// --- Model page ----------------------------------------------------------------------------------

export function createModelController(runId, rerender) {
  const s = {
    run: null,
    art: null,
    ope: { report: null, topSharePct: String(DEFAULT_TOP_SHARE_PCT), submitting: false, error: null },
  };

  async function load() {
    const detail = await getRun(runId);
    s.run = detail.run;
    const art = await artefactsOf(runId, s.run, MODEL_ARTEFACTS);
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
  const s = { run: null, art: null, scoreRuns: [], scoresHref: scoresUrl(runId), summary: null };

  async function load() {
    const [detail, runs] = await Promise.all([getRun(runId), upliftRuns(uc.id)]);
    s.run = detail.run;
    const [art, summary] = await Promise.all([
      artefactsOf(runId, s.run, OUTPUT_ARTEFACTS),
      // How many customers were held back at random, for the "Held back to measure" tile.
      s.run.mode === "score"
        ? optional(s.run, "scoring_summary.json", () => getArtefact(runId, "scoring_summary.json"))
        : null,
    ]);
    s.art = art;
    s.summary = summary;
    s.scoreRuns = runs.filter((r) => r.mode === "score");
  }

  return { state: s, load, bind: () => {} };
}

// --- Campaign results page -----------------------------------------------------------------------

/** `YYYY-MM-DD` from a date input -> the UTC instant of 23:59:59 on that day in the browser's zone. */
export function endOfLocalDay(day) {
  const [year, month, date] = String(day).split("-").map(Number);
  const end = new Date(year, month - 1, date, 23, 59, 59);
  if (Number.isNaN(end.getTime())) return `${day}T23:59:59Z`;
  return end.toISOString().replace(/\.\d{3}Z$/, "Z");
}

export function createCampaignController(uc, runId, rerender) {
  const s = {
    run: null,
    report: null,
    roi: null,
    title: "",
    loadError: null,
    upload: null,
    uploading: false,
    uploadError: null,
    form: {},
    submitting: false,
    submitError: null,
  };

  /**
   * The value view of this campaign (`GET /pilot/roi/{run}`): which way round its outcome counts and
   * the "customers gained" figure, so the verdict here uses the value view's words and sign. Without
   * it (the endpoint absent, or nothing measured) the page states the difference in rates only.
   */
  async function loadRoi() {
    s.roi = null;
    if (!s.report || s.report.status === "immature") return;
    try {
      const roi = await getRoi(runId);
      s.roi = roi && roi.status === "measured" ? roi : null;
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
    }
  }

  /** The campaign's name, from the demo manifest when it lists this run. */
  async function loadTitle() {
    try {
      const demo = await getDemo();
      const campaigns = (demo && demo.manifest && demo.manifest.campaigns) || [];
      const mine = campaigns.find((c) => c.score_run_id === runId);
      s.title = (mine && mine.title) || "";
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
    }
  }

  async function load() {
    const detail = await getRun(runId);
    s.run = detail.run;
    const title = loadTitle();
    if (s.run.mode === "score") {
      try {
        s.report = await getCampaignResults(runId);
      } catch (error) {
        s.loadError = error;
      }
      await loadRoi();
    }
    await title;
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
    // A date picked on screen means "judged at the end of that day" - the end of the day where the
    // user is, since that is the day they picked. The API needs an aware datetime, so the local
    // 23:59:59 is sent as its UTC instant. (Sending `<date>T23:59:59Z` instead made the page read
    // "as of 02 Jun, 5:29 am" in India for a user who picked 1 June, because stamps are shown in
    // local time.)
    if (f.as_of) body.as_of = endOfLocalDay(f.as_of);
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
          await loadRoi();
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

// --- Uplift index --------------------------------------------------------------------------------

/**
 * Each use case's newest uplift model, for the index's status line: `{created_at, approved}`, or
 * `null` for none. Asked for after the list is painted; `onStatus(id, status)` fills one row.
 */
export async function loadIndexStatuses(payload, onStatus) {
  const industry = ((payload && payload.industries) || [])[0];
  const cards = industry ? (industry.stages || []).flatMap((stage) => stage.use_cases || []) : [];
  await Promise.all(
    cards
      .filter((u) => !upliftUnavailable(u))
      .map(async (u) => {
        let status = null;
        try {
          const versions = upliftVersions((await getModels(u.id)).versions);
          const newest = versions
            .slice()
            .sort((a, b) => String(b.version.created_at).localeCompare(String(a.version.created_at)))[0];
          status = newest
            ? { created_at: newest.version.created_at, approved: versions.some((v) => v.is_champion) }
            : null;
        } catch (error) {
          if (!(error instanceof ApiError)) throw error;
        }
        onStatus(u.id, status);
      }),
  );
}
