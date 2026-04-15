"""Top-level import smoke tests."""

from __future__ import annotations


def test_package_imports() -> None:
    import alicatlib

    assert hasattr(alicatlib, "__version__")
    assert isinstance(alicatlib.__version__, str)


def test_key_names_exported() -> None:
    import alicatlib

    for name in (
        "AlicatError",
        "AlicatTimeoutError",
        "AlicatParseError",
        "AlicatFirmwareError",
        "ErrorContext",
        "FirmwareVersion",
    ):
        assert hasattr(alicatlib, name), name
