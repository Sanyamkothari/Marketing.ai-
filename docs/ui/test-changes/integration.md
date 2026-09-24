# Test assertions changed at integration

| File | Test | Old assertion | New assertion | Why |
|---|---|---|---|---|
| tests/integration/test_acceptance.py:339 | the Phase 1 browser journey | `get_by_role("heading", name="Marketing AI")` | `get_by_role("heading", name="Customer Lifecycle")` | Home's H1 is now the journey the user is looking at; the product name moved to the top bar's wordmark (WP1). Same check: the journey returns to Home. |
| tests/prototype/consistency.test.mjs | the uplift entry point and its words are ui/modules/uplift's (DEC-608) | (WP9, temporary) `index.includes("Also: target with uplift") \|\| index.includes(<old pill>)` | `index.includes('<a class="related" href="${esc(routes.setup(uc.id))}">Also: target with uplift ›</a>')` | Re-pins the product's exact markup now that WP5 has landed; WP9's either-or was a stop-gap while both were in flight. This is stricter than the pre-v1 pin, not looser. The retired pill markup (`retiredEntryHtml`) that only this pin kept alive is removed. |
