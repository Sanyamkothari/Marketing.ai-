// The generative screens' own stylesheet, injected once rather than added to `ui/index.html`.
//
// `index.html` is a shared file other phase branches also edit (PARALLEL_WORK_PROTOCOL.md §4), and
// its markers are for a `<script>` tag, not for CSS - there is nowhere in it for a phase's rules to
// live without every other phase reflowing around them. Every rule below reads the same custom
// properties `index.html` already defines (`--ok`, `--bad-t`, `--muted`, ...), so light mode, dark
// mode and the `data-theme` override all apply to these screens with no separate dark-mode block of
// their own - the same reason `ui/dom.js`'s inline SVG helpers need none. Classes are prefixed `g`
// so nothing here can collide with a class a shared file defines now or later.

const CSS = `
.gbackend{display:flex;flex-direction:column;gap:2px;padding:10px 14px;border-radius:8px;border:1px solid var(--line);font-size:12px;margin:8px 0 20px;width:fit-content}
.gbackend b{font-size:13px}
.gbackend span{color:var(--muted)}
.gbackend.g-fake{border-color:var(--warn);background:var(--warn-t);color:var(--warn)}
.gbackend.g-fake span{color:var(--warn)}
.gbackend.g-live{border-color:var(--ok);background:var(--ok-t);color:var(--ok)}
.gbackend.g-live span{color:var(--ink2)}
.gbackend.g-unknown{color:var(--muted)}
.gusage{font-size:12px;color:var(--ink2);margin:0 0 20px}
.gusage b{color:var(--ink);font-weight:600}
.gwarn{color:var(--warn)}
.gchecks{list-style:none;margin:8px 0 0;padding:0;display:flex;flex-direction:column;gap:6px}
.gchecks li{display:flex;gap:8px;align-items:baseline;font-size:12px;color:var(--ink2)}
.gcite{margin:10px 0 0;padding:10px 12px;border-left:3px solid var(--c,var(--brand-blue));background:var(--soft);border-radius:0 8px 8px 0}
.gcite figcaption{font-size:11px;font-weight:600;color:var(--muted);display:flex;justify-content:space-between;gap:8px}
.gcite .gsim{font-weight:400;color:var(--faint)}
.gcite blockquote{margin:6px 0 0;font-size:13px;color:var(--ink2);font-style:italic}
.gref{display:inline-flex;align-items:center;gap:4px;font-size:12px;padding:3px 8px;border-radius:999px;background:var(--soft);border:1px solid var(--line);color:var(--ink2);margin:0 6px 6px 0}
.gref code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.gref-quote{font-style:italic;color:var(--muted);border-style:dashed}
.ggrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px}
.gband-h{font-size:13px;font-weight:600;color:var(--ink);margin:22px 0 -2px}
.gband-h:first-child{margin-top:0}
.gtpl{border:1px solid var(--line2);border-radius:10px;padding:16px;display:flex;flex-direction:column;gap:10px;background:var(--surface)}
.gtpl.blocked{border-color:var(--bad)}
.gtpl-head{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;font-size:13px;font-weight:600}
.gtpl-body{font-size:13px;color:var(--ink2);line-height:1.5}
.gtpl-subj{font-size:12px;color:var(--muted);margin-bottom:2px}
.gtpl-meta{font-size:11px;color:var(--faint)}
.gtpl-block-reason{font-size:12px;color:var(--bad)}
.gtpl-actions{display:flex;gap:8px;margin-top:2px}
.gtpl-actions button{height:32px;padding:0 12px;border-radius:7px;font:inherit;font-size:12px;font-weight:500;cursor:pointer}
.gtpl-actions .g-approve{border:1px solid var(--line2);background:var(--surface);color:var(--ink)}
.gtpl-actions .g-approve:hover{border-color:var(--ok);color:var(--ok)}
.gtpl-actions .g-approve[disabled],.gtpl-actions .g-regen[disabled]{opacity:.5;cursor:not-allowed}
.gtpl-actions .g-regen{border:1px solid var(--line2);background:transparent;color:var(--ink2)}
.gtpl-actions .g-regen:hover{border-color:var(--ink2)}
.gchat{display:flex;flex-direction:column;gap:14px;padding:18px 0 4px}
.gmsg{max-width:78%;padding:12px 14px;border-radius:12px;font-size:13px;line-height:1.5}
.gmsg.user{align-self:flex-end;background:var(--t,var(--p-t));color:var(--ink)}
.gmsg.bot{align-self:flex-start;background:var(--soft);border:1px solid var(--line)}
.gmsg.bot.refused{border-color:var(--warn);background:var(--warn-t)}
.gmsg .gmeta{margin-top:8px;font-size:11px;color:var(--faint);display:flex;gap:10px;flex-wrap:wrap}
.gaskrow{display:flex;gap:10px;margin-top:14px}
.gaskrow input{flex:1;height:40px;border:1px solid var(--line2);border-radius:8px;background:var(--surface);color:var(--ink);font:inherit;font-size:13px;padding:0 14px}
.gaskrow input:focus-visible{outline:2px solid var(--brand-blue);outline-offset:1px}
.gaskrow button{height:40px;padding:0 18px;border:0;border-radius:8px;background:var(--stage);color:var(--stage-ink);font:inherit;font-size:13px;font-weight:600;cursor:pointer}
.gaskrow button[disabled]{opacity:.5;cursor:not-allowed}
.gseg-row{display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px;align-items:center;font-size:12px;color:var(--muted);margin-top:2px}
.gholdout-note{font-size:12px;color:var(--muted);margin:0 0 16px}
.gnotice{display:flex;gap:8px;align-items:flex-start;padding:10px 14px;border-radius:8px;background:var(--warn-t);color:var(--warn);font-size:12px;margin-bottom:18px}
.gsegment{border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-bottom:18px}
.gsegment-h{padding:14px 20px;background:var(--soft);border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:baseline}
.gsegment-h h4{margin:0;font-size:14px}
.gsegment-body{padding:16px 20px;display:flex;flex-direction:column;gap:14px}
.gcause{border-top:1px solid var(--line);padding-top:14px}
.gcause:first-child{border-top:0;padding-top:0}
.gcause p{margin:4px 0 0;font-size:13px;color:var(--ink2)}
.gactions-list{margin:0;padding-left:18px;font-size:13px;color:var(--ink2);display:flex;flex-direction:column;gap:4px}
.gcaveat{font-size:12px;color:var(--faint)}
`;

let injected = false;

export function injectGenerativeStyles() {
  if (injected || typeof document === "undefined") return;
  injected = true;
  const style = document.createElement("style");
  style.id = "generative-styles";
  style.textContent = CSS;
  document.head.appendChild(style);
}
