"""Family D, stage 3: the IPW warm start on logged, NON-randomised campaigns (KDD Cup 98, bank marketing).

Autonomy's warm start (`AgentPolicy._warm_start`, `autonomy.learning.observations.logged_sends`,
`ipw_weights`, `weighted_rate`) learns click rates from a client's past campaigns, weighting each
past send by 1 / its `attrs["logged_probability"]` (clipped at 20). On a randomised campaign that
probability is known. Here it is not: who was mailed (KDD Cup 98) or how a client was called
(bank) was chosen by the business. So the probability has to be **estimated** from what was known
before each contact, and every conclusion rests on "no unmeasured confounding", which these data
cannot check. What this runner can show honestly:

1. **Targeting bias in the raw history**: how different the people contacted were.
2. **Overlap (positivity)**: how many contacts had an estimated probability near 0 or 1, where IPW
   cannot correct anything.
3. **Whether IPW moves the estimate, and how far.** KDD Cup 98 has one reference point: the June
   1997 mailing (97NK) went to *every* donor in the file, so its response rate is a "mail
   everyone" rate for that population at that moment. It is not the same estimand as the history's
   rates (other appeals, other years, and a population selected on giving in 1995-96), so it can
   show how far a warm start's prior is from today's truth, not whether IPW is right.
4. **What the product does with each variant**, read from the real policy: history as a customer
   would load it (sends + gifts), with responses also written as clicks, and with the estimated
   probabilities as `logged_probability`.

All propensity and outcome models are fitted on a **train half split by customer** (seeded hash of
the id) and scored on the test half.

Run (from the worktree root):

    python3 -P scripts/v2_realdata/d_ipw.py kdd98
    python3 -P scripts/v2_realdata/d_ipw.py bank_marketing
    python3 -P scripts/v2_realdata/d_ipw.py --all

Results: /home/user/datasets/_results/d/ipw/<dataset>.json
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parents[1])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from d_adapters import KDD_PROMOS, bank_dates, read_kdd98, yymm  # noqa: E402
from d_common import DATASETS, Clock, command_line, env_info, peak_rss_mb, write_result  # noqa: E402

from autonomy.contracts import (  # noqa: E402
    Audience,
    Brief,
    Channel,
    ContentItem,
    DecisionContext,
    Envelope,
    Event,
    EventType,
    Objective,
    stable_id,
)
from autonomy.contracts.content import ContentStatus, Theme  # noqa: E402
from autonomy.data.folder import StateFolder  # noqa: E402
from autonomy.data.rebuild import rebuild_states  # noqa: E402
from autonomy.fakes import InMemoryEventStore, InMemoryStateStore  # noqa: E402
from autonomy.learning.observations import ipw_weights, logged_sends, weighted_rate  # noqa: E402
from autonomy.proof.ope import evaluate  # noqa: E402
from autonomy.runtime.agent import ClickResponseModel, build_agent_policy  # noqa: E402

CLIP = 20.0  # the warm start's clip (DEFAULT_LOGGED_IPW_CLIP)


# --------------------------------------------------------------------------- small models


def in_train(ids: pd.Series, seed: str = "d-ipw") -> np.ndarray:
    """Half the customers, by a salted hash of the id: the same customer is always on one side."""
    return np.array([int(hashlib.sha256(f"{seed}:{i}".encode()).hexdigest()[:8], 16) % 2 == 0 for i in ids])


def fit_logistic(
    x: np.ndarray, y: np.ndarray, lam: float = 1.0, iters: int = 30
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Ridge logistic regression by Newton steps on standardised features (intercept unpenalised)."""
    mu, sd = x.mean(0), x.std(0)
    sd[sd == 0] = 1.0
    z = np.column_stack([np.ones(len(x)), (x - mu) / sd])
    w = np.zeros(z.shape[1])
    pen = np.full(z.shape[1], lam)
    pen[0] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(z @ w, -30, 30)))
        g = z.T @ (p - y) + pen * w
        h = (z * (p * (1 - p))[:, None]).T @ z + np.diag(pen)
        step = np.linalg.solve(h, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return w, mu, sd


def predict_logistic(model: tuple[np.ndarray, np.ndarray, np.ndarray], x: np.ndarray) -> np.ndarray:
    w, mu, sd = model
    z = np.column_stack([np.ones(len(x)), (x - mu) / sd])
    probability: np.ndarray = 1 / (1 + np.exp(-np.clip(z @ w, -30, 30)))
    return probability


def auc(score: np.ndarray, y: np.ndarray) -> float | None:
    y = y.astype(bool)
    if y.all() or (~y).all():
        return None
    ranks = pd.Series(score).rank().to_numpy()
    n1 = y.sum()
    return float((ranks[y].sum() - n1 * (n1 + 1) / 2) / (n1 * (~y).sum()))


def hajek(y: np.ndarray, w: np.ndarray) -> float:
    return float((w * y).sum() / w.sum()) if w.sum() > 0 else float("nan")


def overlap(p: np.ndarray) -> dict[str, Any]:
    return {
        "n": len(p),
        "p_quantiles_1_10_50_90_99": [
            round(float(np.quantile(p, q)), 4) for q in (0.01, 0.1, 0.5, 0.9, 0.99)
        ],
        "share_below_0.05": round(float((p < 0.05).mean()), 4),
        "share_above_0.95": round(float((p > 0.95).mean()), 4),
        "share_weight_clipped_at_20": round(float((1 / p > CLIP).mean()), 4),
    }


# --------------------------------------------------------------------------- the product's warm start


def product_warm_start(history: list[Event], now: datetime, channel: Channel) -> dict[str, Any]:
    """Hand `history` to a fresh agent policy the way the runtime should (nightly before the first
    decision, then `prepare_day`), and read back what its click model learned."""
    t0 = time.perf_counter()
    store = InMemoryEventStore()
    store.append(history)
    states_store = InMemoryStateStore()
    rebuild_states(store, states_store, StateFolder(), now)
    states = list(states_store.scan())
    brief = Brief(
        brief_id="b_ipw",
        goal_id="g_ipw",
        objective=Objective.CONVERSION,
        audience=Audience(description="everyone"),
        envelope=Envelope(channels=(channel,), budget=1000.0),
        created_at=now,
        approved_by="runner",
        approved_at=now,
    )
    content = [
        ContentItem(
            content_id="c_ipw",
            goal_id="g_ipw",
            channel=channel,
            theme=Theme.REMINDER,
            offer_id="none",
            body="Please give again",
            status=ContentStatus.APPROVED,
            created_at=now,
        )
    ]
    policy = build_agent_policy([brief], content)
    by_cust: dict[str, list[Event]] = {}
    for e in history:
        by_cust.setdefault(e.customer_id, []).append(e)
    policy.nightly([], by_cust, now)
    policy.prepare_day(states, DecisionContext(now=now, briefs=(brief,), content=tuple(content)))
    model = policy.bundle.response
    assert isinstance(model, ClickResponseModel)  # the default bundle's model, which keeps click rates
    sends = logged_sends(by_cust, {})
    keys = sorted({s.action_key() for s in sends})
    seg_of = {s.customer_id: policy.segment(s, now) for s in states}
    segs = sorted(set(seg_of.values()))
    rates: dict[str, dict[str, float]] = {}
    for k in keys:
        rates[k] = {"*pooled*": round(model.click_rate("*", k).mean, 5)}  # ANY
        for sg in segs:
            rates[k][sg] = round(model.click_rate(sg, k).mean, 5)
    return {
        "history_events": len(history),
        "history_sends_used": policy.history_used,
        "logged_sends": len(sends),
        "with_propensity": sum(1 for s in sends if s.propensity is not None),
        "clicked": sum(1 for s in sends if s.clicked),
        "segments": dict(Counter(seg_of.values())),
        "learned_click_rate": rates,
        "seconds": round(time.perf_counter() - t0, 2),
        "_seg_of": seg_of,
    }


def _send(cid: str, ts: datetime, did: str, fmt: str, p: float | None, source: str) -> Event:
    attrs: dict[str, Any] = {"format": fmt}
    if p is not None:
        attrs["logged_probability"] = float(p)
    return Event(
        event_id=stable_id("evt", source, "send", did),
        customer_id=cid,
        type=EventType.SEND,
        ts=ts,
        source=source,
        channel=Channel.WEBHOOK,
        decision_id=did,
        attrs=attrs,
    )


def _click(cid: str, ts: datetime, did: str, source: str) -> Event:
    return Event(
        event_id=stable_id("evt", source, "click", did),
        customer_id=cid,
        type=EventType.CLICK,
        ts=ts,
        source=source,
        channel=Channel.WEBHOOK,
        decision_id=did,
        attrs={"invented": "response written as a click"},
    )


def _buy(cid: str, ts: datetime, did: str, value: float, source: str) -> Event:
    return Event(
        event_id=stable_id("evt", source, "buy", did),
        customer_id=cid,
        type=EventType.PURCHASE,
        ts=ts,
        source=source,
        value=value,
    )


# --------------------------------------------------------------------------- KDD Cup 98


def kdd_panel(raw: pd.DataFrame) -> pd.DataFrame:
    """One row per (donor, promotion 3..24): mailed?, gave?, and what was known before it.

    "Known before" means dated before the promotion's usual mailing month `t`: a mailing counts if
    its ADATE is before `t`, a gift if its RDATE is (a gift with no RDATE is left out, since nobody
    can say when it was known). An earlier version counted every gift to an earlier promotion,
    including the 25% of "gave to the previous appeal" gifts that arrived after this appeal was
    mailed: information the charity did not have when it chose whom to mail.

    `acquired` marks donors whose first-ever gift (FISTDATE) is no later than `t`. The others were
    not donors yet and could not be on a renewal list (43% of unmailed rows, 1% of mailed rows),
    so the IPW analyses use acquired rows only."""
    promos = [k for k in KDD_PROMOS if k != 2]
    sent = {k: yymm(raw[f"ADATE_{k}"]) for k in promos}
    gave_at = {k: yymm(raw[f"RDATE_{k}"]) for k in promos}
    amt = {k: pd.to_numeric(raw[f"RAMNT_{k}"], errors="coerce").fillna(0.0) for k in promos}
    when = {k: sent[k].mode().iloc[0] for k in promos}  # the promotion's usual mailing month
    order = sorted(promos, key=lambda k: when[k])
    first_gift = yymm(raw["FISTDATE"])
    cid = raw.CONTROLN.astype(str).to_numpy()
    rows = []
    for i, k in enumerate(order):
        t = when[k]
        n_mailed = np.zeros(len(raw))
        n_gifts = np.zeros(len(raw))
        last_gift_at = pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns]")
        last_amt = np.zeros(len(raw))
        gave_prev = np.zeros(len(raw))
        for j in order[:i]:
            n_mailed += (sent[j] < t).to_numpy()
            known = ((amt[j] > 0) & sent[j].notna() & gave_at[j].notna() & (gave_at[j] < t)).to_numpy()
            n_gifts += known
            later = known & ~(gave_at[j] <= last_gift_at).fillna(False).to_numpy()
            last_gift_at[later] = gave_at[j][later]
            last_amt = np.where(later, amt[j].to_numpy(), last_amt)
            if j == order[i - 1]:
                gave_prev = known.astype(float)
        months_since = ((t - last_gift_at).dt.days / 30.4).fillna(60.0).clip(upper=60.0).to_numpy()
        mailed = sent[k].notna().to_numpy()
        gave = (amt[k] > 0).to_numpy() & mailed
        rows.append(
            pd.DataFrame(
                {
                    "cid": cid,
                    "promo": KDD_PROMOS[k],
                    "k": k,
                    "when": t,
                    "sent_at": sent[k].to_numpy(),
                    "gift_at": gave_at[k].to_numpy(),
                    "amount": amt[k].to_numpy(),
                    "mailed": mailed.astype(int),
                    "gave": gave.astype(int),
                    "acquired": (first_gift.notna() & (first_gift <= t)).to_numpy(),
                    "x_mailed_before": n_mailed,
                    "x_gifts_before": n_gifts,
                    "x_months_since_gift": months_since,
                    "x_log_last_amount": np.log1p(last_amt),
                    "x_gave_previous_promo": gave_prev,
                    "x_no_gift_yet": (n_gifts == 0).astype(float),
                }
            )
        )
    return pd.concat(rows, ignore_index=True)


