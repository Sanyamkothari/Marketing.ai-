# Test assertions changed at integration

| File | Test | Old assertion | New assertion | Why |
|---|---|---|---|---|
| tests/integration/test_acceptance.py:339 | the Phase 1 browser journey | `get_by_role("heading", name="Marketing AI")` | `get_by_role("heading", name="Customer Lifecycle")` | Home's H1 is now the journey the user is looking at; the product name moved to the top bar's wordmark (WP1). Same check: the journey returns to Home. |
