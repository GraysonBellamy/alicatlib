"""Tests for :class:`alicatlib.manager.AlicatManager`.

Focus (design §5.13):

- Port canonicalisation (POSIX symlinks, Windows case/prefix).
- Lifecycle: ``add`` / ``remove`` / ``close`` / ``__aexit__``, ref-counted
  port sharing.
- Concurrent dispatch: parallel across ports, serialised within one
  port via the client lock.
- :class:`ErrorPolicy` semantics: ``RAISE`` raises ExceptionGroup after
  collecting, ``RETURN`` always returns DeviceResults per-device.
- Validation: unknown names, duplicate adds, mismatched source+serial.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from alicatlib.commands import POLL_DATA, PollRequest
from alicatlib.devices.factory import open_device
from alicatlib.errors import AlicatError, AlicatValidationError
from alicatlib.manager import (
    AlicatManager,
    DeviceResult,
    ErrorPolicy,
    _canonical_port_key,  # pyright: ignore[reportPrivateUsage]
)
from alicatlib.protocol import AlicatProtocolClient
from alicatlib.transport import FakeTransport
from tests._typing import approx

if TYPE_CHECKING:
    from collections.abc import Mapping

    from alicatlib.transport.fake import ScriptedReply


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


# ---------------------------------------------------------------------------
# Fixture builders — scripted FakeTransports for identification + poll
# ---------------------------------------------------------------------------


def _mfg_lines(model: str = "MC-100SCCM-D") -> bytes:
    return b"".join(
        [
            b"A M00 Alicat Scientific\r",
            b"A M01 www.example.com\r",
            b"A M02 Ph   555-000-0000\r",
            b"A M03 info@example.com\r",
            f"A M04 Model Number {model}\r".encode("ascii"),
            b"A M05 Serial Number 123456\r",
            b"A M06 Date Manufactured 01/01/2021\r",
            b"A M07 Date Calibrated   02/01/2021\r",
            b"A M08 Calibrated By     ACS\r",
            b"A M09 Software Revision 10v20.0-R24\r",
        ],
    )


def _df_lines() -> bytes:
    return b"".join(
        [
            b"A D00 ID_ NAME______________________ TYPE_______ WIDTH NOTES___________________\r",
            b"A D01 700 Unit ID                    string          1\r",
            b"A D02 005 Mass Flow                  s decimal     7/2 012 02 SCCM\r",
        ],
    )


def _happy_script(unit_id: str = "A", mass_flow: float = 25.5) -> Mapping[bytes, bytes]:
    # Use `{uid}` for the unit_id so multi-unit buses get distinct scripts.
    u = unit_id
    return {
        f"{u}VE\r".encode("ascii"): f"{u}   10v20.0-R24 Jan  9 2025,15:04:07\r".encode("ascii"),
        f"{u}??M*\r".encode("ascii"): _mfg_lines(),
        f"{u}??D*\r".encode("ascii"): _df_lines(),
        f"{u}\r".encode("ascii"): f"{u} +{mass_flow:06.2f}\r".encode("ascii"),
    }


async def _build_client(script: Mapping[bytes, ScriptedReply]) -> AlicatProtocolClient:
    fake = FakeTransport(script, label="fake://test")
    await fake.open()
    return AlicatProtocolClient(fake, multiline_idle_timeout=0.01, default_timeout=0.1)


# ---------------------------------------------------------------------------
# Port canonicalization
# ---------------------------------------------------------------------------


class TestCanonicalPortKey:
    def test_nonexistent_path_returns_original(self) -> None:
        """Unresolvable paths pass through unchanged (fixture case)."""
        assert _canonical_port_key("/dev/ttyDoesNotExist") == "/dev/ttyDoesNotExist"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink behavior only")
    def test_posix_symlink_resolves_to_target(self) -> None:
        """A symlink and its target collapse to the same canonical key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            real = Path(tmpdir) / "real_tty"
            real.write_text("")
            link = Path(tmpdir) / "by-id-tty"
            link.symlink_to(real)

            key_real = _canonical_port_key(str(real))
            key_link = _canonical_port_key(str(link))
            assert key_real == key_link

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows casing rules only")
    def test_windows_case_insensitive(self) -> None:  # pragma: no cover - Windows-only
        assert _canonical_port_key("COM3") == _canonical_port_key("com3")
        assert _canonical_port_key(r"\\.\COM3") == _canonical_port_key("COM3")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestManagerLifecycle:
    @pytest.mark.anyio
    async def test_empty_manager_closes_cleanly(self) -> None:
        async with AlicatManager() as mgr:
            assert not mgr.names
            assert not mgr.closed
        assert mgr.closed

    @pytest.mark.anyio
    async def test_add_pre_built_device(self) -> None:
        """A user-built :class:`Device` goes in without lifecycle ownership."""
        client = await _build_client(_happy_script())
        async with open_device(client) as dev:
            async with AlicatManager() as mgr:
                got = await mgr.add("fuel", dev)
                assert got is dev
                assert mgr.get("fuel") is dev
                assert mgr.names == ("fuel",)
            # Device still usable after manager close — manager didn't own it.
            assert not dev.session.closed

    @pytest.mark.anyio
    async def test_add_client_source_shared_across_devices(self) -> None:
        """Two adds against the same Client share that Client (no dupe)."""
        script = {**_happy_script("A"), **_happy_script("B")}
        client = await _build_client(script)
        async with AlicatManager() as mgr:
            dev_a = await mgr.add("a", client, unit_id="A")
            dev_b = await mgr.add("b", client, unit_id="B")
            assert dev_a.session is not dev_b.session
            assert dev_a.info.model == dev_b.info.model
            # One port entry, both devices as refs — proves client sharing.
            port_entries = list(mgr._ports.values())  # pyright: ignore[reportPrivateUsage]
            assert len(port_entries) == 1
            assert port_entries[0].refs == {"a", "b"}
            assert port_entries[0].client is client

    @pytest.mark.anyio
    async def test_add_duplicate_name_rejects(self) -> None:
        client = await _build_client(_happy_script())
        async with AlicatManager() as mgr:
            await mgr.add("fuel", client)
            with pytest.raises(AlicatValidationError, match="already in use"):
                await mgr.add("fuel", client)

    @pytest.mark.anyio
    async def test_add_with_serial_and_non_string_source_rejects(self) -> None:
        from alicatlib.transport.base import SerialSettings

        client = await _build_client(_happy_script())
        async with AlicatManager() as mgr:
            with pytest.raises(AlicatValidationError, match="string port sources"):
                await mgr.add(
                    "fuel",
                    client,
                    serial=SerialSettings(port="/dev/ttyUSB0", baudrate=115200),
                )

    @pytest.mark.anyio
    async def test_get_unknown_name_raises(self) -> None:
        async with AlicatManager() as mgr:
            with pytest.raises(AlicatValidationError, match="no device named"):
                mgr.get("nope")

    @pytest.mark.anyio
    async def test_remove_unknown_name_raises(self) -> None:
        async with AlicatManager() as mgr:
            with pytest.raises(AlicatValidationError, match="no device named"):
                await mgr.remove("nope")

    @pytest.mark.anyio
    async def test_remove_drops_the_device(self) -> None:
        client = await _build_client(_happy_script())
        async with AlicatManager() as mgr:
            await mgr.add("fuel", client)
            assert mgr.names == ("fuel",)
            await mgr.remove("fuel")
            assert not mgr.names

    @pytest.mark.anyio
    async def test_close_is_idempotent(self) -> None:
        client = await _build_client(_happy_script())
        mgr = AlicatManager()
        await mgr.add("fuel", client)
        await mgr.close()
        assert mgr.closed
        await mgr.close()  # should not raise
        assert mgr.closed

    @pytest.mark.anyio
    async def test_operations_on_closed_manager_raise(self) -> None:
        client = await _build_client(_happy_script())
        mgr = AlicatManager()
        await mgr.close()
        with pytest.raises(AlicatValidationError, match="closed"):
            await mgr.add("fuel", client)


