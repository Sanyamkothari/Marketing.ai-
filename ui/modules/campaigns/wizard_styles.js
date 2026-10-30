// The "New campaign" wizard's styles (M177; the look, M187 DEC-1617): injected once, scoped under `.cw`.
//
// The components themselves - the page header, the stepper, buttons, chips, stat tiles, the value range, tables,
// attention items, cards - are drawn and styled by the design system (`ui/components.js`, `ui/theme.css`,
// docs/ux/VISUAL_DESIGN.md). This sheet only lays them out for the wizard: the goal cards, the "More goals"
// disclosure, the frame around Guided setup, the Review summary and its small tables, the decision bar, the Send
// success state, and the phone (no squeezed columns, full-width buttons, 40px targets). Every colour reads a theme
// token: brand things read `--brand-blue` and its tints, so a deployment's own colour (M182) reaches them; a tinted
// box's border is mixed from its tone's tokens (color-mix, with the hairline where it is not supported). The one
// exception is the goal card's hover shadow, neutral ink at low alpha like theme.css's own button and pop-up shadows:
// a shadow is depth, not colour, and the theme has no shadow token for a lifted card.

const CSS = `
.cw .stepper{margin:var(--s2, 8px) 0 var(--s6, 32px)}
.cw .stepper .st-step.current + .st-step.current::before{background:var(--brand-blue)}
.cw .cw-body{display:flex;flex-direction:column;gap:var(--s4, 16px)}
.cw .cw-body > .card + .card{margin-top:0}
.cw .cw-card-body{padding:var(--s5, 24px)}
.cw .cw-card-head{display:flex;align-items:center;gap:var(--s3, 12px);margin:0 0 var(--s2, 8px)}
.cw .cw-card-icon{flex:none;display:inline-grid;place-items:center;width:40px;height:40px;border-radius:var(--r, 8px);background:var(--brand-50);color:var(--brand-blue)}
.cw .cw-h{font-size:var(--fs-section, 18px);line-height:var(--lh-section, 26px);font-weight:600;letter-spacing:-0.01em;margin:0;color:var(--ink)}
.cw .cw-h3,.cw .card .cw-h3{font-size:var(--fs-small, 13px);line-height:var(--lh-small, 20px);font-weight:600;letter-spacing:0;margin:0;padding:0;border:0;background:none;color:var(--ink)}
.cw .cw-p{font-size:var(--fs-body, 14px);line-height:var(--lh-body, 22px);color:var(--ink2, var(--ink));margin:var(--s1, 4px) 0}
.cw .cw-note{font-size:var(--fs-caption, 12px);line-height:var(--lh-caption, 16px);color:var(--muted)}
.cw p.cw-note{font-size:var(--fs-small, 13px);line-height:var(--lh-small, 20px)}
.cw .cw-list{margin:var(--s2, 8px) 0 0 18px;padding:0;font-size:var(--fs-small, 13px);line-height:var(--lh-small, 20px);color:var(--ink2, var(--ink))}
.cw .cw-actions{display:flex;flex-wrap:wrap;gap:var(--s3, 12px);align-items:center;margin-top:var(--s4, 16px)}
.cw .cw-tech{padding:0 4px}

/* step 1: the goal cards - an icon, the name, the stage as a quiet tag; a lift on hover, a brand edge when chosen */
.cw .cw-section-title{margin:0 0 var(--s4, 16px)}
.cw .cw-goals{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:var(--s4, 16px)}
.cw .cw-goal{position:relative;display:flex;flex-direction:column;align-items:flex-start;gap:var(--s3, 12px);min-height:156px;padding:20px;text-align:left;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg, 12px);font:inherit;color:inherit;cursor:pointer;transition:transform var(--dur, 140ms) var(--ease, ease-out),box-shadow var(--dur, 140ms) var(--ease, ease-out),border-color var(--dur, 140ms) var(--ease, ease-out),background-color var(--dur, 140ms) var(--ease, ease-out)}
.cw .cw-goal:hover{transform:translateY(-2px);border-color:var(--brand-200);box-shadow:0 6px 16px rgba(16,24,40,.08),0 1px 3px rgba(16,24,40,.06)}
.cw .cw-goal:active{transform:translateY(0);box-shadow:0 1px 2px rgba(16,24,40,.06)}
.cw .cw-goal:focus-visible{outline:2px solid var(--brand-blue);outline-offset:2px}
.cw .cw-goal.is-selected{border-color:var(--brand-blue);box-shadow:0 0 0 1px var(--brand-blue);background:var(--brand-50)}
.cw .cw-goal-icon{display:inline-grid;place-items:center;width:40px;height:40px;border-radius:var(--r, 8px);background:var(--brand-50);color:var(--brand-blue);transition:background-color var(--dur, 140ms) var(--ease, ease-out),color var(--dur, 140ms) var(--ease, ease-out)}
.cw .cw-goal:hover .cw-goal-icon{background:var(--brand-100)}
.cw .cw-goal.is-selected .cw-goal-icon{background:var(--brand-blue);color:var(--btn-ink)}
.cw .cw-goal-name{font-size:var(--fs-card, 15px);line-height:var(--lh-card, 22px);font-weight:600;color:var(--ink)}
.cw .cw-goal-stage{margin-top:auto;display:inline-flex}
.cw .cw-goal-go{position:absolute;top:20px;right:20px;display:inline-grid;color:var(--brand-blue);opacity:0;transform:translateX(-4px);transition:opacity var(--dur, 140ms) var(--ease, ease-out),transform var(--dur, 140ms) var(--ease, ease-out)}
.cw .cw-goal:hover .cw-goal-go,.cw .cw-goal:focus-visible .cw-goal-go,.cw .cw-goal.is-selected .cw-goal-go{opacity:1;transform:none}
.cw .cw-more{margin-top:var(--s5, 24px);padding-top:var(--s4, 16px);border-top:1px solid var(--line)}
.cw .cw-more>summary{list-style:none;display:inline-flex;align-items:center;gap:var(--s2, 8px);min-height:40px;padding:0 var(--s3, 12px) 0 var(--s2, 8px);margin-left:calc(-1 * var(--s2, 8px));border-radius:var(--r, 8px);cursor:pointer;font-size:var(--fs-body, 14px);font-weight:600;color:var(--ink2, var(--ink));transition:background-color var(--dur, 140ms) var(--ease, ease-out),color var(--dur, 140ms) var(--ease, ease-out)}
.cw .cw-more>summary::-webkit-details-marker{display:none}
.cw .cw-more>summary::marker{content:""}
.cw .cw-more>summary:hover{background:var(--brand-50);color:var(--brand-blue)}
.cw .cw-more-chev{display:inline-grid;transition:transform var(--dur, 140ms) var(--ease, ease-out)}
.cw .cw-more[open] .cw-more-chev{transform:rotate(90deg)}
.cw .cw-more-count{display:inline-grid;place-items:center;min-width:20px;height:20px;padding:0 6px;border-radius:999px;background:var(--surface);border:1px solid var(--line2, var(--line));color:var(--ink2, var(--ink));font-size:12px;font-weight:600;font-variant-numeric:tabular-nums}
.cw .cw-more>.cw-p{margin:var(--s2, 8px) 0 var(--s4, 16px);color:var(--muted)}

/* step 2: a calm frame around Guided setup (its own markup is not restyled here) */
.cw .cw-lead{margin:var(--s1, 4px) 0 0;max-width:760px}
.cw .cw-guided{margin-top:var(--s4, 16px)}
.cw .cw-meter{margin-top:var(--s4, 16px)}
.cw .cw-notice .attn{padding:20px var(--s5, 24px);border-radius:var(--r-lg, 12px)}
.cw .cw-notice .attn-text{max-width:760px}

/* a problem inside a card: the tone's icon and its words, in the tone's tint */
.cw .cw-problem{display:flex;gap:var(--s3, 12px);align-items:flex-start;margin:var(--s4, 16px) 0 0;padding:var(--s3, 12px) var(--s4, 16px);border:1px solid var(--line);border-radius:var(--r, 8px);background:var(--soft)}
.cw .cw-problem-icon{flex:none;display:inline-grid;margin-top:2px}
.cw .cw-problem-body{flex:1;min-width:0}
.cw .cw-problem-body>.cw-p:first-child{margin-top:0}
.cw .cw-problem-body>.cw-p:last-child{margin-bottom:0}
.cw .cw-problem.tone-warning{background:var(--warning-t);border-color:var(--line);border-color:color-mix(in srgb, var(--warning) 30%, var(--warning-t))}
.cw .cw-problem.tone-warning .cw-problem-icon{color:var(--warning)}
.cw .cw-problem.tone-critical{background:var(--critical-t);border-color:var(--line);border-color:color-mix(in srgb, var(--critical) 25%, var(--critical-t))}
.cw .cw-problem.tone-critical .cw-problem-icon{color:var(--critical)}
.cw .cw-problem.tone-info{background:var(--brand-50);border-color:var(--brand-200)}
.cw .cw-problem.tone-info .cw-problem-icon{color:var(--brand-blue)}

/* steps 3 and 4: the summary tiles, the small tables, the details and the decision bar */
.cw .stat-row[data-cw-summary]{grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.cw .stat-row[data-cw-summary="value"]{grid-template-columns:minmax(0,1fr) minmax(0,1fr) minmax(0,2fr)}
.cw [data-cw-tile="value"] .vrange{margin-top:var(--s2, 8px)}
.cw [data-cw-tile="value"] .vr-text{flex:1 1 220px}
/* a range with no central estimate (the wizard's are all such) has no dot to carry it: the bar itself is brand blue */
.cw .vrange:not(:has(.vr-dot)) .vr-bar{background:var(--brand-blue)}
.cw .cw-breakdowns{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:var(--s5, 24px) var(--s6, 32px);margin-top:var(--s4, 16px)}
.cw .cw-breakdown{min-width:0}
.cw .cw-breakdown>.cw-note{margin:2px 0 0}
.cw .cw-tbl{width:100%;margin-top:var(--s2, 8px);border-collapse:collapse}
.cw .cw-tbl th,.cw .cw-tbl td{padding-left:0;padding-right:var(--s4, 16px)}
.cw .cw-tbl th:last-child,.cw .cw-tbl td:last-child{padding-right:0}
.cw .cw-tbl th.num,.cw .cw-tbl td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.cw .cw-pri-name{display:block;font-weight:500;color:var(--ink)}
.cw .cw-num{display:inline-flex;align-items:center;justify-content:flex-end;gap:var(--s3, 12px)}
.cw .cw-bar{display:inline-block;width:72px;height:6px;border-radius:3px;background:var(--neutral-t);overflow:hidden}
.cw .cw-bar>span{display:block;height:100%;border-radius:3px;background:var(--brand-blue)}
.cw .cw-kvs{margin:var(--s2, 8px) 0 0}
.cw .cw-kv{display:grid;grid-template-columns:minmax(140px,200px) minmax(0,1fr);gap:var(--s3, 12px);padding:var(--s3, 12px) 0;border-top:1px solid var(--line);font-size:var(--fs-body, 14px);line-height:var(--lh-body, 22px)}
.cw .cw-kv:first-child{border-top:0}
.cw .cw-kv dt{color:var(--muted)}
.cw .cw-kv dd{margin:0;color:var(--ink);display:flex;flex-wrap:wrap;align-items:center;gap:var(--s2, 8px)}
.cw .cw-decide{margin:var(--s5, 24px) calc(-1 * var(--s5, 24px)) calc(-1 * var(--s5, 24px));padding:20px var(--s5, 24px) var(--s5, 24px);border-top:1px solid var(--line);background:var(--soft);border-radius:0 0 var(--r-lg, 12px) var(--r-lg, 12px)}
.cw .cw-decide .cw-actions{margin-top:0}
.cw .cw-decide .cw-note{margin:var(--s3, 12px) 0 0;max-width:760px}

/* the Review page's short strip, and the Data | Model | Output tabs under the approval */
.cw .cw-sum-lead{font-size:20px;line-height:28px;font-weight:600;color:var(--ink);margin:var(--s2, 8px) 0}
.cw .cw-sum-h{margin-top:var(--s5, 24px);font-size:16px}
.cw .cw-sum-scroll{overflow-x:auto}
.cw .cw-th-short{display:none}
.cw .cw-sum-sub{font-weight:600;margin:var(--s3, 12px) 0 var(--s1, 4px)}
.cw .cw-more{margin:var(--s2, 8px) 0}
.cw .cw-more>summary{cursor:pointer;color:var(--brand-blue);font-weight:600;font-size:var(--fs-body, 14px)}
.cw .cw-more-body{margin-top:var(--s2, 8px)}
.cw .cw-tabs{margin-top:var(--s2, 8px)}
.cw .cw-tabs [role="tabpanel"]{display:flex;flex-direction:column;gap:var(--s4, 16px);padding-top:var(--s4, 16px)}
.cw .cw-tabs [role="tabpanel"][hidden]{display:none}
.cw .cw-tabs .stack{display:flex;flex-direction:column;gap:var(--s4, 16px)}
.cw .cw-tabs .cw-sum-h{margin:var(--s2, 8px) 0 0}

/* step 5: the success state - a check, the title, the campaign's status, one sentence, the actions */
.cw .cw-done-body{display:flex;flex-direction:column;align-items:center;text-align:center;max-width:600px;margin:0 auto;padding:40px var(--s5, 24px)}
.cw .cw-done-icon{display:inline-grid;place-items:center;width:56px;height:56px;border-radius:50%;background:var(--good-t);color:var(--good);margin-bottom:var(--s4, 16px)}
.cw .cw-done-icon svg{stroke-width:2.25}
.cw .cw-done-title{font-size:20px;line-height:28px}
.cw .cw-done-chip{margin:var(--s3, 12px) 0 0}
.cw .cw-done-text{margin:var(--s3, 12px) 0 0}
.cw .cw-done-actions{justify-content:center;margin-top:var(--s5, 24px)}

@media (max-width:900px){
  .cw .stat-row[data-cw-summary="value"]{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}
  .cw .stat-row[data-cw-summary="value"] [data-cw-tile="value"]{grid-column:1 / -1}
}
@media (max-width:700px){
  .cw .cw-sum-scroll .cw-tbl th,.cw .cw-sum-scroll .cw-tbl td{padding:8px 6px;font-size:12px;line-height:16px}
  .cw .cw-th-long{display:none}
  .cw .cw-th-short{display:inline}
  .cw .cw-sum-scroll .cw-tbl th.num,.cw .cw-sum-scroll .cw-tbl td.num{white-space:normal}
  .cw .stepper{margin-bottom:var(--s5, 24px)}
  .cw .cw-card-body{padding:var(--s4, 16px)}
  .cw .cw-goals{grid-template-columns:1fr;gap:var(--s3, 12px)}
  .cw .cw-goal{display:grid;grid-template-columns:40px minmax(0,1fr);column-gap:var(--s3, 12px);row-gap:var(--s1, 4px);align-items:center;min-height:0;padding:var(--s4, 16px)}
  .cw .cw-goal-icon{grid-row:1 / span 2;align-self:start}
  .cw .cw-goal-name,.cw .cw-goal-stage{grid-column:2}
  .cw .cw-goal-stage{margin-top:0}
  .cw .cw-goal-go{display:none}
  .cw .cw-goal:hover{transform:none}
  .cw .cw-breakdowns{grid-template-columns:minmax(0,1fr)}
  .cw .cw-bar{width:48px}
  .cw .cw-kv{grid-template-columns:minmax(0,1fr);gap:2px}
  .cw .cw-decide{margin:var(--s4, 16px) calc(-1 * var(--s4, 16px)) calc(-1 * var(--s4, 16px));padding:var(--s4, 16px)}
  .cw .cw-actions{flex-direction:column;align-items:stretch}
  .cw .cw-actions .btn,.cw .cw-notice .attn-action .btn{width:100%;justify-content:center;height:auto;min-height:44px;padding-top:10px;padding-bottom:10px;white-space:normal;text-align:center}
  .cw .cw-notice .attn{padding:var(--s4, 16px)}
  .cw .cw-notice .attn-action{grid-column:1 / -1}
  .cw .cw-done-body{padding:var(--s6, 32px) var(--s4, 16px) var(--s4, 16px)}
  .cw .cw-done-actions{align-self:stretch}
}
/* a phone or any coarse pointer: every action of the wizard's own is a 40px target (Guided setup keeps its own) */
@media (max-width:700px),(pointer:coarse){
  .cw .btn:not([data-cw-guided] .btn),.cw .cw-tech .btn{min-height:40px}
  .cw .btn.sm:not([data-cw-guided] .btn){height:auto;padding-top:var(--s2, 8px);padding-bottom:var(--s2, 8px)}
  .cw .cw-tech summary,.cw .tech summary{min-height:40px;display:flex;align-items:center}
}
@media (prefers-reduced-motion: reduce){
  .cw .cw-goal,.cw .cw-goal-go,.cw .cw-goal-icon,.cw .cw-more-chev,.cw .cw-more>summary{transition:none}
  .cw .cw-goal:hover{transform:none}
}
`;

/** Add the wizard's style sheet to the page once. */
export function injectWizardStyles(doc = document) {
  if (!doc || !doc.head || doc.getElementById("cw-styles")) return;
  const style = doc.createElement("style");
  style.id = "cw-styles";
  style.textContent = CSS;
  doc.head.appendChild(style);
}
