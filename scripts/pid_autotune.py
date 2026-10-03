"""Closed-loop gain autotuner for Alicat flow controllers.

Finds PD/PDF (or PD²I) gains that hold a flow setpoint tightly and recover
quickly from upsets, by running small step experiments on the live device
and searching the gain space with a noise-aware compass (pattern) search.

Subcommands
-----------

``probe``    Read-only. Identifies the device, reports the loop algorithm,
             current gains, setpoint source, ramp / deadband config, valve
             drive, poll rate and hold noise, and says which gain-access
             path ``tune`` would use. Run this first.
``monitor``  Watches the loop holding a setpoint for N minutes and
             characterises every excursion (peak, recovery time, valve
             saturation, pressure correlation). Use it to diagnose the
             real dips / rises and to compare before vs after tuning.
``tune``     The autotuner. Backs up current gains, calibrates the step
             window, evaluates the baseline, searches, confirms the winner
             against the baseline head-to-head, runs a steady-hold test, then
             applies (optionally saves) or restores.
``apply``    Write explicit gains (verified by read-back), optionally saved.
``restore``  Re-apply the gains recorded in a ``backup_gains.json``.

Usage (``--baud`` defaults to auto-detect; pass it to skip the sweep)::

    uv run python scripts/pid_autotune.py probe   --port COM7 --baud 115200
    uv run python scripts/pid_autotune.py monitor --port COM7 --baud 115200 --setpoint 185 \\
        --duration 600
    uv run python scripts/pid_autotune.py tune    --port COM7 --baud 115200 --setpoint 185 \\
        --legacy-registers
    uv run python scripts/pid_autotune.py tune    --port COM7 --baud 115200 --setpoint 185 \\
        --legacy-registers --step-pct 3 --min-flow 160 --max-flow 195 --final-setpoint 185
    uv run python scripts/pid_autotune.py apply   --port COM7 --p 180 --d 3200 --legacy-registers
    uv run python scripts/pid_autotune.py restore --port COM7 --legacy-registers \\
        autotune_runs/<run>/backup_gains.json

A device left in streaming mode answers every command with data frames; pass
``--stop-stream`` once to drop it to polling mode as unit ``--unit-id``.

Method
------

Before the first trial the script moves to ``S`` and waits until flow holds
there (``--approach-timeout``); if it cannot, gas supply or valve authority is
the problem and tuning stops. A calibration run on the current gains then sizes
the per-step window (2.5 × baseline settling time, clamped to
``[--min-window, --max-window]``) and is kept fixed for every later trial.

Each *trial* writes candidate gains (volatile, ``save=0`` where the firmware
allows it), lets the loop settle, measures hold noise at the operating
setpoint ``S``, then runs a step pattern ``S → S−Δ → S → S+Δ → S`` (or
down-only when ``S+Δ`` would leave the safe range). Every step yields
settling time to a ±band, IAE, ITAE, overshoot, rise time and tail noise.
A step ends early once flow has stayed in band for ``--early-stop-hold``
seconds (and at least as long as it took to get there); the in-band tail
error is extrapolated to the full window so short and long steps compare
fairly.
The trial cost is the median over steps of
``w_settle·t_settle + w_iae·IAE + w_overshoot·overshoot`` plus
``w_noise·max(0, σ_hold/σ_hold_baseline − 1)``. Medians make a trial robust to
one step being hit by a real process disturbance.

Setpoint steps are a valid proxy for disturbance recovery: setpoint and
load-disturbance responses share the closed-loop poles (settling speed and
damping), and differ only in their zeros. A loop that settles a small step
fast and without ringing will also recover from a supply-pressure dip
faster. ``monitor`` measures the real disturbances so you can verify that.

The search runs in log₂ space relative to the baseline gains (multiplicative
moves, since Alicat gains are dimensionless 0–65535 integers with unknown
internal scaling). It accepts a move only if it beats the incumbent by a
margin derived from the measured trial-to-trial noise, re-evaluates the
incumbent whenever the step shrinks (to undo "lucky" wins), and expands the
step after consecutive successes in the same direction.

Gain access
-----------

* **10v05+ firmware** — ``LCA`` (algorithm), ``LCGD 0 <save> <p> <d>``
  (PD/PDF gains), ``LCG 0 <save> <p> <i> <d>`` (PD²I gains), per the
  Alicat Serial Primer (Rev. 2). Candidate gains are written with
  ``save=0`` (RAM only), so a power cycle reverts to the stored gains and the
  search does no EEPROM writes. Only the final ``--apply yes`` / ``--save``
  write uses ``save=1``.
* **Pre-10v05 firmware** — no gain commands exist; this script falls back
  to the register interface (``$$R21`` P, ``$$R22`` D, ``$$R23`` I) used by
  the community ``numat/alicat`` driver. It is *not* in the primer, register
  writes are assumed to persist, and every write is verified by read-back.
  ``tune`` / ``apply`` refuse to write registers unless you pass
  ``--legacy-registers``. Keep the ``backup_gains.json`` so ``restore`` can
  put the originals back.

Note on PD/PDF naming: the primer documents that in the PDF algorithm the
``p_gain`` slot acts as the *integral-like* gain and ``d_gain`` as the
*proportional-like* gain. The search treats both as opaque knobs, so this
only matters when reading the results.

Safety
------

* The device must be a flow controller with a serial-settable setpoint.
* Every step target must lie inside ``[--min-flow, --max-flow]`` and full
  scale.
* An in-trial guard aborts a candidate immediately when the error exceeds
  ``2 × --abort-deviation``, when it stays above ``--abort-deviation`` for
  ``--abort-hold`` seconds, when sustained oscillation is detected, or when
  flow leaves ``[--min-flow, --max-flow]``. The last good gains are restored
  at once and the candidate is scored as unstable.
* First Ctrl-C requests a graceful stop (finish the current I/O, restore,
  exit). A second Ctrl-C forces an immediate exit.
* On exit (normal, error or Ctrl-C) the script restores the original gains
  (unless the tuned ones were accepted), algorithm, ramp rate and setpoint
  source, then returns the setpoint to where it was when the script started
  (or ``--final-setpoint``).
* Ramp rate is disabled during ``tune`` (it would mask the loop dynamics)
  and restored afterwards; pass ``--keep-ramp`` to leave it alone.
* If the setpoint source is ``S`` (every setpoint change saved to EEPROM),
  ``tune`` switches it to ``U`` for the run without saving that switch, and
  restores it afterwards. Pass ``--keep-lss`` to leave it alone.

Outputs land in ``--out`` (default ``autotune_runs/<timestamp>-<cmd>``):
``samples.csv`` (every sample), ``trials.csv`` / ``trials.jsonl``,
``backup_gains.json``, ``summary.json``, ``events.csv`` (monitor), and PNG
plots when matplotlib is installed.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import json
import math
import re
import signal
import statistics
import time
from collections import deque
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

import anyio

from alicatlib.commands.base import Command, DecodeContext, ResponseMode
from alicatlib.devices.kind import DeviceKind
from alicatlib.errors import (
    AlicatCapabilityError,
    AlicatCommandRejectedError,
    AlicatError,
    AlicatParseError,
    AlicatTimeoutError,
    AlicatUnitIdMismatchError,
)
from alicatlib.firmware import FirmwareFamily, FirmwareVersion
from alicatlib.registry import LoopControlVariable, Statistic
from alicatlib.sync import Alicat, SyncFlowController
from alicatlib.transport import SerialSettings, SerialTransport

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from alicatlib.devices.models import TimeUnit
    from alicatlib.devices.reading import Reading


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GAIN_MIN: Final = 0
GAIN_MAX: Final = 65_535

# Factory default first, then the other rates Alicat devices are commonly set to.
CANDIDATE_BAUDS: Final[tuple[int, ...]] = (19200, 115200, 9600, 38400, 57600)

MIN_FIRMWARE_GAINS: Final = FirmwareVersion(
    family=FirmwareFamily.V10, major=10, minor=5, raw="10v05"
)
CONTROLLER_KINDS: Final = frozenset({DeviceKind.FLOW_CONTROLLER, DeviceKind.PRESSURE_CONTROLLER})

# Register map used by the community ``numat/alicat`` driver (pre-10v05).
REG_P: Final = 21
REG_D: Final = 22
REG_I: Final = 23
REG_LOOP: Final = 85

VALVE_SATURATED_PCT: Final = 98.0

# An excursion below setpoint lasting this long implies the valve ran out of travel.
SUPPLY_LIMITED_S: Final = 10.0

UNSTABLE_COST: Final = 1e6

# Replies that a retry can cure: garbled bytes on the line, a lost reply, or a
# command the device saw corrupted and rejected. All retried calls are idempotent.
TRANSIENT_ERRORS: Final = (AlicatParseError, AlicatTimeoutError, AlicatCommandRejectedError)


class LinkStats:
    """Process-wide count of transient link errors that were retried."""

    retries = 0


def with_retry[T](
    fn: Callable[[], T],
    *,
    attempts: int = 5,
    extra: tuple[type[Exception], ...] = (),
) -> T:
    """Call ``fn``, retrying transient link errors (and ``extra``) up to ``attempts`` times."""
    retryable = TRANSIENT_ERRORS + extra
    for k in range(attempts):
        try:
            return fn()
        except retryable:
            if k == attempts - 1:
                raise
            LinkStats.retries += 1
            time.sleep(0.02)
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Wire commands not yet in the library catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextRequest:
    """Raw suffix appended verbatim after the command token (include any space)."""

    suffix: str = ""


@dataclass(frozen=True, slots=True)
class TextCommand(Command[TextRequest, str]):
    """Send ``<uid>[<prefix>]<token><suffix>`` and return the reply text.

    The library's ``Session.execute`` still applies firmware / device-kind
    gating, ``?``-rejection detection and error enrichment.
    """

    use_prefix: bool = True

    def encode(self, ctx: DecodeContext, request: TextRequest) -> bytes:
        prefix = ctx.command_prefix.decode("ascii") if self.use_prefix else ""
        return f"{ctx.unit_id}{prefix}{self.token}{request.suffix}\r".encode("ascii")

    def decode(self, response: bytes | tuple[bytes, ...], ctx: DecodeContext) -> str:
        del ctx
        if isinstance(response, tuple):
            raise TypeError(f"{self.name}.decode expected one line, got {len(response)}")
        return response.decode("ascii", errors="replace").strip()


_MODERN_KW: Final[dict[str, Any]] = {
    "response_mode": ResponseMode.LINE,
    "device_kinds": CONTROLLER_KINDS,
    "min_firmware": MIN_FIRMWARE_GAINS,
    "firmware_families": frozenset({FirmwareFamily.V10}),
}
LCA: Final = TextCommand(name="loop_control_algorithm", token="LCA", **_MODERN_KW)  # noqa: S106
LCGD: Final = TextCommand(name="pdf_gains", token="LCGD", **_MODERN_KW)  # noqa: S106
LCG: Final = TextCommand(name="pd2i_gains", token="LCG", **_MODERN_KW)  # noqa: S106


def _register_command(token: str) -> TextCommand:
    return TextCommand(
        name=f"register_{token.strip('$').lower()}",
        token=token,
        response_mode=ResponseMode.LINE,
        device_kinds=CONTROLLER_KINDS,
        use_prefix=False,
    )


# ---------------------------------------------------------------------------
# Gains + access backends
# ---------------------------------------------------------------------------


class Algorithm(IntEnum):
    """Loop-control algorithm codes (``LCA``)."""

    PDF = 1
    PD2I = 2

    @property
    def label(self) -> str:
        return "PD/PDF" if self is Algorithm.PDF else "PD2I"

    @property
    def gain_names(self) -> tuple[str, ...]:
        return ("p", "d") if self is Algorithm.PDF else ("p", "i", "d")


@dataclass(frozen=True, slots=True)
class Gains:
    """One gain set. ``i`` is unused by PD/PDF."""

    algorithm: Algorithm
    p: int
    d: int
    i: int = 0

    def get(self, name: str) -> int:
        return int(getattr(self, name))

    def with_values(self, values: dict[str, int]) -> Gains:
        return replace(
            self,
            p=values.get("p", self.p),
            d=values.get("d", self.d),
            i=values.get("i", self.i),
        )

    def key(self) -> tuple[int, ...]:
        return (int(self.algorithm), self.p, self.d, self.i)

    def describe(self) -> str:
        parts = [f"{n.upper()}={self.get(n)}" for n in self.algorithm.gain_names]
        return f"{self.algorithm.label} " + " ".join(parts)

    def to_json(self) -> dict[str, Any]:
        return {"algorithm": self.algorithm.label, "p": self.p, "d": self.d, "i": self.i}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Gains:
        alg = Algorithm.PD2I if str(data["algorithm"]).upper() == "PD2I" else Algorithm.PDF
        return cls(alg, int(data["p"]), int(data["d"]), int(data.get("i", 0)))


class GainWriteError(RuntimeError):
    """Read-back after a gain write did not match what was written."""


class GainBackend(Protocol):
    """How gains are read and written on a particular firmware."""

    name: str
    writes_persist: bool

    def read(self) -> Gains: ...

    def write(self, gains: Gains, *, save: bool) -> Gains: ...

    def set_algorithm(self, algorithm: Algorithm) -> Algorithm: ...


def _ints(text: str) -> list[int]:
    out: list[int] = []
    for tok in text.replace("=", " ").split():
        try:
            out.append(int(tok))
        except ValueError:
            continue
    return out


class ModernGainBackend:
    """``LCA`` / ``LCGD`` / ``LCG`` (10v05+, primer Rev. 2)."""

    name = "LCA/LCGD/LCG (10v05+)"
    writes_persist = False

    def __init__(self, dev: SyncFlowController) -> None:
        self._dev = dev

    def algorithm(self) -> Algorithm:
        vals = _ints(self._dev.execute(LCA, TextRequest()))
        if not vals:
            raise AlicatError("LCA reply carried no algorithm code")
        return Algorithm(vals[-1])

    def set_algorithm(self, algorithm: Algorithm) -> Algorithm:
        vals = _ints(self._dev.execute(LCA, TextRequest(f" {int(algorithm)}")))
        got = Algorithm(vals[-1]) if vals else self.algorithm()
        if got is not algorithm:
            raise GainWriteError(f"LCA read-back {got.label} != requested {algorithm.label}")
        return got

    def read(self) -> Gains:
        alg = with_retry(self.algorithm)
        return with_retry(
            lambda: self._parse(alg, self._dev.execute(self._cmd(alg), TextRequest()))
        )

    def write(self, gains: Gains, *, save: bool) -> Gains:
        _check_range(gains)
        flag = 1 if save else 0
        if gains.algorithm is Algorithm.PDF:
            suffix = f" 0 {flag} {gains.p} {gains.d}"
        else:
            suffix = f" 0 {flag} {gains.p} {gains.i} {gains.d}"
        reply = self._dev.execute(self._cmd(gains.algorithm), TextRequest(suffix))
        got = self._parse(gains.algorithm, reply)
        if got.key() != gains.key():
            # The set reply should echo the active gains; confirm with a query
            # before declaring failure in case of a firmware echo quirk.
            got = self.read()
        if got.key() != gains.key():
            raise GainWriteError(f"wrote {gains.describe()}, device reports {got.describe()}")
        return got

    @staticmethod
    def _cmd(alg: Algorithm) -> TextCommand:
        return LCGD if alg is Algorithm.PDF else LCG

    @staticmethod
    def _parse(alg: Algorithm, reply: str) -> Gains:
        # PDF:  "<uid> P D 0"    PD2I: "<uid> P I D 0"
        fields_ = reply.split()
        want = 4 if alg is Algorithm.PDF else 5
        vals = _ints(reply)
        if len(fields_) != want or len(vals) != want - 1:
            raise AlicatParseError(f"unexpected {alg.label} gain reply: {reply!r}")
        if alg is Algorithm.PDF:
            return Gains(alg, p=vals[0], d=vals[1])
        return Gains(alg, p=vals[0], i=vals[1], d=vals[2])


_REGISTER_REPLY: Final = re.compile(r"[A-Z]\s+0*(\d+)\s*=\s*(-?\d+)")


class RegisterGainBackend:
    """Pre-10v05 register access (``$$R<n>`` / ``$$W<n>=<v>``), community-documented."""

    writes_persist = True

    def __init__(self, dev: SyncFlowController, read_token: str, write_token: str) -> None:
        self._dev = dev
        self._read = _register_command(read_token)
        self._write = _register_command(write_token)
        self.name = f"registers {read_token}21/22/23 (pre-10v05, not in primer)"

    @classmethod
    def detect(cls, dev: SyncFlowController) -> RegisterGainBackend:
        """Find which register-read spelling the device accepts (reads only)."""
        last: Exception | None = None
        for read_token, write_token in (("$$R", "$$W"), ("R", "W")):
            backend = cls(dev, read_token, write_token)
            try:
                backend.read_register(REG_P)
            except AlicatError as err:
                last = err
                continue
            return backend
        raise AlicatError(f"device answered neither $$R21 nor R21 register reads ({last})")

    def read_register(self, reg: int) -> int:
        def once() -> int:
            reply = self._dev.execute(self._read, TextRequest(str(reg)))
            # Strict "<uid> <reg> = <value>" match so a garbled reply is retried
            # rather than read as a plausible-looking number.
            m = _REGISTER_REPLY.fullmatch(reply)
            if m is None or int(m.group(1)) != reg:
                raise AlicatParseError(f"unexpected register {reg} reply: {reply!r}")
            return int(m.group(2))

        return with_retry(once)

    def algorithm(self) -> Algorithm:
        try:
            code = self.read_register(REG_LOOP)
        except AlicatError:
            return Algorithm.PDF
        return Algorithm.PD2I if code == 2 else Algorithm.PDF

    def set_algorithm(self, algorithm: Algorithm) -> Algorithm:
        # Register 85 may carry other bits; refuse rather than clobber it.
        raise AlicatError("changing the loop algorithm needs 10v05+ firmware (LCA)")

    def read(self) -> Gains:
        return Gains(
            self.algorithm(),
            p=self.read_register(REG_P),
            d=self.read_register(REG_D),
            i=self.read_register(REG_I),
        )

    def write(self, gains: Gains, *, save: bool) -> Gains:
        del save  # register writes are assumed persistent either way
        _check_range(gains)
        current = self.read()
        for reg, name in ((REG_P, "p"), (REG_D, "d"), (REG_I, "i")):
            if current.get(name) == gains.get(name):
                continue
            try:
                with_retry(
                    lambda r=reg, n=name: self._dev.execute(  # type: ignore[misc]
                        self._write, TextRequest(f"{r}={gains.get(n)}")
                    ),
                    attempts=3,
                )
            except AlicatTimeoutError:
                pass  # some firmware does not echo register writes; read-back decides
            got = self.read_register(reg)
            if got != gains.get(name):
                raise GainWriteError(f"register {reg}: wrote {gains.get(name)}, read back {got}")
        return self.read()


def _check_range(gains: Gains) -> None:
    for name in ("p", "d", "i"):
        v = gains.get(name)
        if not GAIN_MIN <= v <= GAIN_MAX:
            raise ValueError(f"gain {name}={v} outside {GAIN_MIN}..{GAIN_MAX}")


def uses_modern_gains(fw: FirmwareVersion) -> bool:
    return fw.family is FirmwareFamily.V10 and fw >= MIN_FIRMWARE_GAINS


def make_backend(dev: SyncFlowController) -> GainBackend:
    if uses_modern_gains(dev.info.firmware):
        return ModernGainBackend(dev)
    return RegisterGainBackend.detect(dev)


# ---------------------------------------------------------------------------
# Ctrl-C handling
# ---------------------------------------------------------------------------


class StopRequested(Exception):  # noqa: N818 — control-flow signal, not an error
    """Raised at a safe point after the first Ctrl-C."""


class StopFlag:
    """First Ctrl-C sets a flag polled at safe points; second one raises."""

    def __init__(self) -> None:
        self.requested = False
        self._previous: Any = None

    def install(self) -> None:
        self._previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._handler)

    def uninstall(self) -> None:
        if self._previous is not None:
            signal.signal(signal.SIGINT, self._previous)

    def _handler(self, signum: int, frame: object) -> None:
        del signum, frame
        if self.requested:
            raise KeyboardInterrupt
        self.requested = True
        print("\n[ctrl-c] stopping after the current read; press again to force exit")

    def check(self) -> None:
        if self.requested:
            raise StopRequested


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Sample:
    """One poll of the device, plus bookkeeping for analysis."""

    t: float
    flow: float
    sp_cmd: float
    sp_dev: float | None
    pressure: float | None
    temperature: float | None
    valve: float | None
    status: str
    trial: int
    segment: str


SAMPLE_FIELDS: Final = [f.name for f in fields(Sample)]

_FLOW_STATS: Final = {
    LoopControlVariable.MASS_FLOW_SETPT: (Statistic.MASS_FLOW, "Mass_Flow"),
    LoopControlVariable.VOL_FLOW_SETPT: (Statistic.VOL_FLOW, "Volu_Flow"),
}
_SETPOINT_STATS: Final = (Statistic.MASS_FLOW_SETPT, Statistic.VOL_FLOW_SETPT, Statistic.SETPT)


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


class Sampler:
    """Polls the controller as fast as the link allows and logs every sample."""

    def __init__(
        self,
        dev: SyncFlowController,
        stop: StopFlag,
        csv_path: Path,
        *,
        vd_every: int,
        controlled: LoopControlVariable | None,
    ) -> None:
        self.dev = dev
        self.stop = stop
        self.t0 = time.monotonic()
        self.vd_every = vd_every
        self._n = 0
        self._valve: float | None = None
        self.valve_supported = vd_every > 0
        stat, name = _FLOW_STATS.get(
            controlled or LoopControlVariable.MASS_FLOW_SETPT,
            _FLOW_STATS[LoopControlVariable.MASS_FLOW_SETPT],
        )
        self._flow_stat = stat
        self._flow_name = name
        self._fh = csv_path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(SAMPLE_FIELDS)

    def close(self) -> None:
        self._fh.close()

    def flow_of(self, reading: Reading) -> float:
        v = _as_float(reading.values_by_statistic.get(self._flow_stat))
        if v is None:
            v = reading.get_float(self._flow_name)
        if v is None:
            raise AlicatError(f"data frame has no {self._flow_name} field: {reading.values}")
        return v

    def sample(self, *, sp_cmd: float, trial: int, segment: str) -> Sample:
        self.stop.check()
        reading = with_retry(self.dev.poll)
        t = time.monotonic() - self.t0
        if self.valve_supported and self._n % self.vd_every == 0:
            try:
                self._valve = self.dev.valve_drive().valves[0]
            except AlicatCapabilityError:
                self.valve_supported = False
            except TRANSIENT_ERRORS:
                LinkStats.retries += 1
        self._n += 1
        sp_dev = None
        for stat in _SETPOINT_STATS:
            sp_dev = _as_float(reading.values_by_statistic.get(stat))
            if sp_dev is not None:
                break
        s = Sample(
            t=t,
            flow=self.flow_of(reading),
            sp_cmd=sp_cmd,
            sp_dev=sp_dev,
            pressure=_as_float(reading.values_by_statistic.get(Statistic.ABS_PRESS)),
            temperature=_as_float(reading.values_by_statistic.get(Statistic.TEMP_STREAM)),
            valve=self._valve,
            status=" ".join(sorted(str(c) for c in reading.status)),
            trial=trial,
            segment=segment,
        )
        self._writer.writerow(
            [f"{v:.4f}" if isinstance(v, float) else ("" if v is None else v) for v in astuple(s)]
        )
        return s

    def collect(
        self,
        duration: float,
        *,
        sp_cmd: float,
        trial: int,
        segment: str,
        guard: Callable[[Sample, float], None] | None = None,
        until: Callable[[Sample, float], bool] | None = None,
    ) -> list[Sample]:
        out: list[Sample] = []
        start = time.monotonic()
        while (elapsed := time.monotonic() - start) < duration:
            s = self.sample(sp_cmd=sp_cmd, trial=trial, segment=segment)
            out.append(s)
            if guard is not None:
                guard(s, elapsed)
            if until is not None and until(s, elapsed):
                break
        return out


def gain_path(start: Gains, end: Gains, ratio: float) -> list[Gains]:
    """Geometric waypoints from ``start`` to ``end`` with each gain moving <= ``ratio`` per hop.

    Ends with ``end``. A gain that is 0 on either side jumps on the final hop.
    """
    if start.algorithm is not end.algorithm:
        return [end]
    names = [n for n in ("p", "d", "i") if start.get(n) > 0 and end.get(n) > 0]
    hops = max(
        (
            math.ceil(abs(math.log(end.get(n) / start.get(n))) / math.log(ratio) - 1e-9)
            for n in names
        ),
        default=1,
    )
    path: list[Gains] = []
    for k in range(1, max(hops, 1)):
        f = k / hops
        path.append(
            start.with_values(
                {n: round(start.get(n) * (end.get(n) / start.get(n)) ** f) for n in names}
            )
        )
    path.append(end)
    return path


def settled_for(target: float, band: float, min_hold: float) -> Callable[[Sample, float], bool]:
    """Early-exit predicate: inside the band for max(min_hold, time to first entry)."""
    first_in: float | None = None
    in_since: float | None = None

    def check(s: Sample, elapsed: float) -> bool:
        nonlocal first_in, in_since
        if abs(s.flow - target) > band:
            in_since = None
            return False
        if first_in is None:
            first_in = elapsed
        if in_since is None:
            in_since = elapsed
        return elapsed - in_since >= max(min_hold, first_in)

    return check


def astuple(s: Sample) -> tuple[Any, ...]:
    return tuple(getattr(s, f) for f in SAMPLE_FIELDS)


# ---------------------------------------------------------------------------
# Signal analysis
# ---------------------------------------------------------------------------


def median3(xs: Sequence[float]) -> list[float]:
    """3-point running median — suppresses single-sample spikes."""
    if len(xs) < 3:
        return list(xs)
    out = [xs[0]]
    out.extend(sorted(xs[k - 1 : k + 2])[1] for k in range(1, len(xs) - 1))
    out.append(xs[-1])
    return out


def detrended_std(ts: Sequence[float], ys: Sequence[float]) -> float:
    """Std of residuals about a least-squares line (slow drift removed)."""
    n = len(ys)
    if n < 3:
        return 0.0
    tm, ym = statistics.fmean(ts), statistics.fmean(ys)
    sxx = sum((t - tm) ** 2 for t in ts)
    slope = sum((t - tm) * (y - ym) for t, y in zip(ts, ys, strict=True)) / sxx if sxx else 0.0
    resid = [y - (ym + slope * (t - tm)) for t, y in zip(ts, ys, strict=True)]
    return math.sqrt(sum(r * r for r in resid) / (n - 2))


def trapz(ts: Sequence[float], fs: Sequence[float]) -> float:
    return sum(0.5 * (fs[k] + fs[k + 1]) * (ts[k + 1] - ts[k]) for k in range(len(ts) - 1))


def count_crossings(errors: Sequence[float], hysteresis: float) -> int:
    """Sign changes of ``errors`` that swing past ±hysteresis."""
    state = 0
    n = 0
    for e in errors:
        if e > hysteresis:
            if state == -1:
                n += 1
            state = 1
        elif e < -hysteresis:
            if state == 1:
                n += 1
            state = -1
    return n


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 10:
        return None
    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return None


@dataclass(slots=True)
class StepMetrics:
    """Response metrics for one setpoint step."""

    start: float
    target: float
    amplitude: float
    settled: bool
    settle_s: float
    rise_s: float | None
    overshoot: float
    iae: float
    itae: float
    tail_std: float
    tail_bias: float
    crossings: int
    valve_max: float | None

    def cost(self, w: Weights) -> float:
        return w.settle * self.settle_s + w.iae * self.iae + w.overshoot * self.overshoot


def step_metrics(
    samples: Sequence[Sample],
    *,
    start_value: float,
    target: float,
    band: float,
    window: float,
) -> StepMetrics:
    """Score one step from the samples recorded after the setpoint change."""
    amp = target - start_value
    if not samples:
        return StepMetrics(
            start_value,
            target,
            amp,
            False,
            1.5 * window,
            None,
            0.0,
            window,
            window**2,
            0.0,
            0.0,
            0,
            None,
        )
    t0 = samples[0].t
    ts = [s.t - t0 for s in samples]
    ys = [s.flow for s in samples]
    yf = median3(ys)
    sign = 1.0 if amp >= 0 else -1.0
    a = abs(amp) or 1.0
    dev = [(y - target) * sign / a for y in yf]  # <0 short of target, >0 overshoot

    rise_s = next((t for t, d in zip(ts, dev, strict=True) if d >= -0.1), None)
    overshoot = max(0.0, *dev) if rise_s is not None else 0.0

    outside = [k for k, y in enumerate(yf) if abs(y - target) > band]
    if not outside:
        settled, settle_s = True, 0.0
    elif ts[-1] - ts[outside[-1]] >= 0.25:
        settled, settle_s = True, ts[outside[-1] + 1]
    else:
        settled, settle_s = False, 1.5 * window

    abs_err = [abs(y - target) / a for y in ys]
    iae = trapz(ts, abs_err)
    itae = trapz(ts, [t * e for t, e in zip(ts, abs_err, strict=True)])
    if settled and ts[-1] < window:
        # Step ended early once settled: extend the in-band tail error to the
        # full window so early-stopped and full-length steps stay comparable.
        last = [e for t, e in zip(ts, abs_err, strict=True) if t >= ts[-1] - 1.0]
        tail_c = statistics.fmean(last)
        iae += tail_c * (window - ts[-1])
        itae += tail_c * (window**2 - ts[-1] ** 2) / 2

    tail = [(t, y) for t, y in zip(ts, ys, strict=True) if t >= 0.7 * ts[-1]]
    tail_t = [t for t, _ in tail]
    tail_y = [y for _, y in tail]
    tail_std = detrended_std(tail_t, tail_y) if len(tail) >= 3 else 0.0
    tail_bias = (statistics.fmean(tail_y) - target) if tail_y else 0.0

    post = [y - target for t, y in zip(ts, yf, strict=True) if rise_s is not None and t >= rise_s]
    crossings = count_crossings(post, max(band, 0.15 * a))
    valves = [s.valve for s in samples if s.valve is not None]
    return StepMetrics(
        start=start_value,
        target=target,
        amplitude=amp,
        settled=settled,
        settle_s=settle_s,
        rise_s=rise_s,
        overshoot=overshoot,
        iae=iae,
        itae=itae,
        tail_std=tail_std,
        tail_bias=tail_bias,
        crossings=crossings,
        valve_max=max(valves) if valves else None,
    )


# ---------------------------------------------------------------------------
# Trial execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Weights:
    """Cost-function weights."""

    settle: float
    iae: float
    overshoot: float
    noise: float


@dataclass(slots=True)
class TrialResult:
    """Everything recorded for one gain candidate."""

    trial: int
    label: str
    gains: Gains
    started: float
    stable: bool
    abort_reason: str | None
    hold_std: float
    hold_mean: float
    steps: list[StepMetrics] = field(default_factory=list)
    cost: float = UNSTABLE_COST
    valve_sat_frac: float | None = None

    def summary_row(self) -> dict[str, Any]:
        settle = [s.settle_s for s in self.steps]
        return {
            "trial": self.trial,
            "label": self.label,
            "algorithm": self.gains.algorithm.label,
            "p": self.gains.p,
            "d": self.gains.d,
            "i": self.gains.i,
            "stable": self.stable,
            "abort_reason": self.abort_reason or "",
            "cost": round(self.cost, 4),
            "settle_med_s": round(statistics.median(settle), 3) if settle else "",
            "settle_max_s": round(max(settle), 3) if settle else "",
            "iae_med_s": _med(self.steps, "iae"),
            "overshoot_med_pct": _med(self.steps, "overshoot", 100),
            "rise_med_s": _med(self.steps, "rise_s"),
            "crossings_max": max((s.crossings for s in self.steps), default=""),
            "hold_std": round(self.hold_std, 5),
            "hold_mean": round(self.hold_mean, 4),
            "valve_sat_frac": "" if self.valve_sat_frac is None else round(self.valve_sat_frac, 3),
        }


def noise_level(r: TrialResult) -> float:
    """Median of the hold noise and each settled step's tail noise.

    Five short windows instead of one, so a single external upset during the
    hold (e.g. supply wander) cannot dominate the noise penalty.
    """
    vals = [r.hold_std, *(s.tail_std for s in r.steps if s.settled and s.tail_std > 0)]
    return statistics.median(vals)


def _med(steps: Sequence[StepMetrics], attr: str, scale: float = 1.0) -> float | str:
    vals = [getattr(s, attr) for s in steps if getattr(s, attr) is not None]
    return round(statistics.median(vals) * scale, 4) if vals else ""


class TrialAborted(Exception):  # noqa: N818 — control-flow signal
    """Raised by the guard when a candidate destabilises the loop."""


@dataclass(slots=True)
class Plan:
    """Experiment geometry shared by every trial."""

    setpoint: float
    step: float
    targets: list[float]
    band: float
    window: float
    settle_s: float
    hold_s: float
    abort_dev: float
    abort_hold: float
    min_flow: float
    max_flow: float
    early_stop_hold: float
    settle_max: float
    gain_ramp: float
    gain_ramp_dwell: float


class Guard:
    """Watches every sample of a trial for instability."""

    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self.target = plan.setpoint
        self.segment = "settle"
        self._over_since: float | None = None
        self._recent: deque[tuple[float, float]] = deque()

    def retarget(self, target: float) -> None:
        self.target = target
        self._over_since = None
        self._recent.clear()

    def __call__(self, s: Sample, elapsed: float) -> None:
        p = self.plan
        self.segment = s.segment
        if not p.min_flow <= s.flow <= p.max_flow:
            raise TrialAborted(
                f"flow {s.flow:.3f} left safe range [{p.min_flow:g}, {p.max_flow:g}]"
            )
        err = s.flow - self.target
        if abs(err) > 2 * p.abort_dev:
            raise TrialAborted(f"error {err:+.3f} exceeded 2x abort deviation")
        if abs(err) > p.abort_dev and elapsed > 0.5:
            if self._over_since is None:
                self._over_since = s.t
            elif s.t - self._over_since > p.abort_hold:
                raise TrialAborted(f"error {err:+.3f} held beyond {p.abort_hold:g}s")
        else:
            self._over_since = None
        # Sustained oscillation: >= 6 large swings within 4 s.
        self._recent.append((s.t, err))
        while self._recent and s.t - self._recent[0][0] > 4.0:
            self._recent.popleft()
        if elapsed > 1.0:
            hyst = max(0.5 * p.step, 2 * p.band)
            if count_crossings([e for _, e in self._recent], hyst) >= 6:
                raise TrialAborted("sustained oscillation detected")


class Runner:
    """Owns the device, the backend, and the experiment loop."""

    def __init__(
        self,
        dev: SyncFlowController,
        backend: GainBackend,
        sampler: Sampler,
        *,
        plan: Plan,
        weights: Weights,
        out: Path,
    ) -> None:
        self.dev = dev
        self.backend = backend
        self.sampler = sampler
        self.plan = plan
        self.weights = weights
        self.out = out
        self.last_good: Gains | None = None
        self.ref_hold_std: float | None = None
        self.results: list[TrialResult] = []
        self.cache: dict[tuple[int, ...], list[TrialResult]] = {}
        self._jsonl = (out / "trials.jsonl").open("a", encoding="utf-8")
        self._trial_no = 0
        self.current: Gains | None = None

    def close(self) -> None:
        self._jsonl.close()

    def set_setpoint(self, value: float) -> None:
        with_retry(lambda: self.dev.setpoint(value))

    def _write_gains(self, gains: Gains) -> Gains:
        got = with_retry(
            lambda: self.backend.write(gains, save=False), attempts=3, extra=(GainWriteError,)
        )
        self.current = got
        return got

    def apply_gains(self, gains: Gains, *, guard: Guard | None = None, ramp: bool = True) -> Gains:
        """Write ``gains``, stepping there geometrically when the move is large.

        Some firmware applies a gain change as a step in valve drive (the D term
        acts on the measured flow, so raising it at high flow cuts drive at once).
        Splitting a large move into <= ``gain_ramp`` ratio steps with a short
        dwell keeps each bump small enough for the loop to absorb.
        """
        p = self.plan
        if ramp and p.gain_ramp > 1 and self.current is not None:
            for mid in gain_path(self.current, gains, p.gain_ramp)[:-1]:
                self._write_gains(mid)
                self.sampler.collect(
                    p.gain_ramp_dwell,
                    sp_cmd=p.setpoint,
                    trial=self._trial_no,
                    segment="gain-ramp",
                    guard=guard,
                )
        return self._write_gains(gains)

    def run_trial(self, gains: Gains, label: str, *, window: float | None = None) -> TrialResult:
        p = self.plan
        win = window if window is not None else p.window
        self._trial_no += 1
        trial = self._trial_no
        print(f"\n[trial {trial:>2}] {label:<14} {gains.describe()}")
        started = time.monotonic() - self.sampler.t0
        result = TrialResult(trial, label, gains, started, True, None, 0.0, 0.0)
        guard = Guard(p)
        try:
            self.apply_gains(gains, guard=guard)
            self.set_setpoint(p.setpoint)
            self.sampler.collect(
                p.settle_s, sp_cmd=p.setpoint, trial=trial, segment="settle", guard=guard
            )
            # Then wait (bounded) until the gain-change transient has died out, so
            # it doesn't leak into the hold / step metrics of this trial.
            self.sampler.collect(
                max(0.0, p.settle_max - p.settle_s),
                sp_cmd=p.setpoint,
                trial=trial,
                segment="settle",
                guard=guard,
                until=settled_for(p.setpoint, p.band, 1.0),
            )
            hold = self.sampler.collect(
                p.hold_s, sp_cmd=p.setpoint, trial=trial, segment="hold", guard=guard
            )
            result.hold_std = detrended_std([s.t for s in hold], [s.flow for s in hold])
            result.hold_mean = statistics.fmean(s.flow for s in hold) if hold else 0.0
            prev = p.setpoint
            all_valves: list[float] = []
            for k, target in enumerate(p.targets):
                self.set_setpoint(target)
                guard.retarget(target)
                until = (
                    settled_for(target, p.band, p.early_stop_hold)
                    if p.early_stop_hold > 0
                    else None
                )
                seg = self.sampler.collect(
                    win,
                    sp_cmd=target,
                    trial=trial,
                    segment=f"step{k + 1}",
                    guard=guard,
                    until=until,
                )
                result.steps.append(
                    step_metrics(seg, start_value=prev, target=target, band=p.band, window=win)
                )
                all_valves.extend(s.valve for s in seg if s.valve is not None)
                prev = target
            if all_valves:
                result.valve_sat_frac = sum(v >= VALVE_SATURATED_PCT for v in all_valves) / len(
                    all_valves
                )
            result.cost = self.score(result)
            self.last_good = gains
        except TrialAborted as err:
            result.stable = False
            result.abort_reason = f"{err} (during {guard.segment})"
            result.cost = UNSTABLE_COST
            print(f"  !! aborted during {guard.segment}: {err}")
            if guard.segment == "settle":
                print("     (right after the gain write: gain changes may not be bumpless)")
            self.recover()
        self._record(result)
        self._print_result(result)
        return result

    def score(self, r: TrialResult) -> float:
        w = self.weights
        step_cost = statistics.median(s.cost(w) for s in r.steps)
        noise = 0.0
        if self.ref_hold_std:
            floor = max(self.ref_hold_std, 5e-4 * self.plan.setpoint)
            noise = max(0.0, noise_level(r) / floor - 1.0)
        return step_cost + w.noise * noise

    def recover(self) -> None:
        """Put back the last stable gains and wait for the loop to come home."""
        if self.last_good is None:
            raise RuntimeError("no known-good gains to recover to")
        print(f"  -> restoring {self.last_good.describe()}")
        self.apply_gains(self.last_good, ramp=False)
        if not self.wait_at_setpoint(30.0, "recover"):
            raise SystemExit(
                "error: flow did not return to the setpoint within 30 s on known-good gains. "
                "That points at the supply (valve fully open), not the gains; check supply "
                "pressure before re-running."
            )

    def wait_at_setpoint(self, timeout: float, segment: str) -> bool:
        """Command S and wait until flow stays within tolerance for 1 s."""
        sp = self.plan.setpoint
        self.set_setpoint(sp)
        tol = max(2 * self.plan.band, 4 * (self.ref_hold_std or 0.0))
        start = time.monotonic()
        inside_since: float | None = None
        last = None
        while time.monotonic() - start < timeout:
            last = self.sampler.sample(sp_cmd=sp, trial=self._trial_no, segment=segment)
            if abs(last.flow - sp) > tol:
                inside_since = None
            elif inside_since is None:
                inside_since = last.t
            elif last.t - inside_since >= 1.0:
                return True
        if last is not None:
            print(f"  flow {last.flow:.3f} vs setpoint {sp:g} after {timeout:g}s")
        return False

    def _record(self, r: TrialResult) -> None:
        self.results.append(r)
        self.cache.setdefault(r.gains.key(), []).append(r)
        row = r.summary_row() | {"steps": [asdict(s) for s in r.steps]}
        self._jsonl.write(json.dumps(row) + "\n")
        self._jsonl.flush()

    def _print_result(self, r: TrialResult) -> None:
        if not r.stable:
            return
        row = r.summary_row()
        print(
            f"  cost={r.cost:7.3f}  settle(med/max)={row['settle_med_s']}/{row['settle_max_s']}s"
            f"  IAE={row['iae_med_s']}s  OS={row['overshoot_med_pct']}%"
            f"  hold sigma={r.hold_std:.4f}"
            + (
                f"  valve>=98%: {100 * r.valve_sat_frac:.0f}%"
                if r.valve_sat_frac is not None
                else ""
            )
        )
        unsettled = sum(not s.settled for s in r.steps)
        if unsettled:
            print(f"  ({unsettled}/{len(r.steps)} steps did not settle inside the window)")

    def mean_cost(self, gains: Gains) -> float:
        runs = self.cache.get(gains.key(), [])
        return statistics.fmean(r.cost for r in runs) if runs else math.inf


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SearchConfig:
    """Knobs for the compass search."""

    names: tuple[str, ...]
    max_trials: int
    step0: float
    step_min: float
    step_max: float
    max_factor: float
    rel_margin: float
    noise_sigma: float


def compass_search(runner: Runner, base: Gains, cfg: SearchConfig) -> Gains:
    """Noise-aware coordinate pattern search over log2(gain / baseline)."""
    lim = math.log2(cfg.max_factor)

    def to_gains(x: dict[str, float]) -> Gains:
        vals = {}
        for n in cfg.names:
            g0 = max(base.get(n), 1)
            vals[n] = int(min(GAIN_MAX, max(1, round(g0 * 2 ** x[n]))))
        return base.with_values(vals)

    x = dict.fromkeys(cfg.names, 0.0)
    incumbent = base
    f_inc = runner.mean_cost(base)
    step = cfg.step0
    last_dir: tuple[str, int] | None = None
    streak = 0
    used = 0

    def margin(f: float) -> float:
        return max(cfg.rel_margin * f, 0.5 * cfg.noise_sigma)

    while used < cfg.max_trials and step >= cfg.step_min:
        dirs = [(n, s) for n in cfg.names for s in (+1, -1)]
        if last_dir in dirs:
            dirs.remove(last_dir)
            dirs.insert(0, last_dir)
        improved = False
        for name, sgn in dirs:
            if used >= cfg.max_trials:
                break
            xc = dict(x)
            xc[name] = max(-lim, min(lim, x[name] + sgn * step))
            cand = to_gains(xc)
            if cand.key() == incumbent.key():
                continue
            if cand.key() in runner.cache:
                fc = runner.mean_cost(cand)
            else:
                fc = runner.run_trial(cand, f"search s={2**step:.2f}x").cost
                used += 1
            if fc < f_inc - margin(f_inc):
                print(f"  ** improvement {f_inc:.3f} -> {fc:.3f} via {name.upper()}{'+-'[sgn < 0]}")
                x, incumbent, f_inc = xc, cand, fc
                streak = streak + 1 if last_dir == (name, sgn) else 1
                last_dir = (name, sgn)
                if streak >= 2:
                    step = min(cfg.step_max, step * 2)
                improved = True
                break
        if not improved:
            step /= 2
            streak = 0
            print(f"  .. no improving direction; step -> x{2**step:.3f}")
            if used < cfg.max_trials and incumbent.key() != base.key():
                runner.run_trial(incumbent, "re-check best")
                used += 1
                f_inc = runner.mean_cost(incumbent)
    return incumbent


# ---------------------------------------------------------------------------
# Device helpers
# ---------------------------------------------------------------------------


def stop_stream(port: str, baud: int, unit_id: str) -> None:
    r"""Send ``\r`` then ``@@ <unit_id>`` so a streaming device drops to polling.

    The leading bare CR terminates any junk already in the device's line
    buffer (e.g. bytes from a wrong-baud attempt) so ``@@`` starts a clean
    line; otherwise the stop command can be swallowed as part of that junk.
    """

    async def _run() -> None:
        t = SerialTransport(SerialSettings(port=port, baudrate=baud))
        await t.open()
        try:
            await t.write(b"\r", timeout=0.5)
            await anyio.sleep(0.1)
            await t.write(f"@@ {unit_id}\r".encode("ascii"), timeout=0.5)
            await anyio.sleep(0.3)
            await t.read_available(idle_timeout=0.3, max_bytes=65_536)
        finally:
            await t.close()

    anyio.run(_run)


@contextmanager
def open_controller(args: argparse.Namespace) -> Iterator[SyncFlowController]:
    """Open the port, auto-detecting baud when ``--baud auto``."""
    bauds = CANDIDATE_BAUDS if args.baud == "auto" else (int(args.baud),)
    last: Exception | None = None
    with ExitStack() as stack:
        dev = None
        for baud in bauds:
            try:
                if args.stop_stream:
                    stop_stream(args.port, baud, args.unit_id)
                dev = stack.enter_context(
                    Alicat.open(
                        args.port,
                        unit_id=args.unit_id,
                        serial=SerialSettings(port=args.port, baudrate=baud),
                        timeout=args.timeout,
                    )
                )
            except (AlicatError, OSError) as err:
                last = err
                if len(bauds) > 1:
                    print(f"  {baud} baud: no answer ({type(err).__name__})")
                if isinstance(err, AlicatUnitIdMismatchError) and not args.stop_stream:
                    print(f"  {baud} baud: device may be streaming; re-run with --stop-stream")
                continue
            print(f"connected: {args.port} @ {baud} baud, unit {args.unit_id}")
            break
        if dev is None:
            raise SystemExit(f"error: could not open {args.port} at {bauds} ({last})")
        if not isinstance(dev, SyncFlowController):
            raise SystemExit(
                f"error: {args.port} opened as {type(dev).__name__}; "
                "this script needs a flow controller"
            )
        yield dev


def try_call[T](fn: Callable[[], T]) -> T | None:
    """Run a read-only query, returning None when the device can't answer it."""
    try:
        return fn()
    except (AlicatCapabilityError, AlicatCommandRejectedError, AlicatTimeoutError):
        return None
    except AlicatError as err:
        print(f"  ({type(err).__name__}: {err})")
        return None


