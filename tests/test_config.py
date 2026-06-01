"""Tests for defensive parsing of environment-driven settings.

Because get_settings() is rebuilt on every DB operation, a malformed
WATCHAGENT_POLL_INTERVAL_SECONDS must never raise, and a 0/negative value must
never slip through (asyncio.sleep(0) would become a tight loop).
"""

from __future__ import annotations

import pytest

from app.config import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
    get_settings,
)


@pytest.mark.parametrize("raw", ["", "   ", "abc", "12.5", "0", "-1", "-30"])
def test_poll_interval_falls_back_on_invalid_or_non_positive(monkeypatch, raw):
    monkeypatch.setenv("WATCHAGENT_POLL_INTERVAL_SECONDS", raw)

    # Must not raise, and must never yield a value below the minimum.
    interval = get_settings().poll_interval_seconds

    assert interval == DEFAULT_POLL_INTERVAL_SECONDS
    assert interval >= MIN_POLL_INTERVAL_SECONDS


def test_poll_interval_uses_default_when_unset(monkeypatch):
    monkeypatch.delenv("WATCHAGENT_POLL_INTERVAL_SECONDS", raising=False)
    assert get_settings().poll_interval_seconds == DEFAULT_POLL_INTERVAL_SECONDS


def test_poll_interval_accepts_valid_positive_value(monkeypatch):
    monkeypatch.setenv("WATCHAGENT_POLL_INTERVAL_SECONDS", "30")
    assert get_settings().poll_interval_seconds == 30
