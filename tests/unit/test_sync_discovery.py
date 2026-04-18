"""Tests for :mod:`alicatlib.sync.discovery`.

Sync wrappers delegate to the async helpers through :class:`SyncPortal`;
these tests confirm the plumbing by monkey-patching the async
primitives and asserting the sync wrappers forward args and return
values faithfully.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alicatlib.devices.discovery import DiscoveryResult
from alicatlib.sync import SyncPortal, find_devices, list_serial_ports, probe
from alicatlib.sync import discovery as sync_discovery

if TYPE_CHECKING:
    import pytest


def _make_result(port: str, unit_id: str = "A", baudrate: int = 19200) -> DiscoveryResult:
    return DiscoveryResult(
        port=port,
        unit_id=unit_id,
        baudrate=baudrate,
        info=None,
        error=None,
    )


class TestListSerialPorts:
    def test_owned_portal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _stub() -> list[str]:
            return ["/dev/ttyUSB0", "/dev/ttyUSB1"]

        monkeypatch.setattr(sync_discovery, "async_list_serial_ports", _stub)
        assert list_serial_ports() == ["/dev/ttyUSB0", "/dev/ttyUSB1"]

    def test_shared_portal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _stub() -> list[str]:
            return ["/dev/ttyUSB0"]

        monkeypatch.setattr(sync_discovery, "async_list_serial_ports", _stub)
        with SyncPortal() as portal:
            assert list_serial_ports(portal=portal) == ["/dev/ttyUSB0"]
            assert portal.running is True


class TestProbe:
    def test_forwards_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        async def _stub(
            port: str, *, unit_id: str, baudrate: int, timeout: float
        ) -> DiscoveryResult:
            captured.update(
                {"port": port, "unit_id": unit_id, "baudrate": baudrate, "timeout": timeout},
            )
            return _make_result(port, unit_id, baudrate)

        monkeypatch.setattr(sync_discovery, "async_probe", _stub)
        result = probe("/dev/ttyUSB0", unit_id="B", baudrate=115200, timeout=0.3)
        assert result.port == "/dev/ttyUSB0"
        assert captured == {
            "port": "/dev/ttyUSB0",
            "unit_id": "B",
            "baudrate": 115200,
            "timeout": 0.3,
        }


class TestFindDevices:
    def test_calls_async_with_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        async def _stub(
            ports: list[str] | None,
            *,
            unit_ids: tuple[str, ...],
            baudrates: tuple[int, ...],
            timeout: float,
            max_concurrency: int,
            stop_on_first_hit: bool,
        ) -> tuple[DiscoveryResult, ...]:
            captured.update(
                {
                    "ports": ports,
                    "unit_ids": unit_ids,
                    "baudrates": baudrates,
                    "timeout": timeout,
                    "max_concurrency": max_concurrency,
                    "stop_on_first_hit": stop_on_first_hit,
                },
            )
            return (_make_result("/dev/ttyUSB0"),)

        monkeypatch.setattr(sync_discovery, "async_find_devices", _stub)
        results = find_devices(
            ["/dev/ttyUSB0", "/dev/ttyUSB1"],
            unit_ids=("A", "B"),
            baudrates=(19200,),
            timeout=0.1,
            max_concurrency=4,
            stop_on_first_hit=True,
        )
        assert len(results) == 1
        assert captured["ports"] == ["/dev/ttyUSB0", "/dev/ttyUSB1"]
        assert captured["unit_ids"] == ("A", "B")
        assert captured["max_concurrency"] == 4
        assert captured["stop_on_first_hit"] is True

    def test_none_ports_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _stub(
            ports: list[str] | None,
            **_: object,
        ) -> tuple[DiscoveryResult, ...]:
            assert ports is None
            return ()

        monkeypatch.setattr(sync_discovery, "async_find_devices", _stub)
        assert find_devices() == ()
