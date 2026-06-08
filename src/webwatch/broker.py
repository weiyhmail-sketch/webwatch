"""券商连接管理 —— 持有单个持久 IB 连接，复用 scalper 的连接层与适配器。

M1 范围：只读（连接 / 账户 / 持仓 / 挂单 / 实时报价）。下单在 M3/M4 加。

关键：webwatch 自己 ``ib = IB()`` 后连接，再 ``IbBrokerAdapter(ib, ...)``，
因此持有**同一个 IB 实例**——模式B 的 ``ib.bracketOrder`` 可直接用（M3）。

设计为对断连**容错**：未连上时各读取方法返回空，``snapshot()`` 标记 connected=False，
前端据此显示状态，而非崩溃。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from decimal import Decimal
from typing import Any, Protocol, TypeVar

from ib_async import IB, LimitOrder, MarketOrder, Stock, StopOrder, Ticker
from scalper.execution.ib_broker_adapter import (
    IbBrokerAdapter,
    connect_live_async,
    connect_paper_async,
)
from scalper.execution.oca_group import DEFAULT_OCA_TYPE, make_oca_group
from scalper.execution.types import OrderId
from scalper.strategy.base import Side

from webwatch.config import Environment, IBKRSettings, PanelConfig
from webwatch.pricing import BracketPrices, Target, compute_bracket
from webwatch.serialize import account_to_dict, order_to_dict, position_to_dict

log = logging.getLogger(__name__)

T = TypeVar("T")

# secrets 里未填真实账户时的占位值（见 secrets.env.example）。
_PLACEHOLDER_ACCOUNTS = frozenset({"", "DU0000000"})

# IBKR 的"信息/状态"类 errorEvent 码（连接 OK、数据农场状态等），非真错误，不展示给用户。
# 202 = 委托单被取消：我们主动撤单时的正常回执（超短线撤单频繁），不当错误弹。
_BENIGN_IB_CODES = frozenset(
    {202, 1100, 1101, 1102, 2103, 2104, 2105, 2106, 2107, 2108, 2109, 2110, 2119, 2137, 2150, 2158, 2168, 2169}
)


# 保护单确认"已挂活"只认 IB 已接受上簿的状态。
# 不含 PendingSubmit/ApiPending/ApiUpdate —— 那些是"尚未上簿"，可能随后被拒，
# 误判为已挂活会留下没保护的裸仓。这些瞬态在 _verify 里继续等，超时则 fail-closed→平仓。
_PROTECTION_OK_STATES = frozenset({"PreSubmitted", "Submitted", "Filled"})
_PROTECTION_BAD_STATES = frozenset({"Cancelled", "ApiCancelled", "Inactive", "ValidationError"})


class ProtectionFailed(Exception):
    """红线 #3：入场已成交但保护单(TP/SL)未挂成功，已紧急平仓。"""

    def __init__(self, *, filled: int, fill_price: Decimal, reason: str, flatten: dict[str, Any]) -> None:
        super().__init__(f"保护单失败已平仓: {reason}")
        self.filled = filled
        self.fill_price = fill_price
        self.reason = reason
        self.flatten = flatten


class EntryUncertain(Exception):
    """入场单超时未达终态：状态未知（可能已成交=裸仓）。需用户立即查持仓，不可当作"未成交"。"""

    def __init__(self, status: str) -> None:
        super().__init__(f"入场单超时未达终态（status={status}），状态未知，请立即检查持仓与挂单！")
        self.status = status


def resolve_account(configured: str, managed: list[str]) -> str:
    """决定适配器用哪个账户做 scoping。

    已配置真实账户 → 用它；否则（空/占位符）回退到 Gateway 返回的
    managedAccounts 首个，让用户不填账户号面板也能正常显示。
    """
    if configured and configured not in _PLACEHOLDER_ACCOUNTS:
        return configured
    return managed[0] if managed else configured


class BrokerLike(Protocol):
    """app 依赖的最小券商接口（测试可注入 fake）。"""

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
    async def watch(self, symbol: str) -> None: ...
    def unwatch(self, symbol: str) -> None: ...
    def snapshot(self) -> dict[str, Any]: ...
    async def place_limit_bracket(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: int,
        entry_limit: Decimal,
        take_profit: Decimal,
        stop_loss: Decimal,
    ) -> dict[str, Any]: ...
    async def place_market_with_protection(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: int,
        take_profit: Target,
        stop_loss: Target,
    ) -> dict[str, Any]: ...
    def risk_inputs(self) -> tuple[Decimal | None, Decimal | None]: ...
    async def cancel_all_orders(self) -> dict[str, Any]: ...
    async def flatten_all(self) -> dict[str, Any]: ...


