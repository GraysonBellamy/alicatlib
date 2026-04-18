"""Comprehensive per-device diagnostic capture for hardware day.

Connects to an Alicat device, auto-detects baud rate and unit id, stops any
active streaming, then walks an opt-in list of *read-only* commands and saves
every reply to a per-device directory. Designed for the
"plug in N devices for two minutes each" workflow — each captured directory
is timestamped and provenance-stamped (model, firmware, baud, port) so the
captures stay disambiguable even after they've been triaged into
``tests/fixtures/responses/``.

The script is intentionally **read-only by default** — every command issued
either queries the device or describes its capabilities. State-changing
commands (setpoint set, gas set, tare, baud change, unit-id change) are
behind a ``--state-changing`` flag and skipped by default. Destructive
commands (reset totalizer, factory restore) are not implemented here at all.

Usage:
    uv run python scripts/diag_capture.py
    uv run python scripts/diag_capture.py --port /dev/ttyUSB0
    uv run python scripts/diag_capture.py --port /dev/ttyUSB0 --baud 115200 --unit-id B
    uv run python scripts/diag_capture.py --out /tmp/v10_diag --note "10v04 found in lab drawer"

Output layout per run::

    <out_dir>/
        meta.json           — JSON: port, baud, unit_id, captured_at, note, ve_raw, model
        ve.txt              — VE
        mm.txt              — ??M*
        dd.txt              — ??D*
        poll.txt            — single A\\r poll
        gg.txt              — ??G* (mass-flow only)
        ls_query.txt        — LS query (controllers)
        lss_query.txt       — LSS query (controllers)
        lv_query.txt        — LV query (controllers, 9v00+)
        dcu_*.txt           — DCU statistic queries
        fpf_*.txt           — FPF statistic queries
        vd_query.txt        — VD valve drive (controllers, 8v18+)
        lcdb_query.txt      — LCDB deadband (controllers, 10v05+)
        lca_query.txt       — LCA loop algorithm (controllers, 10v05+)
        ncb_query.txt       — NCB baud query (10v05+)
        ncs_query.txt       — NCS streaming rate (10v05+)
        zca_query.txt       — ZCA auto-tare (controllers, 10v05+)
        zcp_query.txt       — ZCP power-up tare (10v05+)
        ud_*.txt            — UD user-data slots (8v24+)

The meta.json captures device-identifying info so you can grep across
captures later: "give me every device that exposes BAROMETER" maps to
"every meta.json with capability_baro_present == true".
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import anyio

from alicatlib.protocol import AlicatProtocolClient
from alicatlib.transport import SerialSettings, SerialTransport

# Common Alicat baud rates in observed-frequency order.
# Note: documented Alicat factory default is 19200 but a 2026-04-17 8v17
# device shipped at 115200, so neither is guaranteed.
_CANDIDATE_BAUDS: tuple[int, ...] = (19200, 115200, 9600, 38400, 57600)
_CANDIDATE_UNIT_IDS: str = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Statistic codes worth probing with DCU and FPF on every device. Keep small
# to limit per-device wire time; expand by editing this list and re-running.
# Codes per primer Appendix A:
#   2=abs_press 3=temp_stream 4=vol_flow 5=mass_flow 6=gauge_press
#   7=diff_press 15=baro_press 37=mass_flow_setpt 344=abs_press_2nd
_PROBE_STATISTICS: tuple[tuple[int, str], ...] = (
    (2, "abs_press"),
    (3, "temp_stream"),
    (4, "vol_flow"),
    (5, "mass_flow"),
    (6, "gauge_press"),
    (7, "diff_press"),
    (15, "baro_press"),
    (37, "mass_flow_setpt"),
    (344, "abs_press_2nd"),
)


@dataclass(frozen=True, slots=True)
class CaptureMeta:
    """Per-device provenance for a captured directory."""

    port: str
    baud: int
    unit_id: str
    captured_at: str
    note: str
    ve_raw: str | None = None
    model: str | None = None
    firmware: str | None = None
    capability_baro_present: bool | None = None
    capability_secondary_pressure_present: bool | None = None


# ---------------------------------------------------------------------------
# Discovery / stream-recovery helpers
# ---------------------------------------------------------------------------


async def _drain(transport: SerialTransport) -> int:
    """Drain any buffered bytes; return how many CR-terminated lines we ate."""
    count = 0
    while True:
        try:
            await transport.read_until(b"\r", 0.15)
            count += 1
        except Exception:
            return count


async def _stop_stream_all(transport: SerialTransport) -> None:
    """Send `@@ <id>` for every candidate unit id, then drain.

    We don't know which ID is streaming, so we broadcast stop. This is
    safe: a device not in stream mode silently ignores the redundant @@.
    """
    for letter in _CANDIDATE_UNIT_IDS:
        try:
            await transport.write(f"@@ {letter}\r".encode("ascii"), timeout=0.3)
        except Exception:
            pass
    await anyio.sleep(0.4)
    await _drain(transport)


async def _try_probe(port: str, baud: int) -> tuple[int, str] | None:
    """Open at ``baud``, stop any stream, probe VE on each unit id."""
    transport = SerialTransport(SerialSettings(port=port, baudrate=baud))
    await transport.open()
    try:
        try:
            sniff = await transport.read_until(b"\r", 0.4)
        except Exception:
            sniff = b""
        if sniff:
            await _stop_stream_all(transport)

        client = AlicatProtocolClient(transport)
        for letter in _CANDIDATE_UNIT_IDS:
            try:
                raw = await client.query_line(f"{letter}VE\r".encode("ascii"), timeout=0.6)
            except Exception:
                continue
            if raw and raw.lstrip().startswith(letter.encode("ascii")):
                return baud, letter
        return None
    finally:
        await transport.close()


async def _discover(port: str) -> tuple[int, str]:
    print(f"[discover] probing {port} for baud + unit id …", flush=True)
    for baud in _CANDIDATE_BAUDS:
        result = await _try_probe(port, baud)
        if result is not None:
            baud, letter = result
            print(f"[discover] found unit_id={letter!r} at baud={baud}", flush=True)
            return baud, letter
    raise SystemExit(f"no Alicat device responded on {port} at any candidate baud rate")


# ---------------------------------------------------------------------------
# Capture helpers
# ---------------------------------------------------------------------------


async def _drain_stale_input(client: AlicatProtocolClient) -> int:
    """Drain any bytes the device left in the buffer after a previous reply.

    Defends against the buffer-bleed pattern observed on the 6v21 capture
    (design §16.6.4): a command rejected with `?` may still emit residual
    bytes that bleed into the next command's read. We use the underlying
    transport's drain helper since the protocol client doesn't expose
    one directly.
    """
    transport = client.transport
    drained = await transport.read_available(idle_timeout=0.05)
    if drained:
        print(
            f"[drain] discarded {len(drained)} stale byte(s): {drained!r}",
            flush=True,
        )
        return len(drained)
    return 0


async def _capture_line(
    client: AlicatProtocolClient,
    out_dir: Path,
    label: str,
    wire: bytes,
    *,
    timeout: float = 1.0,
) -> bytes | None:
    """Run a single-line query, save to ``<label>.txt``, return raw reply."""
    await _drain_stale_input(client)
    print(f"[{label}] > {wire!r}", flush=True)
    try:
        raw = await client.query_line(wire, timeout=timeout)
    except Exception as exc:
        print(f"[{label}] ERR: {type(exc).__name__}: {exc}", flush=True)
        (out_dir / f"{label}.txt").write_text(
            f"# scenario: ERROR running {wire!r} ({type(exc).__name__}: {exc})\n",
            encoding="utf-8",
        )
        return None
    print(f"[{label}] < {raw!r}", flush=True)
    (out_dir / f"{label}.txt").write_text(
        f"> {wire.decode('ascii', errors='replace').rstrip()}\n"
        f"< {raw.decode('ascii', errors='replace')}\n",
        encoding="utf-8",
    )
    return raw


async def _capture_lines(
    client: AlicatProtocolClient,
    out_dir: Path,
    label: str,
    wire: bytes,
    *,
    first_timeout: float = 1.0,
    idle_timeout: float = 0.5,
    max_lines: int = 64,
) -> tuple[bytes, ...] | None:
    """Run a multi-line query, save reply, return tuple of lines."""
    await _drain_stale_input(client)
    print(f"[{label}] > {wire!r}", flush=True)
    try:
        lines = await client.query_lines(
            wire,
            first_timeout=first_timeout,
            idle_timeout=idle_timeout,
            max_lines=max_lines,
        )
    except Exception as exc:
        print(f"[{label}] ERR: {type(exc).__name__}: {exc}", flush=True)
        (out_dir / f"{label}.txt").write_text(
            f"# scenario: ERROR running {wire!r} ({type(exc).__name__}: {exc})\n",
            encoding="utf-8",
        )
        return None
    out_lines = [
        "> " + wire.decode("ascii", errors="replace").rstrip(),
        *(f"< {line.decode('ascii', errors='replace')}" for line in lines),
    ]
    for ln in out_lines:
        print(f"[{label}] {ln}", flush=True)
    (out_dir / f"{label}.txt").write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return lines


def _extract_model_from_mm(mm_lines: tuple[bytes, ...] | None) -> str | None:
    """Pull the M04 ``Model Number <model>`` value out of a ??M* response."""
    if not mm_lines:
        return None
    for line in mm_lines:
        text = line.decode("ascii", errors="replace")
        match = re.match(r"^\S+\s+M0?4\s+(?:Model Number\s+)?(\S+)", text)
        if match is not None:
            return match.group(1)
    return None


def _extract_firmware_from_ve(ve_raw: bytes | None) -> str | None:
    """Pull the firmware token out of a VE response (e.g. `10v20.0-R24`)."""
    if not ve_raw:
        return None
    text = ve_raw.decode("ascii", errors="replace").strip()
    # Drop the leading unit-id token; firmware is the next non-whitespace run.
    parts = text.split(None, 2)
    if len(parts) < 2:
        return None
    return parts[1]


def _fpf_reply_indicates_present(path: Path) -> bool:
    """Classify an ``FPF <stat>`` capture as "statistic present on device".

    Per design §16.6.3 the absent pattern is ``A <zero> 1 ---`` and the
    present pattern is ``A <non-zero> <code> <real_label>``. A capture
    file that never ran (missing) or that ERRORed at the wire also
    counts as absent. The parse walks every ``< <reply>`` line in the
    capture so a device that emitted multiple candidate replies is
    classified by the first parsable one.
    """
    if not path.exists():
        return False
    text = path.read_text()
    if "ERROR" in text:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("<"):
            continue
        fields = line[1:].split()
        # Expect ``<uid> <value> <unit_code> <unit_label>`` — 4 fields.
        if len(fields) != 4:
            continue
        _uid, value_s, _code, label = fields
        if label == "---":
            return False
        try:
            value = float(value_s)
        except ValueError:
            continue
        if value > 0:
            return True
    return False


# ---------------------------------------------------------------------------
# Command tier wrappers — each returns nothing; just performs captures.
# ---------------------------------------------------------------------------


async def _capture_identification(
    client: AlicatProtocolClient,
    unit_id: str,
    out_dir: Path,
) -> tuple[bytes | None, tuple[bytes, ...] | None, tuple[bytes, ...] | None]:
    """VE + ??M* + ??D* — the identification triple. Always safe / read-only."""
    ve_raw = await _capture_line(
        client,
        out_dir,
        "ve",
        f"{unit_id}VE\r".encode("ascii"),
    )
    mm_lines = await _capture_lines(
        client,
        out_dir,
        "mm",
        f"{unit_id}??M*\r".encode("ascii"),
        first_timeout=2.0,
        idle_timeout=0.6,
        max_lines=14,
    )
    dd_lines = await _capture_lines(
        client,
        out_dir,
        "dd",
        f"{unit_id}??D*\r".encode("ascii"),
        first_timeout=2.0,
        idle_timeout=0.6,
        max_lines=64,
    )
    return ve_raw, mm_lines, dd_lines


async def _capture_data_readings(
    client: AlicatProtocolClient,
    unit_id: str,
    out_dir: Path,
) -> None:
    """Single poll, plus DCU and FPF on a fixed set of probe statistics."""
    await _capture_line(
        client,
        out_dir,
        "poll",
        f"{unit_id}\r".encode("ascii"),
    )
    for code, slug in _PROBE_STATISTICS:
        await _capture_line(
            client,
            out_dir,
            f"dcu_{slug}",
            f"{unit_id}DCU {code}\r".encode("ascii"),
        )
        await _capture_line(
            client,
            out_dir,
            f"fpf_{slug}",
            f"{unit_id}FPF {code}\r".encode("ascii"),
        )


async def _capture_gas_commands(
    client: AlicatProtocolClient,
    unit_id: str,
    out_dir: Path,
) -> None:
    """??G* + GS query. Skipped silently on devices that don't support them."""
    await _capture_lines(
        client,
        out_dir,
        "gg",
        f"{unit_id}??G*\r".encode("ascii"),
        first_timeout=2.0,
        idle_timeout=0.6,
        max_lines=512,
    )
    await _capture_line(
        client,
        out_dir,
        "gs_query",
        f"{unit_id}GS\r".encode("ascii"),
    )


