"""`--policy scientist`: the bench scores the Scientist's selected policy instead of the agent alone.

Nothing here knows a dataset. For one `BenchData` (a randomised campaign):

1. **Split** as the bench does (customer id hash, `SPLIT_SALT`): TRAIN and TEST.
2. **Select on TRAIN only.** `autonomy.scientist.run_scientist` gets the TRAIN customers as its campaign
   and the FIXED default candidate list (segment rule, uplift model, learning agent; no AI proposes
   anything yet). Inside it, an inner customer split fits each candidate on one half of TRAIN and
   scores it on the other half (off-policy, logged propensities), the safe default is the best blanket
   arm by train evidence (else send nothing), and the written-down rule (best lower bound after Holm,
   else keep the default) picks. TEST is not read by any of this.
3. **Refit and deploy.** The chosen candidate is refitted on all of TRAIN (the learning agent is
   warm-started on all of TRAIN, as the bench does for the agent) and decides for every TEST customer.
4. **Score on TEST** with the bench's own scoring (`common.score`: IPS / SNIPS / DR, paired DR
   differences) against send-to-none, each blanket arm, the best segment rule and the T-learner of the
   bench, and the learning agent: `scientist minus best simple`, with raw and Bonferroni-corrected
   intervals and the 2% non-inferiority tolerance of `common.NON_INFERIORITY_SHARE`.

With `ai=` (see `ai_loop`) the same TEST also scores a second Scientist whose candidates were proposed by
the AI roles (Data Doctor, Proposer) and whose pick was made by the Judge, from recorded Sonnet answers: the
fixed list, the Sonnet-proposed list and the agent are compared on the same customers. The agent runs are
shared (one inner-split run, one TEST run), so the AI loop adds little time.

The learning-agent candidate costs two agent runs per dataset (one inside the selection, one on
TEST), which is why a full `--policy scientist --dataset all` takes about twice the agent bench.
"""

from __future__ import annotations

import gc
from collections.abc import Mapping
from typing import Any

import numpy as np
import numpy.typing as npt

from autonomy.scientist import Campaign, Candidate, Selection, default_candidates, run_scientist
from autonomy.scientist.menu import AgentRunner, BanditAgent
from scripts.v2_realbench import ai_loop
from scripts.v2_realbench.common import (
    NON_INFERIORITY_SHARE,
    BenchData,
    Timings,
    choose_simple,
    fixed,
    multiplicity,
    paired,
    run_agent,
    score,
    simple_name,
    split_by_hash,
    t_learner,
)

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

POLICY_NAME = "SCIENTIST"


def campaign_of(data: BenchData) -> Campaign:
    """The bench's randomised campaign as the Scientist reads it (arm 0 = no contact)."""
    return Campaign(
        customer_ids=tuple(str(c) for c in data.customer_ids),
        arm=data.arm.astype(np.int64),
        propensity=data.propensity.astype(np.float64),
        arm_names=tuple(data.arm_names),
        arm_cost=tuple(float(c) for c in data.arm_cost),
        converted=data.conversion.astype(np.float64),
        margin=data.margin_value.astype(np.float64),
        features=data.features,
        randomised=True,
        source=data.name,
    )


def agent_runner(data: BenchData, seeds: int, timings: Timings) -> AgentRunner:
    """Warm-start the production agent on one customer set and decide for another (`run_agent`)."""
    row_of = {str(c): i for i, c in enumerate(data.customer_ids)}

    def run(fit: Campaign, held: Campaign) -> FloatArray:
        fit_rows = np.array([row_of[c] for c in fit.customer_ids], dtype=np.int64)
        held_rows = np.array([row_of[c] for c in held.customer_ids], dtype=np.int64)
        result = run_agent(data, fit_rows, held_rows, seeds=seeds, timings=timings)
        pi: FloatArray = result.pi
        return pi

    return run


AI_POLICY_NAME = "SCIENTIST (Sonnet)"


