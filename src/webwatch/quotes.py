"""行情订阅 —— 独立模块，webwatch 自有实现，**完全不依赖 scalper 行情代码**。

设计取舍（记录给后续 session）：
- 用 ib_async `reqMktData` 的 **L1 逐笔 tick 流**，而非 scalper 的 `reqHistoricalData` /
  `reqRealTimeBars`（5 秒 K 线）。后者受 HMDS 限流(162)/启动卡死(BUG-8) 困扰，且对"看实时报价
  下手动单"来说粒度太粗、延迟大。逐笔 tick 才贴合超短线面板。
- 行情类型（实时/延迟/冻结）可**运行时切换**：当账户实时行情被竞争会话占用（Error 10197）时，
  用户可一键切到免费的延迟行情(type 3)继续看盘，不必重启。
- 每个报价上报**实际拿到的行情类型**（ticker.marketDataType），前端据此显示"实时/延迟"，
  避免"以为是实时其实是延迟"的误判。

红线：不引入 scalper.data.*；金额展示用数字即可（非下单价，下单价在 pricing/order_service 用 Decimal）。
"""

from __future__ import annotations

import contextlib
import logging
import math
from typing import Any

from ib_async import IB, Stock, Ticker

log = logging.getLogger(__name__)

# IBKR 行情类型码 → 中文名。1=实时 2=冻结 3=延迟 4=延迟冻结。
MARKET_DATA_TYPE_NAMES: dict[int, str] = {1: "实时", 2: "冻结", 3: "延迟", 4: "延迟冻结"}
_DELAYED_TYPES = frozenset({3, 4})
_VALID_TYPES = frozenset({1, 2, 3, 4})


def sanitize_symbol(symbol: str) -> str:
    """清洗代码：只保留字母/数字/`.`/`-`，转大写。杜绝引号/空格等脏字符进 watchlist。"""
    return "".join(c for c in symbol.strip().upper() if c.isalnum() or c in ".-")


def _num(value: float | None) -> float | None:
    """ib_async ticker 的 nan/None → None（JSON / 前端友好）。"""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


class QuoteService:
    """管理 watchlist 的实时行情订阅。连接由外部注入（broker 持有同一 IB 实例）。"""

    def __init__(self, market_data_type: int = 1) -> None:
        self._mdt = market_data_type if market_data_type in _VALID_TYPES else 1
        self._ib: IB | None = None
        self._watchlist: list[str] = []  # 保持添加顺序
        self._contracts: dict[str, Stock] = {}
        self._tickers: dict[str, Ticker] = {}

    # ---- 连接绑定 -------------------------------------------------------

    def attach(self, ib: IB) -> None:
        """连接建立后绑定 IB 并设置行情类型。"""
        self._ib = ib
        ib.reqMarketDataType(self._mdt)

    def detach(self) -> None:
        """断开时清理（不在断开的连接上发撤订阅）。"""
        self._ib = None
        self._tickers.clear()
        self._contracts.clear()

    @property
    def connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    @property
    def market_data_type(self) -> int:
        return self._mdt

    # ---- 订阅管理 -------------------------------------------------------

    async def subscribe(self, symbol: str) -> None:
        sym = sanitize_symbol(symbol)
        if not sym:
            return
        if sym not in self._watchlist:
            self._watchlist.append(sym)
        if self.connected:
            await self._do_subscribe(sym)

    async def _do_subscribe(self, sym: str) -> None:
        assert self._ib is not None
        try:
            contract = Stock(sym, "SMART", "USD")
            await self._ib.qualifyContractsAsync(contract)
            self._contracts[sym] = contract
            # snapshot=False → 持续 streaming；regulatorySnapshot=False → 不额外收费快照
            self._tickers[sym] = self._ib.reqMktData(contract, "", False, False)
        except Exception as exc:  # noqa: BLE001 — 单标的订阅失败不应拖垮整个面板
            log.warning("subscribe %s failed: %s: %s", sym, type(exc).__name__, exc)

    def unsubscribe(self, symbol: str) -> None:
        sym = sanitize_symbol(symbol)
        if sym in self._watchlist:
            self._watchlist.remove(sym)
        contract = self._contracts.pop(sym, None)
        self._tickers.pop(sym, None)
        if contract is not None and self.connected:
            assert self._ib is not None
            try:
                self._ib.cancelMktData(contract)
            except Exception as exc:  # noqa: BLE001
                log.warning("cancelMktData %s: %s", sym, exc)

    async def resubscribe_all(self) -> None:
        """重连后重订所有 watchlist 标的。"""
        if not self.connected:
            return
        for sym in list(self._watchlist):
            await self._do_subscribe(sym)

    async def set_market_data_type(self, mdt: int) -> int:
        """运行时切换行情类型（如实时被占用→切延迟）。返回生效类型。重订以立即刷新。"""
        if mdt not in _VALID_TYPES:
            raise ValueError(f"无效行情类型 {mdt}（应为 1/2/3/4）")
        self._mdt = mdt
        if self.connected:
            assert self._ib is not None
            self._ib.reqMarketDataType(mdt)
            # 先撤再订，确保现有标的按新类型刷新
            for sym in list(self._watchlist):
                contract = self._contracts.get(sym)
                if contract is not None:
                    with contextlib.suppress(Exception):
                        self._ib.cancelMktData(contract)
            self._tickers.clear()
            await self.resubscribe_all()
        return self._mdt

    # ---- 读取 -----------------------------------------------------------

    def watchlist(self) -> list[str]:
        return list(self._watchlist)

    def quotes(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for sym in self._watchlist:
            ticker = self._tickers.get(sym)
            if ticker is None:
                out.append(self._empty_quote(sym))
                continue
            mdt = getattr(ticker, "marketDataType", None)
            bid, ask, last, close = (
                _num(ticker.bid),
                _num(ticker.ask),
                _num(ticker.last),
                _num(ticker.close),
            )
            out.append(
                {
                    "symbol": sym,
                    "bid": bid,
                    "ask": ask,
                    "last": last,
                    "close": close,
                    "data_type": MARKET_DATA_TYPE_NAMES.get(mdt) if mdt else None,
                    "delayed": mdt in _DELAYED_TYPES if mdt else None,
                    "has_data": any(v is not None for v in (bid, ask, last)),
                }
            )
        return out

    @staticmethod
    def _empty_quote(sym: str) -> dict[str, Any]:
        return {
            "symbol": sym,
            "bid": None,
            "ask": None,
            "last": None,
            "close": None,
            "data_type": None,
            "delayed": None,
            "has_data": False,
        }


__all__ = ["QuoteService", "MARKET_DATA_TYPE_NAMES", "sanitize_symbol"]
