"""In-memory stand-ins for every interface, so each stream can build and test alone."""

from autonomy.fakes.channels import FakeChannelAdapter, KeywordTextSignals
from autonomy.fakes.folder import SimpleStateFolder
from autonomy.fakes.policies import ContactEveryonePolicy, NothingPolicy
from autonomy.fakes.stores import (
    InMemoryDecisionLog,
    InMemoryEventStore,
    InMemoryJobQueue,
    InMemoryPriorStore,
    InMemoryStateStore,
    InMemoryWorkspace,
)
from autonomy.fakes.world import FakeEnvironment

__all__ = [
    "ContactEveryonePolicy",
    "FakeChannelAdapter",
    "FakeEnvironment",
    "InMemoryDecisionLog",
    "InMemoryEventStore",
    "InMemoryJobQueue",
    "InMemoryPriorStore",
    "InMemoryStateStore",
    "InMemoryWorkspace",
    "KeywordTextSignals",
    "NothingPolicy",
    "SimpleStateFolder",
]