def _assess(
    prefix: str,
    key: str,
    name: str,
    table: dict[str, dict[str, Any]],
    terms: Mapping[str, Any],
    others: list[str],
    *,
    best_policy: str,
    all_name: str,
    best_on_test: str,
) -> dict[str, Any]:
    """One Scientist variant against every baseline: raw and Bonferroni-corrected intervals, the 2%
    non-inferiority verdict. `key` is where each baseline's table row keeps the paired difference."""
    comparisons: dict[str, dict[str, float]] = {}
    for other in others:
        table[other][key] = paired(terms[name], terms[other])
        comparisons[f"{prefix} minus {other}"] = table[other][key]
    mult = multiplicity(comparisons)
    vs_best = comparisons[f"{prefix} minus {best_policy}"]
    best_value = float(table[best_policy]["dr"]["point"])
    tolerance = NON_INFERIORITY_SHARE * abs(best_value)
    corrected_low = float(mult["comparisons"][f"{prefix} minus {best_policy}"]["corrected"]["low"])
    return {
        "value": table[name]["dr"],
        "contact_rate": table[name]["contact_rate"],
        "arm_shares": table[name]["arm_shares"],
        "vs_best": vs_best,
        "vs_all": comparisons[f"{prefix} minus {all_name}"],
        "vs_best_on_test": comparisons[f"{prefix} minus {best_on_test}"],
        "multiplicity": mult,
        "verdict": {
            "scientist_ties_or_beats_best_simple": bool(vs_best["high"] >= 0.0),
            "scientist_beats_best_simple": bool(vs_best["low"] > 0.0),
            "non_inferior_to_best_simple": bool(corrected_low > -tolerance),
            "corrected_low_vs_best": corrected_low,
            "tolerance": tolerance,
            "tolerance_rule": f"{NON_INFERIORITY_SHARE:.0%} of the best simple policy's DR value ({best_value:.6g})",
            "intervals": "95% normal, paired; corrected = Bonferroni over this dataset's scientist comparisons",
        },
    }