# ---------------------------------------------------------------------------
# Port ref-counting — shared clients via str/client sources
# ---------------------------------------------------------------------------


class TestPortRefCounting:
    @pytest.mark.anyio
    async def test_two_devices_one_client_one_port_entry(self) -> None:
        """The manager tracks one :class:`_PortEntry` per shared client."""
        script = {**_happy_script("A"), **_happy_script("B")}
        client = await _build_client(script)
        async with AlicatManager() as mgr:
            await mgr.add("a", client, unit_id="A")
            await mgr.add("b", client, unit_id="B")
            # Private-state peek: one port entry, two refs.
            port_entries = list(mgr._ports.values())  # pyright: ignore[reportPrivateUsage]
            assert len(port_entries) == 1
            assert port_entries[0].refs == {"a", "b"}

    @pytest.mark.anyio
    async def test_remove_decrements_refs_without_closing_port(self) -> None:
        """Removing one of two devices keeps the shared port alive."""
        script = {**_happy_script("A"), **_happy_script("B")}
        client = await _build_client(script)
        async with AlicatManager() as mgr:
            await mgr.add("a", client, unit_id="A")
            await mgr.add("b", client, unit_id="B")
            await mgr.remove("a")
            port_entries = list(mgr._ports.values())  # pyright: ignore[reportPrivateUsage]
            assert len(port_entries) == 1
            assert port_entries[0].refs == {"b"}

    @pytest.mark.anyio
    async def test_remove_last_ref_drops_port(self) -> None:
        script = {**_happy_script("A")}
        client = await _build_client(script)
        async with AlicatManager() as mgr:
            await mgr.add("a", client, unit_id="A")
            await mgr.remove("a")
            assert mgr._ports == {}  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Concurrent dispatch
