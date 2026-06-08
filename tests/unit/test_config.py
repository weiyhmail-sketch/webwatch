"""config 单测 —— 重点是 M7 的 live 二重互锁。"""

from __future__ import annotations

import pytest

from webwatch.config import Environment, resolve_environment


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEBWATCH_ENV", raising=False)
    monkeypatch.delenv("WEBWATCH_LIVE_CONFIRM", raising=False)


class TestResolveEnvironment:
    def test_default_is_paper(self) -> None:
        env, warn = resolve_environment()
        assert env is Environment.PAPER
        assert warn is None

    def test_live_requires_confirm_else_falls_back_to_paper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WEBWATCH_ENV", "live")  # 只设 env，没设 confirm
        env, warn = resolve_environment()
        assert env is Environment.PAPER  # 安全回退
        assert warn is not None and "互锁" in warn

    def test_live_with_both_interlocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEBWATCH_ENV", "live")
        monkeypatch.setenv("WEBWATCH_LIVE_CONFIRM", "YES")
        env, warn = resolve_environment()
        assert env is Environment.LIVE
        assert warn is None

    def test_confirm_without_live_env_stays_paper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WEBWATCH_LIVE_CONFIRM", "YES")  # 没设 env=live
        env, _ = resolve_environment()
        assert env is Environment.PAPER
