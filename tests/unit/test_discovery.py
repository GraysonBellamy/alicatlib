"""Tests for :mod:`alicatlib.devices.discovery`.

``probe`` opens a real :class:`SerialTransport` internally; we exercise
the identification path via the ``_probe_with_client`` helper with a
:class:`FakeTransport`. ``find_devices`` is tested by monkey-patching
the module-level ``probe`` so we drive the cross-product / concurrency
logic without touching real hardware. ``list_serial_ports`` is a thin
``anyserial`` passthrough and is exercised only by a smoke test — its
content depends on what's plugged into the machine running CI.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from typing import TYPE_CHECKING

import pytest

from alicatlib.commands import Capability
from alicatlib.devices import DeviceKind, Medium, discovery
from alicatlib.devices.discovery import (
    DEFAULT_DISCOVERY_BAUDRATES,
    DiscoveryResult,
    _probe_with_client,  # pyright: ignore[reportPrivateUsage]
    find_devices,
    list_serial_ports,
)
from alicatlib.devices.models import DeviceInfo
from alicatlib.errors import AlicatTimeoutError
from alicatlib.firmware import FirmwareVersion
from alicatlib.protocol import AlicatProtocolClient
from alicatlib.transport import FakeTransport

if TYPE_CHECKING:
    from collections.abc import Mapping

    from alicatlib.transport.fake import ScriptedReply


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mfg_lines() -> bytes:
    return b"".join(
        [
            b"A M01 Alicat Scientific\r",
            b"A M02 www.example.com\r",
            b"A M03 +1 555-0000\r",
            b"A M04 MC-100SCCM-D\r",
            b"A M05 123456\r",
            b"A M06 2021-01-01\r",
            b"A M07 2021-02-01\r",
            b"A M08 ACS\r",
            b"A M09 10v05\r",
            b"A M10 \r",
        ],
    )


def _happy_script() -> dict[bytes, bytes]:
    return {
        b"AVE\r": b"A 10v05 2021-05-19\r",
        b"A??M*\r": _mfg_lines(),
    }


async def _make_client(
    script: Mapping[bytes, ScriptedReply] | None = None,
) -> AlicatProtocolClient:
    fake = FakeTransport(script, label="fake://test")
    await fake.open()
    return AlicatProtocolClient(fake, multiline_idle_timeout=0.01, default_timeout=0.1)


def _fake_info(model: str = "MC-100SCCM-D") -> DeviceInfo:
    """Canned DeviceInfo for fake probe results."""
    return DeviceInfo(
        unit_id="A",
        manufacturer="Alicat",
        model=model,
        serial="123456",
        manufactured="2021-01-01",
        calibrated="2021-02-01",
        calibrated_by="ACS",
        software="10v05",
        firmware=FirmwareVersion.parse("10v05"),
        firmware_date=date(2021, 5, 19),
        kind=DeviceKind.FLOW_CONTROLLER,
        media=Medium.GAS,
        capabilities=Capability.NONE,
    )


# ---------------------------------------------------------------------------
# DiscoveryResult dataclass
# ---------------------------------------------------------------------------


class TestDiscoveryResult:
    def test_ok_when_info_populated(self) -> None:
        res = DiscoveryResult(
            port="/dev/ttyUSB0",
            unit_id="A",
            baudrate=19200,
            info=_fake_info(),
            error=None,
        )
        assert res.ok is True

    def test_not_ok_when_error_populated(self) -> None:
        res = DiscoveryResult(
            port="/dev/ttyUSB0",
            unit_id="A",
            baudrate=19200,
            info=None,
            error=AlicatTimeoutError("timed out"),
        )
        assert res.ok is False

    def test_is_frozen(self) -> None:
        res = DiscoveryResult(
            port="/dev/ttyUSB0",
            unit_id="A",
            baudrate=19200,
            info=None,
            error=None,
        )
        with pytest.raises(FrozenInstanceError):
            res.port = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DEFAULT_DISCOVERY_BAUDRATES
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_includes_factory_default_and_common_alt(self) -> None:
        """19200 is Alicat factory; 115200 is the most common alternative."""
        assert 19200 in DEFAULT_DISCOVERY_BAUDRATES
        assert 115200 in DEFAULT_DISCOVERY_BAUDRATES

    def test_factory_default_first(self) -> None:
        """Try the common case first so a normal fleet resolves in one pass."""
        assert DEFAULT_DISCOVERY_BAUDRATES[0] == 19200


# ---------------------------------------------------------------------------
# _probe_with_client — the identification path under a scripted transport
# ---------------------------------------------------------------------------


class TestProbeWithClient:
    @pytest.mark.anyio
    async def test_happy_path(self) -> None:
        client = await _make_client(_happy_script())
        result = await _probe_with_client(
            client,
            port="/dev/ttyUSB0",
            unit_id="A",
            baudrate=19200,
        )
        assert result.ok
        assert result.info is not None
        assert result.info.model == "MC-100SCCM-D"
        assert result.port == "/dev/ttyUSB0"
        assert result.unit_id == "A"
        assert result.baudrate == 19200

    @pytest.mark.anyio
    async def test_ve_timeout_captured_not_raised(self) -> None:
        """A silent device (no VE reply) surfaces as error, never raises.

        Post design §16.6.8 the factory falls through to ``??M*`` on a
        VE timeout (GP path). With an empty script, ??M* also times out
        and identification raises :class:`AlicatConfigurationError`
        asking for a ``model_hint``. Discovery captures it either way —
        what matters is that the coroutine doesn't raise.
        """
        client = await _make_client()  # empty script → VE and ??M* both time out
        result = await _probe_with_client(
            client,
            port="/dev/ttyUSB0",
            unit_id="A",
            baudrate=19200,
        )
        assert not result.ok
        assert result.error is not None
        assert result.info is None

    @pytest.mark.anyio
    async def test_gp_without_model_hint_errors(self) -> None:
        """GP devices can't run ??M*; identify raises AlicatConfigurationError.

        Discovery captures that and returns a loud error result rather
        than silently skipping the device.
        """
        client = await _make_client({b"AVE\r": b"A GP\r"})
        result = await _probe_with_client(
            client,
            port="/dev/ttyUSB0",
            unit_id="A",
            baudrate=19200,
        )
        assert not result.ok
        assert result.error is not None
        # The error's message mentions model_hint so the operator knows
        # the remediation.
        assert "model_hint" in str(result.error)

    @pytest.mark.anyio
    async def test_unit_id_echoed_on_error(self) -> None:
        """Error results still carry port/unit_id/baudrate so the report is complete."""
        client = await _make_client()
        result = await _probe_with_client(
            client,
            port="/dev/ttyUSB1",
            unit_id="B",
            baudrate=115200,
        )
        assert result.port == "/dev/ttyUSB1"
        assert result.unit_id == "B"
        assert result.baudrate == 115200


# ---------------------------------------------------------------------------
# find_devices — cross-product + concurrency
# ---------------------------------------------------------------------------


class TestFindDevices:
    @pytest.mark.anyio
    async def test_builds_cross_product(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """find_devices probes every (port, unit_id, baudrate) combination."""
        calls: list[tuple[str, str, int]] = []

        async def fake_probe(
            port: str,
            *,
            unit_id: str = "A",
            baudrate: int = 19200,
            timeout: float = 0.2,
        ) -> DiscoveryResult:
            del timeout
            calls.append((port, unit_id, baudrate))
            return DiscoveryResult(
                port=port,
                unit_id=unit_id,
                baudrate=baudrate,
                info=_fake_info(),
                error=None,
            )

        monkeypatch.setattr(discovery, "probe", fake_probe)

        results = await find_devices(
            ports=["/dev/ttyUSB0", "/dev/ttyUSB1"],
            unit_ids=("A", "B"),
            baudrates=(19200, 115200),
        )

        # 2 ports × 2 unit_ids × 2 baudrates = 8 probes.
        assert len(results) == 8
        assert len(calls) == 8
        assert set(calls) == {
            ("/dev/ttyUSB0", "A", 19200),
            ("/dev/ttyUSB0", "A", 115200),
            ("/dev/ttyUSB0", "B", 19200),
            ("/dev/ttyUSB0", "B", 115200),
            ("/dev/ttyUSB1", "A", 19200),
            ("/dev/ttyUSB1", "A", 115200),
            ("/dev/ttyUSB1", "B", 19200),
            ("/dev/ttyUSB1", "B", 115200),
        }

    @pytest.mark.anyio
    async def test_result_order_row_major(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Results land in ports × unit_ids × baudrates order (row-major).

        Stable ordering makes diffing discovery runs across sessions
        meaningful; a reshuffle due to concurrency would defeat that.
        """

        async def fake_probe(
            port: str,
            *,
            unit_id: str = "A",
            baudrate: int = 19200,
            timeout: float = 0.2,
        ) -> DiscoveryResult:
            del timeout
            return DiscoveryResult(
                port=port,
                unit_id=unit_id,
                baudrate=baudrate,
                info=None,
                error=None,
            )

        monkeypatch.setattr(discovery, "probe", fake_probe)

        results = await find_devices(
            ports=["/dev/ttyUSB0", "/dev/ttyUSB1"],
            unit_ids=("A", "B"),
            baudrates=(19200, 115200),
        )

        expected_order = [
            ("/dev/ttyUSB0", "A", 19200),
            ("/dev/ttyUSB0", "A", 115200),
            ("/dev/ttyUSB0", "B", 19200),
            ("/dev/ttyUSB0", "B", 115200),
            ("/dev/ttyUSB1", "A", 19200),
            ("/dev/ttyUSB1", "A", 115200),
            ("/dev/ttyUSB1", "B", 19200),
            ("/dev/ttyUSB1", "B", 115200),
        ]
        actual_order = [(r.port, r.unit_id, r.baudrate) for r in results]
        assert actual_order == expected_order

    @pytest.mark.anyio
    async def test_empty_ports_returns_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No ports → no probes → empty result tuple."""

        async def fake_probe(**_kwargs: object) -> DiscoveryResult:
            pytest.fail("probe must not be called with no ports")

        monkeypatch.setattr(discovery, "probe", fake_probe)
        assert await find_devices(ports=[]) == ()

    @pytest.mark.anyio
    async def test_default_ports_enumerates(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ports=None triggers list_serial_ports() for a full sweep."""
        enumerated = ["/dev/ttyUSB0", "/dev/ttyUSB1"]

        async def fake_list() -> list[str]:
            return enumerated

        async def fake_probe(
            port: str,
            *,
            unit_id: str = "A",
            baudrate: int = 19200,
            timeout: float = 0.2,
        ) -> DiscoveryResult:
            del timeout
            return DiscoveryResult(
                port=port,
                unit_id=unit_id,
                baudrate=baudrate,
                info=None,
                error=None,
            )

        monkeypatch.setattr(discovery, "list_serial_ports", fake_list)
        monkeypatch.setattr(discovery, "probe", fake_probe)

        results = await find_devices(unit_ids=("A",), baudrates=(19200,))
        ports_seen = {r.port for r in results}
        assert ports_seen == set(enumerated)

    @pytest.mark.anyio
    async def test_respects_max_concurrency(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """At most ``max_concurrency`` probes run simultaneously."""
        import anyio

        in_flight = 0
        peak = 0

        async def fake_probe(
            port: str,
            *,
            unit_id: str = "A",
            baudrate: int = 19200,
            timeout: float = 0.2,
        ) -> DiscoveryResult:
            del timeout
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            # Hold the slot briefly so contention is observable.
            await anyio.sleep(0.01)
            in_flight -= 1
            return DiscoveryResult(
                port=port,
                unit_id=unit_id,
                baudrate=baudrate,
                info=None,
                error=None,
            )

        monkeypatch.setattr(discovery, "probe", fake_probe)

        await find_devices(
            ports=[f"/dev/ttyUSB{i}" for i in range(10)],
            unit_ids=("A",),
            baudrates=(19200,),
            max_concurrency=3,
        )
        assert peak <= 3

    @pytest.mark.anyio
    async def test_same_port_probes_serialise(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: probes targeting the same physical port must not
        overlap.

        A serial port can only be held by one transport at a time. A sweep
        that tries two baud rates on one port used to race two ``probe``
        calls against the same device handle — the loser got a spurious
        ``PortBusyError`` or worse a silent read of the other baud's
        byte stream. :func:`find_devices` now takes a per-port lock so
        same-port combinations queue up and different-port combinations
        still run in parallel.
        """
        import anyio

        in_flight_per_port: dict[str, int] = {}
        peak_per_port: dict[str, int] = {}

        async def fake_probe(
            port: str,
            *,
            unit_id: str = "A",
            baudrate: int = 19200,
            timeout: float = 0.2,
        ) -> DiscoveryResult:
            del timeout, unit_id
            in_flight_per_port[port] = in_flight_per_port.get(port, 0) + 1
            peak_per_port[port] = max(peak_per_port.get(port, 0), in_flight_per_port[port])
            await anyio.sleep(0.01)
            in_flight_per_port[port] -= 1
            return DiscoveryResult(
                port=port,
                unit_id="A",
                baudrate=baudrate,
                info=None,
                error=None,
            )

        monkeypatch.setattr(discovery, "probe", fake_probe)

        await find_devices(
            ports=["/dev/ttyUSB0", "/dev/ttyUSB1"],
            unit_ids=("A", "B"),
            baudrates=(19200, 115200),
            max_concurrency=8,
        )
        # 4 combinations per port, but at most 1 should be in flight at a time.
        assert peak_per_port == {"/dev/ttyUSB0": 1, "/dev/ttyUSB1": 1}

    @pytest.mark.anyio
    async def test_different_port_probes_run_in_parallel(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The per-port lock only serialises *same-port* combinations —
        unrelated ports must still run concurrently, otherwise a large
        fleet sweep would serialise the whole world."""
        import anyio

        in_flight = 0
        peak = 0

        async def fake_probe(
            port: str,
            *,
            unit_id: str = "A",
            baudrate: int = 19200,
            timeout: float = 0.2,
        ) -> DiscoveryResult:
            del timeout, unit_id
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await anyio.sleep(0.01)
            in_flight -= 1
            return DiscoveryResult(
                port=port,
                unit_id="A",
                baudrate=baudrate,
                info=None,
                error=None,
            )

        monkeypatch.setattr(discovery, "probe", fake_probe)

        await find_devices(
            ports=[f"/dev/ttyUSB{i}" for i in range(4)],
            unit_ids=("A",),
            baudrates=(19200,),
            max_concurrency=8,
        )
        # 4 distinct ports, 4 probes — per-port lock does not block them.
        assert peak == 4

    @pytest.mark.anyio
    async def test_stop_on_first_hit_skips_other_bauds_same_port(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Once a probe at ``(port, uid, baud)`` succeeds, probes at
        ``(port, uid, baud')`` for ``baud' != baud`` don't run — the bus
        is already known to be at ``baud``.

        The test is scheduling-agnostic: either baud may be the first to
        acquire the port lock under anyio's task group (asyncio and trio
        differ here). The invariant is that *exactly one* probe runs,
        regardless of which baud wins the race.
        """
        calls: list[tuple[str, str, int]] = []

        async def fake_probe(
            port: str,
            *,
            unit_id: str = "A",
            baudrate: int = 19200,
            timeout: float = 0.2,
        ) -> DiscoveryResult:
            del timeout
            calls.append((port, unit_id, baudrate))
            return DiscoveryResult(
                port=port,
                unit_id=unit_id,
                baudrate=baudrate,
                info=_fake_info(),
                error=None,
            )

        monkeypatch.setattr(discovery, "probe", fake_probe)
        results = await find_devices(
            ports=["/dev/ttyUSB0"],
            unit_ids=("A",),
            baudrates=(19200, 115200),
            stop_on_first_hit=True,
        )
        assert len(calls) == 1
        assert calls[0][0] == "/dev/ttyUSB0"
        assert calls[0][1] == "A"
        assert calls[0][2] in (19200, 115200)
        assert len(results) == 1
        assert results[0].ok

    @pytest.mark.anyio
    async def test_stop_on_first_hit_still_probes_other_unit_ids(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Short-circuit is keyed on baud, not on port-as-a-whole. On an
        RS-485 bus, ``(port, A, baud)`` succeeding does not mean
        ``(port, B, baud)`` should be skipped — a second device may
        share the bus.

        Whichever baud wins the first port-lock acquisition becomes the
        confirmed baud for that port, then both unit ids are probed at
        that baud. The test asserts *structure* (one baud for all
        probes; both unit ids present) rather than *which* baud, since
        anyio's task group scheduling differs between asyncio and trio.
        """
        calls: list[tuple[str, str, int]] = []

        async def fake_probe(
            port: str,
            *,
            unit_id: str = "A",
            baudrate: int = 19200,
            timeout: float = 0.2,
        ) -> DiscoveryResult:
            del timeout
            calls.append((port, unit_id, baudrate))
            return DiscoveryResult(
                port=port,
                unit_id=unit_id,
                baudrate=baudrate,
                info=_fake_info(),
                error=None,
            )

        monkeypatch.setattr(discovery, "probe", fake_probe)
        results = await find_devices(
            ports=["/dev/ttyUSB0"],
            unit_ids=("A", "B"),
            baudrates=(19200, 115200),
            stop_on_first_hit=True,
        )
        assert len(results) == 2
        assert len(calls) == 2
        # Both probes shared the baud that won the first port-lock.
        bauds_seen = {c[2] for c in calls}
        assert len(bauds_seen) == 1
        winning_baud = next(iter(bauds_seen))
        assert winning_baud in (19200, 115200)
        # Both unit ids were probed at the winning baud.
        assert {c[1] for c in calls} == {"A", "B"}

    @pytest.mark.anyio
    async def test_stop_on_first_hit_default_false_preserves_full_sweep(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Default keeps the "every combination produces a result" contract."""
        calls: list[tuple[str, str, int]] = []

        async def fake_probe(
            port: str,
            *,
            unit_id: str = "A",
            baudrate: int = 19200,
            timeout: float = 0.2,
        ) -> DiscoveryResult:
            del timeout
            calls.append((port, unit_id, baudrate))
            return DiscoveryResult(
                port=port,
                unit_id=unit_id,
                baudrate=baudrate,
                info=_fake_info(),
                error=None,
            )

        monkeypatch.setattr(discovery, "probe", fake_probe)
        results = await find_devices(
            ports=["/dev/ttyUSB0"],
            unit_ids=("A",),
            baudrates=(19200, 115200),
        )
        assert len(calls) == 2
        assert len(results) == 2

    @pytest.mark.anyio
    async def test_never_raises_on_individual_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One probe's error does not abort the sweep."""

        async def flaky_probe(
            port: str,
            *,
            unit_id: str = "A",
            baudrate: int = 19200,
            timeout: float = 0.2,
        ) -> DiscoveryResult:
            del timeout
            if port == "/dev/ttyUSB1":
                return DiscoveryResult(
                    port=port,
                    unit_id=unit_id,
                    baudrate=baudrate,
                    info=None,
                    error=AlicatTimeoutError("no reply"),
                )
            return DiscoveryResult(
                port=port,
                unit_id=unit_id,
                baudrate=baudrate,
                info=_fake_info(),
                error=None,
            )

        monkeypatch.setattr(discovery, "probe", flaky_probe)
        results = await find_devices(
            ports=["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2"],
            unit_ids=("A",),
            baudrates=(19200,),
        )
        ok = [r for r in results if r.ok]
        err = [r for r in results if not r.ok]
        assert len(ok) == 2
        assert len(err) == 1
        assert err[0].port == "/dev/ttyUSB1"


# ---------------------------------------------------------------------------
# list_serial_ports — smoke test against anyserial
# ---------------------------------------------------------------------------


class TestListSerialPorts:
    @pytest.mark.anyio
    async def test_returns_list_of_strings(self) -> None:
        """Anyserial passthrough — content depends on the test host.

        The list may be empty (no USB serial adapters plugged in), but
        the return type must be ``list[str]`` on every platform.
        """
        ports = await list_serial_ports()
        assert isinstance(ports, list)
        for p in ports:
            assert isinstance(p, str)