def _num(value: float | None) -> float | None:
    """把 ib_async ticker 的 nan/None 归一成 None（方便 JSON / 前端）。"""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


class BrokerManager:
    """真实 IBKR 连接管理。"""

    def __init__(self, settings: IBKRSettings, panel: PanelConfig) -> None:
        self._settings = settings
        self._market_data_type = panel.market_data_type
        self._ib: IB | None = None
        self._adapter: IbBrokerAdapter | None = None
        self._watchlist: list[str] = []  # 保持添加顺序
        self._contracts: dict[str, Stock] = {}
        self._tickers: dict[str, Ticker] = {}
        self._last_error: str | None = None
        # 串行化下单/平仓操作：单个 IB 连接被多个 await 处理器共享，
        # 防止并发下单交叉（OCA 组错乱、撤单撤到刚挂的保护单等）。
        self._order_lock = asyncio.Lock()
        # 成交等待 / 保护单确认超时（秒）。测试可调小。
        self._fill_timeout_s = 5.0
        self._protect_timeout_s = 2.0

    # ---- 连接生命周期 ---------------------------------------------------

    def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    async def connect(self) -> None:
        """连接 Gateway（按环境选 paper/live），并重订阅 watchlist。"""
        s = self._settings
        ib = IB()
        try:
            if s.environment is Environment.PAPER:
                await connect_paper_async(ib, s.ibkr_host, s.ibkr_port, s.ibkr_client_id)
            else:
                await connect_live_async(
                    ib,
                    s.ibkr_host,
                    s.ibkr_port,
                    s.ibkr_client_id,
                    expected_account=s.ibkr_account or None,
                )
            ib.errorEvent += self._on_ib_error
            ib.reqMarketDataType(self._market_data_type)
            self._ib = ib
            # 未填真实账户时自动用 Gateway 探测到的账户做 scoping。
            account = resolve_account(s.ibkr_account, list(ib.managedAccounts()))
            self._adapter = IbBrokerAdapter(ib, paper_account=account)
            self._last_error = None
            # 重新订阅之前 watch 的标的
            for sym in list(self._watchlist):
                await self._subscribe(sym)
            log.info("connected to %s gateway", s.environment.value)
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            log.warning("connect failed: %s", self._last_error)
            if ib.isConnected():
                ib.disconnect()
            self._ib = None
            self._adapter = None

    async def disconnect(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()
        self._ib = None
        self._adapter = None

    def _on_ib_error(
        self,
        req_id: int,
        error_code: int,
        error_string: str,
        contract: Any = None,
    ) -> None:
        """把 IBKR 真错误（如 10197 无市场数据、354 未订阅）抓到 last_error 供前端展示。"""
        if error_code in _BENIGN_IB_CODES:
            return
        sym = f" [{contract.symbol}]" if contract is not None and hasattr(contract, "symbol") else ""
        self._last_error = f"IB {error_code}:{sym} {error_string}"
        log.warning("IB error %s: %s", error_code, error_string)

    # ---- 报价订阅 -------------------------------------------------------

    async def watch(self, symbol: str) -> None:
        sym = symbol.strip().upper()
        if not sym:
            return
        if sym not in self._watchlist:
            self._watchlist.append(sym)
        if self.is_connected():
            await self._subscribe(sym)

    async def _subscribe(self, sym: str) -> None:
        assert self._ib is not None
        try:
            contract = Stock(sym, "SMART", "USD")
            await self._ib.qualifyContractsAsync(contract)
            self._contracts[sym] = contract
            self._tickers[sym] = self._ib.reqMktData(contract, "", False, False)
        except Exception as exc:  # noqa: BLE001 — 单个标的订阅失败不应拖垮面板
            self._last_error = f"watch {sym}: {type(exc).__name__}: {exc}"
            log.warning(self._last_error)

    def unwatch(self, symbol: str) -> None:
        sym = symbol.strip().upper()
        if sym in self._watchlist:
            self._watchlist.remove(sym)
        contract = self._contracts.pop(sym, None)
        self._tickers.pop(sym, None)
        if contract is not None and self._ib is not None and self._ib.isConnected():
            try:
                self._ib.cancelMktData(contract)
            except Exception as exc:  # noqa: BLE001
                log.warning("cancelMktData %s: %s", sym, exc)

    def _quotes(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for sym in self._watchlist:
            t = self._tickers.get(sym)
            if t is None:
                out.append({"symbol": sym, "bid": None, "ask": None, "last": None, "close": None})
                continue
            out.append(
                {
                    "symbol": sym,
                    "bid": _num(t.bid),
                    "ask": _num(t.ask),
                    "last": _num(t.last),
                    "close": _num(t.close),
                }
            )
        return out

    # ---- 状态快照 -------------------------------------------------------

    def _safe(self, fn: Any) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            log.warning("read failed: %s", self._last_error)
            return None

    def snapshot(self) -> dict[str, Any]:
        """组装前端所需的完整只读状态。"""
        connected = self.is_connected()
        account: dict[str, Any] | None = None
        positions: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        if connected and self._adapter is not None:
            a = self._safe(self._adapter.account_summary)
            account = account_to_dict(a) if a is not None else None
            positions = [position_to_dict(p) for p in (self._safe(self._adapter.list_positions) or [])]
            orders = [order_to_dict(o) for o in (self._safe(self._adapter.list_open_orders) or [])]
        return {
            "connected": connected,
            "environment": self._settings.environment.value,
            "market_data_type": self._market_data_type,
            "error": self._last_error,
            "account": account,
            "positions": positions,
            "orders": orders,
            "quotes": self._quotes(),
            "watchlist": list(self._watchlist),
        }

    # ---- 下单（M3：限价 + 模式B 原生 bracket）---------------------------

    async def place_limit_bracket(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: int,
        entry_limit: Decimal,
        take_profit: Decimal,
        stop_loss: Decimal,
    ) -> dict[str, Any]:
        """模式B：用 ib_async 原生 bracket 一次性发出 母单(限价) + 止盈(限价) + 止损(stop)。

        三条单经 ib.bracketOrder 自带 parentId + transmit 链，保护单零空窗。
        ``transmit``：parent/TP=False、SL=True → 全部一起 transmit。
        """
        if not self.is_connected() or self._ib is None:
            raise RuntimeError("未连接 Gateway，无法下单")
        ib = self._ib
        action = "BUY" if side is Side.LONG else "SELL"
        contract = Stock(symbol.strip().upper(), "SMART", "USD")
        async with self._order_lock:
            await ib.qualifyContractsAsync(contract)
            # ib_async 用 float；内部仍以 Decimal 计算，仅在此 IB 边界转 float。
            bracket = ib.bracketOrder(
                action,
                quantity,
                float(entry_limit),
                float(take_profit),
                float(stop_loss),
            )
            for order in bracket:
                ib.placeOrder(contract, order)
            return {
                "symbol": contract.symbol,
                "side": side.value,
                "quantity": quantity,
                "entry_limit": str(entry_limit),
                "take_profit": str(take_profit),
                "stop_loss": str(stop_loss),
                "parent_id": bracket.parent.orderId,
                "take_profit_id": bracket.takeProfit.orderId,
                "stop_loss_id": bracket.stopLoss.orderId,
            }

    # ---- 下单（M4：市价 + 模式A 成交后精确挂 OCA）----------------------

    async def place_market_with_protection(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: int,
        take_profit: Target,
        stop_loss: Target,
    ) -> dict[str, Any]:
        """模式A：市价(IOC)买入 → 拿真实成交价算 TP/SL → 挂 OCA 兄弟单。

        红线 #3：若保护单未挂成功（异常 / 进入终态异常状态），**立即市价平仓** +
        抛 ``ProtectionFailed``，绝不留裸仓。
        """
        if not self.is_connected() or self._ib is None:
            raise RuntimeError("未连接 Gateway，无法下单")
        ib = self._ib
        action = "BUY" if side is Side.LONG else "SELL"
        contract = Stock(symbol.strip().upper(), "SMART", "USD")
        await ib.qualifyContractsAsync(contract)

        async with self._order_lock:
            entry = MarketOrder(action, quantity)
            entry.tif = "IOC"  # 与 scalper 一致：市价单不留挂单残余
            entry_trade = ib.placeOrder(contract, entry)
            done = await self._wait_done(entry_trade, timeout=self._fill_timeout_s)

            # 超时未达终态：入场单状态未知（可能已成交）。绝不静默当作未成交，
            # 否则真成交了却不挂保护单 = 裸仓。大声抛错让用户去查持仓/挂单。
            if not done:
                raise EntryUncertain(entry_trade.orderStatus.status)

            filled = int(entry_trade.orderStatus.filled or 0)
            avg = float(entry_trade.orderStatus.avgFillPrice or 0.0)
            if filled <= 0:
                return {
                    "filled": 0,
                    "status": entry_trade.orderStatus.status,
                    "entry_order_id": entry.orderId,
                    "message": "市价单未成交（IOC 已撤）",
                }

            # 有成交但成交价无效（NaN/<=0）：有仓位却无法算保护单 → 立即平仓。
            if not math.isfinite(avg) or avg <= 0:
                flatten = self._emergency_flatten(contract, side, filled)
                raise ProtectionFailed(
                    filled=filled,
                    fill_price=Decimal("0"),
                    reason=f"成交价无效（{avg}），无法计算保护单",
                    flatten=flatten,
                )

            fill_price = Decimal(str(avg))
            bracket = compute_bracket(fill_price, filled, take_profit, stop_loss, side=side)

            prot_trades: list[Any] = []
            try:
                group, tp_order, sl_order, prot_trades = self._place_oca_protection(
                    contract, side, filled, bracket, entry.orderId
                )
                await self._verify_protection_live(prot_trades, timeout=self._protect_timeout_s)
            except Exception as exc:  # noqa: BLE001 — 任何保护单失败都必须平仓
                # 先撤掉任何已挂出的保护单（避免遗留单腿在平仓后反向开新裸仓），再平仓。
                self._cancel_trades(prot_trades)
                flatten = self._emergency_flatten(contract, side, filled)
                raise ProtectionFailed(
                    filled=filled, fill_price=fill_price, reason=str(exc), flatten=flatten
                ) from exc

            return {
                "filled": filled,
                "fill_price": str(fill_price),
                "symbol": contract.symbol,
                "side": side.value,
                "entry_order_id": entry.orderId,
                "take_profit": str(bracket.take_profit),
                "stop_loss": str(bracket.stop_loss),
                "oca_group": group,
                "take_profit_id": tp_order.orderId,
                "stop_loss_id": sl_order.orderId,
                "tp_below_min_tick": bracket.tp_below_min_tick,
            }

    async def _wait_done(self, trade: Any, timeout: float = 5.0, interval: float = 0.05) -> bool:
        """轮询等待订单到终态。返回 True=已达终态，False=超时未达终态。"""
        for _ in range(max(1, int(timeout / interval))):
            if trade.isDone():
                return True
            await asyncio.sleep(interval)
        return bool(trade.isDone())

    def _cancel_trades(self, trades: list[Any]) -> None:
        """尽力撤掉给定 trade 的订单（用于保护单失败时清理遗留单腿）。"""
        if self._ib is None:
            return
        for t in trades:
            with contextlib.suppress(Exception):
                self._ib.cancelOrder(t.order)

    def _place_oca_protection(
        self,
        contract: Any,
        side: Side,
        quantity: int,
        bracket: BracketPrices,
        entry_order_id: int,
    ) -> tuple[str, Any, Any, list[Any]]:
        """挂 OCA 兄弟单：止盈(限价) + 止损(stop)，一成交即撤另一个。

        若第二腿(止损)挂单抛错，先撤掉已挂出的第一腿(止盈)再抛，避免遗留单腿。
        """
        assert self._ib is not None
        ib = self._ib
        group = make_oca_group(OrderId(f"ww-{entry_order_id}"))
        close_action = "SELL" if side is Side.LONG else "BUY"

        tp_order = LimitOrder(close_action, quantity, float(bracket.take_profit))
        tp_order.ocaGroup = group
        tp_order.ocaType = DEFAULT_OCA_TYPE
        tp_order.tif = "GTC"

        sl_order = StopOrder(close_action, quantity, float(bracket.stop_loss))
        sl_order.ocaGroup = group
        sl_order.ocaType = DEFAULT_OCA_TYPE
        sl_order.tif = "GTC"

        tp_trade = ib.placeOrder(contract, tp_order)
        try:
            sl_trade = ib.placeOrder(contract, sl_order)
        except Exception:
            with contextlib.suppress(Exception):
                ib.cancelOrder(tp_order)
            raise
        return group, tp_order, sl_order, [tp_trade, sl_trade]

    async def _verify_protection_live(
        self, trades: list[Any], timeout: float = 2.0, interval: float = 0.05
    ) -> None:
        """确认保护单挂活。任一进入终态异常或超时未确认 → 抛错（fail-closed）。

        **关键**：任一保护腿 ``Filled`` 表示出场已发生（OCA 正常成交即撤兄弟腿，
        兄弟腿会变 Cancelled —— 这是成功路径，绝不能误判为失败去平仓，否则对已平仓位
        反向开裸仓）。故先判 Filled=成功，再判 BAD。
        """
        for _ in range(max(1, int(timeout / interval))):
            statuses = [t.orderStatus.status for t in trades]
            if any(s == "Filled" for s in statuses):
                return  # 一腿成交 = 出场成功；兄弟腿被 OCA 撤掉是正常的
            if any(s in _PROTECTION_BAD_STATES for s in statuses):
                raise RuntimeError(f"保护单进入异常状态: {statuses}")
            if all(s in _PROTECTION_OK_STATES for s in statuses):
                return
            await asyncio.sleep(interval)
        statuses = [t.orderStatus.status for t in trades]
        if any(s == "Filled" for s in statuses):
            return
        if not all(s in _PROTECTION_OK_STATES for s in statuses):
            raise RuntimeError(f"保护单未确认挂出（超时）: {statuses}")

    # ---- 风控输入 / 撤单 / 全平（M5）-----------------------------------

    def risk_inputs(self) -> tuple[Decimal | None, Decimal | None]:
        """供下单前风控用的 (NAV, 购买力)；未连接或读取失败返回 (None, None)。"""
        if not self.is_connected() or self._adapter is None:
            return (None, None)
        summary = self._safe(self._adapter.account_summary)
        if summary is None:
            return (None, None)
        return (summary.net_liquidation, summary.buying_power)

    async def cancel_all_orders(self) -> dict[str, Any]:
        """撤掉所有挂单（只撤单，不平仓）。进 _order_lock，避免撤掉别的处理器刚挂出的保护单。"""
        if not self.is_connected() or self._adapter is None:
            raise RuntimeError("未连接 Gateway")
        async with self._order_lock:
            self._adapter.cancel_all(None)
            return {"cancelled": True}

    async def flatten_all(self) -> dict[str, Any]:
        """市价全平：先撤所有挂单（含保护单，避免平仓后保护单又开新仓），再市价平掉每个持仓。"""
        if not self.is_connected() or self._ib is None or self._adapter is None:
            raise RuntimeError("未连接 Gateway")
        ib = self._ib
        async with self._order_lock:
            self._adapter.cancel_all(None)
            await asyncio.sleep(0.3)
            closed: list[dict[str, Any]] = []
            # 撤单传播后重读一次持仓，尽量用最新数量（降低 stale 数量风险）。
            for pos in self._adapter.list_positions():
                close_action = "SELL" if pos.side is Side.LONG else "BUY"
                contract = Stock(pos.symbol, "SMART", "USD")
                await ib.qualifyContractsAsync(contract)
                order = MarketOrder(close_action, pos.quantity)
                order.tif = "IOC"
                ib.placeOrder(contract, order)
                closed.append(
                    {"symbol": pos.symbol, "action": close_action, "quantity": pos.quantity}
                )
            log.warning("flatten_all: 平掉 %d 个持仓", len(closed))
            return {"flattened": closed}

    def _emergency_flatten(self, contract: Any, side: Side, quantity: int) -> dict[str, Any]:
        """红线 #3：市价平掉刚成交的仓位，不留裸仓。直接按已确认成交量 ``quantity`` 平。

        为什么直接平 ``quantity`` 而**不**读 ``get_position`` 对账：能走到这里，上游
        ``_verify_protection_live`` 的 Filled-first 已把"任一保护腿成交=出场成功"分流返回，
        故两条调用路径（成交价无效 / 保护单挂失败且无腿成交）下持仓**必为满额 quantity**。
        而 ``ib.positions()`` 缓存由 positionEvent 推送，与成交事件是两条独立流、刚成交那几百
        毫秒常滞后——此刻信任缓存裸 0 会**漏平真实持仓 = 裸仓**（比直接平更危险）。
        """
        assert self._ib is not None
        close_action = "SELL" if side is Side.LONG else "BUY"
        flat = MarketOrder(close_action, quantity)
        flat.tif = "IOC"
        self._ib.placeOrder(contract, flat)
        log.error(
            "PROTECTION FAILED → 紧急平仓 %s %s x%s", contract.symbol, close_action, quantity
        )
        return {"flatten_order_id": flat.orderId, "action": close_action, "quantity": quantity}


__all__ = [
    "BrokerLike",
    "BrokerManager",
    "ProtectionFailed",
    "EntryUncertain",
    "resolve_account",
]