def current_setpoint(dev: SyncFlowController) -> float | None:
    sp = try_call(lambda: dev.setpoint().current)
    if sp is not None:
        return sp
    reading = dev.poll()
    for stat in _SETPOINT_STATS:
        v = _as_float(reading.values_by_statistic.get(stat))
        if v is not None:
            return v
    return None


def full_scale_for(dev: SyncFlowController, lv: LoopControlVariable | None) -> float | None:
    stat = (lv or LoopControlVariable.MASS_FLOW_SETPT).statistic
    fs = dev.info.full_scale.get(stat)
    if fs is not None:
        return fs.value
    flow_stat = _FLOW_STATS[lv or LoopControlVariable.MASS_FLOW_SETPT][0]
    fs = dev.info.full_scale.get(flow_stat)
    if fs is not None:
        return fs.value
    fs_q = try_call(lambda: dev.full_scale(flow_stat))
    return fs_q.value if fs_q is not None else None


def make_out_dir(args: argparse.Namespace, command: str) -> Path:
    if args.out:
        out = Path(args.out)
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = Path("autotune_runs") / f"{stamp}-{command}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def confirm(prompt: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        print(f"{prompt} [auto-yes]")
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


def cmd_probe(args: argparse.Namespace) -> int:  # noqa: PLR0915 — linear report
    with open_controller(args) as dev:
        info = dev.info
        print(f"\nmodel        {info.model}   serial {info.serial}")
        print(f"firmware     {info.firmware.raw} ({info.firmware.family.name})   kind {info.kind}")
        lv_state = try_call(dev.loop_control_variable)
        lv = lv_state.variable if lv_state else None
        print(f"loop var     {lv.name if lv else 'n/a (pre-9v00: assume mass flow)'}")
        fs = full_scale_for(dev, lv)
        print(f"full scale   {fs if fs is not None else 'unknown'}")
        src = try_call(dev.setpoint_source)
        print(
            f"sp source    {src or 'n/a (pre-10v05)'}"
            + ("   <- every setpoint change is saved to EEPROM" if src == "S" else "")
        )
        print(f"setpoint     {current_setpoint(dev)}")
        ramp = try_call(dev.ramp_rate)
        if ramp is not None:
            state = "disabled" if ramp.max_ramp == 0 else f"{ramp.max_ramp} {ramp.rate_unit_label}"
            print(f"ramp rate    {state}")
        db = try_call(dev.deadband_limit)
        if db is not None:
            print(f"deadband     {db.deadband} {db.unit_label}")
        vd = try_call(dev.valve_drive)
        print(f"valve drive  {vd.valves if vd else 'n/a (needs 8v18+)'}")

        print()
        backend: GainBackend | None
        try:
            backend = make_backend(dev)
        except AlicatError as err:
            backend = None
            print(f"gain access  UNAVAILABLE: {err}")
        if backend is not None:
            gains = backend.read()
            print(f"gain access  {backend.name}")
            print(f"algorithm    {gains.algorithm.label}")
            print(f"gains        {gains.describe()}   (raw p={gains.p} d={gains.d} i={gains.i})")
            if gains.algorithm is Algorithm.PDF:
                print("             note: in PD/PDF, P acts integral-like and D proportional-like")
            if backend.writes_persist:
                print(
                    "             note: register writes assumed persistent; tune/apply need "
                    "--legacy-registers"
                )

        print("\nmeasuring poll rate and hold noise for 5 s ...")
        stop = StopFlag()
        out = make_out_dir(args, "probe")
        sampler = Sampler(dev, stop, out / "samples.csv", vd_every=4, controlled=lv)
        try:
            sp = current_setpoint(dev) or 0.0
            ss = sampler.collect(5.0, sp_cmd=sp, trial=0, segment="probe")
        finally:
            sampler.close()
        rate = (len(ss) - 1) / (ss[-1].t - ss[0].t) if len(ss) > 1 else 0.0
        flows = [s.flow for s in ss]
        print(f"poll rate    {rate:.1f} Hz ({len(ss)} samples)")
        print(
            f"flow         mean {statistics.fmean(flows):.4f}  "
            f"sigma {detrended_std([s.t for s in ss], flows):.4f}  "
            f"p2p {max(flows) - min(flows):.4f}"
        )
        valves = [s.valve for s in ss if s.valve is not None]
        if valves:
            print(f"valve drive  {min(valves):.1f}..{max(valves):.1f} %")
            if max(valves) >= VALVE_SATURATED_PCT:
                print(
                    "  WARNING: valve near 100%: controller has no authority left to "
                    "correct dips; check supply pressure before tuning"
                )
        print(f"\nsamples -> {out / 'samples.csv'}")
    return 0


