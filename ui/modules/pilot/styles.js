// Plan E's stylesheet, injected once (the same reasoning as `production/styles.js`). Since v1 the
// buttons, cards, chips, selects, empty and error states are the shared ones in `index.html`
// (docs/ui/FOUNDATION.md); what is left here is layout and the few pieces only this module draws: the
// help "?" and its popover, the feedback dialog's place, the tour card, the report frame and the value
// form's groups. Every rule reads the tokens `index.html` defines, so light, dark and the theme
// override apply, and every class is prefixed `pe-` so nothing here restyles another screen's class.

const CSS = `
.pe-qw{white-space:nowrap}
.pe-q{position:relative;display:inline-grid;place-items:center;width:24px;height:24px;margin-left:4px;padding:0;border:0;background:none;color:var(--brand-blue);font:inherit;font-size:12px;font-weight:600;line-height:1;cursor:pointer;vertical-align:middle}
.pe-q::before{content:"";position:absolute;inset:3px;border:1px solid var(--line2);border-radius:50%;background:var(--surface)}
.pe-q>span{position:relative}
.pe-q:hover::before,.pe-q[aria-expanded="true"]::before{border-color:var(--brand-blue)}
.check .pe-q{margin-left:0}
.pe-pop{position:absolute;z-index:60;width:min(360px,calc(100vw - 32px));padding:12px 16px 16px;border:1px solid var(--line);border-radius:10px;background:var(--surface);color:var(--ink2);box-shadow:0 8px 24px rgba(17,24,39,.16);font-size:13px;line-height:1.5}
.pe-pop[hidden]{display:none}
.pe-pop-head{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}
.pe-pop-title{margin:4px 0 0;font-size:14px;font-weight:600;color:var(--ink)}
.pe-pop p{margin:8px 0 0}
.pe-pop .pe-fix b{display:block;color:var(--ink);font-weight:600}
.pe-x{flex:none;display:inline-grid;place-items:center;width:28px;height:28px;margin:0 -8px 0 0;padding:0;border:0;border-radius:6px;background:none;color:var(--muted);font:inherit;font-size:18px;line-height:1;cursor:pointer}
.pe-x:hover{background:var(--soft);color:var(--ink)}
.pe-fb{position:fixed;z-index:58;right:16px;top:72px;width:min(400px,calc(100vw - 32px));max-width:none;display:flex;flex-direction:column;gap:12px;font-size:13px}
.pe-fb[hidden]{display:none}
.pe-fb h2{margin:0;font-size:15px;font-weight:600}
.pe-fb label{font-size:12px;font-weight:500;color:var(--ink2)}
.pe-fb .pe-hint{margin:-4px 0 0;font-size:12px;color:var(--muted)}
.pe-fb .control{width:100%}
.pe-textarea{width:100%;min-height:96px;resize:vertical;padding:8px 12px;border:1px solid var(--line2);border-radius:8px;background:var(--surface);color:var(--ink);font:inherit;font-size:13px}
.pe-textarea:hover{border-color:var(--ink2)}
.pe-row{display:flex;align-items:center;justify-content:flex-end;gap:12px;flex-wrap:wrap}
.pe-status{margin-right:auto;font-size:12px;color:var(--muted)}
.pe-status .apierr{margin-top:0}
.pe-tour{position:fixed;z-index:58;right:24px;bottom:24px;width:min(400px,calc(100vw - 32px));max-width:none;display:flex;flex-direction:column;gap:12px;font-size:13px}
.pe-tour.at-top{bottom:auto;top:72px}
.pe-tour h2{margin:0;font-size:15px;font-weight:600;color:var(--ink)}
.pe-tour p{margin:0;color:var(--ink2);line-height:1.5}
.pe-step{font-size:12px;font-weight:500;color:var(--muted)}
.pe-dots{display:flex;gap:8px;margin:0;padding:0;list-style:none}
.pe-dots li{width:8px;height:8px;border-radius:50%;background:var(--line2)}
.pe-dots li.on{background:var(--brand-blue)}
.pe-tour .pe-row{justify-content:space-between}
.pe-tour .pe-row .btn-row{gap:8px}
.pe-target{outline:3px solid var(--brand-blue)!important;outline-offset:4px;border-radius:8px}
.pe-offer{position:fixed;z-index:57;left:24px;bottom:24px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;max-width:calc(100vw - 48px);padding:12px 16px;font-size:13px}
.pe-offer p{margin:0;color:var(--ink2)}
.pe-paper{margin-top:24px;padding:24px;border:1px solid var(--line);border-radius:12px;background:var(--soft)}
.pe-frame{display:block;width:100%;min-height:480px;border:0;border-radius:8px;background:#fff;box-shadow:0 1px 3px rgba(17,24,39,.12)}
.pe-code{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin:8px 0 0;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--soft)}
.pe-code code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--ink2);white-space:pre-wrap;overflow-wrap:anywhere}
.pe-body{padding:16px 20px;display:flex;flex-direction:column;gap:12px;font-size:14px}
.pe-body p{margin:0;color:var(--ink2)}
.pe-body details.adv{padding-top:12px}
.pe-body details.adv>div{display:flex;flex-direction:column;gap:12px;padding-top:12px}
.pe-tables{display:flex;flex-wrap:wrap;gap:8px;margin:0;padding:0;list-style:none}
.pe-links{display:flex;flex-wrap:wrap;gap:8px 16px}
.pe-acts{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap}
.pe-name{font-weight:600;color:var(--ink)}
.pe-name + .chip{margin-left:8px}
.pe-foot{margin-top:24px;font-size:12px;color:var(--muted)}
.pe-headline{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.pe-headline .pill{font-size:12px}
.pe-verdict{margin:0;font-size:20px;font-weight:600;line-height:1.35;color:var(--ink)}
.pe-verdict-card .pe-body{gap:16px}
.pe-verdict-card .kpis{margin:0}
.pe-tiles{grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
.pe-body p.pe-range{margin:4px 0 0;font-size:13px;color:var(--muted);font-feature-settings:"tnum"}
.pe-form{display:flex;flex-direction:column;gap:24px;padding-top:16px}
.pe-form fieldset{margin:0;padding:0;border:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px 24px}
.pe-form legend{padding:0 0 12px;font-size:14px;font-weight:600;color:var(--ink)}
.pe-field{display:flex;flex-direction:column;gap:8px;min-width:0}
.pe-field>label,.pe-field>.pe-lbl{font-size:12px;font-weight:500;color:var(--ink2)}
.pe-field .control{width:100%}
.pe-field .control input{width:100%;height:100%;border:0;background:transparent;color:inherit;font:inherit;padding:0 12px;outline:none}
.pe-field .pe-help{margin:0;font-size:12px;color:var(--muted)}
.pe-field .pe-err{margin:0;font-size:12px;color:var(--bad)}
.pe-field .pe-err:empty{display:none}
.pe-field.bad .control{border-color:var(--bad)}
.pe-field.wide{grid-column:1/-1}
.pe-radios{display:flex;flex-direction:column;gap:8px}
.pe-radios label{display:flex;align-items:flex-start;gap:8px;font-size:13px;color:var(--ink)}
.pe-radios input{width:16px;height:16px;margin:2px 0 0;accent-color:var(--brand-blue)}
@media (max-width:700px){
  .pe-pop.sheet,.pe-fb,.pe-tour,.pe-offer{position:fixed;left:0;right:0;top:auto;bottom:0;width:auto;max-width:none;border-radius:12px 12px 0 0;box-shadow:0 -8px 24px rgba(17,24,39,.16)}
  .pe-tour.at-top{top:auto;bottom:0}
  .pe-paper{padding:8px;margin-left:-12px;margin-right:-12px}
  .pe-acts{justify-content:flex-start}
}
`;

let injected = false;

export function injectPilotStyles() {
  if (injected || typeof document === "undefined") return;
  injected = true;
  const style = document.createElement("style");
  style.id = "pe-styles";
  style.textContent = CSS;
  document.head.appendChild(style);
}
