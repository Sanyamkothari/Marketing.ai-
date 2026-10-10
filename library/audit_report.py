"""Render the audit readout section of a dataset's `run_report.md` from its `audit.results.json` (Plan J, DEC-1322).

Every number is read from the results `library/audit.py` wrote; this module computes nothing but formatting
and the yes/no wording that follows from a stored label, flag or interval. A sentence whose truth depends on
a result is chosen by that result, so the section says what happened whichever way it fell.
"""

from __future__ import annotations

from typing import Any

from library.journey_report import _ci, _int, _num, _pct, _pp, _sign


def _usd(value: float | None) -> str:
    if value is None:
        return "—"
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def _p(value: float | None) -> str:
    if value is None:
        return "—"
    return "< 0.001" if value < 0.001 else f"{value:.3f}"


def _section(view: dict[str, Any], key: str) -> dict[str, Any] | None:
    return next((s for s in view.get("sections") or [] if s.get("key") == key), None)


def _line_text(view: dict[str, Any], section: str, label: str) -> str | None:
    found = _section(view, section)
    for line in (found or {}).get("lines") or []:
        if line.get("label") == label:
            value = line.get("value") or {}
            return str(value.get("text")) if value.get("text") is not None else None
    return None


def _cases(results: dict[str, Any]) -> list[dict[str, Any]]:
    return list(results["cases"].values())


def _measured(results: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in _cases(results) if c.get("status") == 201]


def _effect_cell(case: dict[str, Any]) -> str:
    """The case's effect, in one cell of the answer table."""
    report = case["report"]
    if case["outcome_kind"] == "binary":
        arms = report.get("arms") or []
        if arms:
            return "; ".join(f"{a['arm']} {_ci(a['effect'], _pp)}: {_sign(a['effect'])}" for a in arms)
        return f"{_ci(report.get('absolute_lift'), _pp)}: {_sign(report.get('absolute_lift'))}"
    interval = report.get("mean_difference_ci")
    return f"{_ci(interval, _usd)} per customer: {_sign(interval)}"


def _binary_table(add: Any, case: dict[str, Any]) -> None:
    report = case["report"]
    add(
        "| Offer | E-mailed | Converted | Held back | Converted | Difference (95 %) | Extra conversions (95 %) | p |"
    )
    add("|---|---|---|---|---|---|---|---|")
    arms = report.get("arms") or [
        {
            "arm": case["title"],
            "treated_rows": report["treated_rows"],
            "treated_conversions": report["treated_conversions"],
            "treated_rate": report["treated_rate"],
            "control_rows": report["control_rows"],
            "control_conversions": report["control_conversions"],
            "control_rate": report["control_rate"],
            "effect": report["absolute_lift"],
            "incremental_conversions": report["incremental_conversions"],
            "p_value": report["p_value"],
        }
    ]
    for arm in arms:
        add(
            f"| {arm['arm']} | {_int(arm['treated_rows'])} | {_int(arm['treated_conversions'])} ({_pct(arm['treated_rate'])}) "
            f"| {_int(arm['control_rows'])} | {_int(arm['control_conversions'])} ({_pct(arm['control_rate'])}) "
            f"| {_ci(arm['effect'], _pp)}: {_sign(arm['effect'])} "
            f"| {_ci(arm['incremental_conversions'], _int)} | {_p(arm['p_value'])} |"
        )
    add("")


def _spend_table(add: Any, cases: list[dict[str, Any]]) -> None:
    add(
        "| Contrast | E-mailed | Mean spend | Held back | Mean spend | Difference per customer (engine, 95 %) "
        "| Bootstrap (harness, 95 %) | p | In total, dollars (engine) |"
    )
    add("|---|---|---|---|---|---|---|---|---|")
    for case in cases:
        report = case["report"]
        boot = case.get("bootstrap")
        boot_cell = f"{_usd(boot['ci_low'])} to {_usd(boot['ci_high'])}" if boot else "—"
        verdict = case.get("verdict") or {}
        total = (
            f"{verdict['amount']:+,} (likely {verdict['likely_low']:+,} to {verdict['likely_high']:+,})"
            if verdict.get("amount") is not None
            else "—"
        )
        add(
            f"| {case['contrast']} | {_int(report['treated_rows'])} | {_usd(report.get('treated_mean'))} "
            f"| {_int(report['control_rows'])} | {_usd(report.get('control_mean'))} "
            f"| {_ci(report.get('mean_difference_ci'), _usd)}: {_sign(report.get('mean_difference_ci'))} "
            f"| {boot_cell} | {_p(report.get('p_value'))} | {total} |"
        )
    add("")


