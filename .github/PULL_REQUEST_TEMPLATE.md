## Summary

<!-- What changes and why. Link to the design section it realises, if any. -->

## Scope

- [ ] Milestone: <!-- M0/M1/... -->
- [ ] Touches a public API surface
- [ ] Touches transport or protocol layer
- [ ] Changes `codes.json` (regenerate `_codes_gen.py` in the same PR)

## Test plan

- [ ] `uv run pytest` green locally
- [ ] `uv run ruff check .` clean
- [ ] `uv run mypy` clean (no new ignores)
- [ ] New behaviour has a fixture-backed test (no hardware required)
- [ ] Hardware-only tests marked (`hardware`, `hardware_stateful`, `hardware_destructive`)

## Safety checklist (device control changes only)

- [ ] Destructive ops require `confirm=True` before I/O
- [ ] Setpoints range-checked before I/O
- [ ] No new silent fallbacks on capability failure
