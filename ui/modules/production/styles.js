// Phase 4b's own stylesheet (M46-M49), injected once rather than added to `ui/index.html`.
//
// The same reasoning as `ui/modules/generative/styles.js`: `index.html` is a shared file whose
// Phase 4b block is for a `<script>` tag, and every rule below reads the custom properties
// `index.html` already defines (`--line`, `--muted`, `--bad-t`, ...), so light mode, dark mode and the
// `data-theme` override apply with no dark-mode block of their own. Every class is prefixed `pb-`
// ("phase 4b") so nothing here can collide with a class another screen defines now or later - the
// gating rules in particular are applied to *other phases'* controls (DEC-792), and must restyle
// only what they add (`.pb-why`, `.pb-off`), never a class those screens own.
//
// v1 (docs/ui/FOUNDATION.md): buttons, cards, selects, file pickers, notices and tables are the shared
// `.btn`, `.card`, `.control.sel`, `.control.file`, `.notice-card` and `.tbl` from `index.html`; the
// module's own copies of them are gone. What is left is layout these screens need and nobody else
// has. Spacing is 4/8/12/16/24 only; no rule sets a colour value.

const CSS = `
/* kept for the sign-in, account and user-bar screens (signin.js, userbar.js), until they use the
   shared classes: nothing in the admin, privacy, monitoring or approvals screens uses these */
.pb-bar{max-width:1440px;margin:12px auto -12px;padding:0 4px;display:flex;justify-content:flex-end}
.pb-bar:empty{display:none}
.pb-bar-in{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;font-size:12px;color:var(--muted)}
.pb-bar-in b{color:var(--ink);font-weight:600}
.pb-bar-in a,.pb-bar-in button{color:var(--brand-blue);font:inherit;font-size:12px;background:none;border:0;padding:0;cursor:pointer;text-decoration:underline}
.pb-role{display:inline-block;padding:2px 8px;border-radius:999px;background:var(--soft);border:1px solid var(--line);color:var(--ink2);font-size:12px;font-weight:500}
.pb-off-note{display:inline-flex;align-items:center;gap:8px;padding:4px 12px;border-radius:999px;background:var(--warn-t);color:var(--warn);font-weight:500}
@media (max-width:900px){.pb-bar{margin:0;padding:12px 20px;justify-content:flex-start;background:var(--surface);border-bottom:1px solid var(--line)}}
.pb-form{display:flex;flex-direction:column;gap:12px;max-width:420px}
.pb-form.wide{max-width:none}
.pb-field{display:flex;flex-direction:column;gap:8px}
.pb-field .sub{font-size:12px;color:var(--muted)}
.pb-input{height:40px;border:1px solid var(--line2);border-radius:8px;background:var(--surface);color:var(--ink);font:inherit;font-size:13px;padding:0 12px;width:100%}
.pb-signin{max-width:440px}

/* gating (gate.js, controls.actionButton): disabled and explained in place */
.pb-why{display:inline-block;margin-left:8px;font-size:12px;color:var(--muted);vertical-align:middle}
.pb-off{cursor:not-allowed!important}
.pb-off *{cursor:not-allowed!important}
a.pb-off{pointer-events:none;color:var(--muted)}
.linkbtn.pb-off,.cancel.pb-off{color:var(--muted)}
label.control.pb-off{background:var(--soft);color:var(--muted);border-color:var(--line)}
button.pb-off:not(.btn):not(.linkbtn):not(.cancel){background:var(--soft);color:var(--muted);border-color:var(--line)}

/* the admin, privacy, monitoring, campaign and approval screens */
.pb-tabs{margin-bottom:8px}
.apierr.pb-refused{border-left-color:var(--warn);background:var(--soft)}
.pb-screen [hidden]{display:none!important}
.pb-screen .control input:not([type=checkbox]):not([type=radio]):not([type=file]),.pb-screen .control textarea{appearance:none;-webkit-appearance:none;width:100%;height:100%;border:0;background:transparent;color:inherit;font:inherit;padding:0 12px;outline:none}
.pb-screen .control.area{height:auto}
.pb-screen .control textarea{padding:8px 12px;resize:vertical;min-height:64px}
.pb-screen .field.wide{width:100%;max-width:640px}
.pb-hint{font-size:12px;color:var(--muted);line-height:1.4}
.pb-invalid{margin:8px 0 0;font-size:13px;color:var(--bad)}
.pb-small{font-size:12px;color:var(--muted)}
.pb-mono,.pb-screen .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;overflow-wrap:anywhere}
.pb-ok{margin-top:12px;padding:12px 16px;border:1px solid var(--line);border-left:3px solid var(--ok);border-radius:8px;background:var(--ok-t);color:var(--ink2);font-size:13px}
.pb-ok details.tech{margin-top:8px}
.pb-lead{margin:0 0 8px;font-size:20px;font-weight:600;color:var(--ink);line-height:1.35}
.pb-desc{margin:0;font-size:13px;color:var(--ink2);line-height:1.5;max-width:640px}
.pb-legal{margin:24px 0 0;font-size:12px;color:var(--muted);max-width:640px}
.pb-note{margin:0 0 8px;font-size:13px;color:var(--muted)}
.pb-meta{margin:0 0 12px;font-size:13px;color:var(--ink2)}
.pb-stack{display:flex;flex-direction:column;gap:12px}
.pb-row-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.pb-row-actions .spacer{min-width:8px}
.pb-confirm{display:inline-flex;flex-wrap:wrap;gap:8px;align-items:center}
.pb-confirm-q{font-size:13px;color:var(--ink)}
.pb-form-actions{display:flex;flex-wrap:wrap;align-items:center;gap:12px;padding-top:16px;margin-top:4px;border-top:1px solid var(--line)}
.pb-form-actions .spacer{min-width:8px}
.pb-screen details.adv{margin-top:4px}
.pb-screen details.adv .pb-adv-body{display:flex;flex-direction:column;gap:12px;padding-top:12px}
details.pb-reveal>summary{list-style:none}
details.pb-reveal>summary::-webkit-details-marker{display:none}
details.pb-reveal[open]>summary{margin-bottom:16px}
.pb-for-client{font-size:13px;color:var(--ink2)}
.pb-for-client b{font-weight:600;color:var(--ink)}
.pb-roles{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
.pb-roles label{display:grid;grid-template-columns:auto 1fr;gap:4px 8px;align-items:start;align-content:start;font-size:13px;color:var(--ink)}
.pb-roles input{width:16px;height:16px;margin:2px 0 0;accent-color:var(--brand-blue)}
.pb-roles .pb-hint{grid-column:2}
.pb-filters{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end}
.pb-pager{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;padding:12px 20px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
.pill.pb-pill-none{background:var(--soft);color:var(--muted);border:1px solid var(--line)}
.pb-h2h tr.pb-worse td{background:var(--bad-t)}
.pb-measures{padding:8px 0 0;margin-bottom:12px}
.pb-measures>summary{cursor:pointer;font-size:13px;font-weight:500;color:var(--brand-blue);min-height:24px;list-style:none}
.pb-measures>summary::-webkit-details-marker{display:none}
.pb-rowdetails>summary{cursor:pointer;list-style:none;font-weight:500;color:var(--ink)}
.pb-rowdetails>summary::-webkit-details-marker{display:none}
.pb-rowdetails>summary::after{content:" ›";color:var(--muted)}
.pb-rowdetails[open]>summary::after{content:" ⌄"}
.pb-rowdetails p{margin:8px 0 0;font-size:13px;color:var(--ink2);white-space:normal;max-width:560px;line-height:1.5}
.pb-audit-ref{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:12px}
.pb-pad{padding:16px 20px;margin:0}
.pb-fieldset{border:0;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}
.pb-fieldset>legend{padding:0;margin-bottom:8px}
.pb-more-actions{display:inline-block}
.pb-more-actions>summary{list-style:none}
.pb-more-actions>summary::-webkit-details-marker{display:none}
.pb-more-actions[open]>summary{margin-bottom:8px}
.pb-danger-row{margin-top:16px}
.pb-report{margin-top:16px}
.pb-report details.tech{margin-top:12px}
.pb-lookup{margin-top:16px}
.pb-lookup details.tech{margin-top:12px}
.pb-screen .card-body>.pb-desc{margin-bottom:12px}
.pb-screen .tbl td{vertical-align:top}
.pb-cell{min-width:0}
.tbl-stack td .pb-cell{flex:1 1 auto}
.pb-screen td details.tech{border-top:0;padding-top:2px}
.pb-screen td details.tech>summary{min-height:20px}
.pb-msg-row td{background:var(--soft)}
.pb-edit{background:var(--soft)}
.pb-kpis{margin:8px 0 16px}
.pb-screen .notice-card{max-width:none}
.pb-screen .apierr{margin-top:12px}
.pb-screen .card-body>.apierr:first-child{margin-top:0}
.pb-screen .card-body>h4{margin:16px -20px 0;border-top:1px solid var(--line)}
@media (max-width:700px){
  .pb-screen .field,.pb-screen .field.wide{width:100%}
  .pb-row-actions .spacer{display:none}
  .pb-screen .tbl-stack td .pb-row-actions{justify-content:flex-end}
  .pb-screen .tbl-stack td:has(details.pb-rowdetails),.pb-screen .tbl-stack td:has(details.tech){flex-direction:column;align-items:flex-start;text-align:left}
}
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
