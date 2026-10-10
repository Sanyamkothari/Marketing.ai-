"""Render a journey's `run_report.md` from its `journey.results.json` (Plan J M110).

Every number in the report is read from the results the journey wrote; this module computes nothing but
formatting and the yes/no wording that follows from a stored flag or interval. A sentence whose truth
depends on a result is chosen by that result, so the report says what happened whichever way it fell.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def _num(value: float | None, digits: int = 4) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


def _int(value: float | None) -> str:
    return "—" if value is None else f"{round(value):,}"


def _pct(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{100 * value:.{digits}f} %"


def _pp(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{100 * value:+.{digits}f} pts"


def _inr(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}₹{abs(value):,.0f}"


def _ci(interval: dict[str, Any] | None, fmt: Any = _num) -> str:
    if not interval:
        return "—"
    low, high = interval.get("ci_low"), interval.get("ci_high")
    if low is None or high is None:
        return f"{fmt(interval.get('value'))} (no interval)"
    return f"{fmt(interval.get('value'))} ({fmt(low)} to {fmt(high)})"


def _sign(interval: dict[str, Any] | None) -> str:
    """What a 95% interval says: above zero, below zero, or includes zero."""
    if not interval or interval.get("ci_low") is None or interval.get("ci_high") is None:
        return "not measured"
    if interval["ci_low"] > 0:
        return "**above zero**"
    if interval["ci_high"] < 0:
        return "**below zero**"
    return "includes zero"


def _check(checks: list[dict[str, Any]], code: str) -> dict[str, Any] | None:
    return next((c for c in checks if c["code"] == code), None)


def _passed(check: dict[str, Any] | None) -> str:
    if check is None or check.get("passed") is None:
        return "not measured"
    return "**passed**" if check["passed"] else "**failed**"


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
def render_report(results: dict[str, Any], *, results_path: str, command: str) -> str:
    """The Markdown report of one journey; `results_path` and `command` are printed for reproduction."""
    steps = results["steps"]
    lines: list[str] = []
    add = lines.append
    risk, uplift, approval = steps["risk_model"], steps["uplift_model"], steps["approval"]["checks"]
    treat, ope, campaigns = steps["treat_list"], steps["off_policy"], steps["campaigns"]
    levels = list(treat["offer_choice"]["levels"]) if treat.get("offer_choice") else []
    beats = _check(approval, "UPLIFT_NOT_BETTER_THAN_RISK")
    stable = _check(approval, "UPLIFT_UNSTABLE_ACROSS_FOLDS")
    calibrated = _check(approval, "UPLIFT_MISCALIBRATED")
    value = results["value"]
    split = results["split"]

    add(f"# {results['dataset']} — the Plan J journey on real randomised data")
    add("")
    add(
        f"**{results['label'].capitalize()}.** Produced only by `library/run_engine.py`; every number below is read"
    )
    add(f"from [`{results_path}`]({results_path}) of the run this report names, which is git-ignored and")
    add(
        "rebuilt by the command at the end. Nothing here is synthetic and nothing was tuned on the evaluation rows."
    )
    add("")
    add("| | |")
    add("|---|---|")
    add(
        f"| Data | `{results['csv']}`, {_int(results['rows'])} rows (SHA-256 `{results['csv_sha256'][:16]}…`; "
        "the raw file it was prepared from is verified by `fetch.py`) |"
    )
    add(f"| Use case | `{results['use_case']}` |")
    add(
        f"| Split | {_int(split['training']['rows'])} training rows, {_int(split['evaluation']['rows'])} evaluation rows "
        f"(seed {split['seed']}, stratified by {' and '.join(split['stratified_by'])}) |"
    )
    add(f"| Risk model run | `{risk['run_id']}`, {risk['wall_clock_seconds']} s |")
    add(f"| Campaign-effect run | `{uplift['run_id']}`, {uplift['wall_clock_seconds']} s |")
    add(f"| Scoring run | `{treat['run_id']}` |")
    risk_scoring = (steps.get("beats_risk_out_of_sample") or {}).get("risk_score_run_id")
    if risk_scoring:
        add(f"| Risk model's scoring run (section 3) | `{risk_scoring}` |")
    add(f"| Whole journey | {results['wall_clock_seconds']} s |")
    add("")

    # -- the answer ------------------------------------------------------------------------------------
    add("## The answer in one table")
    add("")
    add("| Step | What happened |")
    add("|---|---|")
    add(
        f"| Readiness | the uplift checks on the training rows {'passed' if steps['readiness']['training_checks']['passed'] else 'did NOT pass'}, "
        f"on the evaluation rows {'passed' if steps['readiness']['evaluation_checks']['passed'] else 'did NOT pass'} |"
    )
    roc = risk["test_metrics"].get("roc_auc")
    add(
        f"| Risk model (Phase 1, LightGBM) | test ROC-AUC {_num(roc)}; beats its baseline: "
        f"{'yes' if risk.get('beats_baseline') else 'no'} |"
    )
    add(
        f"| Campaign-effect model (M100, {len(levels) - 1} offers) | AUUC of the first offer {_ci(uplift['evaluation'].get('auuc'))}; "
        f"status `{uplift['model_status']}` |"
    )
    overlap = steps["approval"].get("risk_overlap") or {}
    add(
        f"| Beats risk ranking (M96, the engine's check) | {_passed(beats)} (its risk comparator was fitted on "
        f"{_pct(overlap.get('share_fitted_by_risk'), 0)} of the rows it is judged on; see section 3) |"
    )
    oos = steps.get("beats_risk_out_of_sample") or {}
    for level, comparison in (oos.get("offers") or {}).items():
        add(
            f"| Beats risk ranking, both models out of sample ({level}, evaluation rows) | "
            f"{'**passed**' if comparison.get('beats_risk') else '**failed**'}: "
            f"{_baseline_gap(comparison, 'propensity_model')} |"
        )
    add(f"| Stable across folds (M96) | {_passed(stable)} |")
    add(f"| Calibrated by decile (M96) | {_passed(calibrated)} |")
    oc = treat["offer_choice"] or {}
    counts = ", ".join(f"{k}: {_int(v)}" for k, v in treat["offer_counts"].items())
    add(
        f"| Treat list (M97/M100) | {_int(treat['treat_rows'])} of {_int(treat['treat_list_rows'])} customers get an e-mail ({counts}) |"
    )
    chosen = ope["estimates"][results_outcome(results)]["chosen"]["difference_from_no_email"]
    add(
        f"| Off-policy value on the evaluation rows | chosen list against no e-mail, conversion rate {_ci(chosen, _pp)}: {_sign(chosen)} |"
    )
    for name, campaign in campaigns.items():
        report = campaign.get("report") or {}
        if report.get("outcome_kind") == "continuous":
            interval = report.get("adjusted_interval") or report.get("mean_difference_ci")
            add(
                f"| Campaign on `{name}` (replay, CUPED) | adjusted difference per customer {_ci(interval, lambda v: _num(v, 2))}: {_sign(interval)} |"
            )
        else:
            interval = report.get("absolute_lift")
            add(f"| Campaign on `{name}` (replay) | difference {_ci(interval, _pp)}: {_sign(interval)} |")
        proof = campaign["proof"]
        status = (
            f"built, provenance verified: {proof.get('provenance_verified')}; claim: {proof['view']['claim_label']}"
            if proof["status"] == 200
            else f"refused ({proof['status']})"
        )
        add(f"| Value Proof Pack on `{name}` (M104) | {status} |")
    add("")

    # -- what it means -----------------------------------------------------------------------------------
    add("## What this says, plainly")
    add("")
    add(_plain(results))
    add("")

    # -- readiness ---------------------------------------------------------------------------------------
    add("## 1. Readiness and the power sheet")
    add("")
    add("The data, by group (the outcomes as the file records them):")
    add("")
    add("| Part | Group | Rows | Conversions | Conversion rate | Visit rate | Mean spend ($) |")
    add("|---|---|---|---|---|---|---|")
    for part in ("training", "evaluation"):
        for level, group in split[part]["groups"].items():
            add(
                f"| {part} | {level} | {_int(group['rows'])} | {_int(group['conversions'])} | {_pct(group['conversion_rate'])} | "
                f"{_pct(group['visit_rate'])} | {_num(group['mean_spend'], 3)} |"
            )
    add("")
    for part in ("training_checks", "evaluation_checks"):
        found = steps["readiness"][part]
        rows = found["checks"]
        verdict = "passed" if found["passed"] else "did not pass"
        add(
            f"**Uplift checks on the {part.split('_')[0]} rows** ({verdict}; the same function `POST /uplift/runs` runs):"
        )
        add("")
        if not rows:
            add("- no finding")
        for check in rows:
            add(f"- `{check['code']}` ({check['severity']}): {check['message']}")
        add("")
    add(
        "**The power sheet** (`POST /measurement/power-preview`, base rate from the training rows' no-e-mail group):"
    )
    add("")
    for key, title in (
        ("evaluation_rows", "every evaluation row"),
        ("replay_third", "a third of them, as the replay keeps"),
    ):
        sheet = steps["power"][key]
        add(
            f"*{title}* ({_int(sheet['request']['eligible'])} customers, base rate {_pct(sheet['request']['base_rate'])}):"
        )
        add("")
        add(
            "| Control share | Contacted | Held back | Smallest rise it is sure to see | Cost of holding back |"
        )
        add("|---|---|---|---|---|")
        for point in sheet["preview"]["points"]:
            cost = point.get("cost_of_holdout")
            mde = "—" if point["mde_pp"] is None else f"{point['mde_pp']:.2f} pts"
            add(
                f"| {_pct(point['holdout_share'], 0)} | {_int(point['n_treat'])} | {_int(point['n_control'])} | "
                f"{mde} | {'—' if not cost else _inr(cost['low'])} |"
            )
        add("")

    # -- models ------------------------------------------------------------------------------------------
    add("## 2. The models")
    add("")
    add(
        f"**Risk model** (`POST /runs`, Phase 1): {risk['best_model']}, {_int(risk['models_trained'])} models trained, "
        f"{risk['wall_clock_seconds']} s. Test metrics: "
        + ", ".join(f"{k} {_num(v)}" for k, v in sorted(risk["test_metrics"].items()))
        + f". Against its baseline ({risk.get('baseline_name')}): {'beats it' if risk.get('beats_baseline') else 'does not beat it'}."
    )
    add("")
    evaluation = uplift["evaluation"]
    add(
        f"**Campaign-effect model** (`POST /uplift/runs`, {evaluation.get('learner')} on {evaluation.get('base_model')}), "
        f"measured on its own hold-out of {_int(evaluation.get('rows_evaluated'))} training rows. "
        f"Registered as `{uplift['model_status']}`: a model of several offers is never champion (DEC-1310 (h))."
    )
    add("")
    add("| Offer | Hold-out rows | Effect on conversion (95 %) | AUUC (95 %) |")
    add("|---|---|---|---|")
    for arm in evaluation.get("arms") or []:
        add(f"| {arm['arm']} | {_int(arm['rows'])} | {_ci(arm['effect'], _pp)} | {_ci(arm['auuc'])} |")
    add("")
    apv = uplift.get("arm_policy_value")
    if apv:
        add(
            f"`arm_policy_value.json` (the training hold-out, value-weighted): choosing the e-mail per customer against the "
            f"first offer alone, per customer, {_ci(apv['difference'], lambda v: _num(v, 2))} rupees of value before costs — {apv['summary']}"
        )
        add("")

    # -- approval ----------------------------------------------------------------------------------------
    add("## 3. The approval checks (M96)")
    add("")
    add(
        "As the Approver's screen shows them (`engine.model_gates.approval_checks`); advisory, never blocking:"
    )
    add("")
    for check in approval:
        add(f"- `{check['code']}` — {_passed(check)}. {check['message']}")
    add("")
    comparison = (uplift.get("evaluation") or {}).get("baseline_comparison") or {}
    if comparison:
        add(
            f"The engine's comparison on the campaign-effect run's hold-out ({_int(evaluation.get('rows_evaluated'))} training rows, "
            f"first offer against no e-mail, {_int(comparison.get('bootstrap_samples'))} paired resamples); the check is decided "
            f"by `{comparison.get('risk_baseline')}`:"
        )
        add("")
        _baseline_table(add, comparison)
    if overlap:
        add(
            f"**The comparator saw most of those rows.** The risk model was trained on the same {_int(split['training']['rows'])}-row "
            f"training upload, so {_int(overlap.get('holdout_rows_fitted_by_risk'))} of the {_int(overlap.get('uplift_holdout_rows'))} "
            f"hold-out rows ({_pct(overlap.get('share_fitted_by_risk'), 1)}) were in its training or validation part; only "
            f"{_int(overlap.get('holdout_rows_in_risk_test'))} were in its test part"
            + ("" if overlap.get("exact") else " (at least: its explained test rows are a sample)")
            + ". The `propensity_model` row is therefore scored partly in-sample, which may flatter the risk ranking. "
            + _p_control_agrees(comparison)
            + " The comparison below repeats the check where neither model saw a row."
        )
        add("")
    if oos:
        add(
            f"**Beats risk, both models out of sample.** The {_int(oos.get('rows'))} evaluation rows were scored by the risk model too "
            f"(run `{oos.get('risk_score_run_id')}`, column `{oos.get('risk_score_column')}`). For each offer, the rows the file sent that "
            "offer or no e-mail, ranked by the offer's predicted uplift against the risk score and `p_control`, by the same "
            f"function and rule as M96 (`compare_with_baselines`, {_int(oos.get('bootstrap_samples'))} paired resamples, seed {oos.get('seed')}):"
        )
        add("")
        for level, found in (oos.get("offers") or {}).items():
            add(
                f"*{level}* ({_int(found.get('rows'))} rows, {_int(found.get('treated_rows'))} sent it; uplift AUUC "
                f"{_ci(found.get('uplift_auuc'))}): {found.get('summary')}"
            )
            add("")
            _baseline_table(add, found)

    # -- treat list ----------------------------------------------------------------------------------------
    add("## 4. The treat list (M97, M100)")
    add("")
    add(
        f"The {_int(split['evaluation']['rows'])} evaluation rows were scored without their e-mail group or outcomes. "
        f"Each customer's e-mail was chosen by net value: predicted uplift × `{value['formula'].split(' = ')[0]}` − "
        f"₹{results['contact_cost_inr']} per e-mail."
    )
    add("")
    add("| Offer | Eligible | Sleeping dogs for it | Given it | Predicted net value | Cost |")
    add("|---|---|---|---|---|---|")
    for arm in oc.get("arms") or []:
        add(
            f"| {arm['label']} | {_int(arm['eligible_rows'])} | {_int(arm['sleeping_dog_rows'])} | {_int(arm['offered_rows'])} | "
            f"{_inr(arm['net_value_total'])} | {_inr(arm['cost_total'])} |"
        )
    add("")
    reasons = ", ".join(f"{k}: {_int(v)}" for k, v in sorted((oc.get("reasons") or {}).items()))
    add(
        f"Customers the policy meant to contact: {_int(oc.get('policy_intended_rows'))}, of whom "
        f"{_int(oc.get('policy_intended_held_back_rows'))} are in the engine's own random control group. Reasons per row: {reasons}."
    )
    add("")
    ranking = treat.get("ranking_choice")
    if ranking:
        add(
            f"**The ranking choice (J5).** `ranking_choice.json` says `{ranking.get('ranking')}`"
            f"{' (' + ranking['code'] + ')' if ranking.get('code') else ''}: {ranking.get('reason')}"
        )
        add("")
        crossed = treat.get("scores_action_vs_offer") or {}
        if ranking.get("ranking") == "propensity_model" and crossed:
            add(
                "**Finding.** The fallback re-ranks the run's single-offer contact list (the `action` column of the scores) by "
                "the risk model, but the offer chosen per customer (`offer_choice.parquet`, M100 part B), and therefore the treat "
                "list, is still chosen by the campaign-effect model's net value. On a model of several offers the J5 fallback does "
                "not reach the list that goes out. Recorded as an open question for DEC-1306 / DEC-1310; no engine code was changed."
            )
            add("")
            add("| `action` in the scores | Given an e-mail by the offer choice | Not given one |")
            add("|---|---|---|")
            for action, split_counts in crossed.items():
                add(f"| {action} | {_int(split_counts['offered'])} | {_int(split_counts['not_offered'])} |")
            add("")

    # -- off-policy ----------------------------------------------------------------------------------------
    add("## 5. Measured off-policy on the evaluation rows")
    add("")
    add(
        f"{ope['estimator'].capitalize()}. The evaluation rows' own random e-mail is the logged action, with shares "
        + ", ".join(f"{k} {_pct(v)}" for k, v in ope["arm_shares"].items())
        + ". The chosen list gives "
        + ", ".join(f"{k} {_pct(v)}" for k, v in ope["policy_shares"].items())
        + " of the rows."
    )
    add("")
    for outcome, fmt, unit in (
        (results_outcome(results), _pp, "conversion rate"),
        ("visit", _pp, "visit rate"),
        (results_amount(results), lambda v: _num(v, 3), "dollars"),
    ):
        add(f"**{outcome}** ({unit}, per customer, against sending no e-mail):")
        add("")
        booted = any(
            "bootstrap" in row["difference_from_no_email"] for row in ope["estimates"][outcome].values()
        )
        if booted:
            add(
                "| Policy | E-mails | Value | Difference from no e-mail (95 %, normal) | Interval | Bootstrap 95 % (percentile) | Interval |"
            )
            add("|---|---|---|---|---|---|---|")
        else:
            add("| Policy | E-mails | Value | Difference from no e-mail (95 %) | Interval |")
            add("|---|---|---|---|---|")
        for name, row in ope["estimates"][outcome].items():
            diff = row["difference_from_no_email"]
            shown = _pct(row["value"]["value"]) if fmt is _pp else _num(row["value"]["value"], 3)
            line = f"| {name} | {_int(ope['contacts'][name])} | {shown} | {_ci(diff, fmt)} | {_sign(diff) if name != 'no_email' else ''} |"
            if booted:
                b = diff.get("bootstrap") or {}
                line += f" {_range(b, fmt)} | {_sign(b) if name != 'no_email' else ''} |"
            add(line)
        add("")
        add(
            "Chosen list against each e-mail sent to everyone (paired on the same rows): "
            + "; ".join(
                f"{k.replace('chosen_minus_', '')} {_ci(v, fmt)} ({_sign(v)})"
                + (
                    f", bootstrap {_range(v['bootstrap'], fmt)} ({_sign(v['bootstrap'])})"
                    if "bootstrap" in v
                    else ""
                )
                for k, v in ope["chosen_against_everyone"][outcome].items()
            )
            + "."
        )
        add("")
        if booted:
            boot = ope.get("bootstrap") or {}
            add(
                f"The bootstrap: {boot.get('method')}; {_int(boot.get('resamples'))} resamples, seed {boot.get('seed')}."
            )
            add("")
            add(_skew_caveat(campaigns))
            add("")
    add(
        f"**In rupees** (spend difference × {_int(ope['rows'])} customers × ₹{value['fx_inr_per_usd']:g} per dollar, less ₹"
        f"{results['contact_cost_inr']} per e-mail; revenue before margin):"
    )
    add("")
    add("| Policy | Incremental revenue | E-mail cost | Net (95 %, normal) | Net, bootstrap 95 % |")
    add("|---|---|---|---|---|")
    for name, money in ope["money_on_evaluation_rows"].items():
        add(
            f"| {name} | {_inr(money['incremental_revenue_inr'])} | {_inr(money['contact_cost_inr'])} | "
            f"{_inr(money['net_inr'])} ({_inr(money['net_ci_low_inr'])} to {_inr(money['net_ci_high_inr'])}) | "
            f"{_inr(money.get('net_bootstrap_low_inr'))} to {_inr(money.get('net_bootstrap_high_inr'))} |"
        )
    add("")
    if "OUTCOME_SKEWED" in _skew_caveat(campaigns):
        add("The skew caution above applies to these rupee ranges too.")
        add("")

    # -- campaigns -----------------------------------------------------------------------------------------
    add("## 6. Measured as a campaign, and the Value Proof Pack")
    add("")
    add(
        "The treat list was recorded as a campaign (`POST /campaigns`) and its test plan registered before the journey read any "
        "evaluation row's outcome (the evaluation readiness checks, section 3's out-of-sample comparison and section 5 all come "
        "after it); every plan input comes from training rows. Its outcomes were then uploaded by **replay**: a customer keeps their outcome only when the e-mail the file randomly sent them "
        "is the one the list gave them (no e-mail for the engine's control group). The file's e-mail was drawn independently of "
        "the list, so the kept customers are a random third of each arm and the comparison stays randomised; the others are "
        "counted by the engine as customers without an outcome, never as non-converters."
    )
    add("")
    for name, campaign in campaigns.items():
        report = campaign.get("report") or {}
        replay = campaign["replay"]
        add(f"### `{name}` — {campaign['name']}")
        add("")
        add(
            f"Intended {_int(replay['intended'])} (contacted {_int(replay['intended_treated'])}, held back "
            f"{_int(replay['intended_held_back'])}); kept by the replay: contacted {_int(replay['kept_treated'])} ("
            + ", ".join(f"{k} {_int(v)}" for k, v in replay["kept_by_offer"].items())
            + f"), held back {_int(replay['kept_held_back'])}."
        )
        add("")
        add(f"> {report.get('summary')}")
        add("")
        if report.get("outcome_kind") == "continuous":
            add(
                f"Unadjusted difference per customer {_ci(report.get('mean_difference_ci'), lambda v: _num(v, 3))}; adjusted by "
                f"`{report.get('covariate_column')}` {_ci(report.get('adjusted_interval'), lambda v: _num(v, 3))}; variance removed "
                f"{_pct(report.get('variance_reduction'))}; warnings {report.get('outcome_warnings') or 'none'}."
            )
            add("")
        campaign_verdict = campaign.get("verdict") or {}
        add(f"Verdict: **{campaign_verdict.get('headline')}** — {campaign_verdict.get('detail')}")
        add("")
        headline = str(campaign_verdict.get("headline") or "")
        if (
            campaign_verdict.get("kind") in {"added", "prevented", "harmed"}
            and report.get("outcome_column", "") not in headline
        ):
            add(
                f"**Finding.** The verdict names the use case's own outcome definition, not `{report.get('outcome_column')}`, "
                "the column this campaign was measured on: the campaign page and step 4 both label a verdict with "
                "`target.definition` whatever column was named. The number is right; its words are not. Recorded as an "
                "open question; no engine code was changed."
            )
            add("")
        proof = campaign["proof"]
        if proof["status"] == 200:
            view = proof["view"]
            add(
                f"Value Proof Pack: built (HTML {proof['html_status']}, PDF {proof['pdf_status']}), every figure re-verified by "
                f"`verify_provenance`. Claim: *{view['claim_label']}*. Headline: *{view['headline']}*"
            )
            add("")
            add("| Section | Status | Reason when not measured |")
            add("|---|---|---|")
            for section in view["sections"]:
                add(f"| {section['title']} | {section['status']} | {section.get('reason') or ''} |")
        else:
            add(f"Value Proof Pack: refused, {proof['status']}: {proof.get('refusal')}")
        add("")
        inputs = campaign["value_inputs"]
        add(
            f"Value inputs entered for the pack: ₹{inputs['value_per_outcome']:,.2f} per {'conversion' if report.get('outcome_kind') != 'continuous' else 'dollar of spend'} "
            f"({inputs['value_basis']}), ₹{inputs['contact_cost']} per e-mail."
        )
        add("")
    add(_pack_finding(campaigns))
    add("")
    add(
        "**What the pack's money covers.** The replay keeps outcomes for about a third of the contacted customers, and the pack "
        "credits what it measured on those, while it costs the e-mails of every customer meant to be contacted. Its net value "
        "is therefore an understatement of the list's; section 5's off-policy figure uses every evaluation row."
    )
    add("")

    # -- assumptions -------------------------------------------------------------------------------------------
    add("## 7. Assumptions and settings, all of them")
    add("")
    add(f"- **Exchange rate.** {value['fx_note']}")
    add(
        f"- **Value per conversion.** `{value['formula']}`; scale = mean spend of the {_int(value['converters'])} training converters "
        f"(${_num(value['mean_order_usd'], 2)}) over their mean `history` (${_num(value['mean_history_usd'], 2)}) = {_num(value['scale'])}. "
        f"Revenue, before margin. The pack's value per conversion is the same mean order in rupees, ₹{_num(value['value_per_conversion_inr'], 2)}."
    )
    add(
        f"- **E-mail cost.** ₹{results['contact_cost_inr']} per e-mail (`configs/pilot/value.yaml`'s e-mail benchmark), no offer cost."
    )
    add(
        "- **The covariate's date.** The file has no dates. `history` is dated the day before the campaign record's start, by "
        "assumption (the file defines it as the spend of the year before the e-mail), so the engine's point-in-time rule "
        "(`COVARIATE_NOT_BEFORE_CAMPAIGN`) passes by construction: it is not a check here."
    )
    windows = {c.get("outcome_window_days") for c in campaigns.values()}
    add(
        f"- **The outcome window.** The campaigns were recorded with an outcome window of {', '.join(str(w) for w in sorted(windows, key=str))} "
        "days, and each Pack prints that figure, while the file's outcomes cover two weeks. `POST /campaigns` refuses a "
        "treatment start before the scoring run finished and `measure` refuses while a window is open, so a replayed "
        "campaign cannot carry the real window. A limit of the replay; the outcomes themselves are the file's two weeks."
    )
    add(
        "- **Risk model overrides:** "
        + ", ".join(f"`{k}={v}`" for k, v in results["risk_overrides"].items())
        + "."
    )
    add(
        "- **Campaign-effect overrides:** "
        + ", ".join(f"`{k}={v}`" for k, v in results["uplift_overrides"].items())
        + "."
    )
    add(
        "- **Configuration in the use case:** X-learner on LightGBM, persuadable at +0.5 points and sleeping dog at −0.2 points of "
        "conversion (the defaults are set for rates near 20 %), fold stability on, engine control group 50 %."
    )
    add("")
    add("## Reproduce")
    add("")
    add("```bash")
    add(command)
    add("```")
    add("")
    return "\n".join(lines)


def results_outcome(results: dict[str, Any]) -> str:
    return str(next(iter(results["steps"]["campaigns"])))


def results_amount(results: dict[str, Any]) -> str:
    return str(list(results["steps"]["campaigns"])[-1])


def _plain(results: dict[str, Any]) -> str:
    """The paragraph that says what happened, each clause chosen by a stored result."""
    steps = results["steps"]
    checks = steps["approval"]["checks"]
    beats = _check(checks, "UPLIFT_NOT_BETTER_THAN_RISK")
    ope = steps["off_policy"]
    outcome = results_outcome(results)
    chosen = ope["estimates"][outcome]["chosen"]["difference_from_no_email"]
    blankets = ope["chosen_against_everyone"][outcome]
    parts: list[str] = []
    if beats is not None and beats.get("passed") is False:
        parts.append(
            "The campaign-effect model does **not** beat plain risk ranking on its hold-out: knowing who an e-mail moves is not, "
            "on this file, better established than knowing who buys anyway."
        )
    elif beats is not None and beats.get("passed"):
        parts.append("The campaign-effect model beats plain risk ranking on its hold-out.")
    oos = (steps.get("beats_risk_out_of_sample") or {}).get("offers") or {}
    if oos:
        won = [level for level, found in oos.items() if found.get("beats_risk")]
        lost = [level for level, found in oos.items() if not found.get("beats_risk")]
        if lost and not won:
            parts.append(
                "Repeated on the evaluation rows, where neither model saw a row, it does not beat the risk model's ranking "
                "for any of the e-mails."
            )
        elif won and not lost:
            parts.append(
                "Repeated on the evaluation rows, where neither model saw a row, it beats the risk model's ranking for "
                "every e-mail."
            )
        else:
            parts.append(
                "Repeated on the evaluation rows, where neither model saw a row, it beats the risk model's ranking for "
                + " and ".join(won)
                + " but not for "
                + " and ".join(lost)
                + "."
            )
    sign = _sign(chosen)
    if "above" in sign:
        parts.append(
            "The chosen list measurably raises the conversion rate against sending nothing, on rows no model saw."
        )
    elif "below" in sign:
        parts.append(
            "The chosen list measurably lowers the conversion rate against sending nothing, on rows no model saw."
        )
    else:
        parts.append(
            "Against sending nothing, the chosen list's effect on the conversion rate, measured on rows no model saw, has an "
            "interval that includes zero."
        )
    better = [k for k, v in blankets.items() if v.get("ci_low") is not None and v["ci_low"] > 0]
    worse = [k for k, v in blankets.items() if v.get("ci_high") is not None and v["ci_high"] < 0]
    if worse:
        parts.append(
            "It does measurably **worse** than "
            + " and ".join(w.replace("chosen_minus_everyone_", "sending everyone the ") for w in worse)
            + ": the simple rule wins on this file."
        )
    elif better:
        parts.append(
            "It does measurably better than "
            + " and ".join(b.replace("chosen_minus_everyone_", "sending everyone the ") for b in better)
            + "."
        )
    else:
        parts.append("It cannot be told apart from sending either e-mail to everyone.")
    parts.append(
        "Every step ran end to end through the product's own API, and each Value Proof Pack rests only on the measured "
        "numbers of its campaign, with every figure traced."
        if all(c["proof"]["status"] == 200 for c in steps["campaigns"].values())
        else "Not every Value Proof Pack could be built; section 6 says why."
    )
    return " ".join(parts)


def _baseline_gap(comparison: dict[str, Any], kind: str) -> str:
    row = next((b for b in comparison.get("baselines") or [] if b.get("baseline") == kind), None)
    if row is None or not row.get("available"):
        return "not measured"
    return f"AUUC difference against {row.get('label')} {_ci(row.get('difference'))}"


def _baseline_table(add: Any, comparison: dict[str, Any]) -> None:
    add("| Ranking by | AUUC (95 %) | Uplift minus it (95 %) | Uplift better |")
    add("|---|---|---|---|")
    add(f"| predicted uplift | {_ci(comparison.get('uplift_auuc'))} | | |")
    for row in comparison.get("baselines") or []:
        if not row.get("available"):
            add(f"| `{row.get('baseline')}` | not available: {row.get('reason')} | | |")
            continue
        add(
            f"| `{row.get('baseline')}` ({row.get('label')}) | {_ci(row.get('auuc'))} | {_ci(row.get('difference'))} | "
            f"{'yes' if row.get('uplift_better') else 'no'} |"
        )
    add("")


def _range(interval: dict[str, Any] | None, fmt: Any = _num) -> str:
    if not interval or interval.get("ci_low") is None or interval.get("ci_high") is None:
        return "—"
    return f"{fmt(interval['ci_low'])} to {fmt(interval['ci_high'])}"


def _skew_caveat(campaigns: dict[str, Any]) -> str:
    """The engine's own skew warning, when a campaign on the amount carried it, said under the amount's tables."""
    flagged = [
        name
        for name, campaign in campaigns.items()
        if "OUTCOME_SKEWED" in ((campaign.get("report") or {}).get("outcome_warnings") or [])
    ]
    if not flagged:
        return "The engine raised no skew warning on the amount."
    return (
        f"**Caution: the amount is skewed.** The engine flagged `OUTCOME_SKEWED` on the `{'`, `'.join(flagged)}` campaign: "
        "a few very large amounts dominate the averages, so the normal intervals above may be too narrow. The bootstrap "
        "column is the check on them; neither is exact on so few non-zero amounts."
    )


def _pack_finding(campaigns: dict[str, Any]) -> str:
    """Where the Pack's Method text claims more than a replay can support, quoted from the Pack itself."""
    quoted: list[str] = []
    left_out: list[str] = []
    for name, campaign in campaigns.items():
        proof = campaign.get("proof") or {}
        if proof.get("status") != 200:
            continue
        method = next((x for x in proof["view"]["sections"] if x.get("key") == "method"), None)
        for note in (method or {}).get("notes") or []:
            if ("before the campaign went out" in note or "whether or not the message arrived" in note) and (
                note not in quoted
            ):
                quoted.append(note)
        report = campaign.get("report") or {}
        replay = campaign.get("replay") or {}
        if report.get("rows_without_outcome"):
            left_out.append(
                f"`{name}`: {_int(report['rows_without_outcome'])} of {_int(replay.get('intended'))} intended customers"
            )
    if not quoted:
        return "The Packs' Method text makes no claim about when the control group was drawn or whom outcomes cover."
    names = "; ".join(left_out) or "none"
    return (
        "**Finding.** Each Pack's *Method and limits* says: "
        + " ".join(f"\u201c{q}\u201d" for q in quoted)
        + " On this replay neither holds as written: nothing went out (the file is a 2008 log, and the engine's control group "
        "was drawn when the evaluation rows were scored), and outcomes are counted only for the customers the replay kept, "
        f"while the rest are left out as having no outcome ({names}). The Pack does print that count, and its campaign name "
        "says it is a retrospective replay of a public dataset with a third of each group kept, but its method sentences "
        "are fixed by `causal_basis` alone. The plan's `expectation` text, which says the same, is stored with the plan "
        "and does not reach the Pack. Open question for M104 (DEC-1314): the Method text should depend on whether "
        "outcomes are missing by design (a replay) or by loss. No engine code was changed."
    )


def _p_control_agrees(comparison: dict[str, Any]) -> str:
    """Whether the out-of-sample `p_control` row reaches the same verdict as the comparator that decided the check."""
    rows = {row.get("baseline"): row for row in comparison.get("baselines") or [] if row.get("available")}
    own, decider = rows.get("p_control"), rows.get(comparison.get("risk_baseline"))
    if own is None or decider is None or own is decider:
        return "The `p_control` row, the campaign-effect model's own and out of sample here, is the only clean comparator."
    same = bool(own.get("uplift_better")) == bool(decider.get("uplift_better"))
    return (
        "The `p_control` row is the campaign-effect model's own, out of sample on this hold-out, and it reaches the "
        + (
            "same verdict."
            if same
            else "**opposite** verdict, so the in-sample comparator decided the check."
        )
    )
