// The Reports page (Plan M, M178; its look, M188, DEC-1618): one H1, one sentence, four underline tabs. Pure builders;
// `index.js` loads and binds. The header and the tabs are the look's components (`ui/components.js`: `pageHeader`, `tabs`).
//
//   #/reports                 Value proof (the default)
//   #/reports/value-proof     the Value Proof Packs
//   #/reports/programme       the value proven to date, with its rules
//   #/reports/check           check a past campaign (today's audit form, in three steps)
//   #/reports/models          model reports (technical: a closed section until M180's switch)

import { pageHeader, tabs } from "../../components.js";
import { esc } from "../../dom.js";

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

/** The header: the title, one sentence and (on Value proof, once its list is in) the page's one primary action. */
export function reportsHeaderHtml({ primary = null } = {}) {
  return pageHeader({
    title: "Reports",
    subtitle: "What your campaigns proved, ready to read, share or print.",
    primary,
    attrs: "data-reports-header",
  });
}

/** The underline tabs: a link to each tab's route; the current one is also `aria-current="page"`, as a link to a page is. */
export function reportsTabsHtml(tab) {
  return tabs({
    label: "Reports",
    current: tab,
    items: TABS.map((t) => ({
      id: t.id,
      label: t.label,
      href: tabHref(t.id),
      attrs: `data-reports-tab="${t.id}"${t.technical ? " data-technical" : ""}${t.id === tab ? ' aria-current="page"' : ""}`,
    })),
  });
}

/** The page: header, tab strip, and the body of the current tab (`bodyHtml`, already built). */
export function reportsPageHtml({ tab, bodyHtml = "", primary = null }) {
  return `<main class="screen rp sp" data-module="reports" data-reports-page data-tab="${esc(tab)}">${reportsHeaderHtml({
    primary,
  })}${reportsTabsHtml(tab)}<div class="stack" data-reports-body>${bodyHtml}</div></main>`;
}

