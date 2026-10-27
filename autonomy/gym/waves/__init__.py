"""The repeated-campaign benchmark: real customers and buying, a hidden message effect (docs/v4/WAVES.md).

`data` builds the real half (customers, point-in-time features, receipts to replay) from a receipts table;
`truth` is the hidden additive effect (a random tree of the features, fatigue, opt-outs, pull-forward);
`world.WavesWorld` plugs both into the Gym's loop; `policies` and `uplift` are the policies; `runner.run_one`
scores a run exactly; `report` turns runs into tables. The command line is `scripts/v2_realbench/waves.py`.
"""
