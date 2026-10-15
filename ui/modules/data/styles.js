// The Data page's stylesheet (Plan M, M179; its look, M188 DEC-1618), injected once; tokens only, classes prefixed `dt-`.
// The tabs, the header, the buttons and the status chips are the look's components (ui/theme.css), not drawn here.

const CSS = `
.dt .dt-sec>h2{margin:0 0 var(--s1);font-size:var(--fs-section);line-height:var(--lh-section);font-weight:600;color:var(--ink)}
.dt .dt-sec>p{margin:0 0 var(--s4);font-size:var(--fs-small);line-height:var(--lh-small);color:var(--muted)}
.dt .dt-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;border:1px solid var(--line);border-radius:var(--r-lg);background:var(--surface);overflow:hidden}
.dt .dt-row{display:grid;grid-template-columns:40px minmax(0,2fr) minmax(0,1.6fr) minmax(0,1.3fr) auto;gap:var(--s2) var(--s4);align-items:center;min-height:72px;padding:var(--s3) 20px;border-top:1px solid var(--line);transition:background-color var(--dur) var(--ease)}
.dt .dt-row:first-child{border-top:0}
.dt .dt-row:hover{background:var(--soft)}
.dt .dt-ico{display:inline-grid;place-items:center;flex:none;width:40px;height:40px;border-radius:var(--r);background:var(--brand-50);color:var(--brand-blue)}
.dt .dt-ico.lg{width:48px;height:48px;border-radius:var(--r-lg)}
.dt .dt-name{margin:0;font-size:var(--fs-body);line-height:var(--lh-body);font-weight:600;color:var(--ink);overflow-wrap:anywhere}
.dt .dt-sub{display:block;margin-top:2px;font-size:var(--fs-caption);line-height:var(--lh-caption);font-weight:400;color:var(--muted);overflow-wrap:anywhere}
.dt .dt-cell{font-size:var(--fs-small);line-height:var(--lh-small);color:var(--ink2);overflow-wrap:anywhere}
.dt .dt-cell .badge{max-width:100%}
.dt .dt-cell[data-dt-when]{color:var(--muted)}
.dt .dt-actions{display:flex;flex-wrap:wrap;gap:var(--s1);align-items:center;justify-content:flex-end}
.dt .dt-wide{grid-column:1/-1}
.dt .dt-more{margin-top:var(--s3);display:flex;flex-wrap:wrap;align-items:center;gap:var(--s3)}

/* Add a source: one card, the kinds as quiet tiles inside it (no box in a box) */
.dt .dt-chooser{margin:0 0 var(--s5);padding:20px;border:1px solid var(--line);border-radius:var(--r-lg);background:var(--surface)}
.dt .dt-chooser[hidden]{display:none}
.dt .dt-chooser h2{margin:0 0 var(--s1);font-size:var(--fs-section);line-height:var(--lh-section);font-weight:600;color:var(--ink)}
.dt .dt-chooser>p{margin:0 0 var(--s4);font-size:var(--fs-small);line-height:var(--lh-small);color:var(--muted)}
.dt .dt-kinds{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:var(--s3)}
.dt .dt-kind{display:flex;flex-direction:column;gap:var(--s3);padding:var(--s4);border:0;border-radius:var(--r);background:var(--soft)}
.dt .dt-kind-h{display:flex;align-items:center;gap:var(--s3)}
.dt .dt-kind h3{margin:0;padding:0;border:0;background:none;font-size:var(--fs-body);line-height:var(--lh-body);font-weight:600;color:var(--ink)}
.dt .dt-kind .btn-row{margin-top:auto}
.dt .dt-note{margin:0;font-size:var(--fs-caption);line-height:var(--lh-caption);color:var(--muted)}
.dt .dt-foot{margin-top:var(--s5);font-size:var(--fs-small);color:var(--ink2)}
.dt .dt-foot a{color:var(--brand-blue);font-weight:500}
.dt .dt-ai{margin:0 0 var(--s4);font-size:var(--fs-small);color:var(--ink2)}
.dt .dt-ai a{color:var(--brand-blue);font-weight:500}

/* Check my data: a calm card, one action */
.dt .dt-check{max-width:720px;padding:var(--s5) var(--s5) var(--s5)}
.dt .dt-check-h{display:flex;align-items:flex-start;gap:var(--s4);margin-bottom:var(--s4)}
.dt .dt-check h2{margin:0 0 var(--s1);font-size:var(--fs-section);line-height:var(--lh-section);font-weight:600;color:var(--ink)}
.dt .dt-check p{margin:0;font-size:var(--fs-body);line-height:var(--lh-body);color:var(--ink2)}
.dt .dt-points{list-style:none;margin:0 0 var(--s4);padding:0;display:flex;flex-direction:column;gap:var(--s3)}
.dt .dt-points li{display:flex;align-items:flex-start;gap:var(--s3);font-size:var(--fs-body);line-height:var(--lh-body);color:var(--ink2)}
.dt .dt-points svg{margin-top:2px;color:var(--brand-blue)}
.dt .dt-after{margin-bottom:var(--s4)!important;color:var(--muted)!important}
/* a phone (or any coarse pointer): every action is a 40px target, with room between Edit and Delete */
@media (max-width:700px),(pointer:coarse){
.dt .dt-actions .btn.sm,.dt .dt-kind .btn.sm,.dt .dt-more .btn.sm,.dt .cn-confirm .btn.sm{min-height:40px;height:40px;padding:0 var(--s3)}
.dt .dt-actions{gap:var(--s2)}
}
@media (max-width:700px){
.dt .dt-row{grid-template-columns:40px minmax(0,1fr);align-items:start;padding:var(--s4)}
.dt .dt-row>.dt-cell,.dt .dt-row>.dt-actions{grid-column:2}
.dt .dt-actions{justify-content:flex-start;margin-left:calc(var(--s1) * -1)}
.dt .dt-check{padding:var(--s4)}
.dt .dt-check-h{flex-direction:column;gap:var(--s3)}
}
`;

let injected = false;

export function injectDataStyles() {
  if (injected || typeof document === "undefined") return;
  injected = true;
  const style = document.createElement("style");
  style.id = "dt-styles";
  style.textContent = CSS;
  document.head.appendChild(style);
}
