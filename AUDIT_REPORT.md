# Scaner Audit Report

## Scope

Repository: `sureshr89/Scaner`

This report records findings identified during the initial source review.

## Confirmed finding

### High — inconsistent sector column name

`load_universe()` creates a column named `sector`, while the calculation path references `data["Sector"]`.

This can raise `KeyError: 'Sector'` when sector-level metrics are calculated unless a later transformation renames the column.

### Recommended fix

Use one canonical column name throughout the pipeline. Prefer `sector` and update all calculation/display references to use the same spelling.

## Additional audit items

- Verify Dhan API response schemas and error payload handling.
- Validate that quote freshness is enforced consistently; `MAX_QUOTE_AGE_SECONDS` should be used or removed.
- Add tests for empty API payloads, missing OHLC fields, symbol mapping failures, and all-neutral sectors.
- Add a dependency/security scan in CI.
- Avoid exposing raw provider payloads in user-facing errors if they may contain sensitive metadata.

## Status

This is an initial report. The complete `app.py` could not be retrieved in one intact response through the connected GitHub interface, so no source rewrite is included in this commit. The branch is intentionally not merged.