def _skew_note(cases: list[dict[str, Any]]) -> str:
    flagged = [
        c for c in cases if "OUTCOME_SKEWED" in ((c.get("report") or {}).get("outcome_warnings") or [])
    ]
    if not flagged:
        return "The engine raised no skew warning on the amount."
    names = ", ".join(f"`{c['key']}`" for c in flagged)
    return (
        f"**Caution: the amount is skewed.** The engine flagged `OUTCOME_SKEWED` on {names}: a few very large "
        "amounts dominate the averages, so its normal interval may be too narrow. The bootstrap column is the "
        "harness's check on it (a seeded percentile bootstrap of the difference in means, 2,000 resamples); "
        "neither is exact with so few customers who spent anything."
    )


def _line_range(view: dict[str, Any], section: str, label: str) -> str | None:
    """A line's value and range as the Pack prints them: `+212, likely +150 to +270`."""
    for line in (_section(view, section) or {}).get("lines") or []:
        if line.get("label") == label and line.get("value") and line.get("low") and line.get("high"):
            return f"{line['value']['text']}, likely {line['low']['text']} to {line['high']['text']}"
    return None


def _charged(case: dict[str, Any], whole: dict[str, Any]) -> str:
    """The e-mails the Pack's cost of contacts charges, as the campaign record counts them (`intended_treated`).

    The value lines rest on the e-mailed customers measured (`offers_combined.treated_rows`); when some e-mailed
    customers were left out of the measurement (no outcome) the two counts differ, and the sentence says by how many
    rather than claiming every money line covers the same e-mails.
    """
    measured = int(whole["treated_rows"])
    counts = (case.get("campaign") or {}).get("counts") or {}
    charged = int(counts.get("intended_treated", measured))
    if charged == measured:
        return f"all {_int(charged)} e-mails sent: every money line covers the same e-mails"
    return (
        f"all {_int(charged)} e-mails meant to be sent, while the value lines rest on the {_int(measured)} of them "
        f"measured, so {_int(charged - measured)} e-mails are charged and not credited (no outcome was measured for them)"
    )


