"""Small typed wrappers around test-only helpers."""

from __future__ import annotations

import pytest


def approx(
    expected: float | int,
    *,
    rel: float | None = None,
    abs_tol: float | None = None,
    nan_ok: bool = False,
) -> object:
    """Return ``pytest.approx`` behind one typed boundary for pyright strict."""
    return pytest.approx(  # pyright: ignore[reportUnknownMemberType]
        expected,
        rel=rel,
        abs=abs_tol,
        nan_ok=nan_ok,
    )
