/* Plan M M171 (DEC-1601): the one contact list, in jsdom, against REAL API answers.

   `test_one_contact_list.py` writes a scoring run with the product's own code, asks the real app for
   every response the pages read (the run, the models, the scoring summary, the treat list's summary or
   its refusal) and writes them, with the two CSV files, into a directory named by $CL_FIXTURES. This script
   replays them to the real pages (`ui/pages.js` through `app.js`'s own loader, and `ui/usecase.js`'s Results
   view) and prints what a person would see, as JSON, for the Python side to compare with the files. Run only
   through that test: it needs node and the jsdom of tests/integration/production/ui. */
import fs from "node:fs";
import path from "node:path";
import { installPage, settle } from "../production/ui/harness.mjs";

const DIR = process.env.CL_FIXTURES;
if (!DIR) throw new Error("CL_FIXTURES is not set: run this through test_one_contact_list.py");
const read = (name) => JSON.parse(fs.readFileSync(path.join(DIR, `${name}.json`), "utf8"));
const answers = read("answers"); // "GET /path" -> { status, body }

const server = ({ method, path: p }) => answers[`${method} ${p}`] || null;
const { calls } = installPage(server, { hash: "#/uc/win-back-campaign" });

const UI = new URL("../../../ui/", import.meta.url);
const api = await import(new URL("api.js", UI));
const pages = await import(new URL("pages.js", UI));
const usecase = await import(new URL("usecase.js", UI));

const uc = read("use_case");
const runId = read("ids").run_id;
const detail = answers[`GET /runs/${runId}`].body;
const art = { "scoring_summary.json": answers[`GET /runs/${runId}/artefacts/scoring_summary.json`].body };

const holder = document.createElement("div");
document.body.appendChild(holder);

/** What a person sees on a page: its downloads, the sentence, the note, and what sits under Technical details. */
function describe(html) {
  holder.innerHTML = html;
  const links = (selector) => [...holder.querySelectorAll(selector)].map((a) => ({ text: a.textContent.trim(), href: a.getAttribute("href") }));
  const sentence = holder.querySelector("[data-contact-sentence]");
  const note = holder.querySelector("[data-contact-note]");
  const tech = [...holder.querySelectorAll("details.tech")];
  return {
    primaryDownloads: links("a.btn.primary[download]"),
    primaryButtons: holder.querySelectorAll(".btn.primary").length,
    anyDownloads: links("a[download]"),
    scoresLinkUnderTechnical: tech.some((d) => d.querySelector('a[data-download="scores"]')),
    sentence: sentence ? sentence.textContent.trim() : null,
    note: note ? note.textContent.trim() : null,
    visibleText: holder.textContent.replace(/\s+/g, " "),
  };
}

// The Output page: what `app.js` hands the page (`loadContactList`), then the page itself.
const contactList = await api.loadContactList(runId);
const outputHtml = pages.renderPage("output", uc, detail.run, art, api.scoresUrl(runId), { contactList });

// The Results view of the same run, through the screen's own controller.
const controller = usecase.createController(uc, () => {});
await controller.refreshLists();
controller.sync();
await controller.loadRun(runId);
const resultsHtml = usecase.useCaseHtml(uc, controller.state);
await settle();

const out = {
  contactList: { treatError: contactList.error ? String(contactList.error.message) : null, hasSummary: !!contactList.summary },
  output: describe(outputHtml),
  results: describe(resultsHtml),
  treatSummaryRequests: calls.filter((c) => c.path.endsWith("/treat_list_summary.json")).length,
};
process.stdout.write(JSON.stringify(out));
process.exit(0);