# ---------------------------------------------------------------------------
# monitor
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Excursion:
    """One departure from the ±band around the setpoint."""

    start_t: float
    end_t: float | None
    peak_dev: float
    peak_t: float
    valve_max: float | None
    valve_min: float | None
    pressure_delta: float | None
    crossings: int
    status: str

    @property
    def recovery_s(self) -> float | None:
        return None if self.end_t is None else self.end_t - self.start_t


def detect_excursions(
    samples: Sequence[Sample], *, band: float, recover_hold: float
) -> list[Excursion]:
    ts = [s.t for s in samples]
    err = [y - s.sp_cmd for y, s in zip(median3([s.flow for s in samples]), samples, strict=True)]
    events: list[Excursion] = []
    k = 0
    n = len(samples)
    while k < n:
        if abs(err[k]) <= band:
            k += 1
            continue
        start = k
        pre = [s.pressure for s in samples[max(0, k - 50) : k] if s.pressure is not None]
        j = k
        inside_since: int | None = None
        while j < n:
            if abs(err[j]) <= band:
                if inside_since is None:
                    inside_since = j
                if ts[j] - ts[inside_since] >= recover_hold:
                    break
            else:
                inside_since = None
            j += 1
        end = inside_since if (j < n and inside_since is not None) else None
        stop = end if end is not None else n
        seg = samples[start:stop]
        seg_err = err[start:stop]
        kpk = max(range(len(seg_err)), key=lambda q: abs(seg_err[q]))
        valves = [s.valve for s in seg if s.valve is not None]
        press = [s.pressure for s in seg if s.pressure is not None]
        dp = None
        if pre and press:
            p0 = statistics.fmean(pre)
            dp = max(press, key=lambda v: abs(v - p0)) - p0
        events.append(
            Excursion(
                start_t=ts[start],
                end_t=ts[end] if end is not None else None,
                peak_dev=seg_err[kpk],
                peak_t=seg[kpk].t,
                valve_max=max(valves) if valves else None,
                valve_min=min(valves) if valves else None,
                pressure_delta=dp,
                crossings=count_crossings(seg_err, band),
                status=" ".join(sorted({c for s in seg for c in s.status.split()})),
            )
        )
        k = stop if end is None else j
    return events


