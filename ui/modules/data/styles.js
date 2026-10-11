// The Data page's stylesheet (Plan M, M179), injected once; tokens only, classes prefixed `dt-`.

const CSS = `
.dt .dt-tabs{display:flex;flex-wrap:wrap;gap:4px;margin:20px 0 0;border-bottom:1px solid var(--line)}
.dt .dt-tabs a{padding:10px 16px;font-size:14px;font-weight:500;color:var(--ink2);text-decoration:none;border-bottom:2px solid transparent;margin-bottom:-1px}
.dt .dt-tabs a:hover{color:var(--ink)}
.dt .dt-tabs a[aria-current="page"]{color:var(--brand-blue);border-bottom-color:var(--brand-blue)}
.dt .dt-sec{margin-top:24px}
.dt .dt-sec>h2{margin:0 0 4px;font-size:16px;font-weight:600;color:var(--ink)}
.dt .dt-sec>p{margin:0 0 12px;font-size:13px;color:var(--muted)}
.dt .dt-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;border:1px solid var(--line);border-radius:10px;background:var(--surface)}
.dt .dt-row{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1.4fr) minmax(0,1.4fr) 210px;gap:8px 20px;align-items:start;padding:14px 16px;border-top:1px solid var(--line)}
.dt .dt-row:first-child{border-top:0}
.dt .dt-name{margin:0;font-size:14px;font-weight:600;color:var(--ink);overflow-wrap:anywhere}
.dt .dt-sub{display:block;margin-top:2px;font-size:12px;font-weight:400;color:var(--muted);overflow-wrap:anywhere}
.dt .dt-cell{font-size:13px;color:var(--ink2);line-height:1.45;overflow-wrap:anywhere}
.dt .dt-cell .cn-badge{white-space:normal}
.dt .dt-actions{display:flex;flex-wrap:wrap;gap:6px;align-items:center;justify-content:flex-end}
.dt .dt-wide{grid-column:1/-1}
.dt .dt-more{margin-top:12px}
.dt .dt-chooser{margin-top:16px;padding:16px;border:1px solid var(--line);border-radius:10px;background:var(--soft)}
.dt .dt-chooser h2{margin:0 0 4px;font-size:16px;font-weight:600;color:var(--ink)}
.dt .dt-chooser>p{margin:0 0 12px;font-size:13px;color:var(--muted)}
.dt .dt-kinds{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px}
.dt .dt-kind{display:flex;flex-direction:column;gap:8px;padding:12px 14px;border:1px solid var(--line);border-radius:8px;background:var(--surface)}
.dt .dt-kind h3{margin:0;font-size:14px;font-weight:600;color:var(--ink)}
.dt .dt-kind .btn-row{margin-top:auto}
.dt .dt-note{margin:0;font-size:12px;color:var(--muted);line-height:1.45}
.dt .dt-foot{margin-top:24px;font-size:13px;color:var(--ink2)}
.dt .dt-foot a{color:var(--brand-blue);font-weight:500}
.dt .dt-check{margin-top:24px;padding:20px;border:1px solid var(--line);border-radius:10px;background:var(--surface)}
.dt .dt-check h2{margin:0 0 8px;font-size:16px;font-weight:600;color:var(--ink)}
.dt .dt-check p{margin:0 0 8px;font-size:14px;color:var(--ink2);line-height:1.5}
.dt .dt-check ul{margin:0 0 16px;padding-left:20px;font-size:14px;color:var(--ink2);line-height:1.6}
@media (max-width:700px){.dt .dt-row{grid-template-columns:minmax(0,1fr)}.dt .dt-actions{justify-content:flex-start}}
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
