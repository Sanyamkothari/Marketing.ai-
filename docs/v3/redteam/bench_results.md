# Real bench, CI run 40325405923 (commit 3af057a7): agent vs agent_v3, doubly-robust profit per customer on the TEST half

| Dataset | agent (95% CI) | agent_v3 (95% CI) | v3 minus agent (points only) | agent_v3 share contacted |
|---|---:|---:|---:|---:|
| criteo | 0.0524 (0.0458-0.0590) | 0.0524 (0.0458-0.0590) | +0.0000 | 11.9% |
| hillstrom | 0.6513 (0.5000-0.8026) | 0.2847 (0.1870-0.3825) | -0.3666 | 1.9% |
| megafon | 4.1958 (4.1229-4.2687) | 4.1958 (4.1229-4.2687) | +0.0000 | 46.1% |
| x5 | 72.2296 (71.1432-73.3161) | 70.7599 (69.6604-71.8594) | -1.4697 | 1.5% |

Best simple policies on the same test half (from the agent_v3 report): hillstrom send-to-all Mens e-mail 0.6766 [0.5182, 0.8349]; x5 segment rule 72.33 [71.21, 73.45], T-learner 72.63; criteo segment rule (f8) 0.0506; megafon segment rule 3.733, T-learner 4.2088.
agent_v3 verdicts: criteo non-inferior no (corrected), hillstrom no, megafon yes (beats where features carry signal, calibration within 2x), x5 no.
Reading: v3 changes nothing on criteo and megafon (every customer has an identical profile there: bench gives only signup+consent events, red-team F1) and on hillstrom and x5 v3 nearly stops contacting (1.9% / 1.5%) and loses clearly vs the plain agent. Plain agent: hillstrom 0.6513 vs best simple 0.6766 (slightly below), x5 72.23 vs 72.33 (tie), criteo 0.0524 vs 0.0506 (tie), megafon 4.196 vs segment 3.733 (beats) and ties the T-learner 4.209.
The Gym run (40325406058) is still running at the time of writing.