def summarize_hold(
    samples: Sequence[Sample], *, band: float, recover_hold: float
) -> tuple[dict[str, Any], list[Excursion]]:
    flows = [s.flow for s in samples]
    ts = [s.t for s in samples]
    errs = [s.flow - s.sp_cmd for s in samples]
    events = detect_excursions(samples, band=band, recover_hold=recover_hold)
    rec = [e.recovery_s for e in events if e.recovery_s is not None]
    duration = ts[-1] - ts[0] if len(ts) > 1 else 0.0
    valves = [s.valve for s in samples if s.valve is not None]
    pressures = [
        (s.pressure, e) for s, e in zip(samples, errs, strict=True) if s.pressure is not None
    ]
    corr = pearson([p for p, _ in pressures], [e for _, e in pressures]) if pressures else None
    summary = {
        "duration_s": round(duration, 1),
        "samples": len(samples),
        "rate_hz": round((len(samples) - 1) / duration, 2) if duration else 0,
        "band": band,
        "mean": statistics.fmean(flows),
        "mean_error": statistics.fmean(errs),
        "sigma": detrended_std(ts, flows),
        "max_abs_error": max(abs(e) for e in errs),
        "in_band_pct": 100 * sum(abs(e) <= band for e in errs) / len(errs),
        "events": len(events),
        "events_per_hour": round(3600 * len(events) / duration, 2) if duration else 0,
        "recovery_median_s": round(statistics.median(rec), 3) if rec else None,
        "recovery_p90_s": round(sorted(rec)[int(0.9 * (len(rec) - 1))], 3) if rec else None,
        "recovery_max_s": round(max(rec), 3) if rec else None,
        "unrecovered_events": sum(e.end_t is None for e in events),
        "valve_min_pct": min(valves) if valves else None,
        "valve_max_pct": max(valves) if valves else None,
        "valve_saturated_pct_time": (
            round(100 * sum(v >= VALVE_SATURATED_PCT for v in valves) / len(valves), 2)
            if valves
            else None
        ),
        "pressure_error_corr": None if corr is None else round(corr, 3),
    }
    return summary, events


