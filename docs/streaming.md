# Streaming mode

Streaming mode is a **port-level state transition**, not a
request/response command. When a device enters streaming, it stops
responding to prompts, overwrites its unit-id letter on the wire with
`@` (or a space on 10v20 — see below), and pushes data frames
continuously at a configured rate until stopped. The
[`StreamingSession`](../src/alicatlib/devices/streaming.py) runtime
owns that mode transition, the producer loop, and the teardown
contract.

See [Design](design.md) §5.8 for the authoritative architecture and
§15.3 for the hardware-driven corrections that shaped the implementation.

## Opening a stream

```python
async with await open_device("/dev/ttyUSB0") as dev:
    async with dev.stream(rate_ms=50) as stream:
        async for frame in stream:
            print(frame.get_float("Mass_Flow"))
```

[`Device.stream(...)`](../src/alicatlib/devices/base.py#L839) returns
a `StreamingSession` — both an async context manager and an async
iterator. The context's lifetime is the streaming session's lifetime:
enter sends start-stream, exit sends stop-stream, and a crashed
consumer never leaves the device flooding the bus because teardown
runs under `__aexit__` even when the body raises.

### Parameters

| Parameter | Default | Purpose |
| --- | --- | --- |
| `rate_ms` | `None` | If set, configures `NCS` before entering streaming mode. `0` is "as-fast-as-possible"; distinct from `None` ("leave device at its current rate"). Firmware-gated at V10 >= 10v05 by the `STREAMING_RATE` command. |
| `strict` | `False` | When `True`, `AlicatParseError` from a malformed frame propagates out of `__anext__` and tears the stream down. When `False`, the error is logged at WARN and the producer continues. |
| `overflow` | `DROP_OLDEST` | Buffer backpressure policy — see below. |
| `buffer_size` | `256` | Bounded producer/consumer buffer depth. 256 frames at 50 ms is ~13 s of backlog. |

## Mode transition

Entering streaming mode runs a four-step sequence under the port lock
([devices/streaming.py:169](../src/alicatlib/devices/streaming.py#L169)):

1. **Cached format.** Lazy-probe `??D*` if the session doesn't have a
   cached `DataFrameFormat`. Streaming has to parse every frame, so a
   missing format is a hard error before any producer starts.
2. **Optional `NCS` rate.** Runs as a normal request/response command
   *before* the streaming latch flips, so the dispatch gate still
   allows it.
3. **Atomic latch + start-stream write.** Acquires the port lock,
   verifies the client isn't already streaming, flips
   `AlicatProtocolClient.is_streaming`, writes `{unit_id}@ @\r`
   directly to the transport, releases the lock. Holding the lock
   across the latch + write is what makes the transition atomic with
   respect to other sessions on the same client.
4. **Producer task.** Starts inside a task group whose lifetime
   matches the CM. Cancelled and joined on exit.

## The `is_streaming` latch

`AlicatProtocolClient.is_streaming` is the dispatch gate. While it's
set, every `Session.execute` / `poll` / `request` call on **any**
session sharing this client's port fails fast with
`AlicatStreamingModeError` — measured at 0.088 ms with zero tx on
real hardware. One streamer per port is a hard invariant; attempting
to enter a second `StreamingSession` while one is active raises
immediately without touching the wire.

The latch is cleared in `__aexit__` *after* the stop-stream write
and drain, so the next `poll()` on the session cannot race the
device's shutdown of its continuous push.

## Overflow policy

[`OverflowPolicy`](../src/alicatlib/streaming/__init__.py) controls
what the producer does when the bounded buffer is full:

| Policy | Behaviour |
| --- | --- |
| `DROP_OLDEST` (default) | Evict the oldest queued frame, then enqueue. Latest-data-wins — the right default for high-rate telemetry where staleness matters more than completeness. |
| `DROP_NEWEST` | Drop the frame that was about to be enqueued. Keeps the oldest queued frame; useful when you want to preserve a coherent window. |
| `BLOCK` | Await the slow consumer. Valid but risks the OS-level serial buffer dropping bytes if the consumer stays behind for long — the kernel tty buffer is finite, and the device keeps pushing. |

`StreamingSession.dropped_frames` counts frames the producer had to
discard under `DROP_OLDEST` / `DROP_NEWEST`. Available after the CM
exits.

## Parse-error handling

The producer parses each line through the session's cached
[`DataFrameFormat`](data-frames.md). Malformed frames are handled by
the `strict` flag:

- `strict=False` (default) — logs a WARN with the raw bytes and
  continues. A single bad frame on a flaky RS-485 bus doesn't kill
  the stream.
- `strict=True` — records the error, cancels the producer, and
  re-raises the `AlicatParseError` out of the consumer's
  `__anext__` so the `async for` loop surfaces the real exception.

Transport failures (port yanked, process shutdown) always tear the
stream down — no knob to disable that. The error propagates through
`__anext__` so the consumer sees the cause.

## Wire-shape normalisation

The primer says a streaming device "changes its unit-id letter to
`@`". Empirically on 10v20, the letter is **dropped entirely**,
leaving a leading space. The producer normalises either form back to
the request/response shape by prepending the session's unit id —
`self._session.unit_id.encode("ascii") + stripped[1:]` — so the
single `DataFrameFormat.parse` path handles both. See
[devices/streaming.py:394](../src/alicatlib/devices/streaming.py#L394)
for the exact dispatch.

## Stop-stream and recovery

`__aexit__` is load-bearing ([streaming.py:241](../src/alicatlib/devices/streaming.py#L241)):

1. Cancel the producer task group and close the send side of the
   buffer so any pending `__anext__` receives `StopAsyncIteration` or
   the re-raised strict-mode error.
2. Under the port lock, write `@@ {unit_id}\r` and drain with a
   100 ms idle window. If the transport is already torn down
   (`AlicatTransportError` during the write), the exception is
   suppressed — the device is the caller's problem at that point,
   but the latch still needs clearing.
3. **Always** clear the streaming latch, even if the stop-stream
   write failed. Leaving it set would permanently brick the client
   for request/response use.

### Stale-stream recovery on open

If a prior process left a device streaming,
[`open_device`](../src/alicatlib/devices/factory.py#L988) detects
this during the identification pipeline's passive sniff. The factory
issues the stop-stream bytes directly (bypassing the session layer
because the session doesn't exist yet) and drains before `VE` runs.
The passive sniff and the post-stop drain are both capped at
256 bytes — the uncapped form **deadlocks** `open_device` against a
device continuously streaming at its 50 ms default rate, because the
bus never goes idle for the 100 ms window the read needs to return.
See design §15.3 for the hardware-day diagnosis.

## Sync streaming

[`SyncDevice.stream(...)`](../src/alicatlib/sync/device.py#L342) returns
a [`SyncStreamingSession`](../src/alicatlib/sync/device.py#L506) — a
sync context manager and a sync iterator:

```python
with sync_dev.stream(rate_ms=50) as stream:
    for frame in stream:
        process(frame)
```

The sync wrapper enters and exits the underlying async
`StreamingSession` via `SyncPortal.wrap_async_context_manager`, **not**
`portal.call(__aenter__)`. `portal.call` wraps each call in its own
`CancelScope`; `StreamingSession.__aenter__` enters a long-lived task
group that outlives the entry call, so the nested scope hierarchy
becomes inconsistent at exit and raises
`RuntimeError: Attempted to exit a cancel scope that isn't the current
task's current cancel scope` on real hardware.
`wrap_async_context_manager` lets anyio own the portal-side scope for
the full CM lifetime, which is the fix. Design §15.3 has the full
narrative.

## Streaming vs. `record()`

Two primitives, different use cases:

| Primitive | Timing source | Use when |
| --- | --- | --- |
| [`StreamingSession`](../src/alicatlib/devices/streaming.py) | Device-driven; frames arrive when the device sends them | Highest rates (device's `NCS`-configured cadence), one device per port |
| [`record()`](logging.md#recorder) | Host-driven absolute-target scheduler over `poll()` | Multi-device acquisition, cadence chosen by host, sink integration via `pipe()` |

Both produce `DataFrame` values; both honour overflow policies; both
integrate with sinks (streaming via the iterator + user code;
`record()` via `pipe()`). The streaming runtime is the right choice
for high-rate single-device capture; `record()` is the right choice
for everything else, especially multi-device runs.

See [logging.md](logging.md) for the recorder side and the sink
ecosystem.
