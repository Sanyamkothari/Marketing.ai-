"""Turn the per-run rows of the waves benchmark into one table per world.

Each CI job writes a JSON with one row per seed (`runner.run_one`) and an `.npz` with each seed's true value per
customer. `summarize` joins any number of them:

* **True value** is the mean over seeds. **Share of the oracle** is that mean over the oracle's mean in the same
  world (the oracle is greedy per contact, so it is a ceiling in practice, not a proof of optimality).
* **Paired differences** (against `agent`, against the best simple policy) use the same customers and the same
  luck in every policy (common random numbers), so they are differences of per-customer values: for each seed the
  standard error is that of the summed per-customer differences, and seeds are combined as independent worlds.
  Without the per-customer arrays the difference is of the means and carries no interval.
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

from autonomy.gym.waves.config import WORLDS
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
    """(mean over seeds of the summed per-customer difference a - b, its standard error, seeds paired)."""
    seeds = sorted(set(a) & set(b))
    totals: list[float] = []
    variances: list[float] = []
    for s in seeds:
        d = a[s] - b[s]
        totals.append(float(d.sum()))
        variances.append(float(d.size * d.var(ddof=1)) if d.size > 1 else 0.0)
    if not seeds:
        return float("nan"), None, 0
    return _mean(totals), math.sqrt(sum(variances)) / len(seeds), len(seeds)


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


def render_markdown(summary: Mapping[str, Mapping[str, Any]]) -> str:
    """One table per world, in the order of `WORLDS`."""
    lines: list[str] = []
    for world in [w for w in WORLDS if w in summary] + [w for w in summary if w not in WORLDS]:
        block = summary[world]
        lines.append(f"### {world}")
        lines.append("")
        lines.append(
            f"{block['customers']:,} customers, seeds {block['seeds']}. True value in RUB over 84 days plus the 30-day tail, "
            f"mean of the seeds. Best simple policy here: `{block['best_simple']}`."
        )
        lines.append("")
        lines.append(
            "| Policy | True value | Share of oracle | vs agent (95%) | vs best simple (95%) | "
            "Contacts / cust-wk | Unsub | Measured at day 84 |"
        )
        lines.append("|---|---:|---:|---|---|---:|---:|---:|")
        for e in block["policies"]:
            share = "-" if e["oracle_share"] is None else f"{e['oracle_share']:.0%}"
            ratio = "" if e["measured_over_true"] is None else f" ({e['measured_over_true']:.1f}x true)"
            lines.append(
                f"| {e['policy']} | {_rub(e['true_value'])} | {share} | {_diff(e['vs_agent'])} | "
                f"{_diff(e['vs_best_simple'])} | {e['contacts_per_customer_week']:.2f} | "
                f"{e['unsubscribe_rate']:.1%} | {_rub(e['measured'])}{ratio} |"
            )
        lines.append("")
    return "\n".join(lines)
