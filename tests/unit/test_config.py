"""config 单测 —— 重点是 M7 的 live 二重互锁 + 雷达配置解析。"""

from __future__ import annotations

import pytest

from webwatch.config import Environment, PanelConfig, resolve_environment


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


class TestRadarConfig:
    def test_defaults_when_block_missing(self) -> None:
        r = PanelConfig({}).radar
        assert r.enabled is True
        assert r.max_subscriptions == 35
        assert r.scan_codes == ["TOP_PERC_GAIN", "TOP_PERC_LOSE", "HOT_BY_VOLUME"]
        assert r.scan_interval_s == 25.0 and r.scan_timeout_s == 10.0
        assert r.location == "STK.US.MAJOR" and r.instrument == "STK"
        assert r.min_dollar_volume_usd == 2_000_000.0
        assert r.board_size == 15 and r.evict_after_s == 180.0
        assert r.volume_lot_multiplier == 1.0
        assert "233" in r.generic_ticks and "165" in r.generic_ticks
        assert r.thresholds.spike_1m_pct == 0.5
        assert r.thresholds.sustained_min_candles == 3
        assert r.score_weights.w_1m == 1.0 and r.score_weights.w_vol == 0.3

    def test_yaml_values_roundtrip(self) -> None:
        raw = {
            "radar": {
                "enabled": False,
                "max_subscriptions": 20,
                "scan_codes": ["TOP_PERC_GAIN"],
                "min_price": 5,
                "volume_lot_multiplier": 100,
                "generic_ticks": "233,165",
                "thresholds": {"spike_1m_pct": 0.8, "sustained_min_candles": 4},
                "score_weights": {"w_1m": 2.0},
            }
        }
        r = PanelConfig(raw).radar
        assert r.enabled is False and r.max_subscriptions == 20
        assert r.scan_codes == ["TOP_PERC_GAIN"]
        assert r.min_price == 5.0 and r.volume_lot_multiplier == 100.0
        assert r.generic_ticks == "233,165"
        # 部分覆盖：未给的键回退默认
        assert r.thresholds.spike_1m_pct == 0.8 and r.thresholds.sustained_min_candles == 4
        assert r.thresholds.vol_burst_ratio == 8.0
        assert r.score_weights.w_1m == 2.0 and r.score_weights.w_5m == 0.5
