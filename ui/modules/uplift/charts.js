// The three uplift charts, as inline SVG strings (plan §9.3: no chart library, the prototype's look).
//
// Each function reads one artefact and nothing else, and each returns the prototype's empty-card
// sentence when that artefact is missing - never a curve drawn from made-up points. Colours are the
// custom properties `ui/index.html` defines (`--c` is the use case's type colour, `--bad` the
// sleeping-dog red), so both themes work with no chart-specific palette.
//
// The line and bar charts use a `viewBox` and scale as a picture: they carry axis labels, and a
// chart whose text reflowed with the card width would need a layout engine this UI does not have.
// The segment bars use percentage widths instead, like `ui/dom.js`'s `barTrack`, so their rounded
// ends stay round at any width. Scaled down to a phone the 11-unit tick labels would print at about
// 5px, so `styles.js` enlarges `.tk` (and the axis title, `.tt`) in user units below 560px, and the
// axis titles are short enough to fit at that size. Counts use `dom.js`'s one number locale.

import { EM_DASH, esc, fmtInt, fmtNum, present } from "../../dom.js";
import { fmtPts, fmtRate } from "./format.js";

const W = 640;
const H = 280;
const PAD = { left: 64, right: 16, top: 16, bottom: 60 };

const empty = (text) => `<div class="empty">${esc(text)}</div>`;

/** Up to `count` round tick values covering [min, max] - the usual 1-2-5 steps. */
export function niceTicks(min, max, count = 5) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [];
  if (min === max) {
    const pad = Math.abs(min) || 1;
    return niceTicks(min - pad, max + pad, count);
  }
  const raw = (max - min) / Math.max(1, count - 1);
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) || 10 * magnitude;
  const start = Math.floor(min / step) * step;
  const ticks = [];
  for (let v = start; v <= max + step * 1e-9; v += step) ticks.push(Number(v.toFixed(12)));
  if (ticks[ticks.length - 1] < max) ticks.push(Number((ticks[ticks.length - 1] + step).toFixed(12)));
  return ticks;
}

const xOf = (fraction) => PAD.left + fraction * (W - PAD.left - PAD.right);

function yScale(lo, hi) {
  const span = hi - lo || 1;
  return (v) => PAD.top + (1 - (v - lo) / span) * (H - PAD.top - PAD.bottom);
}

const pt = (x, y) => `${x.toFixed(1)},${y.toFixed(1)}`;

/**
 * `qini_curve.json` -> the Qini chart: the model's curve, the random-targeting line, and the area
 * between them shaded - that area is what the Qini coefficient measures. The y axis is incremental
 * conversions as a share of the hold-out, which is what `QiniPoint.qini` is (per customer).
 */
export function qiniChart(curve) {
  const points = ((curve && curve.points) || [])
    .filter((p) => present(p.fraction) && present(p.qini) && present(p.random))
    .slice()
    .sort((a, b) => a.fraction - b.fraction);
  if (points.length < 2) return empty("This run has not produced qini_curve.json yet.");
  const values = points.flatMap((p) => [p.qini, p.random]);
  const ticks = niceTicks(Math.min(0, ...values), Math.max(0, ...values), 5);
  const lo = ticks[0];
  const hi = ticks[ticks.length - 1];
  const y = yScale(lo, hi);
  const model = points.map((p) => pt(xOf(p.fraction), y(p.qini))).join(" ");
  const random = points.map((p) => pt(xOf(p.fraction), y(p.random))).join(" ");
  const area = `${model} ${points
    .slice()
    .reverse()
    .map((p) => pt(xOf(p.fraction), y(p.random)))
    .join(" ")}`;
  const places = Math.max(0, Math.min(3, 1 - Math.floor(Math.log10((hi - lo) * 100 || 1))));
  const yTicks = ticks
    .map(
      (v) =>
        `<line class="grid" x1="${PAD.left}" x2="${W - PAD.right}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(
          1,
        )}"/><text class="tk" x="${PAD.left - 8}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${esc(
          `${fmtNum(v * 100, places)}%`,
        )}</text>`,
    )
    .join("");
  const xTicks = [0, 0.2, 0.4, 0.6, 0.8, 1]
    .map(
      (f) =>
        `<text class="tk" x="${xOf(f).toFixed(1)}" y="${H - PAD.bottom + 28}" text-anchor="middle">${fmtNum(
          f * 100,
          0,
        )}%</text>`,
    )
    .join("");
  const last = points[points.length - 1];
  const label = `Qini curve over ${points.length} points: gain from targeting by the model vs at random; contacting everyone gives ${fmtNum(
    last.qini * 100,
    1,
  )}% extra responses`;
  return `<div class="uchart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">
    ${yTicks}
    <line class="ax" x1="${PAD.left}" x2="${W - PAD.right}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}"/>
    <polygon class="area" points="${area}"/>
    <polyline class="rand" points="${random}"/>
    <polyline class="model" points="${model}"/>
    ${xTicks}
    <text class="tk tt" x="${(PAD.left + W - PAD.right) / 2}" y="${H - 4}" text-anchor="middle">Customers contacted, best first</text>
  </svg></div><div class="ulegend"><span><i></i>Picked by the model</span><span><i class="r"></i>Picked at random</span><span>Up: extra responses, as a share of the test customers</span></div>`;
}

