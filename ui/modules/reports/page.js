// The Reports page (Plan M, M178): one H1, one sentence, four tabs. Pure builders; `index.js` loads and binds.
//
//   #/reports                 Value proof (the default)
//   #/reports/value-proof     the Value Proof Packs
//   #/reports/programme       the value proven to date, with its rules
//   #/reports/check           check a past campaign (today's audit form, in three steps)
//   #/reports/models          model reports (technical: a closed section until M180's switch)

import { esc, pageHead } from "../../dom.js";

export const TABS = [
  { id: "value-proof", label: "Value proof" },
  { id: "programme", label: "Programme summary" },
  { id: "check", label: "Check a past campaign" },
  { id: "models", label: "Model reports", technical: true },
];

/** Other spellings of a tab, so a link written from the old names or the plan's words still lands. */
const ALIASES = {
  proof: "value-proof",
  proofs: "value-proof",
  value: "value-proof",
  "value-proof-packs": "value-proof",
  summary: "programme",
  "programme-summary": "programme",
  program: "programme",
  audit: "check",
  "check-a-past-campaign": "check",
  past: "check",
  model: "models",
  "model-reports": "models",
  pilot: "models",
};

/** The tab a hash segment names; the first tab for none or an unknown one. */
export function tabOf(segment) {
  let raw = String(segment || "");
  try {
    raw = decodeURIComponent(raw);
  } catch {
    // a malformed escape (`%`, `%E0`) names no tab: it falls to the first one below
  }
  raw = raw.toLowerCase();
  const id = ALIASES[raw] || raw;
  return TABS.some((t) => t.id === id) ? id : TABS[0].id;
}

export const tabHref = (id) => `#/reports/${id}`;

/** The page: heading, tab strip, and the body of the current tab (`bodyHtml`, already built). */
export function reportsPageHtml({ tab, bodyHtml = "" }) {
  const tabs = TABS.map(
    (t) =>
      `<a class="tab${t.id === tab ? " on" : ""}" href="${tabHref(t.id)}" data-reports-tab="${t.id}"${t.technical ? " data-technical" : ""}${
        t.id === tab ? ' aria-current="page"' : ""
      }>${esc(t.label)}</a>`,
  ).join("");
  return `<main class="screen rp sp" data-module="reports" data-reports-page data-tab="${esc(tab)}">${pageHead(
    `<h1 class="h1">Reports</h1><p class="desc">What your campaigns proved, ready to read, share or print.</p>`,
  )}<div class="tabs-bar"><nav class="tabs" aria-label="Reports">${tabs}</nav></div><div class="stack" data-reports-body>${bodyHtml}</div></main>`;
}

const CSS = `
.rp .rp-name{font-weight:600;color:var(--ink)}
.rp .rp-why{display:block;margin-top:4px;font-size:12px;color:var(--muted);line-height:1.4}
.rp .rp-acts{display:inline-flex;gap:8px;justify-content:flex-end}
.rp .rp-note{font-size:13px;color:var(--ink2);line-height:1.5;margin:0}
.rp .rp-error{font-size:13px;color:var(--bad)}
.rp .rp-card>p,.rp .rp-card>form,.rp .rp-card>details{margin-left:20px;margin-right:20px}
.rp .rp-card h3{margin:0}
.rp .rp-card>p.rp-note{margin-top:12px}
.rp .rp-card>:last-child{margin-bottom:16px}
.rp .rp-steps{list-style:none;margin:16px 0 0;padding:0;display:flex;flex-direction:column;gap:20px}
.rp .rp-step{border-top:1px solid var(--line);padding-top:16px}
.rp .rp-step-h{display:flex;align-items:center;gap:10px;margin:0 0 4px;padding:0;border:0;background:none;font-size:15px}
.rp .rp-file{display:flex;flex-direction:column;gap:4px}
.rp .rp-step-n{display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;border-radius:999px;background:var(--btn);color:var(--btn-ink);font-size:12px;font-weight:600}
.rp .rp-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-top:8px}
.rp .rp-form label{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--ink2)}
.rp .rp-wide{grid-column:1/-1}
.rp .rp-check{flex-direction:row!important;align-items:center;gap:8px!important}
.rp fieldset.rp-basis{border:1px solid var(--line);border-radius:8px;margin:0;padding:8px 12px}
.rp fieldset.rp-basis legend{font-size:13px;font-weight:600;color:var(--ink)}
.rp .rp-actions{display:flex;gap:8px;margin-top:16px}
.rp .rp-programme{margin-top:20px}
.rp .rp-programme>summary{font-size:14px;font-weight:600;color:var(--ink);cursor:pointer}
.rp .rp-models{border:1px solid var(--line);border-radius:10px;background:var(--surface);padding:0 20px}
.rp .rp-models>summary{padding:16px 0;font-size:14px;font-weight:600;color:var(--ink);cursor:pointer}
.rp .rp-models-part{padding-bottom:20px}
.rp .rp-models-part h2{margin:0 0 8px;font-size:13px;font-weight:600;color:var(--ink2)}
.rp .tabs-bar{margin-top:8px}
.rp .stack>.head-actions{margin:0}
`;

export function injectStyles(doc = document) {
  if (!doc || !doc.head || doc.getElementById("rp-styles")) return;
  const style = doc.createElement("style");
  style.id = "rp-styles";
  style.textContent = CSS;
  doc.head.appendChild(style);
}
