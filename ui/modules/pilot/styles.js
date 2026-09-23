// Plan E's stylesheet, injected once (the same reasoning as `production/styles.js`): every rule
// reads the custom properties `index.html` defines, so light, dark and the theme override apply, and
// every class is prefixed `pe-` so nothing here restyles a class another screen owns.

const CSS = `
.pe-bar{max-width:1440px;margin:12px auto -12px;padding:0 4px;display:flex;justify-content:flex-start}
.pe-bar:empty{display:none}
.pe-bar-in{display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px;font-size:12px;color:var(--muted)}
.pe-bar-in a,.pe-bar-in button{color:var(--brand-blue);font:inherit;font-size:12px;background:none;border:0;padding:0;cursor:pointer;text-decoration:underline}
.pe-demo{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;background:var(--warn-t);color:var(--warn);font-weight:600}
@media (max-width:900px){.pe-bar{margin:0;padding:10px 16px;background:var(--surface);border-bottom:1px solid var(--line)}}
.pe-q{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;margin-left:6px;border-radius:999px;border:1px solid var(--line2);background:var(--surface);color:var(--brand-blue);font:600 11px/1 inherit;cursor:pointer;vertical-align:middle;padding:0}
.pe-q:hover{border-color:var(--brand-blue)}
.pe-q:focus-visible{outline:2px solid var(--brand-blue);outline-offset:2px}
.pe-pop{position:fixed;z-index:60;max-width:min(360px,calc(100vw - 32px));background:var(--surface);color:var(--ink);border:1px solid var(--line2);border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.18);padding:12px 14px;font-size:13px;line-height:1.45}
.pe-pop[hidden]{display:none}
.pe-pop b{display:block;margin-bottom:4px}
.pe-pop p{margin:6px 0 0}
.pe-pop .pe-fix{color:var(--ink2)}
.pe-fb-btn{position:fixed;right:16px;bottom:16px;z-index:50;border:1px solid var(--line2);background:var(--surface);color:var(--ink);border-radius:999px;padding:8px 14px;font:inherit;font-size:13px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.12)}
.pe-fb-btn:focus-visible{outline:2px solid var(--brand-blue);outline-offset:2px}
.pe-fb{position:fixed;right:16px;bottom:64px;z-index:55;width:min(340px,calc(100vw - 32px));background:var(--surface);border:1px solid var(--line2);border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.2);padding:14px;display:flex;flex-direction:column;gap:10px;font-size:13px}
.pe-fb[hidden]{display:none}
.pe-fb textarea,.pe-fb select,.pe-in{width:100%;border:1px solid var(--line2);border-radius:8px;background:var(--surface);color:var(--ink);font:inherit;font-size:13px;padding:8px 10px}
.pe-fb textarea{min-height:90px;resize:vertical}
.pe-row{display:flex;gap:8px;justify-content:flex-end;align-items:center;flex-wrap:wrap}
.pe-btn{border:1px solid var(--line2);background:var(--surface);color:var(--ink);border-radius:8px;padding:7px 14px;font:inherit;font-size:13px;cursor:pointer}
.pe-btn.primary{background:var(--brand-blue);border-color:var(--brand-blue);color:#fff}
.pe-btn:focus-visible{outline:2px solid var(--brand-blue);outline-offset:2px}
.pe-note{font-size:12px;color:var(--muted)}
.pe-note a{color:var(--brand-blue);text-decoration:underline}
.pe-tour{position:fixed;left:16px;bottom:16px;z-index:58;width:min(380px,calc(100vw - 32px));background:var(--surface);border:1px solid var(--line2);border-left:4px solid var(--brand-blue);border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.2);padding:16px;font-size:13px}
.pe-tour h3{margin:0 0 6px;font-size:15px}
.pe-tour p{margin:0 0 12px;color:var(--ink2);line-height:1.5}
.pe-tour .pe-step{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px}
.pe-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;margin-top:8px}
.pe-card{border:1px solid var(--line);border-radius:12px;padding:18px 20px;background:var(--surface)}
.pe-card h2{margin:0 0 6px;font-size:16px}
.pe-card p{margin:0 0 10px;color:var(--muted);font-size:13px}
.pe-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px;font-size:13px}
.pe-list li{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:baseline;justify-content:space-between;border-top:1px solid var(--line);padding-top:8px}
.pe-list li:first-child{border-top:0;padding-top:0}
.pe-links{display:flex;gap:12px;flex-wrap:wrap}
.pe-links a{color:var(--brand-blue);text-decoration:underline}
.pe-code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;background:var(--soft);border:1px solid var(--line);border-radius:6px;padding:8px 10px;overflow-x:auto;white-space:pre}
.pe-frame{width:100%;min-height:70vh;border:1px solid var(--line);border-radius:10px;background:#fff}
.pe-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin:12px 0}
.pe-form label{display:flex;flex-direction:column;gap:6px;font-size:12px;color:var(--muted)}
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
