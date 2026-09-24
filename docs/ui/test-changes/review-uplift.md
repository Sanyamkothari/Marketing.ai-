# Uplift UI review fixes: test changes

The six UI-only fixes in `ui/modules/uplift/*` (segment uplift restored, held-back counts labelled so
they reconcile, plain-words Setup footer, "solid line" caption, "Available after training." empty
charts, visible reason for a disabled stepper tab). Every assertion below changed because its text
changed on purpose. No assertion was loosened. The new behaviour has new assertions, listed after the table.

| File | Test | Old | New | Why |
|---|---|---|---|---|
| tests/unit/uplift/uplift_ui.test.mjs | output page of a scoring run: four segments, recommended contacts, CI, treat-list download | tile label `Held back to measure` | `All customers held back to measure the campaign` | The tile counts the whole scoring run's control group. The label now says so, which keeps it apart from the persuadables held back. |
| tests/unit/uplift/uplift_ui.test.mjs | output page of a scoring run: … | `Of 300 persuadable customers, 30 are held back at random to measure the campaign or were opted out.` | `Of 300 persuadable customers, 30 persuadables held back at random to measure the campaign or opted out are not on the list.` | The count names whom it counts (persuadables), so it reconciles with the tile. |
| tests/unit/uplift/uplift_ui.test.mjs | output page of a training run points at scoring runs instead of offering a download | `has not produced segments\.json yet` | `Available after training\.` plus `!html.includes("segments.json")` | The empty chart shows no file name. The assertion that the file name is absent is new. |
| tests/unit/uplift/uplift_ui.test.mjs | campaign results: a mature report shows lift with its interval and the p-value | `100 held back as the control group` | `100 held back and measured as this campaign's control group` | The campaign's control count is the measured part of the run's control group. It is labelled that way so it reconciles with the Contact list page's counts. |
| tests/unit/uplift/uplift_ui.test.mjs | setup before a file: step 2 is one line, and score mode has its own footer | `!score.includes("Qini curve, AUUC")` only | the train footer must match `After the run: Model \(how well it finds the persuadable\) → Output` and must not contain `Qini curve, AUUC`. The score footer must contain neither phrase | The footer is in plain words now. The old negative check is kept. |
| tests/unit/uplift/uplift_ui_wiring.test.mjs | the output page reads the run and its uplift artefacts, and 404s become em dashes | `has not produced segments\.json yet` | `Available after training\.` | The empty-chart sentence changed on purpose. |
| tests/unit/uplift/phase1_pages_uplift.test.mjs | an uplift run's Model and Output redirect…; an uplift run's Data page links…; the scoring Output (Phase 1)…; the Data page of a built dataset… | absence of `has not produced` | the same check, plus absence of `Available after training.` | These negative guards were written against the old empty-chart sentence. Without the new sentence they would pass vacuously, so it is added to each. |
| tests/prototype/consistency.test.mjs | the uplift entry point and its words are ui/modules/uplift's (DEC-608) | pinned `After the run: Model (Qini curve, AUUC) → Output (segments, treat list) → Campaign results.` | pinned `After the run: Model (how well it finds the persuadable) → Output (segments, treat list) → Campaign results.` | The pin is kept. `views.js` and `marketing-ai-prototype.html` both changed to the same sentence. |

New assertions (none replaced an old one):

- `uplift_ui.test.mjs`, output page of a scoring run: the segment lines read `30% of customers · uplift +5.0 pts` and `10% of customers · uplift −4.0 pts`.
- `uplift_ui.test.mjs`, model page: the caption says `The further the solid line sits above the dashed one`, and `blue line` is absent.
- `uplift_ui.test.mjs`, new test "a chart whose artefact is missing says when it comes, with no file name on screen": each of `qiniChart`, `decileChart` and `segmentChart` returns exactly `<div class="empty">Available after training.</div>`.
- `uplift_ui.test.mjs`, new test "a segment with no customers has no predicted uplift and shows none": `35% of customers · uplift +15.7 pts`, the aria-label includes `predicted uplift +15.7 pts`, and an empty segment shows no uplift.
- `uplift_ui.test.mjs`, new test "the output page's held-back counts say whom they count, so they reconcile": `579 persuadables held back … are not on the list. Those held back are among the 720 of all customers held back to measure the campaign.` The tile reads `All customers held back to measure the campaign` / `720` / `8.0% of all 9,000 customers, chosen at random`. A count of one reads in the singular.
- `uplift_ui.test.mjs`, the stepper: a step that does not apply has `tabindex="0"` and `aria-describedby="ustep-why-<step>"`, and a visible `<p class="note ustep-why">` gives the reason (`Model: A scoring run uses a model trained earlier.`, `Campaign results: Measured on a scoring run.`). A step that applies has no reason line.
