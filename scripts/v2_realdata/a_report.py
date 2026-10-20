"""Render the family-A result JSONs as markdown tables (so no number in the notes is typed by hand).

    python3 -I scripts/v2_realdata/a_report.py > /tmp/a_tables.md

Reads /home/user/datasets/_results/a_*.json written by the a_*.py runners.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

RESULTS = Path("/home/user/datasets/_results")


def ci(e: dict[str, float] | None, scale: float = 1.0, digits: int = 4) -> str:
    if not e:
        return "-"
    f = f"{{:+.{digits}f}}"
    return f"{f.format(e['point'] * scale)} [{f.format(e['low'] * scale)}, {f.format(e['high'] * scale)}]"


def table(rows: list[dict[str, Any]], *, scale: float = 1.0, digits: int = 4, ref: bool = True) -> str:
    head = "| Policy | Contacted | IPS | SNIPS | DR | DR gain vs send-to-none [95% CI] |"
    sep = "|---|---:|---:|---:|---:|---:|"
    if ref:
        head += " DR gain vs best blanket arm (chosen on train) [95% CI] |"
        sep += "---:|"
    out = [head, sep]
    f = f"{{:.{digits}f}}"
    for r in rows:
        sn = f.format(r["snips"]["point"] * scale) if r["snips"] else "-"
        line = (
            f"| {r['policy']} | {r['contact_share']:.1%} | {f.format(r['ips']['point'] * scale)} | {sn} | "
            f"{f.format(r['dr']['point'] * scale)} | {ci(r['dr_vs_none'], scale, digits)} |"
        )
        if ref:
            line += f" {ci(r.get('dr_vs_reference'), scale, digits)} |"
        out.append(line)
    return "\n".join(out)


def load(name: str) -> dict[str, Any] | None:
    p = RESULTS / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def hillstrom_content_vs_random() -> None:
    """Paired DR: the click agent's men's/women's pick vs a random pick, from the saved matrices."""
    import sys

    import numpy as np
    import pandas as pd

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import a_common as C  # noqa: N812

    npz = RESULTS / "a_hillstrom_cost_0.001_pi.npz"
    if not npz.exists():
        return
    f = pd.read_csv(C.DATASETS / "hillstrom" / "hillstorm_no_indices.csv.gz")
    arms = ("No E-Mail", "Mens E-Mail", "Womens E-Mail")
    arm = f["segment"].map({a: i for i, a in enumerate(arms)}).to_numpy()
    rct = C.Rct(
        "hillstrom",
        np.arange(len(f)).astype(str),
        arm,
        np.full(len(f), 1 / 3),
        arms,
        f[["recency"]].reset_index(drop=True),
        {"spend": f["spend"].to_numpy(float)},
    )
    _tr, te = C.split_customers(len(f), seed=0)
    test = rct.take(te)
    pi = np.load(npz)["AGENT_(click_model)"]
    send = pi[:, 1:].sum(axis=1)
    pick = np.zeros_like(pi)
    pick[:, 1:] = np.where(send[:, None] > 0, pi[:, 1:] / np.maximum(send[:, None], 1e-12), 0.5)
    rand = np.tile([0.0, 0.5, 0.5], (test.n, 1))
    for cost in (0.001,):  # both policies e-mail everyone, so the cost cancels
        prof = 0.4 * test.outcomes["spend"] - cost * (test.arm != 0)
        row = C.evaluate_policies(test, prof, "profit", {"agent pick": pick}, reference=rand)[0]
        print(
            f"- Hillstrom, cost {cost}: the click agent's content pick vs a random pick (both e-mail everyone): "
            f"{ci(row['dr_vs_reference'], digits=4)}"
        )
    print()


def main() -> None:
    hillstrom_content_vs_random()
    h = load("a_hillstrom")
    if h:
        print("## Hillstrom\n")
        print(f"Command: `{h['command']}`\n")
        for block in h["economics"].values():
            for rname in ("conversion", "spend", "profit", "content_choice_profit"):
                if rname in block:
                    title = block["economics"] if "profit" in rname else rname
                    print(f"### {rname} ({title})\n")
                    print(table(block[rname], digits=4))
                    print()
            for v, a in block["agent"].items():
                print(
                    f"- agent {v}: {a['kinds']}, arms {a['arm_counts']}, mean expected net of its sends "
                    f"{a['mean_expected_net_of_chosen_sends']:.3f}, {a['ms_per_decision']} ms/decision"
                )
            print(f"- segment rule columns chosen on validation: {block['segment_rule_columns']}\n")
    x = load("a_x5") or load("a_x5_n100000")
    if x:
        print("## X5\n")
        print(f"Command: `{x['command']}`\n")
        for rname in ("target", "profit"):
            print(f"### {rname}\n")
            print(table(x[rname], digits=4 if rname == "target" else 3))
            print()
        for v, a in x["agent"].items():
            print(
                f"- agent {v}: {a['kinds']}, mean expected net of its sends {a['mean_expected_net_of_chosen_sends']}, "
                f"{a['ms_per_decision']} ms/decision"
            )
        print()
    for name in ("megafon", "starbucks", "lenta", "criteo"):
        d = load(f"a_{name}")
        if not d:
            continue
        print(f"## {name}\n")
        print(
            f"Command: `{d['command']}`; rows {d['rows']}, treated share {d['treated_share']:.4f}, "
            f"break-even cost {d['break_even_cost']:.4f}\n"
        )
        for rname, block in d.items():
            if isinstance(block, dict) and "full_test" in block:
                print(f"### {rname}, full test split\n")
                print(table(block["full_test"], digits=4))
                print(f"\n### {rname}, agent sample\n")
                print(table(block["agent_sample"], digits=4))
                print()
        for v, a in d["agent"].items():
            print(f"- agent {v}: {a['kinds']}, {a['ms_per_decision']} ms/decision")
        print()


if __name__ == "__main__":
    main()