/**
 * `uplift_evaluation.json`'s decile table -> observed uplift per decile as bars around a zero line
 * (a decile can be negative: that is where the sleeping dogs are), with the model's mean predicted
 * uplift as a dot on each. A decile with an empty arm has no observed uplift and gets no bar.
 */
export function decileChart(deciles) {
  const rows = (deciles || []).slice().sort((a, b) => a.decile - b.decile);
  if (!rows.length) return empty("This run has not produced uplift_evaluation.json yet.");
  const values = rows.flatMap((d) => [d.observed_uplift, d.predicted_uplift]).filter(present);
  const ticks = niceTicks(Math.min(0, ...values), Math.max(0, ...values), 5);
  const lo = ticks[0];
  const hi = ticks[ticks.length - 1];
  const y = yScale(lo, hi);
  const plotW = W - PAD.left - PAD.right;
  const slot = plotW / rows.length;
  const barW = Math.min(40, slot * 0.62);
  const places = Math.max(0, Math.min(2, 1 - Math.floor(Math.log10((hi - lo) * 100 || 1))));
  const grid = ticks
    .map(
      (v) =>
        `<line class="grid" x1="${PAD.left}" x2="${W - PAD.right}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(
          1,
        )}"/><text class="tk" x="${PAD.left - 8}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${esc(
          fmtPts(v, places).replace(" pts", ""),
        )}</text>`,
    )
    .join("");
  const bars = rows
    .map((d, i) => {
      const cx = PAD.left + slot * (i + 0.5);
      const label = `Decile ${d.decile}: measured gain ${fmtPts(d.observed_uplift)}, predicted ${fmtPts(
        d.predicted_uplift,
      )}; contacted ${fmtRate(d.treated_rate)}, not contacted ${fmtRate(d.control_rate)}`;
      let bar = "";
      if (present(d.observed_uplift)) {
        const top = Math.min(y(d.observed_uplift), y(0));
        const height = Math.max(1, Math.abs(y(d.observed_uplift) - y(0)));
        bar = `<rect class="${d.observed_uplift < 0 ? "neg" : "pos"}" x="${(cx - barW / 2).toFixed(
          1,
        )}" y="${top.toFixed(1)}" width="${barW.toFixed(1)}" height="${height.toFixed(1)}" rx="3"/>`;
      } else {
        bar = `<text class="tk" x="${cx.toFixed(1)}" y="${(y(0) - 6).toFixed(1)}" text-anchor="middle">${EM_DASH}</text>`;
      }
      const dot = present(d.predicted_uplift)
        ? `<circle class="pred" cx="${cx.toFixed(1)}" cy="${y(d.predicted_uplift).toFixed(1)}" r="3.5"/>`
        : "";
      return `<g role="img" aria-label="${esc(label)}"><title>${esc(label)}</title>${bar}${dot}<text class="tk" x="${cx.toFixed(
        1,
      )}" y="${H - PAD.bottom + 28}" text-anchor="middle">${esc(String(d.decile))}</text></g>`;
    })
    .join("");
  return `<div class="uchart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Gain in each tenth of customers, best first">
    ${grid}
    <line class="zero" x1="${PAD.left}" x2="${W - PAD.right}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}"/>
    ${bars}
    <text class="tk tt" x="${(PAD.left + W - PAD.right) / 2}" y="${H - 4}" text-anchor="middle">Tenth of customers (1 = top 10%)</text>
  </svg></div><div class="ulegend"><span><i class="sq"></i>Measured gain, contacted vs not (points)</span><span><i class="sq n"></i>Contact made it worse</span><span><i class="dot"></i>Gain the model predicted</span></div>`;
}

/**
 * `segments.json` -> the four-segment chart: one bar per segment, sized by its share of customers,
 * with the count, the mean predicted uplift and the segment's action beside it.
 */
export function segmentChart(report) {
  const segments = (report && report.segments) || [];
  if (!segments.length) return empty("This run has not produced segments.json yet.");
  const rows = segments
    .map((s) => {
      const share = present(s.share_pct) ? Math.max(0, Math.min(100, Number(s.share_pct))) : 0;
      const label = `${s.label}: ${present(s.rows) ? fmtInt(s.rows) : EM_DASH} customers, ${
        present(s.share_pct) ? fmtNum(s.share_pct, 1) : EM_DASH
      }%`;
      return `<div class="useg ${esc(s.segment)}"><div><div class="ul">${esc(s.label)}</div><div class="ua">${esc(
        s.action,
      )}</div></div><svg height="10" role="img" aria-label="${esc(
        label,
      )}"><rect x="0" y="0" width="100%" height="10" rx="5" fill="var(--track)"/><rect x="0" y="0" width="${share.toFixed(
        2,
      )}%" height="10" rx="5" fill="var(--seg)"/></svg><div class="un">${
        present(s.rows) ? esc(fmtInt(s.rows)) : EM_DASH
      }<small>${present(s.share_pct) ? `${esc(fmtNum(s.share_pct, 1))}% of customers` : EM_DASH}</small></div></div>`;
    })
    .join("");
  return `<div class="usegs">${rows}</div>`;
}
