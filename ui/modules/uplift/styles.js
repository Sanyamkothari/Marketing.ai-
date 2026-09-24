// The uplift screens' own stylesheet, injected once rather than added to `ui/index.html`.
//
// `index.html` is shared by every phase branch (PARALLEL_WORK_PROTOCOL.md §4) and its PHASE-3B
// block is for one `<script>` line, not for rules. Every rule below reads the custom properties
// `index.html` already defines (`--ok`, `--bad-t`, `--muted`, `--c`, ...), so light mode, dark mode
// and the `data-theme` override apply with no dark-mode block of their own - the same reason
// `ui/dom.js`'s SVG helpers need none. Classes are prefixed `u` so nothing here can collide with a
// class a shared file, or another phase's module, defines now or later. Spacing uses the v1 scale
// (4, 8, 12, 16, 24 px); nothing is smaller than 12px.

const CSS = `
.unotcausal{display:flex;flex-direction:column;gap:4px;padding:12px 16px;border:1px solid var(--warn);border-left:3px solid var(--warn);border-radius:8px;background:var(--warn-t);color:var(--warn);font-size:13px}
.unotcausal b{font-weight:600}
.unotcausal span{color:var(--ink2);font-size:12px}
.uverdict{padding:16px 20px}
.uverdict .uv-row{display:grid;grid-template-columns:28px minmax(0,1fr);gap:12px;align-items:start}
.uverdict .uv-mark{width:28px;height:28px;border-radius:50%;display:grid;place-items:center;font-size:14px;font-weight:700;background:var(--soft);color:var(--ink2)}
.uverdict.ok .uv-mark{background:var(--ok-t);color:var(--ok)}
.uverdict.warn .uv-mark{background:var(--warn-t);color:var(--warn)}
.uverdict.bad .uv-mark{background:var(--bad-t);color:var(--bad)}
.uverdict.ok{border-left:3px solid var(--ok)} .uverdict.warn{border-left:3px solid var(--warn)} .uverdict.bad{border-left:3px solid var(--bad)}
.uv-t{margin:0;font-size:20px;font-weight:600;line-height:1.35;color:var(--ink);font-feature-settings:"tnum"}
.uv-x{margin:8px 0 0;font-size:14px;line-height:1.5;color:var(--ink2);max-width:760px}
.ukpis .kpi{position:relative}
.ukpis .kpi .s{margin-top:4px;font-size:12px;color:var(--muted);font-feature-settings:"tnum"}
.ukpis .kpi .t{position:absolute;top:12px;right:12px}
.ukpis .kpi .l{padding-right:28px}
main[data-module="uplift"] .kpis.ukpis:has(.kpi:nth-child(3):last-child){grid-template-columns:repeat(3,1fr)}
main[data-module="uplift"] .kpis.ukpis:has(.kpi:nth-child(2):last-child){grid-template-columns:repeat(2,1fr)}
.ulead{margin:0;font-size:15px;line-height:1.5;color:var(--ink);max-width:760px}
.ulead .muted{color:var(--muted);font-size:13px}
.udetails{border-top:0;padding-top:0}
.udetails + .udetails,.udetails + details.tech{border-top:1px solid var(--line);padding-top:12px;margin-top:12px}
.udetails .ukv{margin:8px -20px 0}
.udetails .ukv .kv:first-child{border-top:0}
.udetails .caption{padding:8px 0 0}
.udetails-card{gap:0;padding:4px 20px 16px}
.udetails-card > details.tech{padding-top:12px;margin-top:12px;border-top:1px solid var(--line)}
.udetails-card > details.tech:first-child{border-top:0;margin-top:0}
.ufile{margin:0}
.urange{display:block;font-size:12px;color:var(--muted);font-weight:400}
.results > details.tech{margin-top:16px}
.ucap{padding:12px 20px 0}
.uchart{padding:16px 20px 8px}
.uchart svg{display:block;width:100%;max-width:760px;height:auto;overflow:visible}
.uchart .ax{stroke:var(--line2);stroke-width:1}
.uchart .grid{stroke:var(--line);stroke-width:1}
.uchart .zero{stroke:var(--line2);stroke-width:1}
.uchart .tk{fill:var(--muted);font-size:12px;font-family:inherit}
@media (max-width:560px){.uchart{padding:12px 8px 4px}.uchart .tk{font-size:26px}.uchart .tt{font-size:24px}main[data-module="uplift"] th,main[data-module="uplift"] td{padding-left:12px;padding-right:12px}}
.uchart .model{fill:none;stroke:var(--c);stroke-width:2.25;stroke-linejoin:round;stroke-linecap:round}
.uchart .area{fill:var(--t);stroke:none}
.uchart .rand{fill:none;stroke:var(--faint);stroke-width:1.5;stroke-dasharray:5 4}
.uchart .pos{fill:var(--c)}
.uchart .neg{fill:var(--bad)}
.uchart .pred{fill:var(--ink2)}
.ulegend{display:flex;flex-wrap:wrap;gap:8px 16px;padding:0 20px 16px;font-size:12px;color:var(--muted)}
.ulegend i{display:inline-block;width:18px;height:0;border-top:2.25px solid var(--c);vertical-align:middle;margin-right:8px}
.ulegend i.r{border-top:1.5px dashed var(--faint)}
.ulegend i.dot{width:8px;height:8px;border:0;border-radius:50%;background:var(--ink2)}
.ulegend i.sq{width:10px;height:10px;border:0;border-radius:2px;background:var(--c)}
.ulegend i.sq.n{background:var(--bad)}
details.adv.utable{padding:0 20px 16px;border-top:0}
.utable .tbl-wrap{margin:12px -20px 0}
.usegs{padding:16px 20px;display:flex;flex-direction:column;gap:16px}
.useg{display:grid;grid-template-columns:150px minmax(0,1fr) 150px;gap:12px;align-items:center;font-size:13px}
.useg .ul{font-weight:500;color:var(--ink)}
.useg .ua{font-size:12px;color:var(--muted);margin-top:4px}
.useg .un{text-align:right;font-weight:500;font-feature-settings:"tnum"}
.useg .un small{display:block;font-size:12px;font-weight:400;color:var(--muted)}
.useg .un small .uu{white-space:nowrap}
.useg svg{display:block;width:100%;height:10px}
.useg.persuadable{--seg:var(--ok)} .useg.sure_thing{--seg:var(--p)} .useg.lost_cause{--seg:var(--faint)} .useg.sleeping_dog{--seg:var(--bad)}
@media (max-width:700px){.useg{grid-template-columns:1fr}.useg .un{text-align:left}}
.usegmean{padding:0 20px 12px;font-size:13px;color:var(--ink2);display:flex;flex-direction:column;gap:4px}
.usegmean p{margin:0}
.usegmean + .udetails{padding:0 20px 16px}
.card > .udetails{padding:0 20px 16px}
.udetails-card > .udetails{padding:0}
.uwait{padding:24px 20px;display:flex;flex-direction:column;gap:8px}
.uwait b{font-size:20px;font-weight:600;color:var(--ink)}
.uwait span{font-size:13px;color:var(--muted)}
.uform{padding:16px 20px 20px;display:flex;flex-direction:column;gap:12px}
.uform .seg-help{margin:0}
.uform .frow .field{width:240px}
.uform .fhelp{font-size:12px;color:var(--muted)}
.uform input[type=text],.uform input[type=date],.uform input[type=number]{appearance:none;-webkit-appearance:none;width:100%;height:100%;border:0;background:transparent;color:inherit;font:inherit;padding:0 12px}
.uform .actions{margin-top:0}
.uform details.adv{border-top:1px solid var(--line)}
.uform-setup{margin-top:16px}
.field.wide{width:100%;max-width:420px}
main[data-module="uplift"] .fstep .frow{align-items:flex-end}
main[data-module="uplift"] .fstep .frow .field.wide{width:300px}
.uwarn{flex-basis:100%;color:var(--warn)}
.umodel{margin:8px 0 12px;font-size:13px;font-weight:500;color:var(--ink)}
.uacks{display:flex;flex-direction:column;gap:8px;padding-top:16px}
.uack{display:inline-flex;align-items:flex-start;gap:8px;font-size:13px;color:var(--ink2);min-height:24px}
.uack input{width:16px;height:16px;margin-top:2px;accent-color:var(--brand-blue)}
main[data-module="uplift"] .vitem{grid-template-columns:auto minmax(0,1fr)}
main[data-module="uplift"] .vitem > .pill{display:inline-block;align-self:start}
.vrand{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:8px;font-size:12px;color:var(--ink2)}
.vrand .pill{display:inline-block}
.uwarns{border-top:1px solid var(--line)}
.uwarns > summary{cursor:pointer;padding:12px 14px;font-size:12px;font-weight:500;color:var(--brand-blue);list-style:none;min-height:24px}
.uwarns > summary::-webkit-details-marker{display:none}
.urun-intro{margin:0;padding:12px 20px 0;font-size:13px;color:var(--ink2)}
main[data-module="uplift"] .progress li.done .pd.w,main[data-module="uplift"] .progress li .pd.w{color:var(--warn)}
.ucancel{padding:0 20px 16px;font-size:13px;color:var(--ink2)}
.urunsfold{border-top:0;padding:12px 20px}
.urunsfold .runs-list{margin:12px -20px -12px}
.tabs .tab.off{color:var(--muted);background:var(--soft);border-color:var(--line);cursor:default}
.tabs-bar .ustep-why{margin:0;flex-basis:100%;order:3}
.card > details.adv.ufold{border-top:0;padding:16px 20px}
.card > details.adv.ufold > summary{font-size:14px;font-weight:600;color:var(--ink);min-height:24px}
.uwhatif .uform{padding:12px 0 0}
.uope-result{padding-top:12px;border-top:1px solid var(--line);margin-top:12px}
.uope-result .caption{padding:4px 0 8px}
.uhint{margin:0 0 12px}
.uindex{display:flex;flex-direction:column;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--surface)}
.uindex > a,.uindex > .off{display:grid;grid-template-columns:minmax(0,1fr) auto 16px;gap:12px;align-items:center;padding:12px 20px;border-top:1px solid var(--line);font-size:13px;min-height:48px}
.uindex > :first-child{border-top:0}
.uindex > a:hover{background:var(--soft)}
.uindex .n{display:flex;flex-direction:column;gap:2px}
.uindex .n b{font-weight:600;color:var(--ink)}
.uindex .st{font-size:12px;color:var(--muted)}
.uindex .s{font-size:12px;color:var(--muted);text-align:right}
.uindex .s.ok{color:var(--ok)}
.uindex .go{color:var(--faint)}
.uindex > .off{grid-template-columns:minmax(0,1fr) auto;background:var(--soft)}
.uindex > .off b{color:var(--ink2)}
@media (max-width:560px){.uindex > a{grid-template-columns:minmax(0,1fr) 16px}.uindex > a .s{grid-column:1;grid-row:2;text-align:left}.uindex > a .go{grid-column:2;grid-row:1 / span 2}.uindex > .off{grid-template-columns:1fr}.uindex > .off .s{text-align:left}}
@media (max-width:700px){main[data-module="uplift"] .kpis.ukpis{grid-template-columns:1fr !important;gap:12px}main[data-module="uplift"] .kpis.ukpis.n4{grid-template-columns:repeat(2,minmax(0,1fr)) !important}main[data-module="uplift"] .ukpis .kpi{padding:12px 16px}.uv-t{font-size:18px}}
`;

let injected = false;

/** Adds the stylesheet once; a no-op outside a browser, so the renderers stay testable in node. */
export function injectUpliftStyles() {
  if (injected || typeof document === "undefined") return;
  injected = true;
  const style = document.createElement("style");
  style.id = "uplift-styles";
  style.textContent = CSS;
  document.head.appendChild(style);
}
