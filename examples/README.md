# Examples

Runnable scripts demonstrating the alicatlib public surface. Each file is
a standalone `.py` — no package layout, no helper modules. Run with
`uv run python examples/<file>.py` (or plain `python` once the env is
active).

Most examples read the serial port from the `PORT` environment variable
and fall back to `/dev/ttyUSB0`:

```bash
PORT=/dev/ttyUSB1 uv run python examples/01_read_once_sync.py
```

Multi-device examples read `PORT1` / `PORT2`.

## Needs real hardware

- `01_read_once_sync.py` — open a port, poll once, print mass flow / pressure / temperature.
- `02_setpoint_and_hold_sync.py` — ramp a controller to setpoint, hold briefly, return to zero.
- `03_gas_select_sync.py` — read the active gas, change it (non-persistent), read back.
- `04_multi_device_csv_sync.py` — two devices at 10 Hz for 10 s into a CSV file.
- `05_async_basic.py` — async mirror of example 01.
- `06_async_streaming.py` — high-rate streaming into an `InMemorySink`, with an overflow-policy demo.
- `07_async_multi_device_sqlite.py` — async multi-device recording into a SQLite WAL database.
- `08_discover_ports.py` — enumerate serial ports and probe them for Alicat devices.

## Runs anywhere (no hardware)

- `09_offline_with_fake_transport.py` — drive the full stack against a scripted `FakeTransport`.
  Useful as a CI smoke test and for evaluating the library without a device.

## See also

- [../docs/quickstart-sync.md](../docs/quickstart-sync.md)
- [../docs/quickstart-async.md](../docs/quickstart-async.md)
- [../docs/logging.md](../docs/logging.md) — recorder, sinks, `pipe()`.
- [../docs/safety.md](../docs/safety.md) — destructive commands and setpoint rules.
