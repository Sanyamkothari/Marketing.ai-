"""Text -> signals: one swappable slot that answers typed questions about text (plan-v2 section 2).

Three implementations of `autonomy.interfaces.TextSignals`, to be compared on the same eval cases
and the winner kept:

- `LLMTextSignals`: an LLM's structured output, several texts per call.
- `EmbeddingTextSignals`: nearest centroid over embeddings of a few examples per label.
- `JevTextSignals`: TypeSafe's Jev typed-question API. Import it from `autonomy.signals.jev`; it needs
  httpx, which the other two do not.

All three answer only from the labels given, and fall back to the last label (list a catch-all such
as "other" last) with a low probability when they cannot tell.
"""

from autonomy.signals.embedding_signals import EmbeddingTextSignals
from autonomy.signals.llm_signals import LLMTextSignals

__all__ = ["EmbeddingTextSignals", "LLMTextSignals"]