const CSS = `
.rp .rp-name{font-weight:600;color:var(--ink)}
.rp .rp-effect{display:block;color:var(--ink2)}
.rp .rp-net{display:flex;margin-top:var(--s2)}
.rp .rp-net .vr-text{color:var(--ink)}
.rp .rp-why{display:block;margin-top:var(--s2);font-size:var(--fs-caption);line-height:var(--lh-caption);color:var(--muted);max-width:32ch}
.rp .rp-na{color:var(--muted)}
.rp .rp-acts{display:inline-flex;gap:var(--s2);align-items:center;justify-content:flex-end}
.rp .rp-table td{vertical-align:top}
@media (max-width:700px){
.rp .tbl-stack tr{padding:var(--s4) 0}
.rp .tbl-stack td{display:block;padding:var(--s2) var(--s4);text-align:left}
.rp .tbl-stack td::before{display:block;margin-bottom:2px}
.rp .tbl-stack td:first-child::before,.rp .tbl-stack td:last-child::before{display:none}
.rp .tbl-stack td:first-child{font-size:var(--fs-card);line-height:var(--lh-card)}
.rp .rp-acts{justify-content:flex-start}
.rp .rp-why{max-width:none}
.rp .rp-net .vrange{flex-direction:column;align-items:flex-start}
.rp .rp-net .vr-track{flex:none;width:100%;max-width:240px}
}
.rp .rp-note{font-size:var(--fs-small);color:var(--ink2);line-height:var(--lh-small);margin:0}
.rp .rp-error{display:flex;align-items:flex-start;gap:var(--s2);font-size:var(--fs-small);line-height:var(--lh-small);color:var(--critical);margin:0}
.rp .rp-error svg{margin-top:2px}

/* Check a past campaign: one card, three steps, drop zones, labelled fields with help below */
.rp .rp-card>.rp-intro{margin:0;padding:var(--s3) 20px var(--s4)}
.rp .rp-steps{list-style:none;margin:0;padding:0}
.rp .rp-step{padding:var(--s5) 20px;border-top:1px solid var(--line)}
.rp .rp-step-h{display:flex;align-items:center;gap:var(--s3);margin:0 0 var(--s1);padding:0;border:0;background:none;font-size:var(--fs-card);line-height:var(--lh-card)}
.rp .rp-step-n{display:inline-flex;align-items:center;justify-content:center;flex:none;width:24px;height:24px;border-radius:999px;background:var(--brand-blue);color:#fff;font-size:var(--fs-caption);font-weight:600;font-variant-numeric:tabular-nums}
.rp .rp-step>.rp-note{margin-left:36px}
.rp .rp-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:var(--s4);margin:var(--s4) 0 0 36px}
.rp .rp-field{display:flex;flex-direction:column;gap:6px}
.rp .rp-field>label{font-size:var(--fs-small);font-weight:600;color:var(--ink)}
.rp .rp-form .input{font-weight:400}
.rp .rp-wide{grid-column:1/-1}
.rp .rp-file{display:flex;flex-direction:column;gap:var(--s2);min-width:0}
.rp .rp-drop{position:relative;display:grid;grid-template-columns:auto minmax(0,1fr);grid-template-areas:"ico t" "ico go";column-gap:var(--s4);align-items:center;padding:var(--s4) 20px;text-align:left;border:1.5px dashed var(--line2);border-radius:var(--r-lg);background:var(--soft);cursor:pointer;transition:background-color var(--dur) var(--ease),border-color var(--dur) var(--ease)}
.rp .rp-drop-ico{grid-area:ico}
.rp .rp-drop-t{grid-area:t}
.rp .rp-drop-go{grid-area:go}
.rp .rp-drop:hover,.rp .rp-drop.has-file{background:var(--brand-50);border-color:var(--brand-blue)}
.rp .rp-drop:focus-within{outline:2px solid var(--brand-blue);outline-offset:2px}
.rp .rp-drop input[type=file]{position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:pointer;font-size:0}
.rp .rp-drop-ico{display:inline-grid;place-items:center;width:40px;height:40px;border-radius:var(--r);background:var(--surface);border:1px solid var(--line);color:var(--brand-blue)}
.rp .rp-drop-t{font-size:var(--fs-small);font-weight:600;color:var(--ink)}
.rp .rp-drop-go{font-size:var(--fs-small);font-weight:500;color:var(--brand-blue)}
.rp .rp-got{display:inline-flex;align-items:center;gap:6px;font-size:var(--fs-small);font-weight:500;color:var(--good)}
.rp .rp-hint{margin:0;font-size:var(--fs-caption);line-height:var(--lh-caption);color:var(--muted);font-weight:400}
.rp .rp-form .rp-check{flex-direction:row;align-items:center;gap:var(--s2);font-size:var(--fs-small);color:var(--ink)}
.rp fieldset.rp-basis{margin:0;padding:0;border:0;min-width:0}
.rp fieldset.rp-basis legend{padding:0;margin-bottom:var(--s2);font-size:var(--fs-small);font-weight:600;color:var(--ink)}
.rp .rp-choices{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:var(--s3)}
.rp .rp-choice{display:flex;flex-direction:row;align-items:flex-start;font-size:var(--fs-small);color:var(--ink);gap:var(--s3);padding:var(--s3) var(--s4);border:1px solid var(--line2);border-radius:var(--r);background:var(--surface);font-weight:500;cursor:pointer;transition:background-color var(--dur) var(--ease),border-color var(--dur) var(--ease)}
.rp .rp-choice:hover{background:var(--soft)}
.rp .rp-choice:has(input:checked){border-color:var(--brand-blue);background:var(--brand-50)}
.rp .rp-choice:focus-within{outline:2px solid var(--brand-blue);outline-offset:2px}
.rp .rp-choice input{margin-top:3px}
.rp .rp-actions{display:flex;gap:var(--s2);padding:var(--s4) 20px var(--s5);border-top:1px solid var(--line)}
.rp .rp-card form>.rp-error{padding:0 20px var(--s3)}
.rp .rp-programme{margin:0;padding:0;border-top:1px solid var(--line)}
.rp .rp-programme>summary{padding:var(--s4) 20px;font-size:var(--fs-body);font-weight:600;color:var(--ink);cursor:pointer}
.rp .rp-programme>.rp-note{padding:0 20px;margin-bottom:var(--s2)}
.rp .rp-programme .rp-form{margin:var(--s4) 20px 0}
.rp .rp-programme .rp-actions{border-top:0;padding-top:var(--s4)}

/* Programme summary: the KPI row, then the card Results draws (its markup is the same), drawn as a compact table */
.rp [data-proven] .sp-note{padding:0 20px var(--s3);border-top:0;font-size:var(--fs-small);line-height:var(--lh-small);color:var(--muted)}
.rp [data-proven]>.sp-list>.sp-entry{padding:var(--s4) 20px}
.rp [data-proven] .sp-label{font-size:var(--fs-body);color:var(--ink)}
.rp [data-proven] .sp-facts{margin:var(--s2) 0 0;border-top:1px solid var(--line)}
.rp [data-proven] [data-total] [data-line]{display:flex;justify-content:space-between;align-items:baseline;gap:var(--s4);padding:var(--s3) 0;border-bottom:1px solid var(--line);font-size:0}
.rp [data-proven] [data-total] [data-line]:last-child{border-bottom:0}
.rp [data-proven] [data-total] [data-line]>*{font-size:var(--fs-small)}
.rp [data-proven] [data-total] [data-line]>*:last-child{font-weight:600;color:var(--ink);font-variant-numeric:tabular-nums;white-space:nowrap}
.rp [data-proven] .sp-sub{padding:var(--s3) 20px;margin:0;background:var(--soft);border-top:1px solid var(--line);border-bottom:1px solid var(--line);font-size:var(--fs-caption);line-height:var(--lh-caption);color:var(--muted)}

/* Model reports: a closed section of three quiet tables */
.rp .rp-models{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);padding:0}
.rp .rp-models>summary{display:flex;align-items:center;gap:var(--s2);width:100%;box-sizing:border-box;padding:var(--s4) 20px;list-style:none;font-size:var(--fs-card);line-height:var(--lh-card);font-weight:600;color:var(--ink);cursor:pointer}
.rp .rp-models>summary::-webkit-details-marker{display:none}
.rp .rp-models>summary::before{content:"";flex:none;width:7px;height:7px;border-right:1.5px solid var(--muted);border-bottom:1.5px solid var(--muted);transform:rotate(-45deg);transition:transform var(--dur) var(--ease)}
.rp .rp-models[open]>summary::before{transform:rotate(45deg)}
.rp .rp-models[open]>summary{border-bottom:1px solid var(--line)}
.rp .rp-models-part{padding:var(--s4) 0 var(--s2)}
.rp .rp-models-part+.rp-models-part{border-top:1px solid var(--line)}
.rp .rp-models-part h2{margin:0 0 var(--s2);padding:0 20px;font-size:var(--fs-card);line-height:var(--lh-card);font-weight:600;color:var(--ink)}
.rp .rp-models-part>.rp-note{margin:0 20px}
.rp .rp-models .tbl-wrap{border:0;border-radius:0}
.rp .rp-models-part .badge{margin-left:var(--s2)}
`;

export function injectStyles(doc = document) {
  if (!doc || !doc.head || doc.getElementById("rp-styles")) return;
  const style = doc.createElement("style");
  style.id = "rp-styles";
  style.textContent = CSS;
  doc.head.appendChild(style);
}
