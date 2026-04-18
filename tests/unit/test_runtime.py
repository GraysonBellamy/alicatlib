"""Tests for :mod:`alicatlib._runtime` — the eager-task-factory helper."""

from __future__ import annotations

import asyncio

import pytest

from alicatlib._runtime import install_eager_task_factory
from alicatlib.config import AlicatConfig, config_from_env


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


class TestEagerTaskFactory:
    @pytest.mark.anyio
    async def test_installs_on_asyncio_loop(self, anyio_backend: str) -> None:
        """Returns ``True`` and sets the factory only under asyncio."""
        if anyio_backend != "asyncio":
            pytest.skip("asyncio-only test")
        before = asyncio.get_running_loop().get_task_factory()
        try:
            installed = install_eager_task_factory()
            assert installed is True
            assert asyncio.get_running_loop().get_task_factory() is asyncio.eager_task_factory
        finally:
            # Restore so downstream tests aren't affected.
            asyncio.get_running_loop().set_task_factory(before)

    @pytest.mark.anyio
    async def test_noop_on_trio(self, anyio_backend: str) -> None:
        """trio: safe to call, returns False, no crash."""
        if anyio_backend != "trio":
            pytest.skip("trio-only test")
        assert install_eager_task_factory() is False

    def test_outside_any_loop_returns_false(self) -> None:
        """No event loop running → helper returns False, doesn't raise."""
        assert install_eager_task_factory() is False


class TestConfigEagerTasksField:
    def test_default_is_false(self) -> None:
        assert AlicatConfig().eager_tasks is False

    def test_env_loader_reads_truthy_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ALICATLIB_EAGER_TASKS", "1")
        assert config_from_env().eager_tasks is True
        monkeypatch.setenv("ALICATLIB_EAGER_TASKS", "true")
        assert config_from_env().eager_tasks is True
        monkeypatch.setenv("ALICATLIB_EAGER_TASKS", "yes")
        assert config_from_env().eager_tasks is True

    def test_env_loader_reads_falsy_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ALICATLIB_EAGER_TASKS", "0")
        assert config_from_env().eager_tasks is False
        monkeypatch.setenv("ALICATLIB_EAGER_TASKS", "no")
        assert config_from_env().eager_tasks is False

    def test_replace_preserves_other_fields(self) -> None:
        base = AlicatConfig(default_timeout_s=0.3)
        mutated = base.replace(eager_tasks=True)
        assert mutated.eager_tasks is True
        assert mutated.default_timeout_s == 0.3
