"""Small synthetic worlds for the copy tests: customers, drafts and past sends with planted signals."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import numpy as np

from autonomy.agents.base import ScriptedLLM
from autonomy.agents.critic import RUBRIC
from autonomy.content.scoring import DraftResult
from autonomy.contracts import Channel, ContentItem, CustomerState, Theme

NOW = datetime(2026, 10, 1, tzinfo=UTC)
VOICE = "Warm, plain and brief. Never pushy."


def make_states(n: int, seed: int = 0, *, with_features: bool = True) -> list[CustomerState]:
    """`n` customers from three planted behaviours: recent frequent, lapsed one-time, mid."""
    rng = np.random.default_rng(seed)
    states: list[CustomerState] = []
    for i in range(n):
        kind = i % 3
        if kind == 0:  # recent, frequent, high spend
            days, purchases, spend = rng.integers(1, 20), rng.integers(8, 20), rng.uniform(600, 1500)
            sens = rng.uniform(0.0, 0.25)
        elif kind == 1:  # lapsed, one purchase, low spend
            days, purchases, spend = rng.integers(200, 400), 1, rng.uniform(10, 60)
            sens = rng.uniform(0.75, 1.0)
        else:  # in between
            days, purchases, spend = rng.integers(60, 120), rng.integers(2, 5), rng.uniform(100, 250)
            sens = rng.uniform(0.35, 0.65)
        features = {}
        if with_features:
            features = {
                "orders_90d": float(purchases) / 3.0 + float(rng.normal(0, 0.1)),
                "tea_share": float(kind == 2),
            }
        states.append(
            CustomerState(
                customer_id=f"c{i}",
                as_of=NOW,
                first_seen_at=NOW - timedelta(days=int(days) + 400),
                last_purchase_at=NOW - timedelta(days=int(days)),
                purchases=int(purchases),
                revenue=float(spend),
                offer_sensitivity=float(sens),
                top_categories=("tea",) if kind == 2 else ("coffee",),
                features=features,
            )
        )
    return states


def make_item(
    content_id: str,
    *,
    theme: Theme = Theme.REMINDER,
    words: int = 10,
    body: str | None = None,
    channel: Channel = Channel.EMAIL,
    offer_id: str = "none",
) -> ContentItem:
    text = body if body is not None else "Hi {{first_name}}, " + " ".join(["tea"] * words) + "."
    return ContentItem(
        content_id=content_id,
        channel=channel,
        theme=theme,
        offer_id=offer_id,
        subject="A note for you" if channel is Channel.EMAIL else None,
        body=text,
        slots=("first_name",),
        created_at=NOW,
    )


def type_traits(n_types: int) -> dict[str, tuple[float, ...]]:
    """Types whose first trait runs from -1.5 to +1.5; the other four are fixed small values."""
    firsts = np.linspace(-1.5, 1.5, n_types)
    return {f"ct{j:03d}": (float(f), 0.2, -0.1, 0.0, 0.3) for j, f in enumerate(firsts)}


def synth_results(
    seed: int, *, signal: bool, n_types: int = 6, drafts_per_type: int = 40, sends: int = 2000
) -> tuple[list[DraftResult], dict[str, tuple[float, ...]]]:
    """Past sends for drafts of different lengths and (irrelevant) themes.

    With `signal`, copy length matters, and in opposite directions for opposite types: short wins in
    types with a positive first trait, long wins in types with a negative one. Theme labels cannot see
    that. Without it, every draft in a type has the same true rate."""
    rng = np.random.default_rng(seed)
    traits = type_traits(n_types)
    themes = (Theme.REMINDER, Theme.SAVINGS, Theme.EDUCATION)
    results: list[DraftResult] = []
    for type_id, trait in traits.items():
        base = 0.04 + 0.04 * float(rng.random())
        for d in range(drafts_per_type):
            words = int(rng.integers(6, 45))
            theme = themes[int(rng.integers(len(themes)))]
            rate = base
            if signal:
                rate = min(0.5, base * float(np.exp(-0.9 * trait[0] * (words - 25) / 11.0)))
            item = make_item(f"{type_id}_d{d}", theme=theme, words=words)
            bought = int(rng.binomial(sends, rate))
            results.append(DraftResult(item=item, type_id=type_id, sends=sends, purchases=bought))
    return results, traits


# --- scripted models --------------------------------------------------------------------------------


def _field(prompt: str, name: str) -> str:
    match = re.search(rf"^{name}: ([a-z_]+)", prompt, re.MULTILINE)
    assert match, prompt
    return match.group(1)


def creative_script(bad_theme: str | None = None):  # type: ignore[no-untyped-def]
    """A Creative model: `n` distinct variants per call. Variants of `bad_theme` break the Critic's rules."""

    def script(system: str, prompt: str) -> str:
        assert "Creative" in system
        n = int(re.search(r"Write (\d+) distinct variants", prompt).group(1))  # type: ignore[union-attr]
        channel, theme = _field(prompt, "Channel"), _field(prompt, "Theme")
        has_offer = "{{offer_label}}" in prompt
        variants = []
        for k in range(n):
            body = f"Hi {{{{first_name}}}}, a {theme} idea {'abcdefgh'[k]} for {{{{top_category}}}}."
            if has_offer:
                body += " Use {{offer_label}}."
            if theme == bad_theme:
                body = body.replace("idea", "guaranteed idea")
            variants.append({"subject": f"A {theme} note {k}" if channel == "email" else None, "body": body})
        return json.dumps({"variants": variants})

    return script


def judge(score: int = 4) -> ScriptedLLM:
    verdict = json.dumps({"scores": [{"criterion": c, "score": score, "reason": "fine"} for c in RUBRIC]})
    return ScriptedLLM(lambda _s, _p: verdict, model_id="other-family")
