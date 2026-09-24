/* The Setup form's Trained model select (ui/usecase.js, DEC-959) in jsdom, against REAL bodies
   (PB_FIXTURES, written by tests/integration/test_score_model_default_ui.py). The world: a champion
   trained on a prepared file with other columns, a later model trained on client c_demo's built
   dataset, and - once the user trains here - a third model, waiting for approval, trained on the
   very file the user then scores. A stand-in setup source plays the header's client picker: the
   Setup form reads nothing from it but `context().clientId`. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, fixture, installPage, until } from "../harness.mjs";

const ids = fixture("ids");
let world = "before";
let client = null;

const server = (request) => {
  const { method, path, query, body } = request;
  if (method === "GET" && path === "/runs") {
    if (query.client_id) {
      assert.equal(query.mode, "train", "only training runs name the models a client owns");
      return { status: 200, body: fixture(`runs_${query.client_id}_${world}`) };
    }
    return { status: 200, body: fixture(`runs_${world}`) };
  }
  if (method === "GET" && path === "/models") return { status: 200, body: fixture(`models_${world}`) };
  if (method === "POST" && path === "/uploads") return { status: 201, body: fixture(`upload_${nextUpload}`) };
  if (method === "POST" && path === "/runs") {
    if (body.mode === "train") {
      world = "after"; // the run registers its model; the fake does in one step what the job does
      return { status: 202, body: { run_id: ids.justTrainedRun } };
    }
    if (body.model_version_id === ids.champion) return { status: 409, body: fixture("conflict") };
    return { status: 202, body: { run_id: ids.justTrainedRun } };
  }
  if (method === "GET" && path === `/runs/${ids.justTrainedRun}`) return { status: 200, body: fixture("run_just_trained") };
  return null;
};
let nextUpload = "train";
const { w, calls } = installPage(server, { hash: "#/uc/x" });

const router = await import("../../../../../ui/modules/router.js");
router.registerSetupSource({
  name: "test-clients",
  card: () => ({ title: "Upload this month's tables", text: "Replays the saved recipe." }),
  mount: (element) => {
    element.textContent = "raw-tables panel";
  },
  context: () => ({ clientId: client, clientName: client }),
});
const { createController, useCaseHtml } = await import("../../../../../ui/usecase.js");

const uc = fixture("use_case");
const app = $("#app");
/** app.js's `paint`, less the animation: the whole screen redrawn and its events bound again. */
const rerender = () => {
  app.innerHTML = useCaseHtml(uc, controller.state);
  controller.bind(app);
};
const controller = createController(uc, rerender);

/** What app.js does on every paint of the route, the header's client change included. */
async function showUseCase() {
  await controller.refreshLists();
  controller.sync();
  rerender();
}

const select = () => $("#f-scorerun");
const scoreMode = () => $('.seg button[data-mode="score"]').click();
const trainMode = () => $('.seg button[data-mode="train"]').click();

async function chooseFile(name) {
  nextUpload = name;
  const input = $("#f-file");
  const file = new w.File(["a,b\n1,2\n"], `${name}.csv`, { type: "text/csv" });
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  input.dispatchEvent(new w.Event("change", { bubbles: true }));
  await until(() => controller.state.upload && !controller.state.uploading, 2000, `the ${name} upload`);
}

const submit = () => $("#f-setup").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));

test("a new client with no model of its own starts on the champion, and the select is usable", async () => {
  client = ids.newClient;
  await showUseCase();
  scoreMode();
  assert.equal(select().value, ids.champion);
  assert.equal(select().closest(".fstep").classList.contains("locked"), false, "Step 3 is never locked in Score mode");
  assert.ok(
    calls.some((c) => c.path === "/runs" && c.query.client_id === ids.newClient),
    "the client's own training runs were asked for",
  );
});

test("with a client picked in the header, Score mode starts on that client's newest model", async () => {
  client = ids.client;
  await showUseCase();
  assert.equal(select().value, ids.clientModel);
  assert.match(select().selectedOptions[0].textContent, /^WeightedEnsemble_L2 · /);
});

test("right after training, Score mode starts on the model just trained, not the champion", async () => {
  trainMode();
  await chooseFile("train");
  assert.equal($("#f-run").disabled, false, $(".reason").textContent);
  submit();
  await until(() => controller.state.view === "running" || controller.state.view === "results", 3000, "the run");
  controller.stop();
  controller.state.view = "setup";
  rerender();
  scoreMode();
  assert.equal(select().value, ids.justTrained);
  assert.match(select().selectedOptions[0].textContent, /ROC-AUC/);
  const champion = [...select().options].find((o) => o.value === ids.champion);
  assert.match(champion.textContent, /· Champion$/, "the champion is still offered, just not first");
});

const pick = (id) => {
  select().value = id;
  select().dispatchEvent(new w.Event("change", { bubbles: true }));
};

test("a model the user picks is kept across repaints", async () => {
  pick(ids.champion);
  await showUseCase();
  assert.equal(select().value, ids.champion);
});

test("changing the model clears the previous model's SCHEMA_MISMATCH", async () => {
  await chooseFile("score");
  submit();
  await until(() => $(".vlist"), 2000, "the validation report");
  assert.match($(".vlist").textContent, /SCHEMA_MISMATCH/);
  pick(ids.justTrained);
  assert.equal($(".vlist"), null, "the old model's report is gone");
  assert.equal(select().value, ids.justTrained);
  assert.equal($("#f-run").disabled, false, "and the same file can be scored with the chosen model");
});

test("a pick made for one client does not follow the header to another", async () => {
  pick(ids.clientModel);
  await showUseCase();
  assert.equal(select().value, ids.clientModel);
  client = ids.newClient;
  await showUseCase();
  // The model trained here read a prepared file, which names no client, so it is still the default.
  assert.equal(select().value, ids.justTrained);
});
