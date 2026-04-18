# Safety

`alicatlib` drives physical hardware. Safety rules are binding; see the
[Design doc](design.md) §5.20 for the authoritative list.

## Destructive operations require `confirm=True`

Commands that can damage equipment, lose data, or desync the serial link raise
`AlicatValidationError` *before* any I/O if `confirm` is not `True`. Examples:

- Factory restore
- Baud change
- Unit-ID change
- Valve exhaust, valve-hold-closed
- Overpressure disable
- Power-up setpoint
- Gas-mix deletion

## Setpoint validation

`FlowController.setpoint(value, unit)` checks `value` against the device's
full-scale range (cached at session startup). Out-of-range requests raise
`AlicatValidationError` before I/O.

### V1_V7 / pre-9v00 V8_V9 / GP caveat

On firmware families without the `LSS` command, the library cannot probe
the setpoint source. If the device's front-panel source is configured to
**Analog** or **User-knob** rather than Serial, a `dev.setpoint(value)`
call reaches the wire cleanly but the device silently ignores it — the
setpoint follows the analog input instead. The library returns a valid
`SetpointState` built from the post-op data frame, but that frame will
reflect the actual setpoint (driven by analog), not the commanded one.

Users on these firmware families must configure the setpoint source to
Serial via the front panel before opening the device. The library cannot
verify this remotely.

## Display lock recovery

`dev.unlock_display()` is intentionally NOT gated on
`Capability.DISPLAY` — it is the safety escape for a locked device.
Always callable.

On V1_V7 firmware, the device parses any command starting with `AL<X>`
(e.g. `ALS`, `ALSS`, `ALV`) as "lock display with argument X" and sets
the `LCK` status bit. The library's firmware gates protect these
tokens under normal facade use (they never reach V1_V7 hardware), but
third-party code or direct `session.execute(...)` can still trip it.
Call `dev.unlock_display()` to recover.

## Tare preconditions

Tare commands assume the device is in the correct physical state (no flow for
flow tare, line depressurized for pressure tare). These are user
responsibilities; the library documents them in docstrings but cannot verify
them.

## Hardware test tiers

| Marker                          | What it does                                    | Opt-in env var                          |
| ------------------------------- | ----------------------------------------------- | --------------------------------------- |
| `hardware`                      | read-only (identify, poll)                      | `ALICATLIB_TEST_*_PORT` set              |
| `hardware_stateful`             | changes device state (gas, setpoint, tare)      | `ALICATLIB_ENABLE_STATEFUL_TESTS=1`      |
| `hardware_destructive`          | factory reset, baud change, exhaust             | `ALICATLIB_ENABLE_DESTRUCTIVE_TESTS=1`   |