X_KDD = [
    "x_mailed_before",
    "x_gifts_before",
    "x_months_since_gift",
    "x_log_last_amount",
    "x_gave_previous_promo",
    "x_no_gift_yet",
]


def run_kdd98(clock: Clock, max_rows: int | None) -> dict[str, Any]:
    with clock.step("read"):
        raw = read_kdd98(max_rows)
    with clock.step("panel"):
        panel = kdd_panel(raw)
    train_ids = set(raw.CONTROLN.astype(str)[in_train(raw.CONTROLN.astype(str))])
    panel["train"] = panel.cid.isin(train_ids)
    out: dict[str, Any] = {"donors": len(raw), "train_donors": len(train_ids), "panel_rows": len(panel)}
    out["mailing_rate_by_promo"] = {p: round(float(g.mailed.mean()), 4) for p, g in panel.groupby("promo")}
    out["gift_dates_before_mailing"] = int(((panel.gift_at < panel.sent_at) & (panel.gave == 1)).sum())
    out["population"] = {
        "rule": "donor-appeal rows whose first-ever gift (FISTDATE) is no later than the appeal month",
        "rows_kept": int(panel.acquired.sum()),
        "unmailed_rows_not_yet_donors": round(float((~panel.acquired[panel.mailed == 0]).mean()), 4),
        "mailed_rows_not_yet_donors": round(float((~panel.acquired[panel.mailed == 1]).mean()), 4),
    }
    pop = panel[panel.acquired]  # the IPW analyses (1-3); the warm start (4) gets every mailing

    # 1. Targeting bias: who was mailed, among donors with the same history length.
    tb = pop.groupby("mailed")[X_KDD].mean().round(3)
    out["targeting_bias_means"] = {f"mailed={m}": tb.loc[m].to_dict() for m in tb.index}

    # 2. Propensity per promotion, fitted on train donors of the acquired population, then scored on
    #    every row of that promotion (the warm-start variant needs a p for every mailing).
    with clock.step("propensity"):
        panel["p_hat"] = np.nan
        fit_report = {}
        for promo, g in panel.groupby("promo"):
            tr = g[g.train & g.acquired]
            model = fit_logistic(tr[X_KDD].to_numpy(float), tr.mailed.to_numpy(float))
            panel.loc[g.index, "p_hat"] = predict_logistic(model, g[X_KDD].to_numpy(float))
            te = panel.loc[g.index[(~g.train & g.acquired).to_numpy()]]
            a_ = auc(te.p_hat.to_numpy(), te.mailed.to_numpy()) if len(te) else None
            fit_report[promo] = {
                "auc_test": None if a_ is None else round(a_, 4),
                "mailed_share": round(float(g.mailed.mean()), 4),
            }
    out["propensity_auc_test_by_promo"] = fit_report
    pop = panel[panel.acquired]
    mailed = pop[pop.mailed == 1]
    out["overlap_mailed_test"] = overlap(mailed[~mailed.train].p_hat.to_numpy())

    # Balance: do the IPW-weighted mailed donors look like everyone (mailed or not) on what the
    # propensity model saw? Standardised differences, test donors, all promotions pooled.
    test_all = pop[~pop.train]
    tm = test_all[test_all.mailed == 1]
    wt = np.minimum(1 / tm.p_hat.to_numpy(), CLIP)
    bal = {}
    for c in X_KDD:
        sd = float(test_all[c].std()) or 1.0
        bal[c] = {
            "everyone": round(float(test_all[c].mean()), 3),
            "mailed_naive_std_diff": round((float(tm[c].mean()) - float(test_all[c].mean())) / sd, 3),
            "mailed_ipw_std_diff": round(
                (float(np.average(tm[c], weights=wt)) - float(test_all[c].mean())) / sd, 3
            ),
        }
    out["covariate_balance_test"] = bal

    # 3. "Gift rate if everyone were mailed" from the history: naive vs IPW (test donors), against
    #    the one everyone-mailed campaign, 97NK (same donors, TARGET_B).
    te = mailed[~mailed.train]
    w = np.minimum(1 / te.p_hat.to_numpy(), CLIP)
    y = te.gave.to_numpy(float)
    nk = te.promo.str.endswith("NK")
    target = raw.set_index(raw.CONTROLN.astype(str)).TARGET_B
    test_target = target[~target.index.isin(train_ids)]
    out["history_gift_rate_test"] = {
        "all_promos": {"naive": round(float(y.mean()), 4), "ipw_hajek": round(hajek(y, w), 4)},
        "nk_promos_only": {
            "naive": round(float(y[nk].mean()), 4),
            "ipw_hajek": round(hajek(y[nk], w[nk]), 4),
        },
        "by_promo": {
            p: {
                "naive": round(float(g.gave.mean()), 4),
                "ipw": round(hajek(g.gave.to_numpy(float), np.minimum(1 / g.p_hat.to_numpy(), CLIP)), 4),
                "mailed": len(g),
            }
            for p, g in te.groupby("promo")
        },
    }
    te_year = te.promo.str[:2]
    out["history_gift_rate_test"]["by_promo_year"] = {
        "19"
        + yr: {
            "naive": round(float(y[(te_year == yr).to_numpy()].mean()), 4),
            "ipw_hajek": round(hajek(y[(te_year == yr).to_numpy()], w[(te_year == yr).to_numpy()]), 4),
        }
        for yr in sorted(te_year.unique())
    }
    out["check_97NK_everyone_mailed_test"] = {
        "donors": len(test_target),
        "response_rate": round(float(test_target.mean()), 4),
    }

    # 4. The product's warm start on the train donors' history, three ways.
    now = datetime(1997, 6, 1, 14, 0, tzinfo=UTC)
    tr = panel[panel.train & (panel.mailed == 1)]

    def history(clicks: bool, probs: bool) -> list[Event]:
        evs: list[Event] = []
        r: Any
        for r in tr.itertuples(index=False):
            did = f"{r.cid}:{r.promo}"
            ts = pd.Timestamp(r.sent_at).to_pydatetime().replace(tzinfo=UTC) + timedelta(hours=14)
            evs.append(_send(r.cid, ts, did, "mail_" + r.promo[2:], r.p_hat if probs else None, "kdd98"))
            if r.gave:
                g_ts = (
                    pd.Timestamp(r.gift_at).to_pydatetime().replace(tzinfo=UTC) + timedelta(days=14)
                    if pd.notna(r.gift_at)
                    else ts + timedelta(days=21)
                )
                g_ts = max(g_ts, ts + timedelta(hours=1))
                evs.append(_buy(r.cid, g_ts, did, float(r.amount), "kdd98"))
                if clicks:
                    evs.append(_click(r.cid, g_ts, did, "kdd98"))
        return [e for e in evs if e.ts < now]

    variants = {}
    for name, (clicks, probs) in {
        "as_loaded_sends_and_gifts": (False, False),
        "gifts_also_as_clicks": (True, False),
        "gifts_as_clicks_plus_estimated_propensity": (True, True),
    }.items():
        with clock.step(f"warm_start:{name}") as s:
            res = product_warm_start(history(clicks, probs), now, Channel.WEBHOOK)
            s.extra = {"events": res["history_events"]}
        seg_of = res.pop("_seg_of")
        variants[name] = res
    out["product_warm_start_train"] = variants

    # The warm start's NK rate per agent segment vs the 97NK response of TRAIN donors in that segment
    # (the segments are the policy's own, as of June 1997; TARGET_B is not used by the warm start).
    nk_key = next(
        (k for k in variants["gifts_also_as_clicks"]["learned_click_rate"] if k.endswith("mail_NK")), None
    )
    seg_series = pd.Series(seg_of)
    tgt_train = target[target.index.isin(train_ids)]
    comp = {}
    for sg in sorted(set(seg_series)):
        ids = seg_series.index[seg_series == sg]
        actual = tgt_train[tgt_train.index.isin(ids)]
        comp[sg] = {
            "donors": len(actual),
            "97NK_response": round(float(actual.mean()), 4) if len(actual) else None,
            "warm_naive": variants["gifts_also_as_clicks"]["learned_click_rate"].get(nk_key, {}).get(sg),
            "warm_ipw": variants["gifts_as_clicks_plus_estimated_propensity"]["learned_click_rate"]
            .get(nk_key, {})
            .get(sg),
        }
    out["segment_check_vs_97NK_train"] = {"action_key": nk_key, "by_segment": comp}

    # 5. OPE of "mail nobody" or "mail half" on 97NK is impossible: everyone was mailed (p = 1).
    out["ope_on_97NK"] = (
        "not possible: every donor was mailed (logged probability 1), so a policy that skips "
        "anyone has no logged rows to be scored on (positivity fails by construction)"
    )
    return out


