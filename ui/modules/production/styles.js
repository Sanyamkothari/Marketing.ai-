// Phase 4b's own stylesheet (M46-M49), injected once rather than added to `ui/index.html`.
//
// The same reasoning as `ui/modules/generative/styles.js`: `index.html` is a shared file whose
// Phase 4b block is for a `<script>` tag, and every rule below reads the custom properties
// `index.html` already defines (`--line`, `--muted`, `--bad-t`, ...), so light mode, dark mode and the
// `data-theme` override apply with no dark-mode block of their own. Every class is prefixed `pb-`
// ("phase 4b") so nothing here can collide with a class another screen defines now or later - the
// gating rules in particular are applied to *other phases'* controls (DEC-792), and must restyle
// only what they add (`.pb-why`, `.pb-off`), never a class those screens own.

const CSS = `
.pb-bar{max-width:1440px;margin:12px auto -12px;padding:0 4px;display:flex;justify-content:flex-end}
.pb-bar:empty{display:none}
.pb-bar-in{display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px;font-size:12px;color:var(--muted)}
.pb-bar-in b{color:var(--ink);font-weight:600}
.pb-bar-in a,.pb-bar-in button{color:var(--brand-blue);font:inherit;font-size:12px;background:none;border:0;padding:0;cursor:pointer;text-decoration:underline}
.pb-bar-in a:focus-visible,.pb-bar-in button:focus-visible{outline:2px solid var(--brand-blue);outline-offset:2px;border-radius:4px}
.pb-role{display:inline-block;padding:2px 8px;border-radius:999px;background:var(--soft);border:1px solid var(--line);color:var(--ink2);font-size:11px;font-weight:500}
.pb-off-note{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;background:var(--warn-t);color:var(--warn);font-weight:500}
@media (max-width:900px){.pb-bar{margin:0;padding:10px 20px;justify-content:flex-start;background:var(--surface);border-bottom:1px solid var(--line)}}
.pb-why{display:inline-block;margin-left:10px;font-size:12px;color:var(--muted);vertical-align:middle}
.pb-off{opacity:.45;cursor:not-allowed!important}
.pb-off *{cursor:not-allowed!important}
a.pb-off{pointer-events:none}
.pb-form{display:flex;flex-direction:column;gap:14px;max-width:420px}
.pb-form.wide{max-width:none}
.pb-field{display:flex;flex-direction:column;gap:6px}
.pb-field .sub{font-size:12px;color:var(--muted)}
.pb-input{height:38px;border:1px solid var(--line2);border-radius:8px;background:var(--surface);color:var(--ink);font:inherit;font-size:13px;padding:0 12px;width:100%}
.pb-input:hover{border-color:var(--ink2)}
.pb-input:focus-visible{outline:2px solid var(--brand-blue);outline-offset:1px}
select.pb-input{cursor:pointer}
.pb-roles{display:flex;flex-wrap:wrap;gap:6px 16px}
.pb-roles label{display:inline-flex;align-items:center;gap:7px;font-size:13px}
.pb-roles input{width:15px;height:15px;accent-color:var(--brand-blue)}
.pb-ok{margin-top:14px;padding:10px 14px;border:1px solid var(--line);border-left:3px solid var(--ok);border-radius:8px;background:var(--ok-t);color:var(--ink2);font-size:13px}
.pb-filters{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;padding:18px 20px}
.pb-row-actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.pb-edit{padding:14px 20px;background:var(--soft);border-top:1px solid var(--line);display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end}
.pb-edit .pb-field{min-width:220px}
.pb-pager{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;padding:12px 20px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
.pb-pager button{height:32px;padding:0 12px;border-radius:7px;border:1px solid var(--line2);background:var(--surface);color:var(--ink);font:inherit;font-size:12px;font-weight:500;cursor:pointer}
.pb-pager button[disabled]{opacity:.45;cursor:not-allowed}
.pb-mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;overflow-wrap:anywhere}
.pb-small{font-size:12px;color:var(--muted)}
.pb-signin{max-width:440px}
.pill.pb-pill-none{background:var(--soft);color:var(--muted);border:1px solid var(--line)}
.pb-h2h tr.pb-worse td{background:var(--bad-t)}
.pb-decide textarea.pb-input{height:auto;padding:8px 12px;resize:vertical}
`;

let injected = false;

export function injectProductionStyles() {
  if (injected || typeof document === "undefined") return;
  injected = true;
  const style = document.createElement("style");
  style.id = "production-styles";
  style.textContent = CSS;
  document.head.appendChild(style);
}
