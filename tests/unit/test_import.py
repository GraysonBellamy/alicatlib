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


def test_cross_lib_import_symmetry() -> None:
    """Cross-lib import-symmetry contract (spec §6 acceptance criterion).

    Every sibling library (alicatlib, watlowlib, sartoriuslib, nidaqlib)
    exposes this exact import shape. nidaq substitutes ``reading_to_row``
    / ``block_to_rows`` for ``sample_to_row``; for alicat the spelling
    is ``sample_to_row``.

    Verifies *export presence* only — the
    :class:`PollSourceAdapter`'s method signatures differ per lib by
    design (spec §E), so this isn't a runtime-equivalence test.
    """
    from alicatlib import (
        DeviceResult,
        PollSourceAdapter,
        Reading,
        Recording,
        find_devices,
        open_device,
        sample_to_row,
    )
    from alicatlib.units import to_pint

    # Everything pulled in resolves to a real object — the import itself
    # is the test, but assert non-None so a lazy attribute-missing
    # mistake (e.g. a stale __all__) still trips the assertion.
    for obj in (
        DeviceResult,
        PollSourceAdapter,
        Reading,
        Recording,
        find_devices,
        open_device,
        sample_to_row,
        to_pint,
    ):
        assert obj is not None