def _pack_finding(results: dict[str, Any]) -> str:
    """How a Pack of several offers scopes its money lines, quoted from the Pack and the report.

    A report with `offers_combined` (DEC-1314 (r)-(w)) is added up across offers by the Pack, which this
    says with the Pack's own figures; results from before it existed keep the finding raised for M104.
    """
    out: list[str] = []
    for case in _measured(results):
        proof = case.get("proof") or {}
        arms = (case["report"].get("arms") or []) if proof.get("status") == 200 else []
        if len(arms) < 2:
            continue
        view = proof["view"]
        first, rest = arms[0], arms[1:]
        whole = case["report"].get("offers_combined")
        if isinstance(whole, dict):
            label = "Extra outcomes because of the campaign, every offer together"
            credited = _line_range(view, "incremental", label)
            value = _line_range(view, "net_value", "Value of what the campaign changed, every offer together")
            cost = _line_text(view, "net_value", "Cost of contacts, for every customer meant to be contacted")
            parts = " and ".join(
                f"{a['arm']} {a['incremental_conversions']['value']:+,.0f}"
                for a in arms
                if a.get("incremental_conversions")
            )
            out.append(
                f"**Several offers (`{case['key']}`).** The Pack adds the {len(arms)} offers together against the "
                f'one group sent nothing: its headline and "Extra outcomes" ({credited}) are {parts} together, '
                f"and the value of what the campaign changed ({value}) and the net value rest on that sum, while the "
                f"cost of contacts ({cost}) is for {_charged(case, whole)}. The range is the engine's own, from every "
                "e-mailed customer compared with "
                "the shared group at once (`offers_combined` of the report), which counts that group once; the "
                "offers' own ranges are not added. This closes the finding raised for M104 (DEC-1314) in "
                "docs/CROSS_BRANCH_REQUESTS.md on 2026-10-10 (DEC-1322 (g))."
            )
            continue
        credited = _line_text(view, "incremental", "Extra outcomes because of the campaign")
        cost = _line_text(view, "net_value", "Cost of contacts, for every customer meant to be contacted")
        emailed = sum(int(a["treated_rows"]) for a in arms)
        others = ", ".join(
            f"{a['arm']} {a['incremental_conversions']['value']:+,.0f}"
            for a in rest
            if a.get("incremental_conversions")
        )
        out.append(
            f"**Finding (`{case['key']}`).** With {len(arms)} offers the Pack's headline ({view.get('headline')!r}), its "
            f'"Extra outcomes" ({credited}) and the value of what the campaign changed are the first offer\'s, '
            f"{first['arm']}, alone; the other offer's ({others}) is in the offer table and the backfire table but "
            f"not in the headline or the net value, while the cost of contacts ({cost}) is for all {_int(emailed)} "
            "e-mails sent. The net value therefore credits one offer and charges both. Every figure is traced to a "
            "measured record, so provenance passes; it is the scope of the headline that is partial (M100 keeps the "
            "first offer in the single-offer fields, DEC-668 (3), and the Pack reads those fields). Raised for M104 "
            "(DEC-1314) in docs/CROSS_BRANCH_REQUESTS.md on 2026-10-10; no engine code was changed."
        )
    return "\n\n".join(out)


