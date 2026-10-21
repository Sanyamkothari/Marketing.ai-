"""Fit the Gym's real-calibrated scenarios to the dev datasets' TRAIN halves.

    python -m scripts.v2_realbench.gym_fit --data-dir /home/user/datasets            # print the fits
    python -m scripts.v2_realbench.gym_fit --data-dir /home/user/datasets --check    # compare with scenarios.py
    python -m scripts.v2_realbench.gym_fit --data-dir /home/user/datasets --literal  # Python to paste

`real_email_like` is fitted to the Hillstrom e-mail campaign (men's, women's, no e-mail), `real_sms_like`
to the X5 RetailHero SMS campaign (SMS, no SMS). Both use the bench's own train/test split (BLAKE2b of
the customer id, `SPLIT_SALT`): the fit sees TRAIN customers only, so the Gym never encodes anything
about the test half the scoreboard is judged on. The sealed datasets (lenta, starbucks) are not touched.

The estimators are generic and live in `autonomy/gym/calibration.py` (no dataset names in `autonomy/`);
this script is the only place that knows which column is which. The fitted numbers are pasted as
literals into `autonomy/gym/scenarios.py`; `--check` refits and fails if they have drifted.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from autonomy.contracts import Channel
from autonomy.gym.calibration import CampaignFit, fit_campaign, fit_summary
from scripts.v2_realbench.adapters import SAMPLE_SALT, SIZES, hillstrom_frame
from scripts.v2_realbench.common import SPLIT_SALT, Timings, pick_by_hash, split_by_hash

WINDOW_DAYS = 14
"""Hillstrom's outcomes are measured over two weeks. X5's window is not stated in the data; the bench
books a window purchase 3.5 days after the send inside a 14-day proof window, so 14 is used there."""
RUB_PER_USD = 65.5
"""Unit conversion only (the average rate in March 2019), so the Gym's dollar-denominated offers and
complaint cost sit in a sensible ratio to a grocery basket. Not a fitted quantity."""
FATIGUE_UNFITTED = 0.25
"""The Gym's default fatigue per weekly contact. A single-send randomised campaign cannot identify it."""
RECENT_MONTHS = 3
"""Hillstrom: an existing customer who bought within this many months is `recent`, else `lapsed`.
Fixed in advance (a quarter), not tuned."""

HILLSTROM_FILE = ("hillstrom", "hillstorm_no_indices.csv.gz")
HILLSTROM_ARMS = ("No E-Mail", "Mens E-Mail", "Womens E-Mail")

NOT_FITTED_COMMON = (
    "contact fatigue: a single-send randomised campaign has no repeated contacts, so it is not "
    "identifiable; the Gym's library value is kept (fatigue_per_weekly_contact 0.25)",
    "unsubscribe and complaint rates: the data log none; the Gym's library values are kept",
    "send-time effect: no send-time variation in the data; hour sensitivity is set to 0 (no effect)",
    "offers: no offer variation in the data; an offer multiplies the lift by 1 and still costs its "
    "redemption cost",
    "sleeping dogs: not identifiable from a single send; none are planted",
    "customer-to-customer spread inside a group: the Gym's lognormal spread is kept",
)


def _train_mask(ids: np.ndarray) -> np.ndarray:
    train, _ = split_by_hash(ids, salt=SPLIT_SALT)
    mask = np.zeros(len(ids), dtype=bool)
    mask[train] = True
    return mask


def fit_email_like(data_dir: Path) -> CampaignFit:
    """The Hillstrom e-mail campaign, TRAIN half by the bench's split."""
    frame = hillstrom_frame(data_dir / HILLSTROM_FILE[0] / HILLSTROM_FILE[1])
    ids = frame["customer_id"].to_numpy()
    train = _train_mask(ids)
    arm = np.asarray(frame["segment"].map({n: i for i, n in enumerate(HILLSTROM_ARMS)}), dtype=np.int64)
    newbie = frame["newbie"].to_numpy() == 1
    recent = frame["recency"].to_numpy() <= RECENT_MONTHS
    group = np.where(newbie, "new", np.where(recent, "recent", "lapsed"))
    spend = frame["spend"].to_numpy(dtype=np.float64)
    buyers = train & (spend > 0)
    return fit_campaign(
        arm=arm,
        bought=frame["conversion"].to_numpy(dtype=np.float64),
        clicked=frame["visit"].to_numpy(dtype=np.float64),
        group=group,
        group_names=("new", "recent", "lapsed"),
        train=train,
        arms=("mens_email", "womens_email"),
        window_days=WINDOW_DAYS,
        channel=Channel.EMAIL,
        aov_mean=float(spend[buyers].mean()),
        margin_rate=0.4,
        channel_cost=0.001,
        fatigue_per_weekly_contact=FATIGUE_UNFITTED,
        not_fitted=NOT_FITTED_COMMON,
        provenance=(
            "public randomised e-mail campaign (men's e-mail, women's e-mail, no e-mail; 1/3 each; "
            "64,000 customers; outcomes over 2 weeks); fitted on the TRAIN half by customer-id hash"
        ),
        notes=(
            "groups: new (newbie), recent (existing, last bought within 3 months), lapsed (the rest)",
            "click-like signal: a site visit in the window; the control group's visit rate is the floor",
            "margin 0.4 and cost $0.001 per e-mail are the bench's conventions, not data",
        ),
    )


