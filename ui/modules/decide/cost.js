// The Cost screen (`#/cost`, Plan J M108, DEC-1318): what finished runs have cost each month at list
// price, and the exchange rate rupee amounts are worked out with. Its own entry point, imported by
// `ui/index.html`'s PLAN-J block; Settings links to it. A person the API lets save a rate (an Admin)
// gets the form; everyone else sees the saved rate. Every figure is the server's.

import { costPageHtml } from "../../cost.js";
import { deleteFxRate, getFxRate, getSpend, putFxRate } from "../../api.js";
import { errorBox, skeleton } from "../../dom.js";
import { canAccess, registerModule, setActiveNav } from "../router.js";

export const ROUTES = ["cost"];

const hashParts = () => window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);

let appRoot = null;

async function draw(app) {
  const [spend, fxBody] = await Promise.all([getSpend(), getFxRate()]);
  if (hashParts()[0] !== "cost") return;
  app.innerHTML = costPageHtml({ spend, fx: fxBody.fx_rate, canEdit: canAccess("PUT", "/cost/fx-rate") });
  document.title = "Cost · Marketing AI";
  setActiveNav("settings");
}

async function render(app) {
  appRoot = app;
  app.innerHTML = skeleton("list", { title: "Cost" });
  try {
    await draw(app);
  } catch (error) {
    app.innerHTML = `<main class="screen cost" data-module="cost">${errorBox(error, { retry: true })}</main>`;
  }
}

registerModule({ name: "cost", routes: ROUTES, render });

async function act(work) {
  const app = appRoot || document.getElementById("app");
  try {
    await work();
    await draw(app);
  } catch (error) {
    const box = document.createElement("div");
    box.innerHTML = errorBox(error);
    const form = app.querySelector("[data-fx-form]");
    if (form) form.append(box);
  }
}

document.addEventListener("submit", (event) => {
  const form = event.target && event.target.closest ? event.target.closest("[data-fx-form]") : null;
  if (!form) return;
  event.preventDefault();
  const data = new FormData(form);
  act(() =>
    putFxRate({
      inr_per_usd: Number(data.get("inr_per_usd")),
      source: String(data.get("source") || ""),
      as_of: String(data.get("as_of") || ""),
    }),
  );
});

document.addEventListener("click", (event) => {
  const button = event.target && event.target.closest ? event.target.closest("[data-fx-clear]") : null;
  if (button) act(() => deleteFxRate());
});
