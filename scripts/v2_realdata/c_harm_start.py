"""Family C probe: where `ContactHarm.data_start` lands against the agent's start, per goal.

Same flow as `c_sweep.py --nightly` (synthetic customers copied from real Online Retail II
customers; consent, margin and outcomes invented), with each goal's contact-harm model inspected
after the launch tick and after the nightly step: `data_start`, `split_at`, how many sends the
evidence counted, and how many of each goal's agent sends fall before its customers' first
purchase after launch (the moment `data_start` is taken from). Nothing about decision quality can
be read from it; it checks a mechanism.

Run:

    python3 -P scripts/v2_realdata/c_harm_start.py --customers 2000
    python3 -P scripts/v2_realdata/c_harm_start.py --customers 2000 --history
    python3 -P scripts/v2_realdata/c_harm_start.py --customers 10000 --history

Results: /home/user/datasets/_results/c/harm_start/harm_start_<N>[_history].json
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parents[1])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import c_sweep as C  # noqa: E402, N812
from c_common import command_line, env_info, write_result  # noqa: E402

from autonomy.contracts import DecisionKind, EventType, Group  # noqa: E402
from autonomy.data.folder import StateFolder  # noqa: E402
from autonomy.decide.bidders import contact_harm_of  # noqa: E402
from autonomy.proof.split import group_assigner  # noqa: E402
from autonomy.runtime.agent import AgentPolicy  # noqa: E402
from autonomy.runtime.service import AutonomyRuntime, RuntimeConfig, TickReport  # noqa: E402
from autonomy.runtime.settings import workspace_policy_factory  # noqa: E402


def harm_view(rt: AutonomyRuntime) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for gid, (_, pol) in sorted(rt._policies.items()):
        assert isinstance(pol, AgentPolicy), "the workspace policy factory builds an AgentPolicy"
        h = contact_harm_of(pol.bundle.response, create=False)
        if h is None:
            out[gid] = None
            continue
        ev = list(h.evidence.values())
        out[gid] = {
            "data_start": h.data_start,
            "split_at": h.split_at,
            "data_start_after_split": bool(h.data_start and h.split_at and h.data_start > h.split_at),
            "records": len(ev),
            "sends_counted": round(sum(e.sends for e in ev), 1),
            "exposed_days": round(sum(e.exposed_days for e in ev), 1),
            "open_days": round(sum(e.open_days for e in ev), 1),
            "describe": h.describe()[:200] if h.fitted else None,
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--customers", type=int, default=2000)
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--seed", type=int, default=11)
    args = parser.parse_args(argv)
    n, seed = args.customers, args.seed
    states = C.synth_states(n, C.NOW, seed)
    events, ss, dl, ws, _ = C.stores("memory", n)
    if args.history:
        events.append(C.history_events(states, seed))
    else:
        ss.put(states)
    C.make_goals(ws, C.NOW)
    rt = AutonomyRuntime(
        events=events,
        states=ss,
        decisions=dl,
        workspace=ws,
        folder=StateFolder(),
        policy_factory=workspace_policy_factory(ws),
        assign_group=group_assigner(C.SPLIT_SALT, holdout_share=0.1, baseline_share=0.1),
        config=RuntimeConfig(timezone="UTC", holdout_share=0.1, baseline_share=0.1, run_send_jobs=False),
    )
    rep = rt.tick(C.NOW)
    after_tick = harm_view(rt)
    acts = [d for d in rep.decisions if d.kind is DecisionKind.ACT and d.group is Group.AGENT and d.action]
    outs = C.outcome_events(rep, {x.customer_id: x for x in rt.states.scan()}, C.NOW, seed)
    rt.events.append(outs)
    owner: dict[str, set[str]] = {}
    for d in acts:
        assert d.action is not None
        owner.setdefault(d.customer_id, set()).add(d.action.goal_id)
    sends: dict[str, Any] = {}
    for gid in sorted({d.action.goal_id for d in acts if d.action}):
        buys = sorted(
            e.ts for e in outs if e.type is EventType.PURCHASE and gid in owner.get(e.customer_id, ())
        )
        ts = [max(d.action.send_at, C.NOW) for d in acts if d.action and d.action.goal_id == gid]
        sends[gid] = {
            "agent_sends": len(ts),
            "first_purchase_after_launch": buys[0] if buys else None,
            "sends_before_it": sum(1 for t in ts if buys and t < buys[0]),
        }
    later = (C.NOW + timedelta(days=15)).replace(hour=3)
    rt.cursor.nightly_on = None
    rt._start_day(later.date())
    rt._nightly(later, later, TickReport(now=later))
    payload = {
        "customers": n,
        "history_in_log": args.history,
        "command": command_line(),
        "env": env_info(),
        "synthetic": "customers copied from real Online Retail II customers; consent, margin and outcomes invented",
        "launch": C.NOW,
        "harm_after_launch_tick": after_tick,
        "sends_by_goal": sends,
        "harm_after_nightly": harm_view(rt),
    }
    path = write_result("harm_start", f"harm_start_{n}" + ("_history" if args.history else ""), payload)
    for gid, v in payload["harm_after_nightly"].items():
        print(
            gid,
            v and {k: v[k] for k in ("data_start", "data_start_after_split", "sends_counted")},
            sends.get(gid),
        )
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
