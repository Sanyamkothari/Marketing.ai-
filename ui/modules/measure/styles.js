// Step 4's few rules, injected once (as `modules/uplift/styles.js` does) rather than added to the
// shared `ui/index.html`. Tokens only, so light, dark and `data-theme` apply with no block of their
// own; classes are prefixed `measure` so nothing collides. Spacing on the v1 scale (4, 8, 12, 16, 24).

const CSS = `
.card.measure{margin-top:24px;padding:24px}
.card.measure h3{margin:0 0 8px}
.measure-lead{margin:0;font-size:15px;line-height:1.5;color:var(--ink);max-width:760px}
.measure-hint{margin:4px 0 0;font-size:13px}
.measure .btn-row{margin-top:16px}
.measure-upload{position:relative;cursor:pointer}
.measure-upload:focus-within{outline:2px solid var(--c);outline-offset:2px}
.measure-upload.is-busy{opacity:.6;cursor:progress}
.measure-file{position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:inherit}
.measure-result{margin-top:8px;padding:16px 20px;border:1px solid var(--line2);border-left:4px solid var(--muted);border-radius:8px;background:var(--surface)}
.measure-result.ok{border-left-color:var(--ok)}
.measure-result.warn{border-left-color:var(--warn)}
.measure-result.bad{border-left-color:var(--bad)}
.measure-big{margin:0;font-size:24px;font-weight:600;line-height:1.3;color:var(--ink);font-feature-settings:"tnum"}
.measure-sub{margin:8px 0 0;font-size:14px;line-height:1.5;color:var(--ink2);max-width:760px}
.measure-rates{list-style:none;margin:16px 0 0;padding:0;display:grid;gap:8px;max-width:520px}
.measure-rates li{display:grid;grid-template-columns:96px 1fr auto;gap:12px;align-items:baseline;font-size:14px;font-feature-settings:"tnum"}
.measure-arm{font-weight:600;color:var(--ink)}
.measure-rate{color:var(--ink)}
.measure-learn{margin-top:16px;display:flex;flex-wrap:wrap;gap:12px;align-items:center}
.measure-learn p{margin:0;font-size:14px;color:var(--ink2);max-width:640px}
.measure-learn-no{margin:16px 0 0;font-size:13px;max-width:760px}
.measure-busy{margin:12px 0 0;font-size:13px;color:var(--muted)}
.measure-more{margin-top:16px}
.measure-summary{font-size:13px;color:var(--ink2);max-width:760px}
.measure-kv{display:grid;gap:4px;margin:8px 0}
.measure-kv div{display:grid;grid-template-columns:200px 1fr;gap:12px;font-size:13px}
.measure-kv dt{color:var(--muted)}
.measure-kv dd{margin:0;color:var(--ink);font-feature-settings:"tnum"}
@media (max-width:700px){.measure-rates li{grid-template-columns:1fr auto}.measure-rates li .muted{grid-column:1 / -1}.measure-kv div{grid-template-columns:1fr}}
`;

let injected = false;

export function injectMeasureStyles() {
  if (injected || typeof document === "undefined" || !document.head) return;
  injected = true;
  const style = document.createElement("style");
  style.id = "measure-styles";
  style.textContent = CSS;
  document.head.appendChild(style);
}