async def _capture_controller_queries(
    client: AlicatProtocolClient,
    unit_id: str,
    out_dir: Path,
) -> None:
    """LS / LSS / LV / VD / LCDB / LCA — controller-only, all read-only."""
    await _capture_line(client, out_dir, "ls_query", f"{unit_id}LS\r".encode("ascii"))
    await _capture_line(client, out_dir, "lss_query", f"{unit_id}LSS\r".encode("ascii"))
    await _capture_line(client, out_dir, "lv_query", f"{unit_id}LV\r".encode("ascii"))
    await _capture_line(client, out_dir, "vd_query", f"{unit_id}VD\r".encode("ascii"))
    await _capture_line(client, out_dir, "lcdb_query", f"{unit_id}LCDB\r".encode("ascii"))
    await _capture_line(client, out_dir, "lca_query", f"{unit_id}LCA\r".encode("ascii"))


async def _capture_device_setup(
    client: AlicatProtocolClient,
    unit_id: str,
    out_dir: Path,
) -> None:
    """NCB / NCS / ZCA / ZCP / UD slots — all read-only setup queries."""
    await _capture_line(client, out_dir, "ncb_query", f"{unit_id}NCB\r".encode("ascii"))
    await _capture_line(client, out_dir, "ncs_query", f"{unit_id}NCS\r".encode("ascii"))
    await _capture_line(client, out_dir, "zca_query", f"{unit_id}ZCA\r".encode("ascii"))
    await _capture_line(client, out_dir, "zcp_query", f"{unit_id}ZCP\r".encode("ascii"))
    for slot in range(4):
        await _capture_line(
            client,
            out_dir,
            f"ud_slot{slot}",
            f"{unit_id}UD {slot}\r".encode("ascii"),
        )


