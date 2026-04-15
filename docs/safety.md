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

## Tare preconditions

Tare commands assume the device is in the correct physical state (no flow for
flow tare, line depressurized for pressure tare). These are user
responsibilities; the library documents them in docstrings but cannot verify
them.

## Hardware test tiers

| Marker                          | What it does                                    | Opt-in env var                          |
| ------------------------------- | ----------------------------------------------- | --------------------------------------- |
| `hardware`                      | read-only (identify, poll)                      | `PYALICAT_TEST_*_PORT` set              |
| `hardware_stateful`             | changes device state (gas, setpoint, tare)      | `PYALICAT_ENABLE_STATEFUL_TESTS=1`      |
| `hardware_destructive`          | factory reset, baud change, exhaust             | `PYALICAT_ENABLE_DESTRUCTIVE_TESTS=1`   |
