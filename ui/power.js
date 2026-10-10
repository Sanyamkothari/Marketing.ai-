// "Is the held-back group big enough?" - the control-group size card on a scoring run's Output page.
//
// The maths is the browser's copy of `engine/uplift/power.py` (read its docstring for the formula and
// the approximations): the same two-sided pooled two-proportion z-test that
// `engine/uplift/incrementality.py` runs after the campaign, sized before it. Both are checked against
// the same cases (`tests/fixtures/power_cases.json`, `tests/unit/uplift/test_power.py`).
//
// Pure strings and numbers in, strings out, apart from `bindPower`, so node can import it.
// Never fabricated: a figure the run did not write is an em dash, and the card says why.

import { EM_DASH, esc, fmtInt, fmtNum, present } from "./dom.js";

export const DEFAULT_ALPHA = 0.05;
export const DEFAULT_POWER = 0.8;
/** `actions.control_group_fraction`'s upper bound (`engine.config.ActionsConfig`). */
export const MAX_CONTROL_FRACTION = 0.5;

// --- The normal distribution --------------------------------------------------------------------

/** Standard normal CDF to about 1e-15 (Hart's double-precision algorithm, as in West 2005). */
export function normalCdf(x) {
  const a = Math.abs(x);
  let tail;
  if (a > 37) {
    tail = 0;
  } else {
    const e = Math.exp((-a * a) / 2);
    if (a < 7.07106781186547) {
      let b = 0.0352624965998911 * a + 0.700383064443688;
      b = b * a + 6.37396220353165;
      b = b * a + 33.912866078383;
      b = b * a + 112.079291497871;
      b = b * a + 221.213596169931;
      b = b * a + 220.206867912376;
      let d = 0.0883883476483184 * a + 1.75566716318264;
      d = d * a + 16.064177579207;
      d = d * a + 86.7807322029461;
      d = d * a + 296.564248779674;
      d = d * a + 637.333633378831;
      d = d * a + 793.826512519948;
      d = d * a + 440.413735824752;
      tail = (e * b) / d;
    } else {
      let b = a + 0.65;
      b = a + 4 / b;
      b = a + 3 / b;
      b = a + 2 / b;
      b = a + 1 / b;
      tail = e / b / 2.506628274631;
    }
  }
  return x > 0 ? 1 - tail : tail;
}

/** The standard normal quantile, by bisection on `normalCdf` (the CDF is monotone, so this cannot fail). */
export function normalQuantile(p) {
  if (!(p > 0 && p < 1)) throw new RangeError("A quantile needs 0 < p < 1.");
  let low = -40;
  let high = 40;
  for (let i = 0; i < 200; i += 1) {
    const middle = (low + high) / 2;
    if (normalCdf(middle) < p) low = middle;
    else high = middle;
  }
  return (low + high) / 2;
}

// --- The power maths (mirrors engine/uplift/power.py) ---------------------------------------------

/** `[control, treated]` rows as the engine draws them: `round_half_up(n * f)` held out, the rest treated. */
export function armRows(n, fraction) {
  if (!(n > 0) || !(fraction > 0)) return [0, Math.max(n, 0)];
  const control = Math.min(n, Math.floor(n * fraction + 0.5));
  return [control, n - control];
}

/** The chance that the two-sided pooled z-test finds `p0` to `p0 + lift` with arms of these sizes. */
export function powerOfLift(nTreated, nControl, p0, lift, alpha = DEFAULT_ALPHA) {
  if (!(nTreated > 0) || !(nControl > 0)) return 0;
  const p1 = p0 + lift;
  const zAlpha = normalQuantile(1 - alpha / 2);
  const pooled = (nTreated * p1 + nControl * p0) / (nTreated + nControl);
  const seNull = Math.sqrt(pooled * (1 - pooled) * (1 / nTreated + 1 / nControl));
  const seAlt = Math.sqrt((p0 * (1 - p0)) / nControl + (p1 * (1 - p1)) / nTreated);
  const threshold = zAlpha * seNull;
  const shift = Math.abs(lift);
  if (seAlt <= 0) return shift > threshold ? 1 : 0;
  return normalCdf((shift - threshold) / seAlt) + normalCdf((-shift - threshold) / seAlt);
}

/**
 * The smallest lift a holdout of `fraction` of `n` eligible customers detects: `{absolute, relative,
 * controlRows, treatedRows}`, or null when it cannot be said (an empty arm, no room to rise, or even a
 * jump to 100% is not detected). `relative` is null when the baseline is 0.
 */
