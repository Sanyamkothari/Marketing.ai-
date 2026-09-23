// The uplift screens' own stylesheet, injected once rather than added to `ui/index.html`.
//
// `index.html` is shared by every phase branch (PARALLEL_WORK_PROTOCOL.md §4) and its PHASE-3B
// block is for one `<script>` line, not for rules. Every rule below reads the custom properties
// `index.html` already defines (`--ok`, `--bad-t`, `--muted`, `--c`, ...), so light mode, dark mode
// and the `data-theme` override apply with no dark-mode block of their own - the same reason
// `ui/dom.js`'s SVG helpers need none. Classes are prefixed `u` so nothing here can collide with a
// class a shared file, or another phase's module, defines now or later.

const CSS = `
.unotcausal{display:flex;flex-direction:column;gap:2px;padding:12px 16px;border:1px solid var(--warn);border-left:3px solid var(--warn);border-radius:8px;background:var(--warn-t);color:var(--warn);font-size:13px}
.unotcausal b{font-weight:600}
.unotcausal span{color:var(--ink2);font-size:12px}
.uentry{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;margin:-8px 0 20px;font-size:12px;color:var(--muted)}
.uentry a{display:inline-flex;align-items:center;gap:6px;padding:5px 12px;border:1px solid var(--line2);border-radius:999px;color:var(--ink2);font-weight:500}
.uentry a:hover{border-color:var(--ink2);color:var(--ink)}
.uentry a b{font-weight:600;color:var(--ink)}
.uchart{padding:16px 20px 8px}
.uchart svg{display:block;width:100%;height:auto;overflow:visible}
.uchart .ax{stroke:var(--line2);stroke-width:1}
.uchart .grid{stroke:var(--line);stroke-width:1}
.uchart .zero{stroke:var(--line2);stroke-width:1}
.uchart .tk{fill:var(--muted);font-size:11px;font-family:inherit}
@media (max-width:560px){.uchart{padding:12px 8px 4px}.uchart .tk{font-size:19px}.uchart .tt{font-size:16px}main[data-module="uplift"] th,main[data-module="uplift"] td{padding-left:12px;padding-right:12px}}
.uchart .model{fill:none;stroke:var(--c);stroke-width:2.25;stroke-linejoin:round;stroke-linecap:round}
.uchart .area{fill:var(--t);stroke:none}
.uchart .rand{fill:none;stroke:var(--faint);stroke-width:1.5;stroke-dasharray:5 4}
.uchart .pos{fill:var(--c)}
.uchart .neg{fill:var(--bad)}
.uchart .pred{fill:var(--ink2)}
.ulegend{display:flex;flex-wrap:wrap;gap:6px 18px;padding:0 20px 14px;font-size:12px;color:var(--muted)}
.ulegend i{display:inline-block;width:18px;height:0;border-top:2.25px solid var(--c);vertical-align:middle;margin-right:6px}
.ulegend i.r{border-top:1.5px dashed var(--faint)}
.ulegend i.dot{width:8px;height:8px;border:0;border-radius:50%;background:var(--ink2)}
.ulegend i.sq{width:10px;height:10px;border:0;border-radius:2px;background:var(--c)}
.ulegend i.sq.n{background:var(--bad)}
.usegs{padding:18px 20px;display:flex;flex-direction:column;gap:16px}
.useg{display:grid;grid-template-columns:150px minmax(0,1fr) 110px;gap:14px;align-items:center;font-size:13px}
.useg .ul{font-weight:500;color:var(--ink)}
.useg .ua{font-size:12px;color:var(--muted);margin-top:2px}
.useg .un{text-align:right;font-weight:500}
.useg .un small{display:block;font-size:11px;font-weight:400;color:var(--muted)}
.useg svg{display:block;width:100%;height:10px}
.useg.persuadable{--seg:var(--ok)} .useg.sure_thing{--seg:var(--p)} .useg.lost_cause{--seg:var(--faint)} .useg.sleeping_dog{--seg:var(--bad)}
@media (max-width:700px){.useg{grid-template-columns:1fr}.useg .un{text-align:left}}
.usummary{padding:14px 20px;font-size:13px;color:var(--ink2);border-bottom:1px solid var(--line)}
.uwait{padding:26px 20px;display:flex;flex-direction:column;gap:6px}
.uwait b{font-size:20px;font-weight:600;color:var(--ink)}
.uwait span{font-size:13px;color:var(--muted)}
.uform{padding:16px 20px 20px;display:flex;flex-direction:column;gap:14px}
.uform .frow .field{width:220px}
.uform input[type=text],.uform input[type=date]{appearance:none;-webkit-appearance:none;width:100%;height:100%;border:0;background:transparent;color:inherit;font:inherit;padding:0 12px;outline:none}
.uform .actions{margin-top:0}
.uwarn{flex-basis:100%;color:var(--warn)}
.uindex{display:flex;flex-direction:column;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.uindex a{display:flex;justify-content:space-between;gap:12px;padding:12px 20px;border-top:1px solid var(--line);font-size:13px}
.uindex a:first-child{border-top:0}
.uindex a:hover{background:var(--soft)}
.uindex .s{font-size:12px;color:var(--muted)}
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
