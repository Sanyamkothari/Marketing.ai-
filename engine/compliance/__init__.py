"""The pre-send compliance gate (Plan K M134, DEC-1405; design §8.2; `docs/AGENTIC.md` section 9).

Before a treat list leaves the product, every planned contact gets one decision record per customer,
channel and purpose: may it be sent, and if not, why. The record reuses what the run already decided per
customer (M99/M131's `channel_contactability.parquet`: consent per channel, with the region's consent-basis
rules) and adds the checks that are per message, not per customer:

* the DLT matcher (`engine.compliance.dlt`): the approved message must be the registered template with each
  tagged slot filled by a value of its tag's type - pure string logic, linear in the message length, never a
  language model;
* the URL whitelist (`engine.compliance.urls`): every link in the message must be on the client's list;
* WhatsApp opt-in: a customer is sent on a channel that needs an opt-in only when the consent ledger holds a
  valid grant on that channel itself (an all-channel grant is not an opt-in).

`engine.compliance.gate.decide` builds the records (vectorised, linear in the contacts) and
`engine.compliance.handoff` runs it for a treat list, writes `compliance_decisions.parquet` (a row-level
artefact) and its summary, and puts the file's sha256 on the decision ledger as a signed evidence label.

Off by default (`compliance.gate.enabled: false` in `configs/agentic/agentic.yaml`, the gate's one switch; its
settings are in `configs/compliance/compliance.yaml`): nothing here but the switch reader is
imported by a treat list build and every treat list is byte for byte what it was. Modules import pandas,
pyarrow and SQL drivers inside functions only, so `import engine` stays fast.
"""
