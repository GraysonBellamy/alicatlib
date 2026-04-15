# Testing

95%+ of the test suite runs with no hardware attached, using `FakeTransport` and
golden response fixtures. See the [Design doc](design.md) §6 for the full
strategy.

## Running tests

```bash
uv run pytest                              # fast suite, no hardware
uv run pytest -m hardware                  # read-only hardware tests
uv run pytest -m hardware_stateful         # requires PYALICAT_ENABLE_STATEFUL_TESTS=1
```

## Writing a test without hardware

```python
from alicatlib.testing import FakeTransport

async def test_gas_select_encode() -> None:
    fake = FakeTransport({b"AGS 5\r": [b"A 5 N2 Nitrogen\r"]})
    # ... drive a Session through the fake and assert decode output
```

## Capturing fixtures from hardware

```python
from alicatlib.testing import record_session
await record_session(device, scenario="gas-list")
# writes tests/fixtures/responses/gas-list.txt
```
