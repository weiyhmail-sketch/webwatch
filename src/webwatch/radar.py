"""异动雷达服务 —— IBKR 扫描器轮询 + 自有行情订阅 + 1s 指标计算（display-only）。

生命周期镜像 quotes.QuoteService：broker 持有单实例，connect 成功后 attach + start，
断开 stop / detach。所有指标/事件/榜单/订阅预算计算在 radar_metrics 纯函数里（TDD），
本模块只做 IB I/O、调度与缓存。float 先例同 quotes.py：display-only，不进下单链路。

防泄漏要点（ib_async 2.1.0 源码核实，见 plan）：**不要用 reqScannerDataAsync** ——
超时/取消会泄漏 server 端 scanner 订阅且不清理 wrapper 状态（IB 并发 scanner 上限 ~10，
泄漏 10 次后扫描器报废）。用 ``reqScannerSubscription``（立即返回，结果就地填充）+
轮询结果 + finally ``cancelScannerSubscription``（公开取消接口做完整清理）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any, Protocol

from ib_async import IB, ScannerSubscription

from webwatch.config import RadarConfig
from webwatch.radar_metrics import (
    Sample,
    SymbolState,
    build_boards,
    compute_row,
    detect_events,
    f_or_none,
    passes_coarse_filters,
    plan_subscriptions,
)

log = logging.getLogger(__name__)

# 扫描结果出现首行后的批次落定等待（同批尾行可能还在路上）。测试置 0。
_SCAN_SETTLE_S = 0.25
_SCAN_POLL_S = 0.25
_COMPUTE_INTERVAL_S = 1.0
_DELAYED_TYPES = frozenset({3, 4})
_EVENTS_KEEP = 80
_EVENTS_IN_SNAPSHOT = 30
_BACKOFF_MAX_S = 600.0


class QuotesLike(Protocol):
    """雷达对行情服务的最小依赖（QuoteService 结构性满足；测试注入 fake）。"""

    @property
    def market_data_type(self) -> int: ...

    def watchlist(self) -> list[str]: ...

    def ticker(self, symbol: str) -> Any: ...


class RadarService:
    """全市场异动雷达：扫描发现 + 自选并入 + 指标榜单 + 事件流。"""

    def __init__(self, cfg: RadarConfig, quotes: QuotesLike) -> None:
        self._cfg = cfg
        self._quotes = quotes
        self._ib: IB | None = None
        # 雷达**自有**订阅（自选标的的订阅归 QuoteService，这里绝不重复订）。
        self._contracts: dict[str, Any] = {}
        self._tickers: dict[str, Any] = {}
        self._states: dict[str, SymbolState] = {}
        self._last_scan_seen: dict[str, float] = {}  # sym → 最近一次出现在扫描结果（monotonic）
        # 近期 scanner reqId（错误认领用；不删旧值——错误事件可能晚于扫描结束到达）。
        self._scanner_req_ids: deque[int] = deque(maxlen=64)
        self._scan_failures = 0
        self._scanner_error: str | None = None
        self._last_scan_mono: float | None = None
        self._events: deque[dict[str, Any]] = deque(maxlen=_EVENTS_KEEP)
        self._event_seq = 0  # 跨重连单调递增（前端声音水位线，不回拨）
        self._board_syms: set[str] = set()
        self._boards: dict[str, list[dict[str, Any]]] = {"up": [], "down": []}
        self._universe = 0
        self._scan_task: asyncio.Task[None] | None = None
        self._compute_task: asyncio.Task[None] | None = None

    # ---- 生命周期（镜像 QuoteService）-----------------------------------

    @property
    def connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    def attach(self, ib: IB) -> None:
        """连接建立后绑定 IB（不动 reqMarketDataType——那是 QuoteService 的职责）。"""
        self._ib = ib

    def detach(self) -> None:
        """断连路径：只清状态，不向死连接发任何撤订。"""
        self._ib = None
        self._clear()

    def start(self) -> None:
        if not self._cfg.enabled or self._scan_task is not None:
            return
        self._scan_task = asyncio.create_task(self._scan_loop())
        self._compute_task = asyncio.create_task(self._compute_loop())

    async def stop(self) -> None:
        """停循环 + 撤掉**自有**订阅 + 清状态（不碰 QuoteService 的订阅）。"""
        for task in (self._scan_task, self._compute_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._scan_task = None
        self._compute_task = None
        if self.connected:
            assert self._ib is not None
            for contract in list(self._contracts.values()):
                with contextlib.suppress(Exception):
                    self._ib.cancelMktData(contract)
        self._clear()

    def _clear(self) -> None:
        self._contracts.clear()
        self._tickers.clear()
        self._states.clear()
        self._last_scan_seen.clear()
        self._board_syms.clear()
        self._boards = {"up": [], "down": []}
        self._universe = 0
        self._events.clear()
        self._scan_failures = 0
        self._scanner_error = None
        self._last_scan_mono = None
        # _event_seq 故意保留：前端以 id 为声音水位线，回拨会重放旧提醒。

    # ---- 错误认领 / 行情类型联动 ----------------------------------------

    def handles_error(self, req_id: int, error_code: int, error_string: str, contract: Any = None) -> bool:
        """认领属于雷达的 IB 错误（扫描请求 / 自有订阅标的），避免刷屏全局错误横幅。"""
        if req_id in self._scanner_req_ids:
            self._scanner_error = f"IB {error_code}: {error_string}"
            log.warning("radar scanner error %s: %s", error_code, error_string)
            return True
        sym = getattr(contract, "symbol", None)
        if sym is not None and sym in self._contracts:
            log.warning("radar mktdata error %s [%s]: %s", error_code, sym, error_string)
            return True
        return False

    async def on_market_data_type_changed(self) -> None:
        """行情类型切换：自有订阅按新类型重订（generic tick 随之变化），采样窗口清零重预热。"""
        if self.connected:
            assert self._ib is not None
            generic = self._generic_ticks()
            for sym, contract in list(self._contracts.items()):
                with contextlib.suppress(Exception):
                    self._ib.cancelMktData(contract)
                try:
                    self._tickers[sym] = self._ib.reqMktData(contract, generic, False, False)
                except Exception as exc:  # noqa: BLE001 — 单标的失败不拖垮切换
                    log.warning("radar resubscribe %s failed: %s", sym, exc)
                    self._drop_own(sym)
        self._states.clear()  # 新数据流，窗口重新预热（避免新旧类型样本混算）

    def _generic_ticks(self) -> str:
        """实时/冻结才带 generic tick；延迟行情不支持，传空避免每标的报错。"""
        return self._cfg.generic_ticks if self._quotes.market_data_type in (1, 2) else ""

    # ---- 扫描循环 --------------------------------------------------------

    async def _scan_loop(self) -> None:
        while True:
            try:
                await self.scan_cycle()
            except Exception as exc:  # noqa: BLE001 — 后台循环绝不被单轮异常杀死
                log.warning("radar scan cycle error: %s: %s", type(exc).__name__, exc)
                self._note_scan_failure(f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(self._scan_interval())

    def _scan_interval(self) -> float:
        """扫描间隔：连续空扫/失败达阈值后指数退避（×4 起步，封顶 10 分钟），成功即复位。"""
        base = self._cfg.scan_interval_s
        over = self._scan_failures - self._cfg.empty_scans_for_fallback
        if over < 0:
            return base
        return min(base * (4.0 ** (over + 1)), _BACKOFF_MAX_S)

    def _note_scan_failure(self, reason: str) -> None:
        self._scan_failures += 1
        if self._scanner_error is None:
            self._scanner_error = reason

    async def scan_cycle(self, now: float | None = None) -> None:
        """跑一轮全部 scan code：合并榜单 → 订阅预算计划 → 执行订/撤。"""
        if not self._cfg.enabled or not self.connected:
            return
        mono = time.monotonic() if now is None else now

        per_code: list[list[Any]] = []
        for code in self._cfg.scan_codes:
            per_code.append(await self._scan_once(code))
        if not per_code or not any(per_code):
            self._note_scan_failure("扫描结果全空（无权限或休市时段）")
            return
        self._scan_failures = 0
        self._scanner_error = None
        self._last_scan_mono = mono

        # 名次交错合并去重：gain[0], lose[0], hot[0], gain[1] ...（plan_subscriptions 按此序取候选）
        scanned: dict[str, Any] = {}
        ranked: list[str] = []
        for i in range(max(len(rows) for rows in per_code)):
            for rows in per_code:
                if i < len(rows):
                    contract = rows[i].contractDetails.contract
                    sym = str(contract.symbol)
                    if sym not in scanned:
                        scanned[sym] = contract
                        ranked.append(sym)
        for sym in ranked:
            self._last_scan_seen[sym] = mono

        watch = set(self._quotes.watchlist())
        # 已被用户加进自选的标的让出雷达自有订阅（QuoteService 那边已有同一行情流），
        # 保留 state——指标窗口无缝衔接，不重新预热。
        for sym in [s for s in self._contracts if s in watch]:
            self._drop_own(sym, keep_state=True)

        current = {sym: self._last_scan_seen.get(sym, 0.0) for sym in self._contracts}
        to_sub, to_unsub = plan_subscriptions(
            current,
            ranked,
            watch | self._board_syms,  # 自选 + 在榜标的受保护
            now=mono,
            cap=self._cfg.max_subscriptions,
            evict_after_s=self._cfg.evict_after_s,
            max_evictions=self._cfg.max_evictions_per_cycle,
        )
        for sym in to_unsub:
            self._drop_own(sym)
        assert self._ib is not None
        generic = self._generic_ticks()
        for sym in to_sub:
            contract = scanned[sym]
            if not getattr(contract, "exchange", ""):
                contract.exchange = "SMART"  # 扫描合约已带 conId，路由交 SMART
            try:
                self._tickers[sym] = self._ib.reqMktData(contract, generic, False, False)
                self._contracts[sym] = contract
                self._states.setdefault(sym, SymbolState(first_seen_ts=mono))
            except Exception as exc:  # noqa: BLE001 — 单标的订阅失败不拖垮扫描循环
                log.warning("radar subscribe %s failed: %s", sym, exc)

    async def _scan_once(self, scan_code: str) -> list[Any]:
        """单 scan code 一轮：防泄漏模式（轮询结果 + finally 公开取消）。"""
        assert self._ib is not None
        sub = ScannerSubscription(
            numberOfRows=self._cfg.scan_rows,
            instrument=self._cfg.instrument,
            locationCode=self._cfg.location,
            scanCode=scan_code,
            abovePrice=self._cfg.min_price,
            aboveVolume=self._cfg.min_scan_volume,
        )
        data = self._ib.reqScannerSubscription(sub)
        self._scanner_req_ids.append(int(getattr(data, "reqId", -1)))
        try:
            deadline = time.monotonic() + self._cfg.scan_timeout_s
            while not data and time.monotonic() < deadline:
                await asyncio.sleep(min(_SCAN_POLL_S, max(0.0, deadline - time.monotonic())))
            if data and _SCAN_SETTLE_S > 0:
                await asyncio.sleep(_SCAN_SETTLE_S)  # 等同批尾行落定
            return list(data)
        finally:
            with contextlib.suppress(Exception):
                self._ib.cancelScannerSubscription(data)

    def _drop_own(self, sym: str, *, keep_state: bool = False) -> None:
        contract = self._contracts.pop(sym, None)
        self._tickers.pop(sym, None)
        self._last_scan_seen.pop(sym, None)
        if not keep_state:
            self._states.pop(sym, None)
        if contract is not None and self.connected:
            assert self._ib is not None
            with contextlib.suppress(Exception):
                self._ib.cancelMktData(contract)

    # ---- 计算循环 --------------------------------------------------------

    async def _compute_loop(self) -> None:
        while True:
            try:
                self.compute_cycle()
            except Exception as exc:  # noqa: BLE001 — 后台循环绝不被单轮异常杀死
                log.warning("radar compute cycle error: %s: %s", type(exc).__name__, exc)
            await asyncio.sleep(_COMPUTE_INTERVAL_S)

    def compute_cycle(self, now: float | None = None) -> None:
        """对（自有订阅 ∪ 自选）采样并重算指标/事件/榜单。1 秒一轮，纯算术。"""
        if not self._cfg.enabled:
            return
        mono = time.monotonic() if now is None else now
        watch = self._quotes.watchlist()
        watch_set = set(watch)
        tickers: dict[str, Any] = dict(self._tickers)
        for sym in watch:
            if sym not in tickers:
                t = self._quotes.ticker(sym)
                if t is not None:
                    tickers[sym] = t

        rows: list[dict[str, Any]] = []
        lot = self._cfg.volume_lot_multiplier
        for sym, t in tickers.items():
            is_watch = sym in watch_set
            price = f_or_none(getattr(t, "last", None))
            if price is None:
                b, a = f_or_none(getattr(t, "bid", None)), f_or_none(getattr(t, "ask", None))
                if b is not None and a is not None:
                    price = (a + b) / 2.0
            if price is None:
                continue  # 订阅刚建立还没价：跳过本轮
            state = self._states.get(sym)
            if state is None:
                state = SymbolState(first_seen_ts=mono)
                self._states[sym] = state
            state.is_watch = is_watch

            # 当日累计量（股）：tick233 的 rtVolume 按规范就是股数；
            # 否则用 volume × lot 乘数（旧 gateway 按 lots 报量，check_radar.py 核定）。
            rtv = f_or_none(getattr(t, "rtVolume", None))
            if rtv is not None:
                vol_shares: float | None = rtv
            else:
                v = f_or_none(getattr(t, "volume", None))
                vol_shares = v * lot if v is not None else None
            av = f_or_none(getattr(t, "avVolume", None))
            av_shares = av * lot if av is not None else None

            state.samples.append(Sample(ts=mono, price=price, cum_volume=vol_shares))
            row = compute_row(
                sym,
                state,
                mono,
                bid=getattr(t, "bid", None),
                ask=getattr(t, "ask", None),
                last=price,
                close=getattr(t, "close", None),
                day_high=getattr(t, "high", None),
                day_low=getattr(t, "low", None),
                vwap=getattr(t, "vwap", None),
                volume_shares=vol_shares,
                av_volume=av_shares,
                halted_raw=getattr(t, "halted", None),
                delayed=getattr(t, "marketDataType", None) in _DELAYED_TYPES,
                watch=is_watch,
                weights=self._cfg.score_weights,
            )
            if not passes_coarse_filters(
                row["last"],
                row["dollar_vol_today"],
                row["spread_pct"],
                min_price=self._cfg.min_price,
                min_dollar_volume=self._cfg.min_dollar_volume_usd,
                max_spread_pct=self._cfg.max_spread_pct,
                is_watch=is_watch,
            ):
                continue
            for ev in detect_events(state, row, self._cfg.thresholds, mono):
                self._event_seq += 1
                ev["id"] = self._event_seq
                ev["ts"] = datetime.now(UTC).isoformat(timespec="seconds")
                self._events.append(ev)
            rows.append(row)

        up, down = build_boards(rows, self._cfg.board_size)
        self._board_syms = {r["symbol"] for r in up} | {r["symbol"] for r in down}
        self._boards = {"up": up, "down": down}
        self._universe = len(tickers)

    # ---- 快照 ------------------------------------------------------------

    def snapshot_dict(self) -> dict[str, Any]:
        """前端所需雷达状态（无 NaN——所有数值出口都过了 f_or_none）。"""
        if not self._cfg.enabled:
            return {"enabled": False}
        return {
            "enabled": True,
            "status": self._status(time.monotonic()),
            "boards": self._boards,
            "events": list(self._events)[-_EVENTS_IN_SNAPSHOT:],
            "next_event_id": self._event_seq,
        }

    def _status(self, mono: float) -> dict[str, Any]:
        age = None if self._last_scan_mono is None else max(0.0, mono - self._last_scan_mono)
        return {
            "scanner_ok": self._last_scan_mono is not None and self._scan_failures == 0,
            "scanner_error": self._scanner_error,
            "fallback_watchlist_only": self._scan_failures >= self._cfg.empty_scans_for_fallback,
            "last_scan_age_s": f_or_none(age),
            "sub_count": len(self._contracts),
            "cap": self._cfg.max_subscriptions,
            "universe": self._universe,
        }


__all__ = ["RadarService", "QuotesLike"]
