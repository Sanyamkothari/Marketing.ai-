// "Ask your data" stylesheet (also used by the Data Doctor chat for "What the AI looked at"), injected once
// rather than added to `ui/index.html`.
//
// Tokens only (docs/ui/FOUNDATION.md): every colour is a custom property `index.html` defines. Classes keep the
// `ag-` / `ask-` prefixes. The chat bubbles are the generative assistant's (`.gchat`, `.gmsg`, `.gaskrow`),
// injected by `injectGenerativeStyles()`.

import { injectGenerativeStyles } from "../generative/styles.js";

const CSS = `
.ag-nochat{display:flex;flex-direction:column;gap:4px;padding:14px 20px 18px}
.ag-nochat p{margin:0;font-size:13px;color:var(--ink2);line-height:1.5}
.ag-nochat a{color:var(--brand-blue);font-weight:500}
.ag-nochat a:hover{text-decoration:underline}
.ag-nochat .sub{font-size:12px;color:var(--muted)}
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
.ask .ask-lead{margin:0;padding:0 20px 8px;font-size:13px;color:var(--ink2);line-height:1.5}
.ask .gaskrow .btn.quiet{flex:none}
.ask-chart{margin:10px 0 0;padding:10px 12px;background:var(--surface);border:1px solid var(--line);border-radius:8px;color:var(--ink)}
.ask-chart figcaption{font-size:12px;font-weight:600;color:var(--ink2);margin-bottom:8px;overflow-wrap:anywhere}
.ask-chart .bars{padding:0;gap:8px}
.ask-chart .brow{grid-template-columns:minmax(72px,160px) minmax(60px,1fr) auto;gap:10px;font-size:12px}
.ask-chart .brow .pct{font-variant-numeric:tabular-nums;white-space:nowrap}
.ask-chart .ask-rows{display:block;font-size:11px;color:var(--muted)}
.ask-chart .is-suppressed .pct{font-weight:400;color:var(--muted)}
.ask-chart .caption{margin:8px 0 0;padding:0;font-size:11px;color:var(--muted)}
`;

const STYLE_ID = "ask-styles";

export function injectAskStyles() {
  injectGenerativeStyles();
  if (typeof document === "undefined" || !document.head || document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = CSS;
  document.head.appendChild(style);
}
