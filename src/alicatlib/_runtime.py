"""Runtime performance knobs.

Currently just one: :func:`install_eager_task_factory`, an opt-in
:mod:`asyncio` scheduling optimisation (design §5.2). Kept in a dedicated
module so the public API surface stays small and the config dataclass
(:mod:`alicatlib.config`) doesn't grow runtime side effects.
"""

from __future__ import annotations

import asyncio

from alicatlib._logging import get_logger

__all__ = ["install_eager_task_factory"]


_log = get_logger("runtime")


def install_eager_task_factory() -> bool:
    """Install :data:`asyncio.eager_task_factory` on the running event loop.

    Opt-in scheduling optimisation: skips one event-loop round-trip when a
    newly-created task's first ``await`` doesn't suspend — measurable under
    tight command-pair loops, zero cost otherwise. See design §5.2.

    **Caveats.**

    - **asyncio-only.** trio uses a different scheduler; this function is a
      no-op there (returns ``False``) so the call site can run under either
      backend unconditionally.
    - **Loop-global.** Changes the running loop's task factory for *all*
      tasks created after the call, including user code outside this
      library. Enable once near app startup, not per-command.
    - **Semantic shift.** Eager tasks that return or raise before their
      first suspension point never hit the event loop, which can change
      ordering that observers depend on. Don't enable in code with
      subtle sequencing assumptions without testing both ways.

    Returns:
        ``True`` if the factory was installed on an asyncio loop; ``False``
        if running on trio (detected via a missing ``set_task_factory``
        method) or outside any running loop.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _log.debug("install_eager_task_factory: no running asyncio loop — no-op")
        return False
    # trio's event loop (via anyio) lacks set_task_factory; check defensively so
    # the helper is safe to call under either backend.
    if not hasattr(loop, "set_task_factory"):
        _log.debug("install_eager_task_factory: loop %r lacks set_task_factory", loop)
        return False
    loop.set_task_factory(asyncio.eager_task_factory)
    _log.debug("install_eager_task_factory: installed on %r", loop)
    return True
