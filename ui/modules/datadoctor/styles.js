// The Data Doctor screen's layout (Plan O M247, DEC-1817). The look is `ui/theme.css`'s (M185): its cards, buttons,
// chips, form controls and tokens. This adds layout only - the two columns, the spacing on the 8px scale, and the
// 40px touch targets of the rows a person ticks - as the design §13.3 says. Light only (DEC-1122).

const STYLE_ID = "data-doctor-styles";

export const CSS = `
.dds{display:flex;flex-direction:column;gap:var(--s4);min-width:0}
.dds-layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,340px);gap:var(--s5);align-items:start}
.dds-layout>.dds-aside{position:sticky;top:var(--s4)}
@media (max-width:900px){.dds-layout{grid-template-columns:minmax(0,1fr)}.dds-layout>.dds-aside{position:static}}
.dds-head{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:var(--s2) var(--s4);align-items:start;padding:var(--s4) 20px}
.dds-head .dds-title{margin:0;font-size:var(--fs-section);line-height:var(--lh-section);font-weight:600;overflow-wrap:anywhere}
.dds-head .dds-next,.dds-head .dds-reasons{grid-column:1/-1;margin:0}
.dds-head .dds-reasons{padding-left:20px;color:var(--ink2);font-size:var(--fs-small)}
.dds-files{display:flex;gap:var(--s2);align-items:center;margin:var(--s1) 0 0;color:var(--muted);font-size:var(--fs-small);overflow-wrap:anywhere}
.dds-section{display:flex;flex-direction:column;gap:var(--s3)}
.dds-section-t,.dds-group-t{margin:0;font-size:var(--fs-card);line-height:var(--lh-card);font-weight:600}
.dds-card{padding:var(--s4) 20px;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);display:flex;flex-direction:column;gap:var(--s2);min-width:0}
.card .dds-card{border:1px solid var(--line)}
.dds-q.is-open{border-color:var(--brand-200)}
.dds-card-t{margin:0;font-weight:600;overflow-wrap:anywhere}
.dds-card-t.has-icon{display:flex;gap:var(--s2);align-items:flex-start}
.dds-card-t.has-icon>span{flex:1;min-width:0}
.dds-card-t>.ds-ico{margin-top:3px;flex:none}
.dds-tag-row{margin:0}
.dds-todo{margin:0;font-size:var(--fs-body);color:var(--ink)}
.dds-why,.dds-state,.dds-doubt-part{margin:0;font-size:var(--fs-small);line-height:var(--lh-small);color:var(--ink2);overflow-wrap:anywhere}
.dds-state{display:flex;gap:6px;align-items:flex-start}
.dds-state>.ds-ico{margin-top:3px;flex:none}
.dds-label{font-weight:600;color:var(--ink)}
.dds-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:var(--s3)}
ol.dds-list{list-style:decimal;padding-left:var(--s5);gap:var(--s2)}
.dds-card.is-hard{border:2px solid var(--critical);background:var(--critical-t)}
.dds-swap{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--s3)}
.dds-swap-side{display:flex;flex-direction:column;gap:var(--s1);padding:var(--s3);border:1px solid var(--line);border-radius:var(--r-lg);min-width:0}
[data-rv-swap-new]{border-color:var(--brand-blue)}
@media (max-width:600px){.dds-swap{grid-template-columns:minmax(0,1fr)}}
.dds-opts{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:var(--s2) var(--s3)}
.dds-opt{display:flex;flex-direction:column;gap:2px;min-width:0}
.dds-opt .btn{min-height:40px}
.dds-effect{font-size:var(--fs-caption);line-height:var(--lh-caption);color:var(--muted)}
.dds-sentence{margin:0;padding:var(--s3) var(--s4);border-left:3px solid var(--brand-blue);background:var(--brand-50);border-radius:var(--r);color:var(--ink);overflow-wrap:anywhere}
.dds-actions{display:flex;flex-wrap:wrap;gap:var(--s2)}
.dds-actions .btn{min-height:40px}
.dds-items{list-style:none;margin:0;padding:0;display:flex;flex-direction:column}
.dds-items>li{border-top:1px solid var(--line)}
.dds-items>li:first-child{border-top:0}
.dds-tick{display:flex;gap:var(--s3);align-items:flex-start;min-height:40px;padding:var(--s2) 0;cursor:pointer}
.dds-tick input{width:20px;height:20px;margin:2px 0 0;flex:none}
.dds-item-body{display:flex;flex-direction:column;gap:2px;min-width:0;flex:1}
.dds-item-t{overflow-wrap:anywhere}
.dds-item-sub{font-size:var(--fs-caption);line-height:var(--lh-caption);color:var(--muted)}
.dds-standing,.dds-goal{display:flex;gap:var(--s3);align-items:center;justify-content:space-between;min-height:48px;padding:var(--s2) 0;flex-wrap:wrap}
.dds-goal .dds-item-body{flex-direction:column;align-items:flex-start;gap:var(--s1,4px);flex:1 1 16rem;min-width:0}
.dds-standing .btn.sm,.dds-goal .btn{min-height:40px}
.dds-remembered{display:flex;gap:var(--s3);align-items:center;justify-content:space-between;flex-wrap:wrap}
.dds-remembered .btn.sm{min-height:40px}
.dds-head .dds-leftout,.dds-head [data-dds-note]{grid-column:1/-1;margin:0}
.dds-leftout{display:flex;flex-direction:column;gap:var(--s1)}
.dds-leftout .dds-reasons{margin:0;padding-left:20px;color:var(--ink2);font-size:var(--fs-small);overflow-wrap:anywhere}
.dds-group{display:flex;flex-direction:column;gap:var(--s2)}
.dds-approve{padding:var(--s4) 20px;display:flex;flex-direction:column;gap:var(--s3)}
.dds-upload{display:flex;flex-direction:column;gap:var(--s3);align-items:flex-start}
.dds-file{position:absolute;width:1px;height:1px;opacity:0;overflow:hidden}
.dds-file:focus-visible+*,.dds-file-label:focus-within{outline:2px solid var(--brand-blue);outline-offset:2px}
.dds-file-label{min-height:40px;cursor:pointer}
.dds-upload .dds-file:focus+.dds-file-label{outline:2px solid var(--brand-blue)}
.dds-chat{padding:var(--s4);display:flex;flex-direction:column;gap:var(--s3);min-width:0}
.dds-chat .gchat{display:flex;flex-direction:column;gap:var(--s2);max-height:420px;overflow:auto;padding:0}
.dds-chat .gchat:empty{display:none}
.card.dds-chat h3.dds-group-t,.card.dds-chat h4.dds-group-t,.card.dds-approve h3.dds-group-t,.card.dds-approve h4.dds-group-t{padding:0;border:0;background:none;font-size:var(--fs-card)}
.dds-chat-form{display:flex;gap:var(--s2)}
.dds-chat-form .input{flex:1;min-width:0}
.dds-chat-form .btn{min-height:40px}
.dds-nochat{display:flex;flex-direction:column;gap:var(--s1)}
.dds-nochat p{margin:0}
.dds-nochat .btn{min-height:40px}
.card .dds :is(h2,h3,h4,h5),.card.dds-chat :is(h3,h4,h5){padding:0;background:none;border:0}
.dds-opt.is-chosen .btn[disabled]{opacity:1;background:var(--btn);color:var(--btn-ink);border-color:var(--btn)}
.dds-timeline{padding:var(--s3) 20px;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg)}
.dds-timeline>summary{min-height:40px;display:flex;align-items:center;cursor:pointer;font-weight:600}
.dds-events{margin:var(--s2) 0 0;padding:0;list-style:none;display:flex;flex-direction:column;gap:var(--s2)}
.dds-events li{display:grid;grid-template-columns:150px minmax(0,1fr);gap:var(--s3);font-size:var(--fs-small)}
.dds-events time{color:var(--muted);font-variant-numeric:tabular-nums}
.dds-receipt{color:var(--muted)}
.dds-continue{display:flex;flex-direction:column;gap:var(--s2);padding:var(--s4) 20px}
.dds-funnel,.dds-checks{padding:var(--s4) 20px;display:flex;flex-direction:column;gap:var(--s3)}
.dds-funnel-rows,.dds-check-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:var(--s3)}
.dds-funnel-row{display:grid;grid-template-columns:96px minmax(0,1fr) minmax(0,auto);gap:var(--s3);align-items:center;font-size:var(--fs-small)}
.dds-funnel-row:not(:has(.dds-funnel-bar)){grid-template-columns:96px minmax(0,1fr)}
.dds-funnel-label{font-weight:600}
.dds-funnel-bar{position:relative;display:block;height:12px;border-radius:var(--r-pill);background:var(--neutral-t);overflow:hidden}
.dds-funnel-in,.dds-funnel-out{position:absolute;left:0;top:0;bottom:0;border-radius:var(--r-pill)}
.dds-funnel-in{width:100%;background:var(--brand-100)}
.dds-funnel-out{background:var(--brand-blue)}
.dds-funnel-row.is-flag .dds-funnel-out{background:var(--warning)}
.dds-funnel-num{color:var(--ink2);font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.dds-funnel-losses{display:flex;flex-wrap:wrap;gap:var(--s2);align-items:center;font-size:var(--fs-small)}
.dds-funnel-losses .btn{height:auto;white-space:normal;text-align:left;padding-block:var(--s1)}
.dds-funnel-losses .btn,.dds-check .btn,.dds-who .btn,.dds-plan .btn.sm{min-height:40px}
.dds-funnel-losses .btn{height:auto;white-space:normal;text-align:left;max-width:100%}
.dds-check{display:flex;flex-direction:column;align-items:flex-start;gap:var(--s2);padding:var(--s3) var(--s4);border:1px solid var(--line);border-left-width:4px;border-radius:var(--r)}
.dds-check[data-dds-level="stop"],.dds-item[data-dds-level="stop"]{border-left-color:var(--critical)}
.dds-check[data-dds-level="flag"],.dds-item[data-dds-level="flag"]{border-left-color:var(--warning)}
.dds-check[data-dds-level="note"],.dds-item[data-dds-level="note"]{border-left-color:var(--brand-blue)}
.dds-check-lead{font-weight:600;overflow-wrap:anywhere}
.dds-check-cols{color:var(--ink2);font-size:var(--fs-small);overflow-wrap:anywhere}
.dds-more-notes>summary,.dds-later>summary,.dds-sure>summary,.dds-query>summary{min-height:40px;display:flex;align-items:center;cursor:pointer;font-weight:600}
.dds-later,.dds-more-notes{display:flex;flex-direction:column;gap:var(--s2)}
.dds-later>.dds-card{margin-top:var(--s2)}
.dds-plan{display:flex;flex-direction:column;gap:var(--s3)}
.dds-plan-sec .dds-group-t{display:flex;gap:var(--s2);align-items:baseline}
.dds-plan .dds-item{padding-left:var(--s3);border-left:4px solid transparent}
.dds-sure .dds-items{margin-top:var(--s1)}
.dds-sec-check{border-top:1px solid var(--line);padding-top:var(--s3)}
.dds-receipt-box{display:flex;flex-direction:column;gap:var(--s2);margin:var(--s1) 0 var(--s2) 32px;padding:var(--s3);background:var(--bg);border-radius:var(--r)}
.dds-counts{margin:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:var(--s2) var(--s4)}
.dds-counts dt{color:var(--muted);font-size:var(--fs-caption);line-height:var(--lh-caption)}
.dds-counts dd{margin:0;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.dds-samples{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:var(--s3)}
.dds-sample{border-collapse:collapse;font-size:var(--fs-caption);width:100%;table-layout:fixed}
.dds-sample caption{text-align:left;font-weight:600;padding-bottom:var(--s1)}
.dds-sample th,.dds-sample td{border:1px solid var(--line);padding:var(--s1) var(--s2);text-align:left;overflow-wrap:anywhere}
.dds-hidden-cell{color:var(--muted);font-style:italic}
.dds-query pre{margin:var(--s2) 0 0;padding:var(--s3);background:var(--bg);border-radius:var(--r);overflow:auto;max-height:240px;font-size:var(--fs-caption);white-space:pre-wrap;overflow-wrap:anywhere}
.dds-who{display:flex;flex-direction:column;gap:var(--s2);margin-top:var(--s2);padding:var(--s3) var(--s4);border:1px dashed var(--brand-200);border-radius:var(--r)}
.dds-who-message{width:100%;font-family:inherit;font-size:var(--fs-small);resize:vertical}
.dds-parked .dds-item-sub{color:var(--warning)}
.dds-note-list{list-style:disc;padding-left:var(--s5);display:grid;gap:var(--s2)}
.dds-parked{display:flex;gap:var(--s3);align-items:center;justify-content:space-between;flex-wrap:wrap;min-height:48px;padding:var(--s2) 0}
@media (max-width:700px){.dds-head{grid-template-columns:minmax(0,1fr)}.dds-events li{grid-template-columns:minmax(0,1fr)}.dds-standing,.dds-goal{align-items:flex-start}.dds-chat-form{flex-direction:column}}

.ddc{display:flex;flex-direction:column;gap:var(--s4);min-width:0}
.dds-layout:has(.ddc){grid-template-columns:minmax(0,1fr)}
.dds-layout:has(.ddc)>.dds-aside,.setup-grid:has(.ddc)>.setup-side{display:none}
.setup-grid:has(.ddc){grid-template-columns:minmax(0,1fr)}
.ddc-head{display:flex;flex-wrap:wrap;gap:var(--s2) var(--s4);align-items:center}
.ddc-head .dds-title{margin:0;font-size:var(--fs-section);line-height:var(--lh-section);font-weight:600;overflow-wrap:anywhere}
.ddc-head .dds-next,.ddc-head .dds-why{flex-basis:100%;margin:0}
.ddc-layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,400px);gap:var(--s5);align-items:start}
.ddc-conv{display:flex;flex-direction:column;gap:var(--s3);min-width:0}
.ddc-panel{display:flex;flex-direction:column;gap:var(--s3);min-width:0;position:sticky;top:calc(var(--bar-h,56px) + var(--s4));max-height:calc(100vh - var(--bar-h,56px) - 2*var(--s4));overflow:auto}
.ddc-panel>*{flex:none}
.ddc-box{padding:var(--s4);display:flex;flex-direction:column;gap:var(--s3);min-width:0}
.ddc-box .dds-group-t{padding:0;border:0;background:none}
.ddc .dds-opt{align-items:flex-start;max-width:min(100%,26rem)}
.ddc .dds-opt .btn{width:auto;max-width:100%;height:auto;white-space:normal;text-align:left}
.ddc-row{display:flex;gap:var(--s3);align-items:flex-start;min-width:0}
.ddc-user{justify-content:flex-end}
.ddc-avatar{flex:none;width:30px;height:30px;border-radius:var(--r-pill);background:var(--brand-50);color:var(--brand-700);display:flex;align-items:center;justify-content:center;font-size:var(--fs-caption);font-weight:600}
.ddc-stack{display:flex;flex-direction:column;gap:var(--s2);min-width:0;max-width:100%}
.ddc-bubble{background:var(--surface);border:1px solid var(--line);border-radius:4px var(--r-lg) var(--r-lg) var(--r-lg);padding:var(--s3) var(--s4);overflow-wrap:anywhere;min-width:0}
.ddc-user .ddc-bubble{background:var(--brand-blue);border-color:var(--brand-blue);color:var(--btn-ink);border-radius:var(--r-lg) var(--r-lg) 4px var(--r-lg);max-width:80%}
.ddc-indent{margin-left:calc(30px + var(--s3));min-width:0}
.ddc-change{display:flex;gap:var(--s3);align-items:flex-start;padding:var(--s3) var(--s4);background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);flex-wrap:wrap}
.ddc-change>.ds-ico{margin-top:2px;flex:none;color:var(--good)}
.ddc-change.is-pending{border-color:var(--brand-200);background:var(--brand-50)}
.ddc-change.is-pending>.ds-ico,.ddc-change.is-rejected>.ds-ico{color:var(--ink2)}
.ddc-change-body{display:flex;flex-direction:column;gap:2px;flex:1 1 14rem;min-width:0}
.ddc-change .dds-actions{flex:none}
.ddc-goals{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:var(--s3)}
.ddc-goal{align-items:flex-start}
.ddc-goal .btn{align-self:flex-start}
.ddc-fit.is-not{border-color:var(--warning)}
.ddc-suggestion{display:flex;flex-direction:column;gap:var(--s2);align-items:flex-start}
.ddc-trace{font-size:var(--fs-small);color:var(--ink2)}
.ddc-trace>summary{cursor:pointer;min-height:32px;display:flex;align-items:center}
.ddc-trace ul{margin:0;padding-left:var(--s5)}
.ddc-others{font-size:var(--fs-small);color:var(--ink2)}
.ddc-others>summary{cursor:pointer;min-height:32px;display:flex;align-items:center}
.ddc-ready{display:flex;flex-direction:column;gap:var(--s3);padding:var(--s4) var(--s5);background:var(--surface);border:2px solid var(--brand-blue);border-radius:var(--r-lg)}
.ddc-ready.is-blocked{border-color:var(--warning)}
.ddc-ready .dds-group-t{padding:0;border:0;background:none}
.ddc-tick{padding:var(--s2) 0}
.ddc-ask{padding:var(--s2) 0}
.ddc-composer{position:sticky;bottom:0;z-index:1;background:var(--bg);padding:var(--s2) 0 var(--s3);display:flex;flex-direction:column;gap:var(--s2)}
.ddc-composer.is-asking{position:static}
.ddc-prompts{display:flex;flex-wrap:wrap;gap:var(--s2)}
.ddc-prompts .btn{min-height:40px;border:1px solid var(--line);border-radius:var(--r-pill);background:var(--surface)}
.ddc-form{display:flex;gap:var(--s2);align-items:center;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);padding:var(--s2)}
.ddc-form .input{flex:1;min-width:0;border:0;background:transparent;box-shadow:none}
.ddc-form .btn{min-height:40px}
.ddc-checklist{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:var(--s2);font-size:var(--fs-body)}
.ddc-checklist li{display:flex;gap:var(--s2);align-items:center;color:var(--ink2)}
.ddc-checklist li.is-done{color:var(--ink)}
.ddc-checklist li.is-done>.ds-ico{color:var(--good)}
.ddc-table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:var(--r)}
.ddc-table{border-collapse:collapse;width:100%;font-size:var(--fs-caption);font-variant-numeric:tabular-nums;white-space:nowrap}
.ddc-table th,.ddc-table td{border-bottom:1px solid var(--line);padding:var(--s1) var(--s2);text-align:left}
.ddc-table th{background:var(--bg);font-weight:600}
.ddc-table th.is-changed{background:var(--info-t)}
.ddc-mark{font-weight:400;color:var(--brand-700)}
.ddc-panel-toggle{display:none}
.ddc-panel-body{display:flex;flex-direction:column;gap:var(--s3);min-width:0}
.ddc-panel-body>*{flex:none}
.ddc-spin{flex:none;width:14px;height:14px;border:2px solid var(--brand-blue);border-right-color:transparent;border-radius:50%;animation:ds-spin .8s linear infinite}
.ddc-bubble[data-dds-progress]{display:flex;flex-wrap:wrap;gap:var(--s2);align-items:center}
.ddc-progress{position:fixed;left:50%;bottom:var(--s4);transform:translateX(-50%);z-index:50;max-width:min(560px,calc(100vw - 2 * var(--s4)));margin:0;display:flex;gap:var(--s2);align-items:center;padding:var(--s2) var(--s3);background:var(--brand-50);border:1px solid var(--brand-200);border-radius:var(--r-lg);box-shadow:var(--pop-shadow);font-size:var(--fs-small);color:var(--ink)}
.ddc-composer .dds-nochat{font-size:var(--fs-small);line-height:var(--lh-small);color:var(--ink2)}
.ddc-stop{display:flex;flex-direction:column;gap:var(--s1)}
.ddc-detail{font-size:var(--fs-small);color:var(--ink2)}
.ddc-detail>summary{cursor:pointer;min-height:32px;display:flex;align-items:center}
.ddc-detail>p{margin:0 0 var(--s2)}
@media (max-width:900px){.ddc-layout{grid-template-columns:minmax(0,1fr)}.ddc-panel{order:-1;position:static;max-height:none;overflow:visible}.ddc-panel-toggle{display:flex;width:100%;min-height:44px;height:auto;justify-content:flex-start;text-align:left;white-space:normal}.ddc-panel:not(.is-open) .ddc-panel-body{display:none}}
@media (max-width:600px){.ddc-indent{margin-left:0}.ddc-user .ddc-bubble{max-width:100%}.ddc-ready{padding:var(--s4)}.ddc-form{flex-direction:row}.ddc-composer.is-off .ddc-prompts{display:none}}
`;

/** Add the stylesheet once per document. */
export function injectDataDoctorStyles(doc = typeof document === "undefined" ? null : document) {
  if (!doc || doc.getElementById(STYLE_ID)) return;
  const style = doc.createElement("style");
  style.id = STYLE_ID;
  style.textContent = CSS;
  doc.head.appendChild(style);
}