def fit_sms_like(data_dir: Path, timings: Timings | None = None) -> CampaignFit:
    """The X5 RetailHero SMS campaign, TRAIN half of the bench's fixed 100,000-client sample."""
    from scripts.v2_realbench.adapters import X5_MARGIN, X5_SMS_COST, x5_receipts

    timings = timings or Timings(quiet=True)
    root = data_dir / "x5_retailhero"
    up = pd.read_csv(root / "uplift_train.csv.gz")
    keep = pick_by_hash(up["client_id"].astype(str).to_numpy(), SIZES["x5"], salt=SAMPLE_SALT + ":x5")
    up = up.iloc[keep].reset_index(drop=True)
    ids = up["client_id"].astype(str).to_numpy()
    train = _train_mask(ids)
    rec = x5_receipts(data_dir, timings)
    rec = rec[rec["client_id"].isin(set(ids[train]))]
    n_receipts = rec.groupby("client_id").size().reindex(ids).fillna(0).to_numpy(dtype=np.float64)
    amounts = rec["purchase_sum"].to_numpy(dtype=np.float64)
    aov_rub = float(amounts[amounts > 0].mean())
    del rec
    cuts = np.quantile(n_receipts[train], [1 / 3, 2 / 3])
    names = np.array(["light", "medium", "heavy"])
    group = names[np.searchsorted(cuts, n_receipts, side="right")]
    return fit_campaign(
        arm=up["treatment_flg"].to_numpy(dtype=np.int64),
        bought=up["target"].to_numpy(dtype=np.float64),
        clicked=None,
        group=group,
        group_names=("light", "medium", "heavy"),
        train=train,
        arms=("sms",),
        window_days=WINDOW_DAYS,
        channel=Channel.WHATSAPP,
        aov_mean=aov_rub / RUB_PER_USD,
        margin_rate=X5_MARGIN,
        channel_cost=X5_SMS_COST / RUB_PER_USD,
        fatigue_per_weekly_contact=FATIGUE_UNFITTED,
        not_fitted=(
            *NOT_FITTED_COMMON,
            "the window length: the data do not state it; 14 days (the bench's proof window) is assumed",
        ),
        provenance=(
            "public randomised SMS campaign of a grocery loyalty programme (SMS or none, 50/50; "
            "100,000 clients of the bench's fixed sample; outcome: bought in the window); fitted on "
            "the TRAIN half by client-id hash"
        ),
        notes=(
            "groups: terciles of the number of receipts before the campaign (light, medium, heavy)",
            "the Gym's WhatsApp slot stands in for SMS (the world has no SMS content or history); "
            "e-mail consent is 0, so the only channel is the paid per-message one",
            f"money in dollars at {RUB_PER_USD} RUB per USD (unit conversion only); order value is the "
            "mean receipt of the TRAIN clients; margin 0.25 and 2 RUB per SMS are the bench's conventions",
            "the SMS campaign logged no clicks: click floor 0, click slope 0",
        ),
    )


FITS = {"email": fit_email_like, "sms": fit_sms_like}


def _round(obj: Any, digits: int = 6) -> Any:
    """Every float in a fit (dataclasses and tuples included) cut to `digits` significant digits."""
    if isinstance(obj, float):
        return float(f"{obj:.{digits}g}")
    if isinstance(obj, tuple):
        return tuple(_round(v, digits) for v in obj)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.replace(
            obj, **{f.name: _round(getattr(obj, f.name), digits) for f in dataclasses.fields(obj)}
        )
    return obj


def rounded(fit: CampaignFit) -> CampaignFit:
    """The fit with every float cut to 6 significant digits (what is stored in scenarios.py)."""
    out: CampaignFit = _round(fit)
    return out


def literal(fit: CampaignFit) -> str:
    """The fit as a Python expression (paste into scenarios.py)."""
    return re.sub(r"<Channel\.(\w+): '[^']*'>", r"Channel.\1", repr(rounded(fit)))