export function minDetectableLift(n, fraction, p0, alpha = DEFAULT_ALPHA, power = DEFAULT_POWER) {
  const [control, treated] = armRows(n, fraction);
  const room = 1 - p0;
  if (room <= 0 || control <= 0 || treated <= 0) return null;
  if (powerOfLift(treated, control, p0, room, alpha) < power) return null;
  let low = 0;
  let high = room;
  for (let i = 0; i < 80; i += 1) {
    const middle = (low + high) / 2;
    if (powerOfLift(treated, control, p0, middle, alpha) >= power) high = middle;
    else low = middle;
  }
  return { absolute: high, relative: p0 > 0 ? high / p0 : null, controlRows: control, treatedRows: treated };
}

/**
 * The smallest control group that detects the absolute `lift`: `{fraction, controlRows, treatedRows}`,
 * or null for "not possible even at the maximum fraction".
 */
export function minControlFraction(n, p0, lift, alpha = DEFAULT_ALPHA, power = DEFAULT_POWER) {
  if (!(lift > 0) || p0 + lift > 1) return null;
  const largest = Math.floor(n * MAX_CONTROL_FRACTION);
  if (largest < 1 || n - largest < 1) return null;
  const enough = (control) => powerOfLift(n - control, control, p0, lift, alpha) >= power;
  if (!enough(largest)) return null;
  let low = 1;
  let high = largest;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (enough(middle)) high = middle;
    else low = middle + 1;
  }
  return { fraction: low / n, controlRows: low, treatedRows: n - low };
}

// --- What the card needs from a scoring run -------------------------------------------------------

/**
 * Everything the card reads, from `scoring_summary.json` (and the use case's problem type): eligible
 * customers, control rows, and the baseline rate with where it came from. Any piece the run did not
 * write is null. The baseline is the run's `score_mean`, the model's average predicted chance over
 * everyone scored, and only when the score is a probability (a yes/no model); a regression score is
 * not a rate, so the card says so instead of using it. Training's positive rate lives in another
 * run's `evaluation.json`, which a scoring run does not carry.
 */
export function powerInputs(uc, summary) {
  const s = summary || {};
  const scored = Number(s.rows_scored);
  const suppressed = (s.suppressed || []).reduce((sum, row) => sum + (Number(row.rows) || 0), 0);
  const eligible = present(s.rows_scored) && Number.isFinite(scored) ? scored - suppressed : null;
  const held = present(s.control_group_rows) ? Number(s.control_group_rows) : null;
  const binary = !uc || !uc.problem_type || uc.problem_type === "binary_classification";
  const mean = present(s.score_mean) ? Number(s.score_mean) : null;
  const baseline = binary && mean !== null && mean > 0 && mean < 1 ? mean : null;
  return {
    eligible: eligible !== null && eligible > 0 ? eligible : null,
    heldRows: held,
    baseline,
    baselineSource: "the model's average predicted chance across everyone scored",
  };
}

/** A percentage with at most one decimal and no trailing ".0": 10, 2.5, 12.3. */
const pctText = (fraction) => `${fmtNum(Math.round(fraction * 1000) / 10, 1).replace(/\.0$/, "")}%`;
const pointsText = (absolute) => `${fmtNum(absolute * 100, 1)} points`;

/** The sentence under the card's title, from `powerInputs`; a dash line with the reason when it cannot be said. */
export function headlineText(inputs) {
  const { eligible, heldRows, baseline } = inputs;
  if (eligible === null || heldRows === null) {
    return { text: `${EM_DASH} The run did not record how many customers were eligible or held back.`, ok: false };
  }
  if (heldRows <= 0) {
    return {
      text: "Nobody was held back in this run, so the campaign's effect cannot be measured. Set a control group to measure it.",
      ok: false,
    };
  }
  if (baseline === null) {
    return {
      text: `${EM_DASH} The run has no usable baseline rate (the score is not a yes/no chance), so the smallest detectable lift cannot be worked out.`,
      ok: false,
    };
  }
  const fraction = heldRows / eligible;
  const found = minDetectableLift(eligible, fraction, baseline);
  const lead = `With ${fmtInt(eligible)} eligible customers and a ${pctText(fraction)} control group`;
  if (!found) {
    return { text: `${lead}, this campaign cannot reliably detect any lift: the held-back group is too small.`, ok: false };
  }
  const relative = found.relative === null ? "" : ` (${fmtNum(found.relative * 100, 0)}% relative)`;
  return {
    text: `${lead}, this campaign can reliably detect a lift of ${pointsText(found.absolute)}${relative} or more.`,
    ok: true,
  };
}

