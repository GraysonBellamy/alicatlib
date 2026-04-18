"""Tests for ``alicatlib.config``."""

from __future__ import annotations

import pytest

from alicatlib.config import AlicatConfig, config_from_env
from tests._typing import approx


class TestAlicatConfig:
    def test_defaults(self) -> None:
        cfg = AlicatConfig()
        assert cfg.default_timeout_s == approx(0.5)
        assert cfg.default_baudrate == 19200
        assert cfg.drain_before_write is False

    def test_frozen(self) -> None:
        cfg = AlicatConfig()
        with pytest.raises(AttributeError):
            cfg.default_baudrate = 9600  # type: ignore[misc]

    def test_replace_copies_on_write(self) -> None:
        cfg = AlicatConfig()
        new = cfg.replace(default_baudrate=115200)
        assert cfg.default_baudrate == 19200
        assert new.default_baudrate == 115200


class TestConfigFromEnv:
    def test_returns_defaults_when_env_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in (
            "ALICATLIB_DEFAULT_TIMEOUT_S",
            "ALICATLIB_DEFAULT_BAUDRATE",
            "ALICATLIB_DRAIN_BEFORE_WRITE",
        ):
            monkeypatch.delenv(key, raising=False)
        cfg = config_from_env()
        assert cfg == AlicatConfig()

    def test_reads_well_known_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALICATLIB_DEFAULT_TIMEOUT_S", "1.25")
        monkeypatch.setenv("ALICATLIB_DEFAULT_BAUDRATE", "115200")
        monkeypatch.setenv("ALICATLIB_DRAIN_BEFORE_WRITE", "true")
        cfg = config_from_env()
        assert cfg.default_timeout_s == approx(1.25)
        assert cfg.default_baudrate == 115200
        assert cfg.drain_before_write is True

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("1", True),
            ("true", True),
            ("YES", True),
            ("on", True),
            ("0", False),
            ("false", False),
            ("no", False),
            ("", False),
            ("banana", False),  # unparseable falls through to default
        ],
    )
    def test_bool_env_parsing(
        self, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
    ) -> None:
        monkeypatch.setenv("ALICATLIB_DRAIN_BEFORE_WRITE", value)
        cfg = config_from_env()
        assert cfg.drain_before_write is expected

    def test_unparseable_numeric_falls_back_silently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALICATLIB_DEFAULT_TIMEOUT_S", "not-a-number")
        monkeypatch.setenv("ALICATLIB_DEFAULT_BAUDRATE", "also-bad")
        cfg = config_from_env()
        assert cfg.default_timeout_s == approx(0.5)
        assert cfg.default_baudrate == 19200

    def test_custom_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALICATIO_DEFAULT_BAUDRATE", "9600")
        cfg = config_from_env(prefix="ALICATIO_")
        assert cfg.default_baudrate == 9600
