"""Top-level import smoke tests."""

from __future__ import annotations


def test_package_imports() -> None:
    import alicatlib

    assert hasattr(alicatlib, "__version__")
    assert isinstance(alicatlib.__version__, str)
    assert "__version__" in alicatlib.__all__


def test_key_names_exported() -> None:
    import alicatlib

    for name in (
        "AlicatError",
        "AlicatTimeoutError",
        "AlicatParseError",
        "AlicatFirmwareError",
        "AlicatMissingHardwareError",
        "AlicatManager",
        "ErrorContext",
        "Gas",
        "FirmwareVersion",
        "Unit",
        "find_devices",
        "list_serial_ports",
        "open_device",
        "probe",
    ):
        assert hasattr(alicatlib, name), name
        assert name in alicatlib.__all__, name


def test_readme_async_imports_are_top_level_exports() -> None:
    from alicatlib import Gas, open_device

    assert Gas.N2.value == "N2"
    assert callable(open_device)
