/* A run the use-case screen watches (ui/usecase.js `poll`) tells the page when it stops running
   (`RUN_FINISHED_EVENT`, `ui/dom.js`), so what it changed elsewhere is read again: the approvals badge
   on Results recounts after a training run (docs/UI_AUDIT.md §8.4 item 1; the badge's side is
   tests/integration/production/ui/approvals/badge.test.mjs). REAL bodies from PB_FIXTURES, written by
   tests/integration/test_score_model_default_ui.py. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, fixture, installPage, until } from "../harness.mjs";

const ids = fixture("ids");
let world = "before";

const server = ({ method, path }) => {
  if (method === "GET" && path === "/runs") return { status: 200, body: fixture(`runs_${world}`) };
  if (method === "GET" && path === "/models") return { status: 200, body: fixture(`models_${world}`) };
  if (method === "POST" && path === "/uploads") return { status: 201, body: fixture("upload_train") };
  if (method === "POST" && path === "/runs") {
    world = "after";
    return { status: 202, body: { run_id: ids.justTrainedRun } };
  }
  if (method === "GET" && path === `/runs/${ids.justTrainedRun}`) return { status: 200, body: fixture("run_just_trained") };
  return null;
};
const { w } = installPage(server, { hash: "#/uc/x" });

const { RUN_FINISHED_EVENT } = await import("../../../../../ui/dom.js");
const { createController, useCaseHtml } = await import("../../../../../ui/usecase.js");

const uc = fixture("use_case");
const app = $("#app");
const rerender = () => {
  app.innerHTML = useCaseHtml(uc, controller.state);
  controller.bind(app);
};
const controller = createController(uc, rerender);

const heard = [];
w.addEventListener(RUN_FINISHED_EVENT, (event) => heard.push(event.detail));

test("a training run the screen watched announces that it finished, with the run", async () => {
  const run = fixture("run_just_trained").run;
  assert.equal(run.mode, "train");
  assert.equal(run.state, "done");
  await controller.refreshLists();
  controller.sync();
  rerender();
  $('.seg button[data-mode="train"]').click();
  const input = $("#f-file");
  const file = new w.File(["a,b\n1,2\n"], "train.csv", { type: "text/csv" });
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  input.dispatchEvent(new w.Event("change", { bubbles: true }));
  await until(() => controller.state.upload && !controller.state.uploading, 2000, "the upload");
  assert.equal($("#f-run").disabled, false, $(".reason") ? $(".reason").textContent : "");
  $("#f-setup").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => controller.state.view === "running", 3000, "the running view");
  assert.equal(heard.length, 0, "nothing is announced while the run is only submitted");
  await until(() => heard.length > 0, 5000, "the poll seeing the run finish");
  controller.stop();
  assert.equal(heard.length, 1);
  assert.equal(heard[0].run_id, run.run_id);
  assert.equal(heard[0].mode, "train");
  assert.equal(heard[0].state, "done");
});
