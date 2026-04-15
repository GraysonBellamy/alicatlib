# Contributing to alicatlib

Thanks for your interest. This project is a clean-room rewrite of `pyAlicat`;
please read [docs/design.md](docs/design.md) before making non-trivial changes
— most design decisions are already made and documented there.

## Dev setup

```bash
git clone https://github.com/ulfsri/alicatlib
cd alicatlib
uv sync --all-extras --dev
uv run pre-commit install
```

## Core checks (must pass before merging)

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

## Adding a new Alicat command

Per the design doc §5.4 and §14, a new command is:

1. One `Command` subclass in `src/alicatlib/commands/<group>.py` with
   `encode`/`decode`, `response_mode`, firmware gating, `device_kinds`.
2. One request dataclass and one response dataclass (frozen, slotted).
3. One facade one-liner on `Device` / `FlowController` (or a `@sync_version`
   for the sync API).
4. One fixture-backed unit test hitting `cmd.encode(...)` and `cmd.decode(...)`
   plus one `FakeTransport` round-trip test.

**Nothing else.** No hand-written `write_readline` paths; no per-command
branching in `Session`.

## Safety

Any command that can damage hardware or lose data must set `destructive=True`
on its `Command` spec and accept `confirm=True` at the facade. The `Session`
rejects `confirm is not True` before any I/O.

## Commits

Conventional-style short prefixes are helpful but not mandatory:

- `feat:` new user-visible behaviour
- `fix:` bugfix
- `refactor:` internal cleanup
- `docs:` docs only
- `ci:` pipeline changes
- `chore:` tooling/version bumps

## Tests that need hardware

Mark them with `hardware`, `hardware_stateful`, or `hardware_destructive` per
the tiers in [docs/testing.md](docs/testing.md). These are skipped in CI by
default.
