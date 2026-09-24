// The generative screens' own stylesheet, injected once rather than added to `ui/index.html`.
//
// `index.html` is a shared file other phase branches also edit (PARALLEL_WORK_PROTOCOL.md §4), and
// its markers are for a `<script>` tag, not for CSS - there is nowhere in it for a phase's rules to
// live without every other phase reflowing around them. Every rule below reads the same custom
// properties `index.html` already defines (`--ok`, `--bad-t`, `--muted`, ...), so light mode, dark
// mode and the `data-theme` override all apply to these screens with no separate dark-mode block of
// their own. Classes are prefixed `g` so nothing here can collide with a class a shared file defines.
//
// v1 UI: buttons, inputs, notices and cards come from the shared components (`.btn`, `.control`,
// `.card.notice-card`, `details.tech`); this sheet holds only the layout these screens add, and the
// classes that replaced their one-off `style=""` attributes. Spacing 4/8/12/16/24; text 12px or more.

const CSS = `
.gbackend{display:inline-flex;align-items:center;gap:8px;min-height:24px;margin:0 0 16px;font-size:13px;color:var(--ink2)}
.gbackend::before{content:"i";display:inline-grid;place-items:center;width:18px;height:18px;border-radius:50%;border:1px solid var(--line2);font-size:12px;font-weight:600;color:var(--muted);flex:none}
.gbackend:hover{color:var(--brand-blue);text-decoration:underline}
.gbackend.g-unknown{color:var(--muted)}
.gusage{font-size:12px;color:var(--ink2)}
.gusage b{color:var(--ink);font-weight:600}
.gwarn{color:var(--warn)}
.gdetails{margin-top:0}
.gdetails-body{display:flex;flex-direction:column;gap:8px;padding-top:8px}
.gcounts{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.card>details.tech{margin:0 20px;padding:12px 0 16px}
.gchecks{list-style:none;margin:8px 0 0;padding:0;display:flex;flex-direction:column;gap:8px}
.gchecks li{display:flex;gap:8px;align-items:baseline;font-size:12px;color:var(--ink2)}
.gcite{margin:12px 0 0;padding:8px 12px;border-left:3px solid var(--c,var(--brand-blue));background:var(--surface);border-radius:0 8px 8px 0}
.gcite figcaption{font-size:12px;font-weight:600;color:var(--muted);display:flex;justify-content:space-between;gap:8px}
.gcite .gsim{font-weight:400;color:var(--muted)}
.gcite blockquote{margin:4px 0 0;font-size:13px;color:var(--ink2);font-style:italic}
.gref{display:inline-flex;align-items:center;gap:4px;font-size:12px;padding:4px 8px;border-radius:999px;background:var(--soft);border:1px solid var(--line);color:var(--ink2);margin:0 8px 8px 0}
.gref code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.gref-quote{font-style:italic;color:var(--muted);border-style:dashed}
.gbody{padding:16px 20px}
.gbody>p{margin:0 0 12px;font-size:13px;color:var(--ink2);max-width:640px}
.gbody>p.gmuted{color:var(--muted)}
.gverdict{margin:0;font-size:20px;font-weight:600;color:var(--ink)}
.gverdict-sub{margin:4px 0 0;font-size:13px;color:var(--muted)}
.gsummary{display:flex;flex-direction:column;gap:12px;padding:16px 20px}
.gsummary p{margin:0;font-size:13px;color:var(--ink2);max-width:720px}
.gsummary p.gverdict{font-size:20px;color:var(--ink)}
.gsummary p.gverdict-sub{margin-top:4px;color:var(--muted)}
.gsummary .tnum b{font-weight:600;color:var(--ink)}
.gapprover{display:flex;flex-direction:column;gap:8px;max-width:320px}
.gapprover label{font-size:12px;color:var(--muted)}
.gapprover .fhint{margin:0}
.gscreen .control input[type=text]{width:100%;height:100%;border:0;background:transparent;color:inherit;font:inherit;padding:0 12px;outline:none}
.gscreen .control input[readonly]{color:var(--ink2)}
.gscreen .control:has(input[readonly]){background:var(--soft)}
.gbands{display:flex;flex-direction:column;gap:24px;padding:0 20px 20px}
.card h4.gband-h{display:flex;align-items:center;gap:8px;margin:0 0 12px;padding:0;background:none;border:0;font-size:14px;font-weight:600;color:var(--ink)}
.ggrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px}
.gtpl{border:1px solid var(--line2);border-radius:10px;padding:16px;display:flex;flex-direction:column;gap:12px;background:var(--surface)}
.gtpl.blocked{border-color:var(--bad)}
.gtpl-head{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;font-size:13px;font-weight:600}
.gtpl-body{font-size:13px;color:var(--ink2);line-height:1.5}
.gtpl-subj{font-size:12px;color:var(--muted);margin-bottom:4px}
.gtpl-meta{font-size:12px;color:var(--muted)}
.gtpl-block-reason{font-size:12px;color:var(--bad)}
.gtpl-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:auto}
.gtpl details.tech{padding-top:8px}
.gtpl details.tech p{margin:8px 0 0}
.gchat{display:flex;flex-direction:column;gap:16px;padding:16px 20px 4px}
.gchat .empty{padding:0}
.gmsg{max-width:78%;padding:12px 16px;border-radius:12px;font-size:13px;line-height:1.5}
.gmsg.user{align-self:flex-end;background:var(--t,var(--p-t));color:var(--ink)}
.gmsg.bot{align-self:flex-start;background:var(--soft);border:1px solid var(--line)}
.gmsg.bot.refused{border-color:var(--warn);background:var(--warn-t)}
.gmsg .gmeta{margin-top:8px;font-size:12px;color:var(--muted);display:flex;gap:12px;flex-wrap:wrap}
.gaskrow{display:flex;gap:12px;padding:12px 20px 20px}
.gaskrow .control{flex:1 1 auto;height:40px}
@media (max-width:700px){.gmsg{max-width:100%}.gaskrow{flex-direction:column}.gaskrow .control{flex:none}.gaskrow .btn{width:100%}}
.gseg-row{display:flex;justify-content:space-between;flex-wrap:wrap;gap:12px;align-items:center;font-size:12px;color:var(--muted);margin-top:4px}
.gsegments{display:flex;flex-direction:column;gap:16px;padding:16px 20px}
.gsegments .caption{padding:0}
.gsegment{border:1px solid var(--line);border-radius:10px;overflow:hidden}
.gsegment-h{padding:12px 20px;background:var(--soft);border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:baseline;font-size:12px;color:var(--muted)}
.card .gsegment-h h4{margin:0;padding:0;background:none;border:0;font-size:14px;color:var(--ink)}
.gsegment-body{padding:16px 20px;display:flex;flex-direction:column;gap:16px}
.gsegment-body .empty{padding:0}
.gheadline{margin:0;font-size:14px;font-weight:600;color:var(--ink)}
.gcause{border-top:1px solid var(--line);padding-top:12px}
.gcause:first-child{border-top:0;padding-top:0}
.gcause-h{display:flex;justify-content:space-between;gap:12px;align-items:baseline;font-size:13px}
.gcause-refs{margin-top:8px}
.gactions-h{display:block;font-size:12px;font-weight:600;color:var(--ink)}
.gactions-list{margin:4px 0 0;padding-left:16px;font-size:13px;color:var(--ink2);display:flex;flex-direction:column;gap:4px}
.gcaveat{font-size:12px;color:var(--muted)}
.gform{margin-top:16px}
.gform .actions .btn{margin:0}
.gopt{font-weight:400;color:var(--muted)}
.gdocs{margin-top:12px;border:1px solid var(--line);border-radius:8px}
.gdocs .runrow:first-child{border-top:0}
.gcols{margin-top:12px}
.gtabs{margin-bottom:4px}
.gpass{font-size:28px;font-weight:700;color:var(--ink)}
.gpass-track{margin-top:12px}
.gevalhead{padding:16px 20px 4px}
.gevaltbl{margin-top:12px}
.grow{margin-top:24px}
.grow + #g-chat{margin-top:24px}
.gcard-foot{padding:0 20px 16px}
.glink-row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
.gtest-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.greport{margin-top:16px;display:flex;flex-direction:column;gap:12px}
.greport .gverdict{display:flex;align-items:center;gap:8px}
.greport .gverdict .ok{color:var(--ok)}
.gmodels{border:1px solid var(--line);border-radius:8px}
.gmodels .runrow:first-child{border-top:0}
.gmodels .r3{text-align:left;white-space:normal;max-width:320px;font-size:12px;color:var(--muted)}
.gsource{display:flex;flex-direction:column;gap:12px;margin:0;padding:0;border:0;min-width:0}
.gsource legend{padding:0;margin-bottom:8px}
.gsource .check{height:auto;min-height:24px;align-items:flex-start}
.gsource .check input{margin-top:2px}
.gsource .check small{display:block;font-size:12px;color:var(--muted);font-weight:400}
.gprofile{width:280px;margin-top:4px}
@media (max-width:700px){.gprofile{width:100%}}
.gsave-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:16px;padding-top:16px;border-top:1px solid var(--line)}
.gsave-row .spacer{flex:1}
.gscreen .card>details.tech:last-child{padding-bottom:16px}
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