def diagnose(summary: dict[str, Any], events: Sequence[Excursion]) -> list[str]:
    notes: list[str] = []
    have_valve = any(e.valve_max is not None for e in events)
    sat = [e for e in events if e.valve_max is not None and e.valve_max >= VALVE_SATURATED_PCT]
    if have_valve and len(sat) >= max(1, len(events) // 3):
        notes.append(
            f"{len(sat)}/{len(events)} excursions hit >= {VALVE_SATURATED_PCT:.0f}% valve drive: "
            "the controller is out of authority (supply pressure too low for this flow, "
            "or valve undersized). Gain tuning cannot fix saturation; raise supply "
            "pressure or lower the setpoint relative to full scale."
        )
    stuck = [
        e
        for e in events
        if e.peak_dev < 0 and (e.recovery_s is None or e.recovery_s > SUPPLY_LIMITED_S)
    ]
    if not have_valve and stuck:
        notes.append(
            f"{len(stuck)} excursions sat below setpoint for > {SUPPLY_LIMITED_S:g}s (or never "
            "recovered). A loop with integral action only stays low that long when the valve "
            "is fully open, i.e. the upstream supply sagged. This firmware cannot report "
            "valve drive to confirm; check supply pressure before tuning."
        )
    corr = summary["pressure_error_corr"]
    if corr is not None and abs(corr) >= 0.5:
        notes.append(
            f"device pressure moves with flow error (r={corr:+.2f}). If the pressure sensor "
            "is downstream of the valve (reads ~barometric at zero flow) this is just "
            "back-pressure following flow; if it is upstream, supply swings are driving "
            "the upsets."
        )
    osc = [e for e in events if e.crossings >= 3]
    if osc:
        notes.append(
            f"{len(osc)} excursions rang (>= 3 swings across the band): loop is "
            "underdamped; `tune` should find gains with less overshoot."
        )
    med = summary["recovery_median_s"]
    if med is not None and med > 3.0 and not sat and not stuck:
        notes.append(f"median recovery {med:.1f}s: loop is sluggish; `tune` should shorten this.")
    if not events:
        notes.append("no excursions outside the band during this window.")
    return notes


def cmd_monitor(args: argparse.Namespace) -> int:  # noqa: PLR0915 — linear orchestration
    stop = StopFlag()
    stop.install()
    out = make_out_dir(args, "monitor")
    try:
        with open_controller(args) as dev:
            lv_state = try_call(dev.loop_control_variable)
            lv = lv_state.variable if lv_state else None
            initial_sp = current_setpoint(dev)
            sp = args.setpoint if args.setpoint is not None else initial_sp
            if sp is None:
                raise SystemExit("error: could not read the setpoint; pass --setpoint")
            if args.setpoint is not None and (initial_sp is None or abs(sp - initial_sp) > 1e-6):
                print(f"setting setpoint {initial_sp} -> {sp} (left there on exit)")
                dev.setpoint(sp)
            band = args.band if args.band is not None else args.band_pct / 100 * sp
            sampler = Sampler(dev, stop, out / "samples.csv", vd_every=args.vd_every, controlled=lv)
            samples: list[Sample] = []
            print(
                f"monitoring {args.duration:g}s at {sp:g} (band +/-{band:.3f}); Ctrl-C stops early"
            )
            try:
                last_print = 0.0
                start = time.monotonic()
                while time.monotonic() - start < args.duration:
                    s = sampler.sample(sp_cmd=sp, trial=0, segment="monitor")
                    samples.append(s)
                    if s.t - last_print >= 10:
                        last_print = s.t
                        vd = "" if s.valve is None else f"  valve {s.valve:5.1f}%"
                        print(f"  t={s.t:7.1f}s  flow {s.flow:9.3f}  err {s.flow - sp:+8.3f}{vd}")
            except StopRequested:
                print("stopping early")
            finally:
                sampler.close()
            if len(samples) < 10:
                raise SystemExit("too few samples to analyse")
            summary, events = summarize_hold(samples, band=band, recover_hold=args.recover_hold)
            with (out / "events.csv").open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(
                    [
                        "start_t",
                        "recovery_s",
                        "peak_dev",
                        "peak_t",
                        "valve_min",
                        "valve_max",
                        "pressure_delta",
                        "crossings",
                        "status",
                    ]
                )
                for e in events:
                    w.writerow(
                        [
                            f"{e.start_t:.3f}",
                            "" if e.recovery_s is None else f"{e.recovery_s:.3f}",
                            f"{e.peak_dev:.4f}",
                            f"{e.peak_t:.3f}",
                            e.valve_min,
                            e.valve_max,
                            "" if e.pressure_delta is None else f"{e.pressure_delta:.4f}",
                            e.crossings,
                            e.status,
                        ]
                    )
            notes = diagnose(summary, events)
            summary["diagnosis"] = notes
            write_json(out / "summary.json", summary)
            print_hold_summary("monitor", summary)
            print("\ndiagnosis:")
            for n in notes:
                print(f"  - {n}")
            plot_monitor(out, samples, events, sp, band)
            print(f"\noutputs -> {out}")
    finally:
        stop.uninstall()
    return 0


def print_hold_summary(title: str, s: dict[str, Any]) -> None:
    print(f"\n== {title} ==")
    print(f"  duration {s['duration_s']}s  rate {s['rate_hz']} Hz  band +/-{s['band']:.3f}")
    print(
        f"  mean {s['mean']:.4f}  mean err {s['mean_error']:+.4f}  sigma {s['sigma']:.4f}  "
        f"max|err| {s['max_abs_error']:.4f}  in-band {s['in_band_pct']:.1f}%"
    )
    print(
        f"  excursions {s['events']} ({s['events_per_hour']}/h)  recovery med/p90/max "
        f"{s['recovery_median_s']}/{s['recovery_p90_s']}/{s['recovery_max_s']} s  "
        f"unrecovered {s['unrecovered_events']}"
    )
    if s["valve_max_pct"] is not None:
        print(
            f"  valve {s['valve_min_pct']:.1f}..{s['valve_max_pct']:.1f}%  "
            f"saturated {s['valve_saturated_pct_time']}% of time"
        )
    if s["pressure_error_corr"] is not None:
        print(f"  corr(device pressure, flow error) = {s['pressure_error_corr']:+.3f}")


# ---------------------------------------------------------------------------
# tune
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SavedState:
    """Device configuration captured before tune touches anything."""

    gains: Gains
    setpoint: float | None
    ramp: tuple[float, TimeUnit] | None = None
    lss: str | None = None
    algorithm_changed: bool = False


def build_plan(args: argparse.Namespace, fs: float | None) -> Plan:
    sp = args.setpoint
    step = args.step if args.step is not None else args.step_pct / 100 * sp
    caps = [x for x in (args.max_flow, fs) if x is not None]
    hi_cap = min(caps) if caps else None
    lo_cap = args.min_flow if args.min_flow is not None else 0.0
    up_ok = hi_cap is None or sp + step <= hi_cap
    down_ok = sp - step >= lo_cap
    if not down_ok:
        raise SystemExit(f"error: S-step = {sp - step:g} is below --min-flow {lo_cap:g}")
    targets = [sp - step, sp, sp + step, sp] if up_ok else [sp - step, sp, sp - step, sp]
    if not up_ok:
        print(f"note: S+step = {sp + step:g} exceeds {hi_cap:g}; using down-steps only")
    abort_dev = (
        args.abort_deviation if args.abort_deviation is not None else max(3 * step, 0.1 * sp)
    )
    max_flow = args.max_flow if args.max_flow is not None else sp + step + 2 * abort_dev
    if fs is not None:
        max_flow = min(max_flow, 1.05 * fs)
    min_flow = args.min_flow if args.min_flow is not None else max(0.0, sp - step - 2 * abort_dev)
    band = args.band if args.band is not None else args.band_pct / 100 * sp
    return Plan(
        setpoint=sp,
        step=step,
        targets=targets,
        band=band,
        window=args.window or args.max_window,
        settle_s=args.settle,
        hold_s=args.hold,
        abort_dev=abort_dev,
        abort_hold=args.abort_hold,
        min_flow=min_flow,
        max_flow=max_flow,
        early_stop_hold=args.early_stop_hold,
        settle_max=max(args.settle, args.settle_max),
        gain_ramp=args.gain_ramp,
        gain_ramp_dwell=args.gain_ramp_dwell,
    )


def cmd_tune(args: argparse.Namespace) -> int:  # noqa: PLR0912, PLR0915 — linear orchestration
    stop = StopFlag()
    out = make_out_dir(args, "tune")
    write_json(out / "args.json", vars(args))
    with open_controller(args) as dev:
        info = dev.info
        print(f"device: {info.model}  fw {info.firmware.raw}  serial {info.serial}")
        lv_state = try_call(dev.loop_control_variable)
        lv = lv_state.variable if lv_state else None
        if lv is not None and lv not in _FLOW_STATS:
            raise SystemExit(f"error: loop variable is {lv.name}; this tuner handles flow loops")
        fs = full_scale_for(dev, lv)
        lss = try_call(dev.setpoint_source)
        if lss == "A":
            raise SystemExit("error: setpoint source is analog (LSS=A); switch to S/U first")

        backend = make_backend(dev)
        if backend.writes_persist and not args.legacy_registers:
            raise SystemExit(
                f"error: firmware {info.firmware.raw} has no LCG/LCGD; gains would be written "
                "through registers 21/22/23 (community-documented, persistent). Re-run with "
                "--legacy-registers to accept that."
            )
        original = with_retry(backend.read)
        saved = SavedState(gains=original, setpoint=current_setpoint(dev), lss=lss)
        ramp = try_call(dev.ramp_rate)
        if ramp is not None and ramp.max_ramp > 0:
            saved.ramp = (ramp.max_ramp, ramp.time_unit)
        backup = {
            "port": args.port,
            "unit_id": args.unit_id,
            "model": info.model,
            "serial": info.serial,
            "firmware": info.firmware.raw,
            "backend": backend.name,
            "captured_at": datetime.now().astimezone().isoformat(),
            "gains": original.to_json(),
            "setpoint": saved.setpoint,
            "ramp": saved.ramp and [saved.ramp[0], int(saved.ramp[1])],
            "setpoint_source": lss,
        }
        write_json(out / "backup_gains.json", backup)
        print(f"backup -> {out / 'backup_gains.json'}")
        print(f"gain access: {backend.name}")
        print(f"current gains: {original.describe()}")

        plan = build_plan(args, fs)
        weights = Weights(args.w_settle, args.w_iae, args.w_overshoot, args.w_noise)
        for t in [plan.setpoint, *plan.targets]:
            if not plan.min_flow <= t <= plan.max_flow:
                raise SystemExit(
                    f"error: target {t:g} outside [{plan.min_flow:g}, {plan.max_flow:g}]"
                )
        print(
            f"plan: S={plan.setpoint:g}  step={plan.step:.3f}  "
            f"targets={[round(t, 3) for t in plan.targets]}"
            f"\n      band +/-{plan.band:.3f}  abort dev {plan.abort_dev:.3f}"
            f"  safe range [{plan.min_flow:.2f}, {plan.max_flow:.2f}]"
        )
        if saved.setpoint is not None and abs(saved.setpoint - plan.setpoint) > plan.step:
            print(
                f"note: device setpoint is {saved.setpoint}; tuning will move it to {plan.setpoint}"
            )
        est = (plan.settle_s + plan.hold_s + len(plan.targets) * min(args.max_window, 8)) * (
            args.max_trials + args.baseline_repeats + 2 * args.confirm_repeats + 1
        )
        print(f"estimated duration: ~{est / 60:.0f} min (Ctrl-C stops safely)")
        if not confirm("Proceed? Flow will be stepped around the setpoint.", assume_yes=args.yes):
            return 1

        sampler = Sampler(dev, stop, out / "samples.csv", vd_every=args.vd_every, controlled=lv)
        runner = Runner(dev, backend, sampler, plan=plan, weights=weights, out=out)
        runner.last_good = original
        accepted: Gains | None = None
        rc = 0
        stop.install()
        try:
            base = original
            if args.algorithm != "keep":
                want = Algorithm.PD2I if args.algorithm == "pd2i" else Algorithm.PDF
                if want is not original.algorithm:
                    backend.set_algorithm(want)
                    saved.algorithm_changed = True
                    base = backend.read()
                    runner.last_good = None
                    print(f"switched algorithm -> {base.describe()} (restored on exit)")
            if saved.lss == "S" and not args.keep_lss:
                dev.setpoint_source("U", save=False)
                print("setpoint source S -> U for the run (not saved; restored on exit)")
            if saved.ramp is not None and not args.keep_ramp:
                dev.ramp_rate(0.0, saved.ramp[1])
                print(f"ramp {saved.ramp[0]} disabled for the run (restored on exit)")
            elif saved.ramp is not None:
                print("WARNING: ramp rate active; step metrics will reflect the ramp")

            runner.last_good = base
            print(f"\napproaching S={plan.setpoint:g} ...")
            if not runner.wait_at_setpoint(args.approach_timeout, "approach"):
                raise SystemExit(
                    "error: flow never settled at the operating setpoint on the current gains. "
                    "Check gas supply pressure (valve may be fully open) before tuning."
                )
            if args.window is None:
                print("\n== calibration: baseline with long window ==")
                calib = runner.run_trial(base, "calibrate", window=args.max_window)
                if not calib.stable:
                    raise SystemExit("baseline gains were unstable under the guard; aborting")
                med_settle = statistics.median(s.settle_s for s in calib.steps)
                plan.window = max(args.min_window, min(args.max_window, 2.5 * med_settle + 1.0))
                print(
                    f"step window -> {plan.window:.1f}s (baseline median settle {med_settle:.2f}s)"
                )
                runner.cache.clear()
            runner.ref_hold_std = None

            print("\n== baseline ==")
            base_runs = [runner.run_trial(base, "baseline") for _ in range(args.baseline_repeats)]
            if any(not r.stable for r in base_runs):
                # The production gains tripping the guard is far more likely an
                # external upset than instability; keep the 1e6 sentinel out of
                # the baseline mean so it can't make every candidate look better.
                print("WARNING: a baseline run was aborted; excluding it from the baseline")
                base_runs = [r for r in base_runs if r.stable]
                runner.cache[base.key()] = list(base_runs)
            if not base_runs:
                raise SystemExit("baseline gains never completed a clean trial; aborting")
            runner.ref_hold_std = statistics.median(noise_level(r) for r in base_runs)
            for r in base_runs:
                r.cost = runner.score(r)
            noise_sigma = (
                statistics.stdev(r.cost for r in base_runs) if len(base_runs) >= 2 else 0.0
            )
            print(
                f"baseline cost {runner.mean_cost(base):.3f} (trial noise sigma {noise_sigma:.3f})"
            )
            sat = [r.valve_sat_frac for r in base_runs if r.valve_sat_frac is not None]
            if sat and max(sat) > 0.05:
                print(
                    "WARNING: valve drive >= 98% during baseline steps; the loop is near "
                    "its authority limit; results on the upper steps are supply-limited."
                )

            names = tuple(
                n for n in base.algorithm.gain_names if base.get(n) > 0 or n in args.tune_zero
            )
            if args.only:
                names = tuple(n for n in names if n in args.only)
            print(f"\n== search over {', '.join(n.upper() for n in names)} ==")
            cfg = SearchConfig(
                names=names,
                max_trials=args.max_trials,
                step0=math.log2(args.initial_factor),
                step_min=math.log2(args.final_factor),
                step_max=math.log2(args.initial_factor) * 2,
                max_factor=args.max_factor,
                rel_margin=args.margin,
                noise_sigma=noise_sigma,
            )
            best = compass_search(runner, base, cfg)

            print("\n== confirmation (alternating baseline / best) ==")
            if best.key() != base.key():
                for _ in range(args.confirm_repeats):
                    runner.run_trial(base, "confirm-base")
                    runner.run_trial(best, "confirm-best")
            jb, jx = runner.mean_cost(base), runner.mean_cost(best)
            gain_pct = 100 * (jb - jx) / jb if jb else 0.0
            print(f"mean cost: baseline {jb:.3f}  best {jx:.3f}  ({gain_pct:+.1f}% better)")
            if best.key() != base.key() and jx >= jb * (1 - args.margin):
                print("best candidate did not beat the baseline by the margin; keeping baseline")
                best = base

            hold_summaries: dict[str, Any] = {}
            if args.hold_test > 0:
                for label, g in (("baseline", base), ("best", best)):
                    if label == "best" and g.key() == base.key():
                        break
                    runner.apply_gains(g)
                    runner.set_setpoint(plan.setpoint)
                    sampler.collect(
                        plan.settle_s,
                        sp_cmd=plan.setpoint,
                        trial=0,
                        segment=f"holdtest-{label}-settle",
                    )
                    hs = sampler.collect(
                        args.hold_test, sp_cmd=plan.setpoint, trial=0, segment=f"holdtest-{label}"
                    )
                    summ, _ = summarize_hold(hs, band=plan.band, recover_hold=args.recover_hold)
                    hold_summaries[label] = summ
                    print_hold_summary(f"hold test: {label} {g.describe()}", summ)

            summary = {
                "original": original.to_json(),
                "baseline": base.to_json(),
                "best": best.to_json(),
                "baseline_cost": jb,
                "best_cost": jx,
                "improvement_pct": gain_pct,
                "plan": asdict(plan),
                "weights": asdict(weights),
                "trials": len(runner.results),
                "link_retries": LinkStats.retries,
                "hold_tests": hold_summaries,
            }
            write_json(out / "summary.json", summary)
            plot_tune(out, runner, base, best)
            print_leaderboard(runner)

            if best.key() == original.key():
                print("\nresult: keep current gains")
            else:
                print(f"\nresult: {original.describe()}  ->  {best.describe()}")
                decision = args.apply
                if decision == "ask" and args.yes:
                    print("(--yes does not apply gains; re-run `apply` or pass --apply yes)")
                    decision = "no"
                elif decision == "ask":
                    decision = "yes" if confirm("Apply tuned gains?", assume_yes=False) else "no"
                if decision == "yes":
                    save = args.save or (
                        not args.yes
                        and not backend.writes_persist
                        and confirm(
                            "Save to EEPROM so they survive power cycles?", assume_yes=False
                        )
                    )
                    with_retry(
                        lambda: backend.write(best, save=save), attempts=3, extra=(GainWriteError,)
                    )
                    accepted = best
                    persisted = save or backend.writes_persist
                    print(
                        f"applied {best.describe()}"
                        + (" and saved" if persisted else " (volatile)")
                    )
        except StopRequested:
            print("\nstop requested")
            rc = 130
        finally:
            stop.uninstall()
            restore_after_tune(dev, backend, saved, accepted, args)
            write_trials_csv(out, runner.results)
            runner.close()
            if LinkStats.retries:
                print(f"\nlink: {LinkStats.retries} garbled/lost replies were retried")
            sampler.close()
            print(f"\noutputs -> {out}")
    return rc


def restore_after_tune(
    dev: SyncFlowController,
    backend: GainBackend,
    saved: SavedState,
    accepted: Gains | None,
    args: argparse.Namespace,
) -> None:
    """Undo every temporary change, retrying each step independently."""

    def attempt(what: str, fn: Callable[[], object]) -> None:
        for k in range(3):
            try:
                fn()
            except (AlicatError, GainWriteError) as err:
                print(f"  restore {what}: attempt {k + 1} failed ({err})")
                time.sleep(0.3)
            else:
                print(f"  restored {what}")
                return
        print(f"  !! could not restore {what}; use `restore` with backup_gains.json")

    print("\nrestoring device state ...")
    if accepted is None:
        if saved.algorithm_changed:
            attempt("algorithm", lambda: backend.set_algorithm(saved.gains.algorithm))
        elif args.gain_ramp > 1:
            # Step back gradually so restoring a lower/higher D doesn't bump the flow.
            try:
                current = with_retry(backend.read)
                for mid in gain_path(current, saved.gains, args.gain_ramp)[:-1]:
                    with_retry(lambda g=mid: backend.write(g, save=False))  # type: ignore[misc]
                    time.sleep(args.gain_ramp_dwell)
            except (AlicatError, GainWriteError) as err:
                print(f"  gain ramp-back interrupted ({err}); writing originals directly")
        attempt(f"gains {saved.gains.describe()}", lambda: backend.write(saved.gains, save=False))
    if saved.ramp is not None and not args.keep_ramp:
        ramp = saved.ramp
        attempt("ramp rate", lambda: dev.ramp_rate(ramp[0], ramp[1]))
    # LSS first so the final setpoint write behaves as it did before the run.
    if saved.lss == "S" and not args.keep_lss:
        attempt("setpoint source S", lambda: dev.setpoint_source("S", save=False))
    final = args.final_setpoint if args.final_setpoint is not None else saved.setpoint
    if final is not None:
        attempt(f"setpoint {final:g}", lambda: dev.setpoint(final))


def write_trials_csv(out: Path, results: Sequence[TrialResult]) -> None:
    if not results:
        return
    rows = [r.summary_row() for r in results]
    with (out / "trials.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def print_leaderboard(runner: Runner, top: int = 8) -> None:
    by_gain: dict[tuple[int, ...], list[TrialResult]] = {}
    for r in runner.results:
        if r.label != "calibrate":
            by_gain.setdefault(r.gains.key(), []).append(r)
    ranked = sorted(by_gain.values(), key=lambda rs: statistics.fmean(r.cost for r in rs))
    print("\n== leaderboard (mean cost over repeats) ==")
    print(f"  {'gains':<34} {'n':>2} {'cost':>9} {'settle':>7} {'OS%':>6} {'hold sd':>8}")
    for rs in ranked[:top]:
        stable = [r for r in rs if r.stable]
        cost = statistics.fmean(r.cost for r in rs)
        settle = [s.settle_s for r in stable for s in r.steps]
        os_ = [s.overshoot for r in stable for s in r.steps]
        hold = [r.hold_std for r in stable]
        print(
            f"  {rs[0].gains.describe():<34} {len(rs):>2} {cost:>9.3f} "
            f"{statistics.median(settle) if settle else math.nan:>7.2f} "
            f"{100 * statistics.median(os_) if os_ else math.nan:>6.1f} "
            f"{statistics.median(hold) if hold else math.nan:>8.4f}"
        )


# ---------------------------------------------------------------------------
# apply / restore
# ---------------------------------------------------------------------------


def _write_gains(args: argparse.Namespace, build: Callable[[Gains], Gains]) -> int:
    with open_controller(args) as dev:
        backend = make_backend(dev)
        if backend.writes_persist and not args.legacy_registers:
            raise SystemExit("error: register-based firmware; pass --legacy-registers to write")
        current = backend.read()
        target = build(current)
        out = make_out_dir(args, args.command)
        write_json(
            out / "backup_gains.json",
            {"gains": current.to_json(), "port": args.port, "serial": dev.info.serial},
        )
        print(f"current: {current.describe()}   (backup -> {out / 'backup_gains.json'})")
        print(f"target:  {target.describe()}")
        switch = target.algorithm is not current.algorithm
        if switch and backend.writes_persist:
            raise SystemExit("error: algorithm differs and this firmware cannot switch it (no LCA)")
        prompt = "Switch algorithm and write these gains?" if switch else "Write these gains?"
        if not confirm(prompt, assume_yes=args.yes):
            return 1
        if switch:
            backend.set_algorithm(target.algorithm)
        got = backend.write(target, save=args.save)
        print(
            f"device now reports {got.describe()}"
            + (" (saved)" if args.save or backend.writes_persist else " (volatile)")
        )
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    def build(cur: Gains) -> Gains:
        vals = {n: v for n, v in (("p", args.p), ("d", args.d), ("i", args.i)) if v is not None}
        return cur.with_values(vals)

    return _write_gains(args, build)


def cmd_restore(args: argparse.Namespace) -> int:
    data = json.loads(Path(args.backup).read_text(encoding="utf-8"))
    gains = Gains.from_json(data["gains"])
    print(f"backup from {data.get('captured_at', '?')} serial {data.get('serial', '?')}")
    return _write_gains(args, lambda _cur: gains)


# ---------------------------------------------------------------------------
# Plots (optional)
# ---------------------------------------------------------------------------


def _pyplot() -> Any:
    try:
        mpl = importlib.import_module("matplotlib")
        mpl.use("Agg")
        plt = importlib.import_module("matplotlib.pyplot")
    except ImportError:
        print("(matplotlib not installed; skipping plots: `uv pip install matplotlib`)")
        return None
    return plt


def _load_samples(out: Path) -> list[dict[str, str]]:
    with (out / "samples.csv").open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def plot_tune(out: Path, runner: Runner, base: Gains, best: Gains) -> None:
    plt = _pyplot()
    if plt is None:
        return
    rows = _load_samples(out)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 8))
    for gains, color in ((base, "tab:gray"), (best, "tab:blue")):
        trials = {r.trial for r in runner.cache.get(gains.key(), []) if r.stable}
        for trial in sorted(trials)[-1:]:
            seg_rows = [r for r in rows if int(r["trial"]) == trial and r["segment"] != "settle"]
            if not seg_rows:
                continue
            t0 = float(seg_rows[0]["t"])
            ts = [float(r["t"]) - t0 for r in seg_rows]
            ax1.plot(ts, [float(r["flow"]) for r in seg_rows], color=color, label=gains.describe())
            ax1.step(ts, [float(r["sp_cmd"]) for r in seg_rows], color="k", lw=0.8, where="post")
    ax1.set_xlabel("s")
    ax1.set_ylabel("flow")
    ax1.legend()
    ax1.set_title("hold + step pattern: baseline vs best")
    costs = [min(r.cost, 3 * runner.mean_cost(base)) for r in runner.results]
    ax2.plot(range(1, len(costs) + 1), costs, "o-")
    ax2.set_xlabel("trial")
    ax2.set_ylabel("cost (clipped)")
    fig.tight_layout()
    fig.savefig(out / "tune.png", dpi=120)
    plt.close(fig)


