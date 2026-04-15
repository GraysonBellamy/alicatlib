# Changelog

All notable changes to this project will be documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Project scaffolding (pyAlicat v2 rewrite, renamed to `alicatlib`): `src/alicatlib` package layout with typed subpackages.
- `errors.py` with `AlicatError` hierarchy and typed `ErrorContext`.
- `firmware.py` with `FirmwareVersion` parser and ordering.
- `config.py` with `AlicatConfig` and `config_from_env`.
- `codes.json` shipped under `registry/data/`.
- Hatchling build, `uv`-managed dev env, `ruff` format+lint, `mypy --strict`.
- GitHub Actions CI (lint, types, tests on 3.12/3.13 × Linux/macOS/Windows,
  build, codegen idempotency), release (trusted PyPI publishing), and docs
  (mkdocs-material + mkdocstrings deployed to Pages).
- Pre-commit hooks: ruff, mypy, codespell, whitespace.
- Design document at `docs/design.md`.
