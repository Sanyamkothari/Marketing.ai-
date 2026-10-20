"""Open Bandit (ZOZOTOWN) samples: check the repo's OPE on real logged propensities.

    python3 -I scripts/v2_realdata/a_open_bandit.py

The four sample files shipped with zr-obp (10,000 rows each: random/all, random/men, bts/all,
bts/men) are read with the repo reader `autonomy.proof.replay.read_open_bandit`. Autonomy's agent
cannot be run on this data: its actions are channel x offer x content (x item only through a
refill or recommender lookup), not "which of 80 items in which of 3 slots", so there is no way to
ask it for a probability per item. What can be checked is the evaluation machinery itself:

1. On-policy truth vs off-policy estimate: the CTR the uniform-random policy actually got (the
   `random` log's mean click) against the IPS / SNIPS / DR estimate of that same uniform policy
   computed from the `bts` (Thompson sampling) log of the same campaign, using bts's logged
   propensities. The logs cover the same week but different traffic, so agreement within the
   interval is the check, not equality.
2. The repo's own `replay` (fit a segment policy on half of the random log, score it on the other
   half), to show how far 10,000 rows go with 80 or 34 actions.

Results: /home/user/datasets/_results/a_open_bandit.json.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import a_common as C  # noqa: N812

from autonomy.proof.replay import evaluate, read_open_bandit, replay

ROOT = C.DATASETS / "open_bandit_sample"


def est(e: object) -> dict[str, float] | None:
    return None if e is None else {"point": e.point, "low": e.low, "high": e.high}  # type: ignore[attr-defined]


def main() -> None:
    payload: dict[str, object] = {
        "dataset": "open_bandit_sample",
        "command": "python3 -I scripts/v2_realdata/a_open_bandit.py",
    }
    for camp in ("all", "men"):
        rnd = read_open_bandit(ROOT / f"random_{camp}_{camp}.csv", behavior_policy="random", campaign=camp)
        bts = read_open_bandit(ROOT / f"bts_{camp}_{camp}.csv", behavior_policy="bts", campaign=camp)
        k = rnd.n_actions
        truth = float(rnd.reward.mean())
        se = float(rnd.reward.std(ddof=1) / np.sqrt(rnd.n_rows))
        # the logged propensity is P(item | slot); a uniform policy over items gives 1/k to each
        uni = evaluate(
            lambda lg: np.full((lg.n_rows, lg.n_actions), 1.0 / lg.n_actions),
            bts,
            policy_name="uniform random",
        )
        print(
            f"\n[{camp}] {k} items; random log CTR {truth:.5f} (+/- {1.96 * se:.5f}); bts log CTR {bts.reward.mean():.5f}"
        )
        print(
            f"  uniform policy estimated from the bts log: IPS {uni.ips.point:.5f} [{uni.ips.low:.5f}, {uni.ips.high:.5f}]"
            f"  SNIPS {uni.snips.point if uni.snips else float('nan'):.5f}  DR {uni.dr.point:.5f} [{uni.dr.low:.5f}, {uni.dr.high:.5f}]"
            f"  ESS {uni.effective_sample_size:.0f} of {bts.n_rows}, max weight {uni.max_weight:.1f}"
        )
        rep = replay(rnd, segment_columns=["position"], test_share=0.5, seed=0)
        print(f"  repo replay on the random log: {rep.summary}")
        payload[camp] = {
            "n_items": k,
            "random_log_ctr": truth,
            "random_log_ctr_ci95": [truth - 1.96 * se, truth + 1.96 * se],
            "bts_log_ctr": float(bts.reward.mean()),
            "bts_propensity_min_max": [float(bts.propensity.min()), float(bts.propensity.max())],
            "uniform_from_bts": {
                "ips": est(uni.ips),
                "snips": est(uni.snips),
                "dr": est(uni.dr),
                "ess": uni.effective_sample_size,
                "max_weight": uni.max_weight,
            },
            "replay_random_log": {
                "summary": rep.summary,
                "dr": est(rep.target.dr),
                "dr_vs_logging": est(rep.target.dr_vs_logging),
                "ess": rep.target.effective_sample_size,
                "segments": len(rep.policy.table),
            },
        }
    C.save("a_open_bandit", payload)


if __name__ == "__main__":
    main()
