"""Which use cases have a Guided-setup helper (Plan G, G10)."""

from __future__ import annotations

from engine.config import UseCaseConfig

__all__ = ["agent_available"]


def agent_available(config: UseCaseConfig) -> bool:
    """True when the use case trains a model and its `agent:` block is enabled.

    Generative use cases have no advanced settings and no table to prepare, so they get no helper
    whatever their `agent.enabled` says; the flag is how a trainable use case opts out.
    """
    return config.agent.enabled and config.trainable_in_phase_1
