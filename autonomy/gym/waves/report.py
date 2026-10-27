"""Turn the per-run rows of the waves benchmark into one table per world.

Each CI job writes a JSON with one row per seed (`runner.run_one`) and an `.npz` with each seed's true value per
customer. `summarize` joins any number of them:

* **True value** is the mean over seeds. **Share of the oracle** is that mean over the oracle's mean in the same
  world (the oracle is greedy per contact, so it is a ceiling in practice, not a proof of optimality).
* **Paired differences** (against `agent`, against the best simple policy) use the same customers and the same
  luck in every policy (common random numbers), so they are differences of per-customer values. The point is the
  mean over seeds of the summed per-customer difference. Its standard error has two parts: the sampling of
  customers, from each customer's difference averaged over the seeds (`n x var`; the seeds share their customers,
  so they are not independent samples of them), and the spread between worlds, the variance of the per-seed
  totals over the number of seeds. Without the per-customer arrays the difference is of the means and carries no
  interval.
* **The best simple policy** is the one of `send_none`, `send_all_sms`, `send_all_email`, `weekly_sms` and
  `best_fixed` with the highest mean true value *in this very run*. Choosing it after the fact flatters it, which
  is the cautious direction for a claim that something beats it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import numpy.typing as npt

from autonomy.gym.waves.config import HORIZON_DAYS, WORLDS
from autonomy.gym.waves.policies import AGENT, ORACLE, SIMPLE_POLICIES

FloatArray = npt.NDArray[np.float64]
Z95 = 1.959963984540054
Rows = Sequence[Mapping[str, Any]]
PerCustomer = Mapping[tuple[str, str, int], FloatArray]
"""(world, policy, seed) -> true value per customer."""


def _mean(xs: Sequence[float]) -> float:
    return float(sum(xs) / len(xs)) if xs else float("nan")


def paired_difference(
    a: Mapping[int, FloatArray], b: Mapping[int, FloatArray]
) -> tuple[float, float | None, int]:
    """(mean over seeds of the summed per-customer difference a - b, its standard error, seeds paired).

    The standard error is `sqrt(n x var_i(mean over seeds of d_i) + var_s(total_s) / S)`: customer sampling, with
    each customer counted once whatever the number of seeds, plus the spread of the per-seed totals between worlds.
    """
    seeds = sorted(set(a) & set(b))
    if not seeds:
        return float("nan"), None, 0
    d = np.vstack([a[s] - b[s] for s in seeds])
    totals = d.sum(axis=1)
    per_customer = d.mean(axis=0)
    n = per_customer.size
    var_customers = float(n * per_customer.var(ddof=1)) if n > 1 else 0.0
    var_worlds = float(totals.var(ddof=1)) / len(seeds) if len(seeds) > 1 else 0.0
    return float(totals.mean()), math.sqrt(var_customers + var_worlds), len(seeds)


def summarize(rows: Rows, per_customer: PerCustomer | None = None) -> dict[str, dict[str, Any]]:
    """world -> {"policies": [row per policy], "best_simple": name, "oracle": mean or None, "seeds": [...]}."""
    per_customer = per_customer or {}
    worlds: dict[str, dict[str, list[Mapping[str, Any]]]] = {}
    for r in rows:
        worlds.setdefault(str(r["world"]), {}).setdefault(str(r["policy"]), []).append(r)
    out: dict[str, dict[str, Any]] = {}
    for world, by_policy in worlds.items():
        means = {p: _mean([float(r["true_value"]) for r in rs]) for p, rs in by_policy.items()}
        simple = [p for p in SIMPLE_POLICIES if p in means]
        best_simple = max(simple, key=lambda p: means[p]) if simple else None
        oracle = means.get(ORACLE)

        def arrays(
            policy: str, world: str = world, by_policy: dict[str, list[Mapping[str, Any]]] = by_policy
        ) -> dict[int, FloatArray]:
            return {
                int(r["seed"]): per_customer[(world, policy, int(r["seed"]))]
                for r in by_policy[policy]
                if (world, policy, int(r["seed"])) in per_customer
            }

        table: list[dict[str, Any]] = []
        for policy, rs in by_policy.items():
            values = [float(r["true_value"]) for r in rs]
            entry: dict[str, Any] = {
                "policy": policy,
                "seeds": sorted(int(r["seed"]) for r in rs),
                "true_value": _mean(values),
                "true_value_sd": float(np.std(values, ddof=1)) if len(values) > 1 else None,
                "oracle_share": (means[policy] / oracle) if oracle is not None and oracle > 1e-9 else None,
                "contacts_per_customer_week": _mean([float(r["contacts_per_customer_week"]) for r in rs]),
                "share_contacted": _mean([float(r["share_contacted"]) for r in rs]),
                "unsubscribe_rate": _mean([float(r["unsubscribe_rate"]) for r in rs]),
                "measured": _mean([float(r["measured"]) for r in rs]),
                "measured_halfwidth": _mean(
                    [
                        (float(r["measured_high"]) - float(r["measured_low"])) / 2
                        for r in rs
                        if "measured_high" in r
                    ]
                ),
                "measured_over_true": (
                    _mean([float(r["measured"]) for r in rs]) / means[policy]
                    if abs(means[policy]) > 1e-9
                    else None
                ),
                "seconds": _mean([float(r.get("seconds", 0.0)) for r in rs]),
            }
            for label, other in (("vs_agent", AGENT), ("vs_best_simple", best_simple)):
                if other is None or other not in by_policy or other == policy:
                    entry[label] = None
                    continue
                diff, se, paired = paired_difference(arrays(policy), arrays(other))
                if paired == 0:
                    shared = sorted({int(r["seed"]) for r in rs} & {int(r["seed"]) for r in by_policy[other]})
                    gaps = [
                        float(next(x["true_value"] for x in rs if int(x["seed"]) == s))
                        - float(next(x["true_value"] for x in by_policy[other] if int(x["seed"]) == s))
                        for s in shared
                    ]
                    entry[label] = (
                        {"vs": other, "diff": _mean(gaps), "low": None, "high": None, "seeds": len(gaps)}
                        if gaps
                        else None
                    )
                else:
                    entry[label] = {
                        "vs": other,
                        "diff": diff,
                        "low": None if se is None else diff - Z95 * se,
                        "high": None if se is None else diff + Z95 * se,
                        "seeds": paired,
                    }
            table.append(entry)
        order = {p: i for i, p in enumerate((*SIMPLE_POLICIES, AGENT))}
        table.sort(key=lambda e: (e["policy"] == ORACLE, order.get(e["policy"], 99), e["policy"]))
        out[world] = {
            "policies": table,
            "best_simple": best_simple,
            "oracle": oracle,
            "seeds": sorted({int(r["seed"]) for rs in by_policy.values() for r in rs}),
            "customers": int(next(iter(by_policy.values()))[0]["customers"]),
            "days": sorted({int(r.get("days", HORIZON_DAYS)) for rs in by_policy.values() for r in rs}),
            "refits": {
                policy: {
                    int(r["seed"]): list(r["policy_diagnostics"].get("fits", []))
                    for r in rs
                    if isinstance(r.get("policy_diagnostics"), Mapping)
                }
                for policy, rs in by_policy.items()
                if any(isinstance(r.get("policy_diagnostics"), Mapping) for r in rs)
            },
        }
    return out


def _rub(x: float | None) -> str:
    return "n/a" if x is None or math.isnan(x) else f"{x:,.0f}"


def _diff(d: Mapping[str, Any] | None) -> str:
    if d is None:
        return "-"
    if d["low"] is None:
        return f"{d['diff']:+,.0f}"
    return f"{d['diff']:+,.0f} [{d['low']:+,.0f}, {d['high']:+,.0f}]"


def _value(e: Mapping[str, Any]) -> str:
    sd = e.get("true_value_sd")
    return _rub(e["true_value"]) if sd is None else f"{_rub(e['true_value'])} (sd {_rub(sd)})"


def _num(v: Any) -> str:
    return "n/a" if v is None else f"{float(v):.3g}"


def _refit_lines(refits: Mapping[str, Mapping[int, Sequence[Mapping[str, Any]]]]) -> list[str]:
    """One line per policy and seed that reports its refits: day, what the gate chose, its lower bound and the share
    of customers the model would have contacted."""
    lines: list[str] = []
    for policy, by_seed in refits.items():
        for seed, fits in sorted(by_seed.items()):
            if not fits:
                continue
            parts = [
                f"day {f.get('day')}: {f.get('chose')} (lb {_num(f.get('lb'))}, model contacts "
                f"{_num(f.get('model_contact'))})"
                for f in fits
            ]
            lines.append(f"- `{policy}` seed {seed} refits: " + "; ".join(parts))
    return lines


def render_markdown(summary: Mapping[str, Mapping[str, Any]]) -> str:
    """One table per world, in the order of `WORLDS`."""
    lines: list[str] = []
    for world in [w for w in WORLDS if w in summary] + [w for w in summary if w not in WORLDS]:
        block = summary[world]
        days = block.get("days") or [HORIZON_DAYS]
        span = " or ".join(str(d) for d in days)
        lines.append(f"### {world}")
        lines.append("")
        lines.append(
            f"{block['customers']:,} customers, seeds {block['seeds']}, {span} decision days. True value in RUB of "
            "every contact made, each counted in full (its 14-day effect and its 30-day payback, even past the last "
            f"decision day); mean of the seeds, sd between seeds in brackets. Best simple policy here: "
            f"`{block['best_simple']}`."
        )
        lines.append("")
        lines.append(
            "| Policy | True value | Share of oracle | vs agent (95%) | vs best simple (95%) | "
            "Contacts / cust-wk | Unsub | Measured at the last day (± 95% half-width) |"
        )
        lines.append("|---|---:|---:|---|---|---:|---:|---:|")
        for e in block["policies"]:
            share = "-" if e["oracle_share"] is None else f"{e['oracle_share']:.0%}"
            half = e["measured_halfwidth"]
            band = "" if math.isnan(half) else f" ± {half:,.0f}"
            lines.append(
                f"| {e['policy']} | {_value(e)} | {share} | {_diff(e['vs_agent'])} | "
                f"{_diff(e['vs_best_simple'])} | {e['contacts_per_customer_week']:.2f} | "
                f"{e['unsubscribe_rate']:.1%} | {_rub(e['measured'])}{band} |"
            )
        refits = _refit_lines(block.get("refits") or {})
        if refits:
            lines.append("")
            lines.extend(refits)
        lines.append("")
    return "\n".join(lines)