# --------------------------------------------------------------------------- bank marketing

X_BANK_NUM = [
    "age",
    "previous",
    "emp.var.rate",
    "cons.price.idx",
    "cons.conf.idx",
    "euribor3m",
    "nr.employed",
]
X_BANK_CAT = ["job", "marital", "education", "default", "housing", "loan", "poutcome", "month", "day_of_week"]


def bank_design(raw: pd.DataFrame) -> np.ndarray:
    cats = pd.get_dummies(raw[X_BANK_CAT].astype(str), drop_first=True, dtype=float)
    num = raw[X_BANK_NUM].astype(float)
    num["pdays_known"] = (raw.pdays != 999).astype(float)
    return np.column_stack([num.to_numpy(), cats.to_numpy()])


def run_bank(clock: Clock, max_rows: int | None) -> dict[str, Any]:
    raw = pd.read_csv(DATASETS / "bank_marketing" / "bank-additional-full.csv", sep=";", nrows=max_rows)
    raw["cid"] = [f"bank{i:05d}" for i in range(len(raw))]
    raw["call_at"] = bank_dates(raw)
    raw["year"] = raw.call_at.dt.year
    x = bank_design(raw)
    a = (raw.contact == "cellular").to_numpy(float)
    y = (raw.y == "yes").to_numpy(float)
    tr = in_train(raw.cid)
    te = ~tr
    out: dict[str, Any] = {
        "clients": len(raw),
        "train": int(tr.sum()),
        "test": int(te.sum()),
        "action": "contact type: cellular vs telephone (the only varied choice; everyone was called)",
        "excluded_covariates": [
            "duration (known only after the call)",
            "campaign (calls in this campaign, counted up to and including the last)",
        ],
    }
    out["raw_rates"] = {
        "cellular": round(float(y[a == 1].mean()), 4),
        "telephone": round(float(y[a == 0].mean()), 4),
        "difference": round(float(y[a == 1].mean() - y[a == 0].mean()), 4),
    }
    ct = pd.crosstab([raw.year, raw.month], raw.contact)
    one_sided = ct[(ct.min(axis=1) < 20)]
    out["positivity_by_month"] = {
        "months": len(ct),
        "months_with_under_20_of_one_type": len(one_sided),
        "clients_in_those_months": int(one_sided.sum().sum()),
        "examples": {
            "-".join(str(part) for part in cast(tuple[Any, ...], key)): row.to_dict()
            for key, row in one_sided.head(6).iterrows()
        },
    }
    with clock.step("propensity"):
        pm = fit_logistic(x[tr], a[tr])
        p_cell = predict_logistic(pm, x)
    out["propensity_auc_test"] = round(auc(p_cell[te], a[te]) or float("nan"), 4)
    p_logged = np.where(a == 1, p_cell, 1 - p_cell)
    out["overlap_test_logged_action"] = overlap(p_logged[te])
    with clock.step("outcome_models"):
        q1 = predict_logistic(fit_logistic(x[tr & (a == 1)], y[tr & (a == 1)]), x)
        q0 = predict_logistic(fit_logistic(x[tr & (a == 0)], y[tr & (a == 0)]), x)
    res: dict[str, Any] = {}
    for name, pi in {"all_cellular": a, "all_telephone": 1 - a}.items():
        q_t = q1 if name == "all_cellular" else q0
        q_l = np.where(a == 1, q1, q0)
        for clip in (None, CLIP):
            rep = evaluate(y[te], p_logged[te], pi[te], q_logged=q_l[te], q_target=q_t[te], clip=clip)
            res[f"{name}{'' if clip is None else '_clip20'}"] = {
                "ips": round(rep.ips.value.point, 4),
                "snips": None if rep.snips is None else round(rep.snips.value.point, 4),
                "snips_95": (
                    None
                    if rep.snips is None
                    else [round(rep.snips.value.low, 4), round(rep.snips.value.high, 4)]
                ),
                "dr": None if rep.dr is None else round(rep.dr.value.point, 4),
                "dr_95": (
                    None if rep.dr is None else [round(rep.dr.value.low, 4), round(rep.dr.value.high, 4)]
                ),
                "effective_n": round(rep.snips.effective_n if rep.snips else 0.0, 1),
                "max_weight": round(rep.ips.max_weight, 1),
            }
    out["ope_test"] = res
    dr_c, dr_t = res["all_cellular"]["dr"], res["all_telephone"]["dr"]
    out["adjusted_difference_dr_unclipped"] = None if dr_c is None or dr_t is None else round(dr_c - dr_t, 4)
    c20, t20 = res["all_cellular_clip20"], res["all_telephone_clip20"]
    out["adjusted_difference_clip20"] = {
        "snips": round(c20["snips"] - t20["snips"], 4),
        "dr": round(c20["dr"] - t20["dr"], 4),
    }

    # The product's warm start on the train clients: the call as a send, `y` as a click (invented).
    now = (raw.call_at.max() + pd.Timedelta(days=1)).to_pydatetime().replace(tzinfo=UTC)
    trf = raw[tr]

    def history(probs: bool) -> list[Event]:
        evs: list[Event] = []
        r: Any
        for r, p in zip(trf.itertuples(index=False), p_logged[tr], strict=True):
            ts = r.call_at.to_pydatetime().replace(tzinfo=UTC)
            did = f"{r.cid}:call"
            evs.append(_send(r.cid, ts, did, r.contact, p if probs else None, "bank"))
            if r.y == "yes":
                evs.append(_click(r.cid, ts + timedelta(hours=1), did, "bank"))
        return evs

    variants = {}
    for name, probs in {"y_as_click": False, "y_as_click_plus_estimated_propensity": True}.items():
        with clock.step(f"warm_start:{name}"):
            r = product_warm_start(history(probs), now, Channel.WEBHOOK)
        r.pop("_seg_of")
        variants[name] = r
    out["product_warm_start_train"] = variants
    w = np.minimum(1 / p_logged[tr], CLIP)
    out["numpy_check_train"] = {
        "naive": {
            "cellular": round(float(y[tr & (a == 1)].mean()), 4),
            "telephone": round(float(y[tr & (a == 0)].mean()), 4),
        },
        "ipw_hajek_clip20": {
            "cellular": round(hajek(y[tr & (a == 1)], w[a[tr] == 1]), 4),
            "telephone": round(hajek(y[tr & (a == 0)], w[a[tr] == 0]), 4),
        },
        "product_weighted_rate_cellular": [
            round(v, 4)
            for v in weighted_rate(
                [bool(v) for v in y[tr & (a == 1)]], ipw_weights(list(p_logged[tr & (a == 1)]))
            )
        ],
    }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("datasets", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--max-rows", type=int, default=None, help="first N rows only (a quick check)")
    args = ap.parse_args(argv)
    names = ["bank_marketing", "kdd98"] if args.all else args.datasets
    for n in names:
        clock = Clock()
        t0 = time.perf_counter()
        res = (run_kdd98 if n == "kdd98" else run_bank)(clock, args.max_rows)
        res.update(
            command=command_line(),
            env=env_info(),
            steps=clock.as_list(),
            peak_rss_mb=round(peak_rss_mb(), 1),
            max_rows=args.max_rows,
        )
        path = write_result("ipw", n if args.max_rows is None else f"{n}_first{args.max_rows}", res)
        print(f"{n}: {time.perf_counter() - t0:.1f}s -> {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