# ---------------------------------------------------------------------------
# Main capture sequence
# ---------------------------------------------------------------------------


async def _amain(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.baud and args.unit_id:
        baud, unit_id = args.baud, args.unit_id
        print(
            f"[discover] using user-supplied baud={baud} unit_id={unit_id!r}",
            flush=True,
        )
    else:
        baud, unit_id = await _discover(args.port)

    transport = SerialTransport(SerialSettings(port=args.port, baudrate=baud))
    await transport.open()
    try:
        # In case streaming restarted between discover and now.
        try:
            sniff = await transport.read_until(b"\r", 0.2)
        except Exception:
            sniff = b""
        if sniff:
            await _stop_stream_all(transport)

        client = AlicatProtocolClient(transport)

        # 1. Identification (always)
        ve_raw, mm_lines, _ = await _capture_identification(client, unit_id, out_dir)

        # 2. Data readings (always)
        await _capture_data_readings(client, unit_id, out_dir)

        # 3. Gas commands (mass-flow only — skipped silently if device rejects)
        if not args.skip_gas:
            await _capture_gas_commands(client, unit_id, out_dir)

        # 4. Controller queries (skipped silently if device rejects)
        if not args.skip_controller:
            await _capture_controller_queries(client, unit_id, out_dir)

        # 5. Device-setup queries
        if not args.skip_setup:
            await _capture_device_setup(client, unit_id, out_dir)

        # Pull semantic info from the captures for meta.json.
        firmware = _extract_firmware_from_ve(ve_raw)
        model = _extract_model_from_mm(mm_lines)
        # Capability hints. Per design §16.6.3, an FPF on an absent
        # statistic returns the sentinel shape ``A <zero> 1 ---`` (value
        # is zero *and* the unit label is ``---``). A real reading has a
        # non-zero value and a real label. Checking only for the absence
        # of ``ERROR`` previously marked absent devices as present.
        baro_path = out_dir / "fpf_baro_press.txt"
        baro_present = _fpf_reply_indicates_present(baro_path)
        sec_path = out_dir / "fpf_abs_press_2nd.txt"
        sec_present = _fpf_reply_indicates_present(sec_path)

        meta = CaptureMeta(
            port=args.port,
            baud=baud,
            unit_id=unit_id,
            captured_at=datetime.now().astimezone().isoformat(),
            note=args.note,
            ve_raw=ve_raw.decode("ascii", errors="replace") if ve_raw else None,
            model=model,
            firmware=firmware,
            capability_baro_present=baro_present,
            capability_secondary_pressure_present=sec_present,
        )
        (out_dir / "meta.json").write_text(
            json.dumps(asdict(meta), indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\n[done] captures saved to {out_dir}/", flush=True)
        print(f"  model:    {model!r}", flush=True)
        print(f"  firmware: {firmware!r}", flush=True)
        print(f"  baud:     {baud}", flush=True)
        print(f"  unit_id:  {unit_id!r}", flush=True)
    finally:
        await transport.close()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--port",
        default="/dev/ttyUSB0",
        help="Serial port path (default: /dev/ttyUSB0)",
    )
    p.add_argument(
        "--baud",
        type=int,
        default=0,
        help="Baud rate (skip discovery if --unit-id is also set)",
    )
    p.add_argument(
        "--unit-id",
        default="",
        help="Unit id letter (skip discovery if --baud is also set)",
    )
    p.add_argument(
        "--out",
        default=f"{tempfile.gettempdir()}/alicat_diag_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        help="Output directory (default: <tmpdir>/alicat_diag_<timestamp>)",
    )
    p.add_argument(
        "--note",
        default="",
        help="Free-text provenance note (e.g. 'lab drawer 2, MCR-200SLPM-D')",
    )
    p.add_argument(
        "--skip-gas",
        action="store_true",
        help="Skip ??G* and GS captures (use on liquid / pressure devices)",
    )
    p.add_argument(
        "--skip-controller",
        action="store_true",
        help="Skip LS / LSS / LV / VD / LCDB / LCA (use on plain meters)",
    )
    p.add_argument(
        "--skip-setup",
        action="store_true",
        help="Skip NCB / NCS / ZCA / ZCP / UD captures",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        anyio.run(_amain, args)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
