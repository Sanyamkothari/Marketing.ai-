// Guided setup's own stylesheet, injected once (as the generative, pilot and uplift modules do) rather
// than added to `ui/index.html`, a shared file whose Plan G block is for a `<script>` tag only.
//
// Tokens only (docs/ui/FOUNDATION.md): every colour is a custom property `index.html` defines, so
// light, dark and the `data-theme` override apply with no block of their own. Classes are prefixed
// `ag-` so nothing here can collide with a shared class. The chat bubbles are the generative
// assistant's (`.gchat`, `.gmsg`, `.gaskrow`), injected by `injectGenerativeStyles()`.

import { injectGenerativeStyles } from "../generative/styles.js";

const CSS = `
.ag{display:flex;flex-direction:column}
.ag .fstep:first-of-type{padding-top:16px;border-top:1px solid var(--line)}
.ag-lead{margin:0 0 8px;font-size:13px;color:var(--ink2);line-height:1.5}
.ag-group{margin-top:12px;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.ag-group>h4{margin:0;padding:10px 14px;background:var(--soft);border-bottom:1px solid var(--line);font-size:12px;font-weight:600;color:var(--ink2)}
.ag-group>.ag-cap{margin:0;padding:8px 14px 0;font-size:12px;color:var(--muted);line-height:1.45}
.ag-item{display:grid;grid-template-columns:auto minmax(0,1fr);gap:10px;padding:12px 14px;border-top:1px solid var(--line);font-size:13px;align-items:start;cursor:pointer}
.ag-group>h4 + .ag-item,.ag-group>.ag-cap + .ag-item{border-top:0}
.ag-item input{width:16px;height:16px;margin:2px 0 0;accent-color:var(--brand-blue)}
.ag-item .ag-t{color:var(--ink);font-weight:500;overflow-wrap:anywhere}
.ag-item .ag-r{display:block;margin-top:3px;font-size:12px;color:var(--muted);line-height:1.45}
.ag-item .pill{margin-left:8px;padding:1px 8px}
.ag-q{padding:12px 14px;border-top:1px solid var(--line);font-size:13px}
.ag-group>h4 + .ag-q{border-top:0}
.ag-q .ag-qt{color:var(--ink);font-weight:500;line-height:1.5}
.ag-q .pill{margin-left:8px;padding:1px 8px}
.ag-opts{display:flex;flex-direction:column;gap:8px;margin-top:8px}
.ag-opt{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.ag-opt .btn[aria-pressed="true"]{border-color:var(--brand-blue);color:var(--brand-blue)}
.ag-opt .ag-eff{font-size:12px;color:var(--muted)}
.ag-stop p{margin:0;padding:14px 20px 0;line-height:1.55}
.ag-sum{margin-top:12px;display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px 24px}
.ag-sum h5{margin:0 0 4px;font-size:12px;font-weight:600;color:var(--ink2)}
.ag-sum ul{margin:0;padding-left:18px;font-size:13px;color:var(--ink);line-height:1.5}
.ag-sum li{overflow-wrap:anywhere}
.ag-sum .ag-none{font-size:13px;color:var(--muted)}
.ag-note{margin:8px 0 0;font-size:12px;color:var(--muted)}
.ag-steps{margin:8px 0 0;padding-left:18px;font-size:13px;color:var(--ink2);line-height:1.5}
.ag-steps li{overflow-wrap:anywhere}
.ag-preview{margin-top:12px;display:flex;flex-direction:column;gap:12px}
.ag-preview h5{margin:0;font-size:12px;font-weight:600;color:var(--ink2)}
.ag-preview .tbl-wrap{border:1px solid var(--line);border-radius:8px}
.ag .actions{margin-top:16px}
.ag-done{margin:0;font-size:14px;color:var(--ink);line-height:1.5}
.ag-done .ok{font-weight:600}
.ag-aside[hidden]{display:none}
.ag-practice{display:flex;align-items:center;gap:8px;margin:0;padding:12px 20px 0;font-size:12px;color:var(--muted)}
.ag-practice::before{content:"i";display:inline-grid;place-items:center;width:18px;height:18px;border-radius:50%;border:1px solid var(--line2);font-size:12px;font-weight:600;color:var(--muted);flex:none}
.ag-chat .gchat{max-height:420px;overflow:auto}
.ag-chat .gmsg{max-width:90%;overflow-wrap:anywhere}
.ag-chat .gaskrow .btn{flex:none}
.ag-chat .control input[type=text]{width:100%;height:100%;border:0;background:transparent;color:inherit;font:inherit;padding:0 12px;outline:none}
.ag-chat .gaskrow{padding:12px 16px 16px}
.ag-sent{margin-top:8px;font-size:12px;color:var(--muted)}
.ag-sent>summary{cursor:pointer;padding:2px 0;border-radius:4px}
.ag-sent>summary:focus-visible,.ag-sent-pre:focus-visible{outline:2px solid var(--brand-blue);outline-offset:2px}
.ag-sent-list{list-style:none;margin:6px 0 0;padding:0;display:flex;flex-direction:column;gap:8px}
.ag-sent-tool{display:block;color:var(--ink2);font-weight:600}
.ag-sent-meta{display:block;color:var(--muted)}
.ag-sent-pre{margin:4px 0 0;padding:8px 10px;max-height:140px;overflow:auto;background:var(--surface);border:1px solid var(--line);border-radius:6px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;line-height:1.45;color:var(--ink);white-space:pre-wrap;overflow-wrap:anywhere}
.ag-sent-more{color:var(--muted);font-style:italic}
.ag-sent-foot{margin:8px 0 0;font-size:12px;color:var(--muted)}
.ag-access{margin:0;padding:0 20px 16px;font-size:12px;color:var(--muted);line-height:1.45}
.ag-access a{color:var(--brand-blue)}
.ag-previewbar{margin-top:12px}
@media (max-width:700px){.ag-opt{flex-direction:column;align-items:flex-start}.ag-sum{grid-template-columns:1fr}}
`;

const STYLE_ID = "agent-styles";

export function injectAgentStyles() {
  injectGenerativeStyles();
  if (typeof document === "undefined" || !document.head || document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = CSS;
  document.head.appendChild(style);
}
