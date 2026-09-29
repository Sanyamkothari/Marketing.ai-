// The Connections page's and the connection picker's stylesheet, injected once (as the agent,
// generative, pilot and uplift modules do) rather than added to `ui/index.html`, a shared file whose
// PLAN-G block holds a `<script>` tag only. Tokens only (docs/ui/FOUNDATION.md), so light, dark and the
// `data-theme` override apply with nothing of their own; classes are prefixed `cn-` / `cp-`.

const CSS = `
.cn .cn-sec{margin-top:28px}
.cn .cn-sec>h2{margin:0 0 4px;font-size:16px;font-weight:600;color:var(--ink)}
.cn .cn-sec>p{margin:0 0 12px;font-size:13px;color:var(--muted)}
.cn-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}
.cn-card{display:flex;flex-direction:column;gap:8px;padding:16px;border:1px solid var(--line);border-radius:10px;background:var(--surface)}
.cn-card h3{margin:0;font-size:14px;font-weight:600;color:var(--ink);overflow-wrap:anywhere}
.cn-card .cn-kind{font-size:12px;color:var(--muted)}
.cn-card .cn-text{margin:0;font-size:13px;color:var(--ink2);line-height:1.45}
.cn-card .cn-note{margin:0;font-size:12px;color:var(--muted);line-height:1.45}
.cn-card .btn-row{margin-top:auto;padding-top:4px}
.cn-badge{align-self:flex-start;max-width:100%;overflow-wrap:anywhere;white-space:normal;line-height:1.35}
.cn-form{display:flex;flex-direction:column;gap:14px;max-width:560px}
.cn-form .field{width:100%}
.cn-form .field>span:first-child{font-size:13px;font-weight:500;color:var(--ink)}
.cn-form .control input,.cn-form .control textarea{width:100%;height:100%;border:0;background:transparent;color:inherit;font:inherit;padding:0 12px;outline:none}
.cn-form .control.area{height:auto}
.cn-form .control textarea{min-height:110px;padding:10px 12px;resize:vertical;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.cn-form .cn-check{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--ink)}
.cn-form .cn-check input{width:16px;height:16px;accent-color:var(--brand-blue)}
.cn-form details.adv .cn-advbody{display:flex;flex-direction:column;gap:14px;padding-top:14px}
.cn-steps{list-style:none;margin:12px 0 0;padding:0;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.cn-steps li{display:grid;grid-template-columns:24px minmax(0,1fr);gap:10px;padding:10px 14px;border-top:1px solid var(--line);font-size:13px}
.cn-steps li:first-child{border-top:0}
.cn-steps .cn-ico{font-weight:700;text-align:center}
.cn-steps .ok .cn-ico{color:var(--ok)} .cn-steps .failed .cn-ico{color:var(--bad)}
.cn-steps .warning .cn-ico{color:var(--warn)} .cn-steps .skipped .cn-ico{color:var(--muted)}
.cn-steps .cn-sl{font-weight:500;color:var(--ink)}
.cn-steps .cn-sm{color:var(--ink2);line-height:1.45}
.cn-steps .cn-fix{margin-top:2px;color:var(--muted);line-height:1.45}
.cn-verdict{margin:16px 0 0;font-size:14px;font-weight:600}
.cn-verdict.ok{color:var(--ok)} .cn-verdict.bad{color:var(--bad)}
.cn-confirm{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-size:13px;color:var(--ink)}
.cp{display:flex;flex-direction:column;gap:10px;margin-top:10px;padding:14px;border:1px solid var(--line);border-radius:10px;background:var(--soft)}
.cp .cp-head{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;font-size:13px;color:var(--ink2)}
.cp .cp-head b{color:var(--ink);overflow-wrap:anywhere}
.cp-list{display:flex;flex-direction:column;border:1px solid var(--line);border-radius:8px;background:var(--surface);max-height:320px;overflow:auto}
.cp-item{display:flex;align-items:center;justify-content:space-between;gap:10px;width:100%;padding:10px 12px;border:0;border-top:1px solid var(--line);background:transparent;color:var(--ink);font:inherit;font-size:13px;text-align:left;cursor:pointer}
.cp-item:first-child{border-top:0}
.cp-item:hover{background:var(--soft)}
.cp-item[disabled]{cursor:default;color:var(--muted)}
.cp-item .cp-name{overflow-wrap:anywhere}
.cp-item .cp-meta{flex:none;font-size:12px;color:var(--muted)}
.cp .cp-note{margin:0;font-size:12px;color:var(--muted);line-height:1.45}
.ai .ai-status{margin:0 0 16px}
.ai .ai-note,.ai .ai-pick{margin:0;font-size:13px;color:var(--ink2);line-height:1.5}
.ai .ai-note a{color:var(--brand-blue);font-weight:500}
.ai .ai-form{max-width:640px}
.ai .ai-providers{margin:0;padding:0;border:0;min-width:0}
.ai .ai-providers legend{padding:0;margin:0 0 8px;font-size:13px;font-weight:500;color:var(--ink)}
.ai .ai-pgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}
.ai .ai-pcard{position:relative;display:flex;flex-direction:column;gap:2px;padding:12px 14px;border:1px solid var(--line2);border-radius:10px;background:var(--surface);cursor:pointer}
.ai .ai-pcard:hover{border-color:var(--ink2)}
.ai .ai-pcard input{position:absolute;opacity:0;width:100%;height:100%;inset:0;margin:0;cursor:pointer}
.ai .ai-pcard:focus-within{outline:2px solid var(--brand-blue);outline-offset:2px}
.ai .ai-pcard.on{border-color:var(--brand-blue);box-shadow:inset 0 0 0 1px var(--brand-blue)}
.ai .ai-pcard.on .ai-pname::after{content:" ✓";color:var(--brand-blue)}
.ai .ai-pname{font-size:14px;font-weight:600;color:var(--ink);overflow-wrap:anywhere}
.ai .ai-psub{font-size:12px;color:var(--muted)}
.ai .ai-fields{display:flex;flex-direction:column;gap:14px}
.ai .field>label{font-size:13px;font-weight:500;color:var(--ink)}
.ai .field.ai-wide{width:100%}
.ai .ai-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px}
.ai .ai-saved{margin:0;font-size:12px;font-weight:500;color:var(--ok)}
.ai .ai-warn{margin:0;font-size:12px;color:var(--warn);line-height:1.45}
.ai .ai-problem{margin:0;font-size:13px;font-weight:500;color:var(--bad)}
.ai .ai-privacy{margin:0;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--soft);font-size:13px;color:var(--ink2);line-height:1.5}
.ai .ai-actions .spacer{flex:1}
.ai .ai-out{margin-top:16px;max-width:640px}
.ai .ai-result{display:flex;flex-direction:column;gap:4px;padding:12px 14px;border:1px solid var(--line);border-radius:8px}
.ai .ai-result.ok{border-color:var(--ok)} .ai .ai-result.bad{border-color:var(--bad)}
.ai .ai-verdict{margin:0;font-size:14px;font-weight:600}
.ai .ai-result.ok .ai-verdict{color:var(--ok)} .ai .ai-result.bad .ai-verdict{color:var(--bad)}
.ai .ai-fix{margin:0;font-size:13px;color:var(--ink2);line-height:1.45}
.ai .ai-result .sub{margin:0;font-size:12px;color:var(--muted)}
.ai .ai-facts{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:4px 16px;margin:0;padding:12px 20px 18px;font-size:13px}
.ai .ai-facts>div{display:contents}
.ai .ai-facts dt{color:var(--muted)} .ai .ai-facts dd{margin:0;color:var(--ink);overflow-wrap:anywhere}
.ai .notice-card{margin-bottom:16px}
.orline .up-src:empty{display:none}
.up-src .cp-none a{color:var(--brand-blue);font-weight:500}
@media (max-width:700px){.cn-grid{grid-template-columns:minmax(0,1fr)}}
`;

let injected = false;

export function injectConnectionStyles() {
  if (injected || typeof document === "undefined") return;
  injected = true;
  const style = document.createElement("style");
  style.id = "cn-styles";
  style.textContent = CSS;
  document.head.appendChild(style);
}
