// The "New campaign" wizard's styles (M177): injected once, scoped under `.cw`, built on the page's own tokens
// (ui/index.html), so both layouts and both themes draw it with the colours every other screen uses.

const CSS = `
.cw .cw-steps{list-style:none;display:flex;flex-wrap:wrap;gap:8px;margin:20px 0 0;padding:0}
.cw .cw-step{display:flex;align-items:center;gap:8px;padding:6px 12px 6px 6px;border:1px solid var(--line);border-radius:999px;font-size:13px;color:var(--muted);background:var(--surface)}
.cw .cw-step .cw-no{display:inline-grid;place-items:center;width:22px;height:22px;border-radius:50%;background:var(--soft);font-size:12px;font-weight:600}
.cw .cw-step.now{border-color:var(--ink);color:var(--ink);font-weight:600}
.cw .cw-step.now .cw-no{background:var(--btn, var(--ink));color:var(--btn-ink, var(--surface))}
.cw .cw-step.done{color:var(--ink2, var(--ink))}
.cw .cw-step.done .cw-no{background:var(--ok-t, var(--soft));color:var(--ok, var(--ink))}
.cw .cw-body{display:flex;flex-direction:column;gap:16px;margin-top:20px}
.cw .cw-card-body{padding:18px 20px}
.cw .cw-h{font-size:16px;font-weight:600;margin:0 0 8px;color:var(--ink)}
.cw .cw-h3{font-size:14px;font-weight:600;margin:16px 0 6px;color:var(--ink)}
.cw .cw-p{font-size:14px;line-height:1.55;color:var(--ink2, var(--ink));margin:6px 0}
.cw .cw-note{font-size:12px;color:var(--muted)}
.cw .cw-goals{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;margin-top:8px}
.cw .cw-goal{display:flex;flex-direction:column;align-items:flex-start;gap:6px;text-align:left;padding:14px 16px;border:1px solid var(--line2, var(--line));border-radius:10px;background:var(--surface);font:inherit;color:inherit;cursor:pointer;min-height:72px}
.cw .cw-goal:hover{border-color:var(--ink2);background:var(--soft)}
.cw .cw-goal:focus-visible{outline:2px solid var(--brand-blue);outline-offset:2px}
.cw .cw-goal-name{font-size:14px;font-weight:600;color:var(--ink)}
.cw .cw-goal-stage{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:1px 8px}
.cw .cw-more{margin-top:16px}
.cw .cw-more>summary{cursor:pointer;font-size:13px;font-weight:600;color:var(--ink)}
.cw .cw-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:16px}
.cw .cw-problem{border-left:3px solid var(--brand-yellow, var(--line));background:var(--soft);padding:8px 12px;margin:10px 0}
.cw .cw-list{margin:6px 0 0 18px;padding:0;font-size:13px}
.cw .cw-tbl{width:100%;margin-top:8px}
.cw .cw-tbl .num{text-align:right;font-feature-settings:"tnum"}
.cw .cw-kpis{margin:4px 0 12px}
.cw .cw-kvs{margin:0}
.cw .cw-kv{display:grid;grid-template-columns:minmax(120px,200px) minmax(0,1fr);gap:12px;padding:10px 0;border-top:1px solid var(--line);font-size:14px}
.cw .cw-kv:first-child{border-top:0}
.cw .cw-kv dt{color:var(--muted)}
.cw .cw-kv dd{margin:0;color:var(--ink)}
.cw .cw-meter{margin-top:12px}
.cw .cw-guided{margin-top:12px}
.cw .cw-tech{padding:0 4px}
@media (max-width:700px){.cw .cw-kv{grid-template-columns:1fr;gap:2px}.cw .cw-step .cw-sl{display:none}.cw .cw-step.now .cw-sl{display:inline}}
`;

/** Add the wizard's style sheet to the page once. */
export function injectWizardStyles(doc = document) {
  if (!doc || !doc.head || doc.getElementById("cw-styles")) return;
  const style = doc.createElement("style");
  style.id = "cw-styles";
  style.textContent = CSS;
  doc.head.appendChild(style);
}