def evaluate_scientist(
    data: BenchData,
    *,
    seeds: int,
    timings: Timings,
    with_agent: bool = True,
    ai: ai_loop.AiSetup | None = None,
) -> dict[str, Any]:
    """The Scientist on one dataset (see the module docstring): selection on TRAIN, score on TEST.

    With `ai`, the AI roles' loop also runs (profile of TRAIN, Data Doctor, Proposer, held-out evidence,
    Judge) and its pick is scored on the same TEST under `block["ai"]`. A prompt with no recorded answer
    raises `AnswerPending` (nothing is guessed); the Doctor and Proposer prompts come before any
    expensive work."""
    train, test = split_by_hash(data.customer_ids)
    profit = data.profit()
    k, nt = data.n_arms, len(test)
    campaign = campaign_of(data)
    train_campaign = campaign.take(train)
    runner = agent_runner(data, seeds, timings) if with_agent else None
    if runner is not None:
        runner = ai_loop.cached_runner(runner)
    plan: ai_loop.AiPlan | None = None
    if ai is not None:
        with timings.step("ai: profile of TRAIN, Data Doctor, Proposer (recorded answers)"):
            plan = ai_loop.plan(data, train, ai)
    candidates = default_candidates(runner)
    with timings.step("scientist: select on TRAIN (inner split, fit, off-policy scores, rule)"):
        selection = run_scientist(train_campaign, candidates)
    ai_candidates: tuple[Candidate, ...] = ()
    ai_selection: Selection | None = None
    ai_table = None
    ai_decision = None
    ai_run: Candidate | None = None
    if ai is not None and plan is not None:
        ai_candidates = ai_loop.proposal_candidates(plan.proposal.value, data, runner)
        with timings.step("ai: evaluate the proposed candidates on TRAIN (same inner split)"):
            ai_selection = run_scientist(train_campaign, ai_candidates)
        with timings.step("ai: Judge (recorded answer)"):
            ai_table, ai_decision = ai_loop.judge(ai, plan, ai_selection)
        idx = ai_loop.deployable_index(ai_decision, plan.ids, ai_selection)
        ai_run = ai_selection.default.policy() if idx is None else ai_candidates[idx]
    with timings.step("scientist: refit the selected policy on all of TRAIN, decide for TEST"):
        agent_pi: FloatArray | None = None
        if runner is not None:
            agent_pi = runner(train_campaign, campaign.take(test))
        pi_sci = _policy_pi(data, selection.run, train_campaign, test, agent_pi)
        pi_ai = None if ai_run is None else _policy_pi(data, ai_run, train_campaign, test, agent_pi)
    with timings.step("baselines on train (segment rule, T-learner, best simple policy)"):
        rule, best_name, val_table = choose_simple(data, train, profit)
        rule_pi = rule(data.logged(test, profit, "profit"))
        t_pi, _ = t_learner(data, train, test)
    rule_name = "segment rule (" + "+".join(rule.columns) + ")"
    policies: dict[str, FloatArray] = {simple_name(data, a): fixed(nt, k, a) for a in range(k)}
    policies[rule_name] = rule_pi
    policies["T-learner (logistic)"] = t_pi
    if agent_pi is not None:
        policies["AGENT"] = agent_pi
    others = list(policies)
    policies[POLICY_NAME] = pi_sci
    if pi_ai is not None:
        policies[AI_POLICY_NAME] = pi_ai
    best_policy = rule_name if best_name == "segment rule" else best_name
    tr_means = [float(profit[train][data.arm[train] == a].mean()) for a in range(1, k)]
    all_name = simple_name(data, 1 + int(np.argmax(tr_means)))
    with timings.step("score on test (IPS / SNIPS / DR)"):
        table, terms = score(data, test, profit, "profit", policies)
    simple = [simple_name(data, a) for a in range(k)] + [rule_name]
    best_on_test = max(simple, key=lambda n: table[n]["dr"]["point"])
    main = _assess(
        "scientist",
        "scientist_minus_this",
        POLICY_NAME,
        table,
        terms,
        others,
        best_policy=best_policy,
        all_name=all_name,
        best_on_test=best_on_test,
    )
    out: dict[str, Any] = {
        "dataset": data.name,
        "policy": "scientist",
        "rows": data.n,
        "train": len(train),
        "test": nt,
        "arms": list(data.arm_names),
        "economics": data.economics,
        "reward": "profit",
        "notes": list(data.notes),
        "selection": selection.to_dict(),
        "picked": _picked(selection),
        "candidate_list": [c.spec() for c in candidates],
        "scientist": {k_: main[k_] for k_ in ("value", "contact_rate", "arm_shares")},
        "policies": table,
        "best_simple": {
            "name": best_policy,
            "chosen_on": "train only (as the agent bench does)",
            "validation": val_table,
            "scientist_minus_best_simple": main["vs_best"],
        },
        "send_to_all": {"name": all_name, "scientist_minus_send_to_all": main["vs_all"]},
        "best_simple_on_test": {
            "name": best_on_test,
            "scientist_minus": main["vs_best_on_test"],
            "note": "diagnostic only: chosen on TEST, so it flatters the baseline",
        },
        "multiplicity": main["multiplicity"],
        "verdict": main["verdict"],
    }
    if ai is not None and plan is not None and ai_selection is not None and ai_run is not None:
        sonnet = _assess(
            "scientist (Sonnet)",
            "scientist_ai_minus_this",
            AI_POLICY_NAME,
            table,
            terms,
            others,
            best_policy=best_policy,
            all_name=all_name,
            best_on_test=best_on_test,
        )
        record = ai_loop.describe_ai(plan, ai_candidates, ai_selection, ai_table, ai_decision, ai.llm.calls)
        idx = None if ai_decision is None else ai_loop.pick_index(ai_decision, plan.ids)
        arms = ai_selection.evaluations.arm_names if ai_selection.evaluations is not None else ()
        record["deployed"] = ai_run.label(arms) if arms else "send nothing"
        record["deployed_kind"] = ai_run.kind
        record["agreement"] = {
            "judge_pick_equals_evidence_rule": bool(
                ai_decision is not None and ai_decision.final_pick == ai_decision.rule_pick
            ),
            "judge_vetoed": None if ai_decision is None else ai_decision.vetoed_by,
            "select_rule_outcome": ai_selection.outcome,
            "select_rule_chosen": ai_selection.chosen_label,
            "guard_kept_default_over_judge": bool(
                ai_decision is not None
                and ai_loop.pick_index(ai_decision, plan.ids) is not None
                and ai_loop.deployable_index(ai_decision, plan.ids, ai_selection) is None
            ),
            "select_rule_agrees_with_judge": bool(
                ai_decision is not None
                and (
                    (idx is None and ai_selection.chosen is None)
                    or (idx is not None and ai_selection.chosen is ai_candidates[idx])
                )
            ),
        }
        record.update(sonnet)
        record["sonnet_minus_fixed_list"] = paired(terms[AI_POLICY_NAME], terms[POLICY_NAME])
        record["sonnet_minus_fixed_list_note"] = "one paired comparison, not corrected (information only)"
        out["ai"] = record
    return out


def _policy_pi(
    data: BenchData,
    run: Candidate,
    train_campaign: Campaign,
    test: IntArray,
    agent_pi: FloatArray | None,
) -> FloatArray:
    """The probabilities of `run` on TEST, refitted on all of TRAIN (the agent's own for a `BanditAgent`)."""
    if isinstance(run, BanditAgent):
        assert agent_pi is not None
        return agent_pi
    fitted = run.fit(train_campaign)
    test_features = data.features.iloc[test].reset_index(drop=True)
    pi: FloatArray = fitted.probabilities(test_features, None)
    gc.collect()
    return pi