/** What a typed control percentage and/or target lift (in points) answer; strings, never throws. */
export function tryText(inputs, controlPercent, targetPoints) {
  const { eligible, baseline } = inputs;
  if (eligible === null || baseline === null) return "";
  const lines = [];
  const control = String(controlPercent ?? "").trim();
  const target = String(targetPoints ?? "").trim();
  if (control !== "") {
    const percent = Number(control);
    if (!(percent > 0 && percent <= MAX_CONTROL_FRACTION * 100)) {
      lines.push("Enter a control group between 0% and 50%.");
    } else {
      const found = minDetectableLift(eligible, percent / 100, baseline);
      lines.push(
        found
          ? `At ${pctText(percent / 100)} held back (${fmtInt(found.controlRows)} customers), a lift of ${pointsText(found.absolute)}${
              found.relative === null ? "" : ` (${fmtNum(found.relative * 100, 0)}% relative)`
            } or more can be detected.`
          : `At ${pctText(percent / 100)} held back, no lift can be reliably detected: the group is too small.`,
      );
    }
  }
  if (target !== "") {
    const points = Number(target);
    if (!(points > 0)) {
      lines.push("Enter a lift greater than 0 points.");
    } else {
      const sizing = minControlFraction(eligible, baseline, points / 100);
      lines.push(
        sizing
          ? `To detect a lift of ${fmtNum(points, 1)} points, hold back at least ${pctText(Math.ceil(sizing.fraction * 1000) / 1000)} (${fmtInt(
              sizing.controlRows,
            )} customers).`
          : `A lift of ${fmtNum(points, 1)} points cannot be reliably detected, even holding back the maximum 50%.`,
      );
    }
  }
  return lines.join(" ");
}

/** The card, as HTML. `titleHtml` wraps it in the page's own card helper, so this stays free of pages.js. */
export function powerCardBody(uc, summary) {
  const inputs = powerInputs(uc, summary);
  const head = headlineText(inputs);
  const canTry = head.ok && inputs.eligible !== null && inputs.baseline !== null;
  const controlDefault = inputs.heldRows !== null && inputs.eligible ? Math.round((inputs.heldRows / inputs.eligible) * 1000) / 10 : "";
  const basis = inputs.baseline === null
    ? ""
    : ` It assumes customers who are not contacted would convert at ${fmtNum(inputs.baseline * 100, 1)}%, ${inputs.baselineSource}. The real rate may differ, so treat this as a guide.`;
  const tryIt = canTry
    ? `<div class="frow" data-power-try>
        <div class="field sm"><label class="flabel" for="power-control">Try a different control group (%)</label>
          <div class="control"><input id="power-control" type="number" min="0.1" max="50" step="0.5" inputmode="decimal" value="${esc(String(controlDefault))}" data-power-control></div></div>
        <div class="field sm"><label class="flabel" for="power-lift">Or: the lift you want to detect (points)</label>
          <div class="control"><input id="power-lift" type="number" min="0.1" step="0.5" inputmode="decimal" placeholder="for example 2" data-power-lift></div></div>
      </div>
      <p class="fhint" data-power-answer role="status" aria-live="polite"></p>`
    : "";
  return `<div class="card-body" data-power-card${canTry ? ` data-eligible="${esc(String(inputs.eligible))}" data-baseline="${esc(String(inputs.baseline))}" data-held="${esc(String(inputs.heldRows))}"` : ""}>
      <p class="pg-p" data-power-headline>${esc(head.text)}</p>
      ${head.ok ? `<p class="caption">${esc(`The detectable effect is the smallest lift the after-campaign comparison would call real (95% confidence) at least 8 times in 10.${basis}`)}</p>` : ""}
      ${tryIt}
    </div>`;
}

/** Wire the two inputs: every keystroke recomputes the answer line from the card's own data attributes. */
export function bindPower(root) {
  root.querySelectorAll("[data-power-card]").forEach((cardEl) => {
    const control = cardEl.querySelector("[data-power-control]");
    const lift = cardEl.querySelector("[data-power-lift]");
    const answer = cardEl.querySelector("[data-power-answer]");
    if (!control || !lift || !answer) return;
    const inputs = {
      eligible: Number(cardEl.dataset.eligible),
      heldRows: Number(cardEl.dataset.held),
      baseline: Number(cardEl.dataset.baseline),
    };
    const update = () => {
      const typedControl = control.value;
      // Untouched control box and no target: the headline above already answers it.
      answer.textContent = tryText(inputs, typedControl, lift.value);
    };
    control.addEventListener("input", update);
    lift.addEventListener("input", update);
  });
}