# ---------------------------------------------------------------------------


class TestManagerPoll:
    @pytest.mark.anyio
    async def test_poll_returns_frame_per_device(self) -> None:
        """Each device's poll result arrives wrapped in a DeviceResult."""
        script_a = _happy_script("A", mass_flow=10.0)
        script_b = _happy_script("B", mass_flow=25.0)
        client = await _build_client({**script_a, **script_b})
        async with AlicatManager() as mgr:
            await mgr.add("a", client, unit_id="A")
            await mgr.add("b", client, unit_id="B")
            results = await mgr.poll()
            assert set(results.keys()) == {"a", "b"}
            for result in results.values():
                assert result.ok
                assert isinstance(result, DeviceResult)
            assert results["a"].value is not None
            assert results["b"].value is not None
            assert results["a"].value.values["Mass_Flow"] == approx(10.0)
            assert results["b"].value.values["Mass_Flow"] == approx(25.0)

    @pytest.mark.anyio
    async def test_poll_names_subset(self) -> None:
        script = {**_happy_script("A"), **_happy_script("B")}
        client = await _build_client(script)
        async with AlicatManager() as mgr:
            await mgr.add("a", client, unit_id="A")
            await mgr.add("b", client, unit_id="B")
            results = await mgr.poll(names=["a"])
            assert set(results.keys()) == {"a"}

    @pytest.mark.anyio
    async def test_poll_unknown_name_raises(self) -> None:
        client = await _build_client(_happy_script())
        async with AlicatManager() as mgr:
            await mgr.add("fuel", client)
            with pytest.raises(AlicatValidationError, match="unknown device name"):
                await mgr.poll(names=["ghost"])

    @pytest.mark.anyio
    async def test_poll_across_two_physical_ports_runs_concurrently(self) -> None:
        """Two separate clients → two task-group children → parallel I/O.

        This test can't measure wall-clock concurrency deterministically,
        but it does exercise the two-port code path (different port_keys
        → separate groups in ``_dispatch``).
        """
        client_a = await _build_client(_happy_script("A"))
        client_b = await _build_client(_happy_script("A"))  # unit A on both
        async with AlicatManager() as mgr:
            await mgr.add("port0", client_a, unit_id="A")
            await mgr.add("port1", client_b, unit_id="A")
            results = await mgr.poll()
            assert results["port0"].ok
            assert results["port1"].ok
            assert results["port0"].value is not None
            assert results["port1"].value is not None
            # Distinct port_keys → two separate port entries.
            assert len(mgr._ports) == 2  # pyright: ignore[reportPrivateUsage]


class TestManagerRequest:
    @pytest.mark.anyio
    async def test_request_dv_across_devices(self) -> None:
        from alicatlib.registry import Statistic

        # Only need VE/??M*/??D* to open, plus DV reply for poll-alike.
        script_a: Mapping[bytes, bytes] = {
            **_happy_script("A"),
            b"ADV 10 2\r": b"+14.62\r",
        }
        client = await _build_client(script_a)
        async with AlicatManager() as mgr:
            await mgr.add("a", client, unit_id="A")
            results = await mgr.request([Statistic.ABS_PRESS], averaging_ms=10)
            assert results["a"].ok
            assert results["a"].value is not None
            assert results["a"].value.values == {Statistic.ABS_PRESS: 14.62}