def same(a: CampaignFit, b: CampaignFit, rel: float = 1e-5) -> list[str]:
    """Where two fits differ by more than `rel` (empty if they match)."""
    diffs: list[str] = []

    def walk(x: Any, y: Any, path: str) -> None:
        if dataclasses.is_dataclass(x) and not isinstance(x, type):
            for f in dataclasses.fields(x):
                walk(getattr(x, f.name), getattr(y, f.name), f"{path}.{f.name}")
        elif isinstance(x, tuple):
            if len(x) != len(y):
                diffs.append(f"{path}: length {len(x)} vs {len(y)}")
                return
            for i, (u, v) in enumerate(zip(x, y, strict=True)):
                walk(u, v, f"{path}[{i}]")
        elif isinstance(x, float):
            if abs(x - y) > rel * max(abs(x), abs(y), 1e-12):
                diffs.append(f"{path}: {x} vs {y}")
        elif x != y:
            diffs.append(f"{path}: {x!r} vs {y!r}")

    walk(a, b, "fit")
    return diffs


def render(name: str, fit: CampaignFit) -> str:
    """The fit as markdown for docs/v2/GYM_BAR.md."""
    c = fit.clicks
    lines = [
        f"#### {name}",
        "",
        f"{fit.provenance}. TRAIN customers: {fit.n_train:,}. Window: {fit.window_days} days.",
        "",
        "| Group | Customers | Share | P(buy in window), no message (raw / shrunk) | Daily rate | "
        + " | ".join(f"Lift, {a} (raw +/- se / shrunk)" for a in fit.arms)
        + " | Relative lift (shrunk) |",
        "|---|---:|---:|---:|---:|" + "---:|" * len(fit.arms) + "---:|",
    ]
    for row in fit_summary(fit):
        lifts = " | ".join(
            f"{lr:+.4f} +/- {se:.4f} / {ls:+.4f}"
            for lr, se, ls in zip(row["lift_raw"], row["lift_se"], row["lift"], strict=True)
        )
        rel = ", ".join("n/a" if r is None else f"{r:+.1%}" for r in row["relative_lift"])
        lines.append(
            f"| {row['group']} | {row['n']:,} | {row['share']:.1%} | {row['control_rate_raw']:.4f} / "
            f"{row['control_rate']:.4f} | {row['daily_rate']:.5f} | {lifts} | {rel} |"
        )
    lines += [
        "",
        f"Order value ${fit.aov_mean:,.2f} (gamma shape 2), margin {fit.margin_rate:.0%}, contact cost "
        f"${fit.channel_cost:.5f}, pooled extra chance of buying per message {fit.pooled_lift():+.4f} "
        f"on a base of {fit.pooled_control_rate():.4f}.",
    ]
    if c.logged:
        lines.append(
            f"Clicks: control click-like rate {c.control_rate:.4f} (the Gym's `click_floor`), treated "
            f"{c.treated_rate:.4f}; extra clicks per extra purchase {c.per_extra_purchase:.2f} "
            f"(bootstrap 95% {c.per_extra_purchase_low:.2f} to {c.per_extra_purchase_high:.2f}; the Gym's "
            f"`click_per_extra_purchase`); only {c.caused_share_of_treated_clicks:.0%} of a treated "
            f"customer's clicks are caused by the message; click-purchase correlation among treated "
            f"customers {c.click_purchase_phi:+.3f}."
        )
    else:
        lines.append("Clicks: the campaign logged none; `click_floor` 0 and `click_per_extra_purchase` 0.")
    lines += ["", "Not fitted (kept at library values or set neutral):"]
    lines += [f"* {x}" for x in fit.not_fitted]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fit the Gym's real-calibrated scenarios (TRAIN halves only).")
    ap.add_argument("--data-dir", type=Path, default=Path("/home/user/datasets"))
    ap.add_argument("--which", choices=[*FITS, "all"], default="all")
    ap.add_argument("--check", action="store_true", help="refit and compare with autonomy/gym/scenarios.py")
    ap.add_argument("--literal", action="store_true", help="print the Python literal of each fit")
    ap.add_argument("--json", type=Path, help="write the fits here")
    args = ap.parse_args(argv)
    names = list(FITS) if args.which == "all" else [args.which]
    fits = {n: rounded(FITS[n](args.data_dir)) for n in names}
    status = 0
    for n, fit in fits.items():
        if args.literal:
            print(f"# {n}\n{literal(fit)}\n")
        elif not args.check:
            print(render(n, fit) + "\n")
        if args.check:
            from autonomy.gym import scenarios

            stored = {"email": scenarios.REAL_EMAIL_FIT, "sms": scenarios.REAL_SMS_FIT}[n]
            diffs = same(fit, stored)
            print(f"{n}: " + ("matches autonomy/gym/scenarios.py" if not diffs else "DIFFERS"))
            for d in diffs:
                print("  " + d)
            status |= 1 if diffs else 0
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps({n: dataclasses.asdict(f) for n, f in fits.items()}, indent=1, default=str)
        )
    return status


if __name__ == "__main__":
    sys.exit(main())
