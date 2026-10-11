"""Plan K: agents that check, not agents that send (M130-M144; `docs/AGENTIC.md`).

An agent here - ours, or another company's over MCP - can only **read** through typed tools that wrap the
engine's existing functions, and **propose** through proposal objects that a person approves. Five
invariants hold for every change in this package, each with a test (design §0):

* **I1** No tool can write outside our storage or send anything: the registry has no `write` kind.
* **I2** Every number a person or another agent sees comes from a deterministic tool result.
* **I3** Nothing becomes real without a person: proposal → default-deny policy → `engine.approvals`.
* **I4** The holdout cannot be shrunk, skipped or reassigned by any agent path.
* **I5** Every proposal, approval, refusal and evidence label is on the hash-chained ledger.

Everything is behind `agentic.enabled` (`configs/agentic/agentic.yaml`, default false); with it off the
product is byte-identical to Plan J's. Modules import pandas, numpy and SQL drivers inside functions only,
so `import engine` stays fast.
"""