def render_audit(results: dict[str, Any], *, results_path: str, command: str) -> str:
    """The `run_report.md` section for an audit's results, numbered from 8 (the journey's has seven)."""
    cases = _cases(results)
    done = _measured(results)
    lines: list[str] = []
    add = lines.append

    add("## 8. The audit readout on the original campaign (M103, public dataset, retrospective audit)")
    add("")
    add(
        f"**{results['label'].capitalize()}.** The journey above measured a list the engine made. This section reads the "
        "campaign that **actually ran**: Hillstrom's own e-mail test, with who got which e-mail as the assignment "
        "and visit, conversion and spend as the outcomes, posted to `POST /campaigns/audit` the way a client's "
        "past campaign would be. No model is trained and nothing is tuned; it is the whole file, as it ran. Every number "
        f"below is read from [`{results_path}`]({results_path}), committed beside this report (aggregates only: no customer "
        "row) and rewritten by the command at the end of this section."
    )
    add("")
    add("| | |")
    add("|---|---|")
    add(
        f"| Data | `{results['csv']}`, {_int(results['rows'])} rows (SHA-256 `{results['csv_sha256'][:16]}…`) |"
    )
    groups = results["groups"]
    add(
        "| Groups in the original campaign | " + ", ".join(f"{k} {_int(v)}" for k, v in groups.items()) + " |"
    )
    add(
        f"| Outcomes file | {_int(results['outcomes_file']['rows'])} rows: `{'`, `'.join(results['outcomes_file']['columns'])}` |"
    )
    add(
        f"| Campaign dated | {results['treatment_start'][:10]}, outcome window {results['outcome_window_days']} days |"
    )
    add(f"| Campaign ids | {', '.join('`' + c['campaign_id'] + '`' for c in done)} |")
    add(f"| Whole audit | {results['wall_clock_seconds']} s |")
    add("")

    # -- the answer ------------------------------------------------------------------------------------------
    add("### 8.1 The answer in one table")
    add("")
    add("| Campaign audited | Label | Randomness check | Effect (95 %) |")
    add("|---|---|---|---|")
    for case in cases:
        if case.get("status") != 201:
            code = ((case.get("refusal") or {}).get("detail") or {}).get("code")
            add(f"| {case['title']} | refused: `{code}` | | |")
            continue
        audit = case["audit"]
        check = audit["randomness"]
        check_cell = (
            f"{check['status']}: guessing score {_num(check.get('auc'), 3)} (chance 0.500, limit {_num(check['threshold'], 2)})"
            if check.get("auc") is not None
            else f"{check['status']}: {check.get('reason')}"
        )
        add(f"| {case['title']} | **{audit['label']}** | {check_cell} | {_effect_cell(case)} |")
    add("")

    # -- the label ------------------------------------------------------------------------------------------
    add("### 8.2 What the numbers may claim")
    add("")
    labels = {c["audit"]["label"] for c in done}
    if done and labels == {"Causal"}:
        worst = max(c["audit"]["randomness"].get("auc") or 0.0 for c in done)
        first = done[0]
        details = [
            c
            for c in first["assignment_columns"]
            if c != first["campaign"]["primary_key"] and c not in ("segment", "emailed")
        ]
        add(
            "The label is **Causal** for every campaign audited: it was stated that the e-mail was assigned at random (the "
            "route requires the statement and never assumes it), and the engine **verified** it. It tried to predict who "
            f"was e-mailed from the customer details in the assignment file ({first['audit']['randomness']['columns_used']} "
            f"columns: `{'`, `'.join(details)}`) and could not do better than a guessing score of {_num(worst, 3)} at worst "
            "(0.500 is chance; the limit is 0.60). With several offers it tests each against the shared control and "
            "reports the worst. Because the outcomes are in a separate file, the test could not have used them."
        )
    else:
        for case in done:
            add(f"- `{case['key']}`: **{case['audit']['label']}**. {case['audit']['explanation']}")
    add("")
    notes = sorted({n for c in done for n in c["audit"].get("notes") or []})
    if notes:
        add("The engine's own notes on these audits:")
        add("")
        for note in notes:
            add(f"- {note}")
        add("")

    # -- the effects ------------------------------------------------------------------------------------------
    binary = [c for c in done if c["outcome_kind"] == "binary"]
    spend = [c for c in done if c["outcome_kind"] == "continuous"]
    add("### 8.3 What each e-mail changed")
    add("")
    for case in binary:
        add(f"**{case['title']}** (campaign `{case['campaign_id']}`; {case['contrast']}).")
        add("")
        _binary_table(add, case)
        verdict = case.get("verdict")
        if verdict:
            arms = case["report"].get("arms") or []
            if len(arms) > 1:
                verdict_detail = str(verdict.get("detail") or "").rstrip(".")
                add(
                    f"The engine's verdict sentence reads the first offer only ({arms[0]['arm']}): "
                    f"{verdict.get('headline')}. {verdict_detail}. The per-offer table above is the reading "
                    "for each e-mail."
                )
            else:
                add(
                    f"The engine's verdict for the campaign as a whole: {verdict.get('headline')}. {verdict.get('detail')}."
                )
        else:
            add('The engine draws no "the campaign added N" verdict from this audit.')
        add("")
    if spend:
        add("**Spend, in dollars** (the file's money; the Packs convert it at the stated rate).")
        add("")
        _spend_table(add, spend)
        add(_skew_note(spend))
        add("")
        refused_spend = [c for c in cases if c.get("status") != 201 and c["outcome_kind"] == "continuous"]
        if refused_spend:
            add(
                "The route measures a yes/no outcome for each of several offers but an amount only for one contrast: "
                "the three-group file on the amount was posted as well and refused (below), so spend is read for any "
                "e-mail against none and, from a file of the two groups concerned, for each e-mail against none."
            )
        else:
            add(
                "Spend is read for any e-mail against none and, from a file of the two groups concerned, for each."
            )
        add(
            "No adjustment by an earlier amount is made: an audit has no test plan registered in advance, and the "
            "adjusted estimate (M102) must be named in one."
        )
        add("")
    refused = [c for c in cases if c.get("status") != 201]
    for case in refused:
        detail = (case.get("refusal") or {}).get("detail") or {}
        add(
            f"`{case['key']}` ({case['title']}) was refused by the route: `{detail.get('code')}`: {detail.get('message')}"
        )
        add("")

    # -- the packs ------------------------------------------------------------------------------------------
    add("### 8.4 Value Proof Packs (M104)")
    add("")
    unbuilt = [
        c["key"] for c in done if c["proof"]["status"] != 200 or not c["proof"].get("provenance_verified")
    ]
    if unbuilt:
        add(
            f"A Pack was asked for each campaign, named \"... - {results['label']}\" in the campaign name the Pack "
            "prints. Not every Pack was built and verified (see the table: "
            + ", ".join(f"`{k}`" for k in unbuilt)
            + "). For the others, provenance was re-verified (`engine.pilot.proof.verify_provenance`: every "
            "figure resolves to a measured record)."
        )
    else:
        add(
            f"A Pack was built for each campaign, named \"... - {results['label']}\" in the campaign name the Pack "
            "prints, and its provenance re-verified (`engine.pilot.proof.verify_provenance`: every figure resolves "
            "to a measured record)."
        )
    add("")
    add("| Pack | Built | Claim | Headline |")
    add("|---|---|---|---|")
    for case in done:
        proof = case["proof"]
        if proof["status"] != 200:
            detail = (proof.get("refusal") or {}).get("detail") or {}
            add(f"| `{case['key']}` | refused: `{detail.get('code')}` | | |")
            continue
        add(
            f"| `{case['key']}` | provenance verified: {proof.get('provenance_verified')}; HTML {proof['html_status']}, "
            f"PDF {proof['pdf_status']} | {proof['view']['claim_label']} "
            f"| {proof['view']['headline']} |"
        )
    add("")
    finding = _pack_finding(results)
    if finding:
        add(finding)
        add("")
    add(
        "Value inputs: "
        f"one conversion is worth ₹{results['value']['value_per_conversion_inr']:,.2f} ("
        f"{results['value']['basis']}; {_int(results['value']['buyers'])} buyers, mean spend "
        f"${results['value']['mean_order_usd']:,.2f}), one dollar of spend ₹{results['value']['fx_inr_per_usd']:,.0f}, and one "
        f"e-mail costs ₹{results['contact_cost_inr']}. {results['value']['fx_note']}"
    )
    add("")

    # -- the programme ------------------------------------------------------------------------------------------
    programme = results["programme"]
    add("### 8.5 The programme readout" + ("" if programme["applies"] else " does not apply"))
    add("")
    if programme["applies"]:
        add("The programme route accepted the request; its readout is not part of this report.")
    else:
        add(
            "`POST /campaigns/programme` reads the whole customer base against the **universal hold-out** (M92) over a "
            "finished period, intent to treat. That hold-out is drawn by the engine when it scores; Hillstrom's file was "
            "randomised by someone else, once, three ways, and has none. The route was asked once and refused: "
            f"`{programme['code']}` ({programme['status']}): \"{programme['message']}\" No programme number is produced or "
            "claimed."
        )
    add("")

    # -- assumptions ------------------------------------------------------------------------------------------
    add("### 8.6 Assumptions and limits")
    add("")
    add(f"- **Dates.** {results['start_note']}")
    add(
        "- **Randomisation is stated and verified, not proven by the file.** The request said the e-mail was assigned at random, "
        "as the route requires; the engine's check could not contradict it. A check that passes cannot prove randomness, it "
        "can only fail to find a pattern in the customer details the file carries."
    )
    add(
        "- **Intent to treat.** Every customer in the assignment file is compared, whether or not an e-mail reached them: the "
        "file does not say who opened or received one, so no contact file was added and the Packs' delivery section says so."
    )
    add(
        "- **A different question from the journey's.** The journey measured the model's treat list on the half of the file "
        "no model saw (a third of each arm kept). This measures the e-mails themselves on all 64,000 customers, the way the "
        "test was designed; the two are not the same estimate and are not meant to agree."
    )
    add(
        "- **Rupee figures** in the Packs are estimates that move with the stated value inputs; the measured counts and "
        "dollar amounts do not."
    )
    add(
        "- **Visits** were not audited; the outcomes file carries them and the route would read them the same way as conversion."
    )
    add("")
    add("### 8.7 Reproduce")
    add("")
    add("```bash")
    add(command)
    add("```")
    add("")
    return "\n".join(lines)