class TestManagerExecute:
    @pytest.mark.anyio
    async def test_execute_poll_command_per_device(self) -> None:
        """``execute`` dispatches different requests to different devices."""
        script_a = _happy_script("A", mass_flow=10.0)
        script_b = _happy_script("B", mass_flow=50.0)
        client = await _build_client({**script_a, **script_b})
        async with AlicatManager() as mgr:
            await mgr.add("a", client, unit_id="A")
            await mgr.add("b", client, unit_id="B")
            results = await mgr.execute(
                POLL_DATA,
                {"a": PollRequest(), "b": PollRequest()},
            )
            assert set(results.keys()) == {"a", "b"}
            assert results["a"].ok
            assert results["b"].ok

    @pytest.mark.anyio
    async def test_execute_unknown_name_rejects(self) -> None:
        client = await _build_client(_happy_script())
        async with AlicatManager() as mgr:
            await mgr.add("a", client)
            with pytest.raises(AlicatValidationError, match="no device named"):
                await mgr.execute(POLL_DATA, {"ghost": PollRequest()})


# ---------------------------------------------------------------------------
# Error policies
# ---------------------------------------------------------------------------


class TestErrorPolicies:
    @pytest.mark.anyio
    async def test_return_policy_surfaces_per_device_errors(self) -> None:
        """Under RETURN, a failing device produces DeviceResult.error."""
        # Script A succeeds; B's poll is unscripted → FakeTransport raises,
        # which the protocol client surfaces as AlicatTransportError.
        script = {**_happy_script("A")}
        # Add B's identification but omit its poll reply entirely.
        script.update(
            {
                b"BVE\r": b"B   10v20.0-R24 Jan  9 2025,15:04:07\r",
                b"B??M*\r": _mfg_lines(),
                b"B??D*\r": _df_lines(),
            },
        )
        client = await _build_client(script)
        async with AlicatManager(error_policy=ErrorPolicy.RETURN) as mgr:
            await mgr.add("a", client, unit_id="A")
            await mgr.add("b", client, unit_id="B")
            results = await mgr.poll()
            assert results["a"].ok
            assert not results["b"].ok
            assert results["b"].error is not None

    @pytest.mark.anyio
    async def test_raise_policy_collects_all_into_exception_group(self) -> None:
        """Under RAISE, the manager raises ExceptionGroup with all failures."""
        # Both A and B are missing their poll replies → both error.
        script: dict[bytes, bytes] = {
            b"AVE\r": b"A   10v20.0-R24 Jan  9 2025,15:04:07\r",
            b"A??M*\r": _mfg_lines(),
            b"A??D*\r": _df_lines(),
            b"BVE\r": b"B   10v20.0-R24 Jan  9 2025,15:04:07\r",
            b"B??M*\r": _mfg_lines(),
            b"B??D*\r": _df_lines(),
        }
        client = await _build_client(script)
        async with AlicatManager(error_policy=ErrorPolicy.RAISE) as mgr:
            await mgr.add("a", client, unit_id="A")
            await mgr.add("b", client, unit_id="B")
            with pytest.raises(ExceptionGroup) as excinfo:
                await mgr.poll()
            assert len(excinfo.value.exceptions) == 2
            assert all(isinstance(e, AlicatError) for e in excinfo.value.exceptions)


# ---------------------------------------------------------------------------
# Partial-open cleanup
# ---------------------------------------------------------------------------


class TestPartialOpenCleanup:
    @pytest.mark.anyio
    async def test_add_failure_does_not_leak_port_entry(self) -> None:
        """If ``open_device`` raises during identification, the port is torn down."""
        # Missing every identification reply → identify will time out / error.
        fake = FakeTransport({}, label="fake://fail")
        await fake.open()
        client = AlicatProtocolClient(fake, multiline_idle_timeout=0.01, default_timeout=0.05)
        async with AlicatManager() as mgr:
            with pytest.raises(AlicatError):
                await mgr.add("broken", client)
            # Port entry either never got added (good) or got torn down.
            assert not mgr.names
