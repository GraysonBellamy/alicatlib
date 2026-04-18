"""Tests for :class:`alicatlib.sync.manager.SyncAlicatManager`.

Uses the same scripted-FakeTransport pattern as ``test_manager.py``
but drives everything through :class:`SyncPortal`. Pre-built clients
are the usual fixture — passing a ``str`` port path would exercise
real :class:`SerialTransport` construction which these tests avoid.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from alicatlib.commands import POLL_DATA, PollRequest
from alicatlib.errors import AlicatValidationError
from alicatlib.protocol import AlicatProtocolClient
from alicatlib.registry import Statistic
from alicatlib.sync import (
    DeviceResult,
    ErrorPolicy,
    SyncAlicatManager,
    SyncFlowController,
    SyncPortal,
)
from alicatlib.transport import FakeTransport
from tests._typing import approx

if TYPE_CHECKING:
    from collections.abc import Mapping

    from alicatlib.transport.fake import ScriptedReply


# ---------------------------------------------------------------------------
# Fixture builders
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
    u = unit_id
    return {
        f"{u}VE\r".encode("ascii"): f"{u}   10v20.0-R24 Jan  9 2025,15:04:07\r".encode("ascii"),
        f"{u}??M*\r".encode("ascii"): _mfg_lines(),
        f"{u}??D*\r".encode("ascii"): _df_lines(),
        f"{u}\r".encode("ascii"): f"{u} +{mass_flow:06.2f}\r".encode("ascii"),
    }


async def _build_client(
    script: Mapping[bytes, ScriptedReply],
) -> AlicatProtocolClient:
    fake = FakeTransport(script, label="fake://test")
    await fake.open()
    return AlicatProtocolClient(fake, multiline_idle_timeout=0.01, default_timeout=0.1)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_empty_manager_opens_and_closes(self) -> None:
        with SyncAlicatManager() as mgr:
            assert mgr.names == ()
            assert not mgr.closed
        assert mgr.closed

    def test_closed_manager_raises_on_reentry(self) -> None:
        mgr = SyncAlicatManager()
        with mgr:
            pass
        with pytest.raises(RuntimeError, match="not reusable"):
            # Inner portal (owned) was one-shot; re-entering should fail.
            mgr.__enter__()

    def test_error_policy_defaults_to_raise(self) -> None:
        mgr = SyncAlicatManager()
        assert mgr.error_policy is ErrorPolicy.RAISE

    def test_error_policy_return_propagates(self) -> None:
        with SyncAlicatManager(error_policy=ErrorPolicy.RETURN) as mgr:
            assert mgr.error_policy is ErrorPolicy.RETURN

    def test_portal_requires_open(self) -> None:
        mgr = SyncAlicatManager()
        with pytest.raises(RuntimeError, match="not open"):
            _ = mgr.portal

    def test_owned_portal_stops_after_exit(self) -> None:
        with SyncAlicatManager() as mgr:
            captured = mgr.portal
            assert captured.running is True
        assert captured.running is False

    def test_shared_portal_survives_multiple_managers(self) -> None:
        with SyncPortal() as portal:
            with SyncAlicatManager(portal=portal) as mgr1:
                assert mgr1.portal is portal
            assert portal.running is True
            with SyncAlicatManager(portal=portal) as mgr2:
                assert mgr2.portal is portal
            assert portal.running is True


# ---------------------------------------------------------------------------
# add / get / remove
# ---------------------------------------------------------------------------


class TestAddRemove:
    def test_add_client_source_returns_sync_device(self) -> None:
        with SyncAlicatManager() as mgr:
            client = mgr.portal.call(_build_client, _happy_script())
            dev = mgr.add("fuel", client)
            assert isinstance(dev, SyncFlowController)
            assert mgr.names == ("fuel",)

    def test_get_returns_cached_instance(self) -> None:
        with SyncAlicatManager() as mgr:
            client = mgr.portal.call(_build_client, _happy_script())
            dev1 = mgr.add("fuel", client)
            dev2 = mgr.get("fuel")
            assert dev1 is dev2

    def test_duplicate_name_raises(self) -> None:
        with SyncAlicatManager() as mgr:
            client = mgr.portal.call(_build_client, _happy_script())
            mgr.add("fuel", client)
            with pytest.raises(AlicatValidationError, match="already in use"):
                mgr.add("fuel", client)

    def test_get_unknown_raises(self) -> None:
        with (
            SyncAlicatManager() as mgr,
            pytest.raises(AlicatValidationError, match="no device named"),
        ):
            mgr.get("ghost")

    def test_remove_drops_name(self) -> None:
        with SyncAlicatManager() as mgr:
            client = mgr.portal.call(_build_client, _happy_script())
            mgr.add("fuel", client)
            mgr.remove("fuel")
            assert mgr.names == ()
            with pytest.raises(AlicatValidationError):
                mgr.get("fuel")

    def test_add_accepts_sync_device_source(self) -> None:
        """A :class:`SyncDevice` is unwrapped before delegation."""
        from alicatlib.sync import Alicat

        fake = FakeTransport(_happy_script())
        with (
            SyncPortal() as portal,
            Alicat.open(fake, portal=portal) as dev,
            SyncAlicatManager(portal=portal) as mgr,
        ):
            registered = mgr.add("fuel", dev)
            # Registered wrapper points at the same async Device.
            assert registered.info.model == dev.info.model
            assert mgr.names == ("fuel",)


# ---------------------------------------------------------------------------
# Concurrent I/O
# ---------------------------------------------------------------------------


class TestPoll:
    def test_poll_returns_device_results(self) -> None:
        script = {**_happy_script("A", mass_flow=10.0), **_happy_script("B", mass_flow=25.0)}
        with SyncAlicatManager() as mgr:
            client = mgr.portal.call(_build_client, script)
            mgr.add("a", client, unit_id="A")
            mgr.add("b", client, unit_id="B")
            results = mgr.poll()
            assert set(results.keys()) == {"a", "b"}
            assert all(isinstance(r, DeviceResult) for r in results.values())
            assert results["a"].ok
            assert results["b"].ok
            assert results["a"].value is not None
            assert results["b"].value is not None
            assert results["a"].value.values["Mass_Flow"] == approx(10.0)
            assert results["b"].value.values["Mass_Flow"] == approx(25.0)

    def test_poll_subset_by_name(self) -> None:
        script = {**_happy_script("A"), **_happy_script("B")}
        with SyncAlicatManager() as mgr:
            client = mgr.portal.call(_build_client, script)
            mgr.add("a", client, unit_id="A")
            mgr.add("b", client, unit_id="B")
            results = mgr.poll(names=["a"])
            assert set(results.keys()) == {"a"}


class TestRequest:
    def test_request_mass_flow_across_devices(self) -> None:
        """`request` drives the DV command across every managed device."""
        script = {
            **_happy_script("A"),
            **_happy_script("B"),
            b"ADV 1 5\r": b"+11.00\r",
            b"BDV 1 5\r": b"+22.00\r",
        }
        with SyncAlicatManager() as mgr:
            client = mgr.portal.call(_build_client, script)
            mgr.add("a", client, unit_id="A")
            mgr.add("b", client, unit_id="B")
            results = mgr.request([Statistic.MASS_FLOW])
            assert results["a"].ok
            assert results["b"].ok
            assert results["a"].value is not None
            assert results["b"].value is not None
            assert results["a"].value.values[Statistic.MASS_FLOW] == approx(11.0)
            assert results["b"].value.values[Statistic.MASS_FLOW] == approx(22.0)


class TestExecute:
    def test_execute_per_device_requests(self) -> None:
        script = {**_happy_script("A"), **_happy_script("B")}
        with SyncAlicatManager() as mgr:
            client = mgr.portal.call(_build_client, script)
            mgr.add("a", client, unit_id="A")
            mgr.add("b", client, unit_id="B")
            results = mgr.execute(
                POLL_DATA,
                {"a": PollRequest(), "b": PollRequest()},
            )
            assert results["a"].ok
            assert results["b"].ok