def _picked(selection: Selection) -> dict[str, Any]:
    ev = selection.evaluations
    arms = ev.arm_names if ev is not None else ()
    return {
        "outcome": selection.outcome,
        "run": selection.run.label(arms) if arms else "send nothing",
        "kind": selection.run.kind,
        "default": selection.default.label,
    }


# --- rendering -----------------------------------------------------------------------------------------


def _fmt(e: Mapping[str, float] | None, digits: int = 4) -> str:
    if e is None:
        return "n/a"
    return f"{e['point']:+.{digits}f} [{e['low']:+.{digits}f}, {e['high']:+.{digits}f}]"


def render_scientist(report: Mapping[str, Any]) -> str:
    """The Scientist's scoreboard as markdown: what it picked and how that scored on TEST."""
    lines = [
        "| Dataset | Outcome | Safe default (train) | Picked | Value (DR) | Contact rate | "
        "Picked minus best simple [95% CI] | Non-inferior (corrected, 2%) | Picked minus send-to-all [95% CI] |",
        "|---|---|---|---|---:|---:|---:|---|---:|",
    ]
    for name, b in report["datasets"].items():
        if "error" in b:
            lines.append(f"| {name} | error: {b['error']} | | | | | | | |")
            continue
        d = 4 if max(abs(float(p["dr"]["point"])) for p in b["policies"].values()) < 10 else 2
        v = b["verdict"]
        lines.append(
            f"| {name} | {b['picked']['outcome']} | {b['picked']['default']} | {b['picked']['run']} | "
            f"{b['scientist']['value']['point']:.{d}f} | {b['scientist']['contact_rate']:.1%} | "
            f"{_fmt(b['best_simple']['scientist_minus_best_simple'], d)} (best simple: {b['best_simple']['name']}) | "
            f"{'yes' if v['non_inferior_to_best_simple'] else 'no'} (low {v['corrected_low_vs_best']:+.4g} vs "
            f"tolerance {v['tolerance']:.4g}) | {_fmt(b['send_to_all']['scientist_minus_send_to_all'], d)} |"
        )
    for name, b in report["datasets"].items():
        if "error" in b:
            continue
        d = 4 if max(abs(float(p["dr"]["point"])) for p in b["policies"].values()) < 10 else 2
        sel = b["selection"]
        lines += [
            "",
            f"### {name}: the Scientist's selection (on TRAIN only)",
            "",
            sel["explanation"],
            "",
            "| Candidate | Held-out value (DR) | Gain over the default [95% CI] | Raw p | Holm p | Eligible | Flags |",
            "|---|---:|---:|---:|---:|---|---|",
        ]
        for c in sel["verdicts"]:
            if c["status"] != "ok":
                lines.append(f"| {c['label']} | failed: {c['note']} | | | | no | |")
                continue
            flags = "; ".join([*c["blocking"], *c["warnings"]]) or "-"
            lines.append(
                f"| {c['label']} | {_fmt(c['value'], d)} | {_fmt(c['gain_vs_default'], d)} | {c['p_raw']:.3g} | "
                f"{c['p_holm']:.3g} | {'yes' if c['eligible'] else 'no'} | {flags} |"
            )
        lines += [
            "",
            f"### {name}: profit per customer on TEST ({b['test']:,} of {b['rows']:,}; {b['economics']})",
            "",
            "| Policy | Contacted | DR [95% CI] | DR gain vs send-to-none | Scientist minus this [95% CI] |"
            + (" Sonnet-proposed minus this [95% CI] |" if "ai" in b else ""),
            "|---|---:|---:|---:|---:|" + ("---:|" if "ai" in b else ""),
        ]
        for pname, p in b["policies"].items():
            variant = pname in (POLICY_NAME, AI_POLICY_NAME)
            lines.append(
                f"| {pname} | {p['contact_rate']:.1%} | {_fmt(p['dr'], d)} | {_fmt(p['dr_vs_none'], d)} | "
                f"{'-' if variant else _fmt(p.get('scientist_minus_this'), d)} |"
                + (f" {'-' if variant else _fmt(p.get('scientist_ai_minus_this'), d)} |" if "ai" in b else "")
            )
    ai_rows = [(n, b) for n, b in report["datasets"].items() if "error" not in b and "ai" in b]
    if ai_rows:
        lines += ["", *render_ai(ai_rows)]
    return "\n".join(lines)


def _digits(b: Mapping[str, Any]) -> int:
    return 4 if max(abs(float(p["dr"]["point"])) for p in b["policies"].values()) < 10 else 2