def plot_monitor(
    out: Path, samples: Sequence[Sample], events: Sequence[Excursion], sp: float, band: float
) -> None:
    plt = _pyplot()
    if plt is None:
        return
    fig, axes = plt.subplots(3, 1, figsize=(12, 8), sharex=True)
    ts = [s.t for s in samples]
    axes[0].plot(ts, [s.flow for s in samples], lw=0.7)
    axes[0].axhspan(sp - band, sp + band, color="tab:green", alpha=0.15)
    for e in events:
        axes[0].axvspan(e.start_t, e.end_t or ts[-1], color="tab:red", alpha=0.15)
    axes[0].set_ylabel("flow")
    valves = [(s.t, s.valve) for s in samples if s.valve is not None]
    if valves:
        axes[1].plot(*zip(*valves, strict=True), lw=0.7)
    axes[1].set_ylabel("valve %")
    press = [(s.t, s.pressure) for s in samples if s.pressure is not None]
    if press:
        axes[2].plot(*zip(*press, strict=True), lw=0.7)
    axes[2].set_ylabel("pressure")
    axes[2].set_xlabel("s")
    fig.tight_layout()
    fig.savefig(out / "monitor.png", dpi=120)
    plt.close(fig)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _gain_list(text: str) -> tuple[str, ...]:
    names = tuple(x.strip().lower() for x in text.split(",") if x.strip())
    bad = [n for n in names if n not in {"p", "d", "i"}]
    if bad:
        raise argparse.ArgumentTypeError(f"unknown gain(s) {bad}; use p,d,i")
    return names


