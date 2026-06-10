"""RadarService 单测 —— fake IB / fake quotes，无 Gateway、无真实 ib_async 调用。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from webwatch.config import RadarConfig
from webwatch.radar import RadarService

NAN = float("nan")


class FakeRadarTicker:
    """带雷达所需全部字段的 ticker（默认全 nan = IB 未就绪态）。"""

    def __init__(self, symbol: str) -> None:
        self.contract = SimpleNamespace(symbol=symbol)
        self.marketDataType = 1
        self.bid = NAN
        self.ask = NAN
        self.last = NAN
        self.close = NAN
        self.high = NAN
        self.low = NAN
        self.volume = NAN
        self.vwap = NAN
        self.halted = NAN
        self.rtVolume = NAN
        self.avVolume = NAN

    def quote(self, *, last: float, close: float | None = None, vol: float = 1e7,
              av: float = 5e7, bid: float | None = None, ask: float | None = None) -> None:
        """快速设一组正常行情。"""
        self.last = last
        self.close = close if close is not None else last
        self.bid = bid if bid is not None else last - 0.01
        self.ask = ask if ask is not None else last + 0.01
        self.high = max(self.last, self.close)
        self.low = min(self.last, self.close)
        self.volume = vol
        self.rtVolume = vol
        self.avVolume = av
        self.vwap = last
        self.halted = 0.0


class FakeScanRow:
    def __init__(self, symbol: str, exchange: str = "") -> None:
        self.contractDetails = SimpleNamespace(
            contract=SimpleNamespace(symbol=symbol, exchange=exchange, primaryExchange="NASDAQ")
        )


class FakeScanList(list[FakeScanRow]):
    def __init__(self, rows: list[FakeScanRow], req_id: int) -> None:
        super().__init__(rows)
        self.reqId = req_id


class FakeRadarIB:
    def __init__(self) -> None:
        self.connected = True
        self.scan_results: dict[str, list[str]] = {}  # scanCode → 代码列表
        self.scan_calls: list[str] = []
        self.cancelled_scans: list[int] = []
        self.subscribed: list[tuple[str, str]] = []  # (symbol, genericTicks)
        self.cancelled: list[str] = []
        self.tickers: dict[str, FakeRadarTicker] = {}
        self._next_req = 100

    def isConnected(self) -> bool:  # noqa: N802
        return self.connected

    def reqScannerSubscription(self, sub: Any) -> FakeScanList:  # noqa: N802
        self.scan_calls.append(sub.scanCode)
        self._next_req += 1
        rows = [FakeScanRow(s) for s in self.scan_results.get(sub.scanCode, [])]
        return FakeScanList(rows, self._next_req)

    def cancelScannerSubscription(self, data: FakeScanList) -> None:  # noqa: N802
        self.cancelled_scans.append(data.reqId)

    def reqMktData(self, contract: Any, generic: str, snapshot: bool, reg: bool) -> FakeRadarTicker:  # noqa: N802
        self.subscribed.append((contract.symbol, generic))
        t = self.tickers.get(contract.symbol) or FakeRadarTicker(contract.symbol)
        self.tickers[contract.symbol] = t
        return t

    def cancelMktData(self, contract: Any) -> None:  # noqa: N802
        self.cancelled.append(contract.symbol)


class FakeQuotes:
    """RadarService 依赖的最小 quotes 接口（QuotesLike）。"""

    def __init__(self) -> None:
        self.wl: list[str] = []
        self.tk: dict[str, FakeRadarTicker] = {}
        self.market_data_type = 1

    def watchlist(self) -> list[str]:
        return list(self.wl)

    def ticker(self, symbol: str) -> FakeRadarTicker | None:
        return self.tk.get(symbol)


def mk_cfg(**over: Any) -> RadarConfig:
    raw: dict[str, Any] = {
        "scan_timeout_s": 0.05,
        "scan_interval_s": 0.01,
        "max_subscriptions": 35,
        "empty_scans_for_fallback": 3,
    }
    raw.update(over)
    return RadarConfig(raw)


def mk_service(
    cfg: RadarConfig | None = None,
) -> tuple[RadarService, FakeRadarIB, FakeQuotes]:
    quotes = FakeQuotes()
    svc = RadarService(cfg or mk_cfg(), quotes)
    ib = FakeRadarIB()
    svc.attach(ib)
    return svc, ib, quotes


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """扫描结果批次落定等待在测试里清零（fake 同步填充，无需等待）。"""
    monkeypatch.setattr("webwatch.radar._SCAN_SETTLE_S", 0.0)


class TestScanCycle:
    async def test_subscribes_interleaved_rank_up_to_cap(self) -> None:
        svc, ib, _ = mk_service(mk_cfg(max_subscriptions=3))
        ib.scan_results = {
            "TOP_PERC_GAIN": ["A", "B", "C"],
            "TOP_PERC_LOSE": ["D"],
            "HOT_BY_VOLUME": [],
        }
        await svc.scan_cycle(now=1000.0)
        # 名次交错合并：A(gain1) D(lose1) B(gain2) C(gain3) → cap 3
        assert [s for s, _ in ib.subscribed] == ["A", "D", "B"]
        # 每个 scan code 的订阅都被取消（防泄漏）
        assert len(ib.cancelled_scans) == 3

    async def test_generic_ticks_realtime_vs_delayed(self) -> None:
        svc, ib, quotes = mk_service()
        ib.scan_results = {"TOP_PERC_GAIN": ["A"]}
        await svc.scan_cycle(now=1000.0)
        assert ib.subscribed[0][1] == mk_cfg().generic_ticks  # 实时 → 全 generic
        quotes.market_data_type = 3
        svc2, ib2, _ = (RadarService(mk_cfg(), quotes), FakeRadarIB(), None)
        svc2.attach(ib2)
        ib2.scan_results = {"TOP_PERC_GAIN": ["B"]}
        await svc2.scan_cycle(now=1000.0)
        assert ib2.subscribed[0][1] == ""  # 延迟 → 不带 generic tick

    async def test_watchlist_symbols_never_radar_subscribed(self) -> None:
        svc, ib, quotes = mk_service()
        quotes.wl = ["AAPL"]
        ib.scan_results = {"TOP_PERC_GAIN": ["AAPL", "NEW"]}
        await svc.scan_cycle(now=1000.0)
        assert [s for s, _ in ib.subscribed] == ["NEW"]

    async def test_own_sub_released_when_added_to_watchlist(self) -> None:
        svc, ib, quotes = mk_service()
        ib.scan_results = {"TOP_PERC_GAIN": ["ABCD"]}
        await svc.scan_cycle(now=1000.0)
        assert [s for s, _ in ib.subscribed] == ["ABCD"]
        quotes.wl = ["ABCD"]  # 用户把它加进自选 → 雷达让出自有订阅
        await svc.scan_cycle(now=1010.0)
        assert "ABCD" in ib.cancelled

    async def test_eviction_of_stale_symbols(self) -> None:
        svc, ib, _ = mk_service(mk_cfg(max_subscriptions=2, evict_after_s=180))
        ib.scan_results = {"TOP_PERC_GAIN": ["A", "B"]}
        await svc.scan_cycle(now=1000.0)
        assert {s for s, _ in ib.subscribed} == {"A", "B"}
        # 5 分钟后 A、B 不再出现在扫描里 → 驱逐换新
        ib.scan_results = {"TOP_PERC_GAIN": ["C", "D"]}
        await svc.scan_cycle(now=1300.0)
        assert set(ib.cancelled) == {"A", "B"}
        assert {s for s, _ in ib.subscribed} == {"A", "B", "C", "D"}

    async def test_exchange_defaults_to_smart(self) -> None:
        svc, ib, _ = mk_service()
        ib.scan_results = {"TOP_PERC_GAIN": ["A"]}
        await svc.scan_cycle(now=1000.0)
        # FakeScanRow exchange="" → 订阅前补 SMART
        assert svc._contracts["A"].exchange == "SMART"

    async def test_empty_scans_trigger_fallback_and_backoff(self) -> None:
        svc, ib, _ = mk_service()
        ib.scan_results = {}  # 全空
        base = mk_cfg().scan_interval_s
        for i in range(3):
            await svc.scan_cycle(now=1000.0 + i)
        st = svc.snapshot_dict()["status"]
        assert st["fallback_watchlist_only"] is True
        assert svc._scan_interval() > base  # 退避生效
        # 一次成功 → 复位
        ib.scan_results = {"TOP_PERC_GAIN": ["A"]}
        await svc.scan_cycle(now=1010.0)
        st = svc.snapshot_dict()["status"]
        assert st["fallback_watchlist_only"] is False
        assert svc._scan_interval() == base

    async def test_empty_scan_never_evicts(self) -> None:
        svc, ib, _ = mk_service(mk_cfg(max_subscriptions=1))
        ib.scan_results = {"TOP_PERC_GAIN": ["A"]}
        await svc.scan_cycle(now=1000.0)
        ib.scan_results = {}
        await svc.scan_cycle(now=2000.0)  # 一次坏扫描不能清空雷达
        assert ib.cancelled == []

    async def test_disconnected_noop(self) -> None:
        svc, ib, _ = mk_service()
        ib.connected = False
        await svc.scan_cycle(now=1000.0)
        assert ib.scan_calls == [] and ib.subscribed == []


class TestHandlesError:
    async def test_scanner_req_and_own_symbol_claimed(self) -> None:
        svc, ib, _ = mk_service()
        ib.scan_results = {"TOP_PERC_GAIN": ["A"]}
        await svc.scan_cycle(now=1000.0)
        scan_req = ib.cancelled_scans[0]
        assert svc.handles_error(scan_req, 162, "scanner blocked", None) is True
        assert "162" in (svc.snapshot_dict()["status"]["scanner_error"] or "")
        own = SimpleNamespace(symbol="A")
        assert svc.handles_error(999, 354, "not subscribed", own) is True
        other = SimpleNamespace(symbol="MSFT")
        assert svc.handles_error(999, 354, "not subscribed", other) is False
        assert svc.handles_error(998, 200, "no security", None) is False


class TestComputeCycle:
    async def test_watch_symbol_metrics_flow_to_board_and_events(self) -> None:
        svc, _, quotes = mk_service()
        t = FakeRadarTicker("AAPL")
        t.quote(last=100.0, close=99.0)
        quotes.wl = ["AAPL"]
        quotes.tk["AAPL"] = t
        svc.compute_cycle(now=1000.0)
        t.quote(last=101.0, close=99.0)  # 60 秒内 +1% → spike_1m
        svc.compute_cycle(now=1060.0)
        snap = svc.snapshot_dict()
        up = snap["boards"]["up"]
        assert [r["symbol"] for r in up] == ["AAPL"]
        assert up[0]["watch"] is True
        assert up[0]["chg_1m_pct"] is not None and up[0]["chg_1m_pct"] > 0.9
        evs = snap["events"]
        assert any(e["type"] == "spike_1m" and e["symbol"] == "AAPL" for e in evs)
        # 事件带 id 与 UTC 时间戳
        assert all("id" in e and "ts" in e for e in evs)

    async def test_event_ids_monotonic(self) -> None:
        svc, _, quotes = mk_service()
        t = FakeRadarTicker("AAPL")
        t.quote(last=100.0)
        quotes.wl = ["AAPL"]
        quotes.tk["AAPL"] = t
        svc.compute_cycle(now=1000.0)
        t.quote(last=101.0)
        svc.compute_cycle(now=1060.0)  # spike up
        t.quote(last=99.0)
        svc.compute_cycle(now=1300.0)  # 冷却后 spike down
        ids = [e["id"] for e in svc.snapshot_dict()["events"]]
        assert ids == sorted(ids) and len(set(ids)) == len(ids) and len(ids) >= 2

    async def test_own_subscription_rows_and_filters(self) -> None:
        svc, ib, _ = mk_service()
        ib.scan_results = {"TOP_PERC_GAIN": ["HOTX", "TINY"]}
        await svc.scan_cycle(now=1000.0)
        ib.tickers["HOTX"].quote(last=20.0, close=19.0, vol=2e6, av=1e7)  # 成交额 $40M
        ib.tickers["TINY"].quote(last=2.0, close=1.9, vol=100_000.0, av=1e6)  # $200K < $2M
        svc.compute_cycle(now=1001.0)
        snap = svc.snapshot_dict()
        names = {r["symbol"] for r in snap["boards"]["up"]}
        assert "HOTX" in names and "TINY" not in names  # 粗滤拦掉小成交额
        assert snap["status"]["sub_count"] == 2
        assert snap["status"]["universe"] == 2

    async def test_nan_ticker_skipped_and_snapshot_json_safe(self) -> None:
        svc, ib, quotes = mk_service()
        ib.scan_results = {"TOP_PERC_GAIN": ["NODATA"]}
        await svc.scan_cycle(now=1000.0)
        # NODATA 全 nan；自选 AAPL 正常
        t = FakeRadarTicker("AAPL")
        t.quote(last=100.0, close=98.0)
        quotes.wl = ["AAPL"]
        quotes.tk["AAPL"] = t
        svc.compute_cycle(now=1001.0)
        snap = svc.snapshot_dict()
        json.dumps(snap, allow_nan=False)  # 任何 NaN 都会让这行抛错
        assert {r["symbol"] for r in snap["boards"]["up"]} == {"AAPL"}

    async def test_delayed_flag_propagates(self) -> None:
        svc, _, quotes = mk_service()
        t = FakeRadarTicker("AAPL")
        t.quote(last=100.0, close=98.0)
        t.marketDataType = 3
        quotes.wl = ["AAPL"]
        quotes.tk["AAPL"] = t
        svc.compute_cycle(now=1000.0)
        rows = svc.snapshot_dict()["boards"]["up"]
        assert rows and rows[0]["delayed"] is True

    async def test_lot_multiplier_applied_when_no_rtvolume(self) -> None:
        svc, _, quotes = mk_service(mk_cfg(volume_lot_multiplier=100))
        t = FakeRadarTicker("AAPL")
        t.quote(last=100.0, close=98.0, vol=50_000.0)  # lots 报量
        t.rtVolume = NAN  # 无 tick233 → 走 volume × multiplier
        quotes.wl = ["AAPL"]
        quotes.tk["AAPL"] = t
        svc.compute_cycle(now=1000.0)
        row = svc.snapshot_dict()["boards"]["up"][0]
        assert row["dollar_vol_today"] == 100.0 * 50_000.0 * 100


class TestLifecycleAndMdt:
    async def test_start_stop_cancels_tasks_and_own_subs(self) -> None:
        svc, ib, _ = mk_service()
        ib.scan_results = {"TOP_PERC_GAIN": ["A"]}
        await svc.scan_cycle(now=1000.0)
        svc.start()
        assert svc._scan_task is not None and svc._compute_task is not None
        await asyncio.sleep(0)  # 让任务起跑
        await svc.stop()
        assert svc._scan_task is None and svc._compute_task is None
        assert "A" in ib.cancelled  # 只撤自有订阅
        assert svc.snapshot_dict()["status"]["sub_count"] == 0

    async def test_detach_clears_without_ib_calls(self) -> None:
        svc, ib, _ = mk_service()
        ib.scan_results = {"TOP_PERC_GAIN": ["A"]}
        await svc.scan_cycle(now=1000.0)
        svc.detach()
        assert ib.cancelled == []  # 断连路径不向死连接发撤订
        assert svc.snapshot_dict()["status"]["sub_count"] == 0

    async def test_disabled_service_inert(self) -> None:
        svc, ib, _ = mk_service(mk_cfg(enabled=False))
        svc.start()
        assert svc._scan_task is None
        svc.compute_cycle(now=1000.0)
        assert svc.snapshot_dict() == {"enabled": False}

    async def test_mdt_change_resubscribes_and_clears_windows(self) -> None:
        svc, ib, quotes = mk_service()
        ib.scan_results = {"TOP_PERC_GAIN": ["A"]}
        await svc.scan_cycle(now=1000.0)
        ib.tickers["A"].quote(last=10.0, close=9.5)
        svc.compute_cycle(now=1001.0)
        assert svc._states["A"].samples  # 有样本
        quotes.market_data_type = 3
        await svc.on_market_data_type_changed()
        assert "A" in ib.cancelled
        # 按新类型（延迟 → 空 generic）重订
        assert ib.subscribed[-1] == ("A", "")
        assert not svc._states  # 采样窗口清空（新数据流重新预热）