def render_ai(rows: list[tuple[str, Mapping[str, Any]]]) -> list[str]:
    """The AI loop's markdown: the three-way comparison, then per dataset what each role said."""
    lines = [
        "## Fixed list vs Sonnet-proposed vs the agent (same TEST customers)",
        "",
        "| Dataset | Variant | Picked | Value (DR) | Contacted | Minus best simple [95% CI] | "
        "Non-inferior (corrected, 2%) |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for name, b in rows:
        d = _digits(b)
        ai = b["ai"]
        pol = b["policies"]
        v = b["verdict"]
        lines.append(
            f"| {name} | fixed list | {b['picked']['run']} ({b['picked']['outcome']}) | "
            f"{b['scientist']['value']['point']:.{d}f} | {b['scientist']['contact_rate']:.1%} | "
            f"{_fmt(b['best_simple']['scientist_minus_best_simple'], d)} | "
            f"{'yes' if v['non_inferior_to_best_simple'] else 'no'} (low {v['corrected_low_vs_best']:+.4g}) |"
        )
        av = ai["verdict"]
        lines.append(
            f"| {name} | Sonnet-proposed | {ai['deployed']} (judge: {ai['judge']['final_pick']}) | "
            f"{ai['value']['point']:.{d}f} | {ai['contact_rate']:.1%} | {_fmt(ai['vs_best'], d)} | "
            f"{'yes' if av['non_inferior_to_best_simple'] else 'no'} (low {av['corrected_low_vs_best']:+.4g}) |"
        )
        if "AGENT" in pol:
            lines.append(
                f"| {name} | the agent | learning agent | {pol['AGENT']['dr']['point']:.{d}f} | "
                f"{pol['AGENT']['contact_rate']:.1%} | | |"
            )
    for name, b in rows:
        d = _digits(b)
        ai = b["ai"]
        dg = ai["diagnosis"]["value"]
        lines += [
            "",
            f"### {name}: the AI loop (recorded Sonnet answers; selection on TRAIN only)",
            "",
            f"**Data Doctor** ({ai['diagnosis']['source']}): optimise `{dg['outcome_metric']}`; clicks: "
            f"`{dg['click_trust']}` ({dg['click_trust_reason']}); enough data: {dg['enough_data']}; "
            f"pitfalls: {', '.join(p['code'] for p in dg['pitfalls']) or 'none'}."
            + (
                f" Rejected: {'; '.join(ai['diagnosis']['problems'])}." if ai["diagnosis"]["problems"] else ""
            ),
            "",
            f"**Proposer** ({ai['proposal']['source']})"
            + (f", rejected: {'; '.join(ai['proposal']['problems'])}" if ai["proposal"]["problems"] else "")
            + ":",
            "",
            "| Id | Kind | Arm / columns | Settings | Rationale |",
            "|---|---|---|---|---|",
        ]
        for cid, c in zip(ai["candidate_ids"], ai["proposal"]["value"]["candidates"], strict=True):
            st = c["settings"]
            lines.append(
                f"| {cid} | {c['kind']} | {c.get('arm') or ', '.join(c['columns']) or '-'} | lookback "
                f"{st['lookback_days']}, floor {st['exploration_floor']}, budget {st['weekly_contact_budget']} | "
                f"{c['rationale']} |"
            )
        lines += [
            "",
            "| Id | n evaluated | Gain over the current set-up | Corrected low | Holm p |",
            "|---|---:|---:|---:|---:|",
        ]
        for c in ai["evidence"]["candidates"]:
            p = "n/a" if c["adjusted_p"] is None else f"{c['adjusted_p']:.3g}"
            lines.append(
                f"| {c['candidate_id']} | {c['n_eval']:,} | {c['gain_estimate']:+.{d}f} | "
                f"{c['gain_low']:+.{d}f} | {p} |"
            )
        j = ai["judge"]
        ag = ai["agreement"]
        lines += [
            "",
            f"**Judge** ({j['source']}): evidence rule picks `{j['rule_pick']}`, final pick `{j['final_pick']}`"
            f"{', vetoed by ' + j['vetoed_by'] if j['vetoed_by'] else ''}; the selection rule inside the "
            f"Scientist {'agrees' if ag['select_rule_agrees_with_judge'] else 'does not agree'}. "
            f"{j['report']['explanation']}",
            "",
            "| Role | Model calls | Repair rounds | Used | Problems |",
            "|---|---:|---:|---|---|",
        ]
        for role, r in ai["validation"].items():
            lines.append(
                f"| {role} | {r['model_calls']} | {r['repair_rounds']} | {r['used']} | "
                f"{'; '.join(r['problems']) or '-'} |"
            )
    return lines