def build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915 — flat option table
    ap = argparse.ArgumentParser(
        description="Autotune Alicat flow-controller loop gains.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--port", required=True, help="serial port, e.g. COM7")
        p.add_argument("--baud", default="auto", help="baud rate or 'auto'")
        p.add_argument("--unit-id", default="A")
        p.add_argument("--timeout", type=float, default=0.5, help="per-command timeout (s)")
        p.add_argument("--out", help="output directory (default autotune_runs/<stamp>-<cmd>)")
        p.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompts")
        p.add_argument(
            "--stop-stream",
            action="store_true",
            help="take a streaming device to polling mode (@@ <unit-id>) before opening",
        )

    p_probe = sub.add_parser(
        "probe",
        help="read-only device + loop report",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    common(p_probe)
    p_probe.set_defaults(func=cmd_probe)

    p_mon = sub.add_parser(
        "monitor",
        help="characterise excursions while holding a setpoint",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    common(p_mon)
    p_mon.add_argument("--setpoint", type=float, help="set this first (default: keep current)")
    p_mon.add_argument("--duration", type=float, default=600.0, help="seconds")
    p_mon.add_argument("--band-pct", type=float, default=0.5, help="excursion band, %% of SP")
    p_mon.add_argument("--band", type=float, help="excursion band in flow units (overrides %%)")
    p_mon.add_argument(
        "--recover-hold",
        type=float,
        default=2.0,
        help="seconds back inside the band that count as recovered",
    )
    p_mon.add_argument(
        "--vd-every", type=int, default=3, help="query valve drive every N polls (0 = never)"
    )
    p_mon.set_defaults(func=cmd_monitor)

    p_tune = sub.add_parser(
        "tune", help="run the autotuner", formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    common(p_tune)
    g = p_tune.add_argument_group("operating point")
    g.add_argument("--setpoint", type=float, required=True, help="operating setpoint S")
    g.add_argument("--step-pct", type=float, default=3.0, help="step size, %% of S")
    g.add_argument("--step", type=float, help="step size in flow units (overrides %%)")
    g.add_argument("--band-pct", type=float, default=0.3, help="settling band, %% of S")
    g.add_argument("--band", type=float, help="settling band in flow units (overrides %%)")
    g.add_argument(
        "--final-setpoint",
        type=float,
        help="setpoint to leave on exit (default: what it was at start)",
    )
    g = p_tune.add_argument_group("timing")
    g.add_argument("--settle", type=float, default=3.0, help="s after gain change, ignored")
    g.add_argument("--hold", type=float, default=5.0, help="s of hold-noise measurement")
    g.add_argument("--window", type=float, help="fixed step window s (default: calibrate)")
    g.add_argument("--min-window", type=float, default=5.0)
    g.add_argument("--max-window", type=float, default=20.0)
    g.add_argument(
        "--hold-test",
        type=float,
        default=45.0,
        help="final steady-hold test per gain set, s (0 = skip)",
    )
    g.add_argument("--recover-hold", type=float, default=2.0)
    g.add_argument("--vd-every", type=int, default=4, help="valve-drive query cadence")
    g.add_argument(
        "--settle-max",
        type=float,
        default=15.0,
        help="max s to wait after a gain change for flow to return in band",
    )
    g.add_argument(
        "--gain-ramp",
        type=float,
        default=1.25,
        help="max ratio per gain write when moving to new gains (<= 1 = jump directly)",
    )
    g.add_argument(
        "--gain-ramp-dwell", type=float, default=0.5, help="s between ramped gain writes"
    )
    g.add_argument(
        "--early-stop-hold",
        type=float,
        default=3.0,
        help="end a step once in-band this long (and as long as it took to get there); 0 = off",
    )
    g.add_argument(
        "--approach-timeout",
        type=float,
        default=60.0,
        help="s allowed to first reach and hold S before tuning starts",
    )
    g = p_tune.add_argument_group("safety")
    g.add_argument("--min-flow", type=float, help="absolute lower flow limit (abort below)")
    g.add_argument("--max-flow", type=float, help="absolute upper flow limit (abort above)")
    g.add_argument(
        "--abort-deviation",
        type=float,
        help="error that aborts a trial (default max(3*step, 10%% S))",
    )
    g.add_argument(
        "--abort-hold", type=float, default=1.5, help="s the error may exceed --abort-deviation"
    )
    g.add_argument(
        "--legacy-registers",
        action="store_true",
        help="allow register writes on pre-10v05 firmware",
    )
    g.add_argument("--keep-ramp", action="store_true", help="do not disable ramp rate")
    g.add_argument("--keep-lss", action="store_true", help="do not switch LSS S -> U")
    g = p_tune.add_argument_group("search")
    g.add_argument(
        "--algorithm",
        choices=("keep", "pdf", "pd2i"),
        default="keep",
        help="loop algorithm to tune (10v05+ to switch)",
    )
    g.add_argument("--only", type=_gain_list, help="tune only these gains, e.g. 'p,d'")
    g.add_argument(
        "--tune-zero",
        type=_gain_list,
        default=(),
        help="also tune gains that start at 0 (they start from 1)",
    )
    g.add_argument("--max-trials", type=int, default=30, help="search evaluations")
    g.add_argument("--baseline-repeats", type=int, default=2)
    g.add_argument("--confirm-repeats", type=int, default=2)
    g.add_argument("--initial-factor", type=float, default=2.0, help="first multiplicative move")
    g.add_argument("--final-factor", type=float, default=1.1, help="stop when moves get this small")
    g.add_argument("--max-factor", type=float, default=16.0, help="max distance from baseline")
    g.add_argument("--margin", type=float, default=0.05, help="relative improvement to accept")
    g = p_tune.add_argument_group("cost weights")
    g.add_argument("--w-settle", type=float, default=1.0, help="per second of settling")
    g.add_argument("--w-iae", type=float, default=1.0, help="per second of normalised IAE")
    g.add_argument("--w-overshoot", type=float, default=2.0, help="per 100%% overshoot")
    g.add_argument("--w-noise", type=float, default=2.0, help="per 100%% increase in hold std-dev")
    g = p_tune.add_argument_group("result")
    g.add_argument("--apply", choices=("ask", "yes", "no"), default="ask")
    g.add_argument("--save", action="store_true", help="save applied gains to EEPROM")
    p_tune.set_defaults(func=cmd_tune)

    p_apply = sub.add_parser(
        "apply", help="write explicit gains", formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    common(p_apply)
    p_apply.add_argument("--p", type=int)
    p_apply.add_argument("--d", type=int)
    p_apply.add_argument("--i", type=int)
    p_apply.add_argument("--save", action="store_true")
    p_apply.add_argument("--legacy-registers", action="store_true")
    p_apply.set_defaults(func=cmd_apply)

    p_rest = sub.add_parser(
        "restore",
        help="re-apply gains from backup_gains.json",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    common(p_rest)
    p_rest.add_argument("backup", help="path to backup_gains.json")
    p_rest.add_argument("--save", action="store_true")
    p_rest.add_argument("--legacy-registers", action="store_true")
    p_rest.set_defaults(func=cmd_restore)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "tune" and args.only is None:
        args.only = ()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
