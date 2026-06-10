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
import re
from decimal import Decimal
from typing import Any, NoReturn, Protocol, TypeVar

from ib_async import IB, LimitOrder, MarketOrder, Stock, StopOrder
from scalper.execution.ib_broker_adapter import (
    IbBrokerAdapter,
    connect_live_async,
    connect_paper_async,
)
from scalper.execution.oca_group import DEFAULT_OCA_TYPE, make_oca_group
from scalper.execution.types import OrderId
from scalper.strategy.base import Side

from webwatch.config import Environment, IBKRSettings, PanelConfig
from webwatch.pricing import BracketPrices, Target, compute_bracket, stop_limit_price
from webwatch.quotes import QuoteService
from webwatch.serialize import account_to_dict, order_to_dict, position_to_dict
from webwatch.session import is_rth, session_label

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


# 账户号样式（U/DU + 数字）。错误文本可能携带（IBKR 异常/连接信息），外泄前必须脱敏。
_ACCOUNT_RE = re.compile(r"\b(D?U)\d{4,}\b")


def _redact(text: str) -> str:
    """错误文本脱敏：账户号绝不进前端/日志（红线 #5）。"""
    return _ACCOUNT_RE.sub(r"\1***", text)


def _finite(value: Any) -> float | None:
    """转 float；None / NaN / IBKR 未就绪哨兵(极大值) → None。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or abs(f) > 1e15:
        return None
    return f


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
    def is_live(self) -> bool: ...
    async def watch(self, symbol: str) -> None: ...
    def unwatch(self, symbol: str) -> None: ...
    async def set_market_data_type(self, mdt: int) -> int: ...
    async def trades_today(self, limit: int = 100) -> list[dict[str, Any]]: ...
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
        outside_rth: bool = False,
        stop_limit_offset_pct: Decimal | None = None,
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


class BrokerManager:
    """真实 IBKR 连接管理。"""

    def __init__(
        self, settings: IBKRSettings, panel: PanelConfig, *, env_warning: str | None = None
    ) -> None:
        self._settings = settings
        self._env_warning = env_warning  # live 互锁回退等提示，前端醒目展示
        self._ib: IB | None = None
        self._adapter: IbBrokerAdapter | None = None
        self._account = ""
        # 行情独立模块（webwatch 自有，不依赖 scalper 行情代码）。
        self._quotes = QuoteService(panel.market_data_type)
        self._last_error: str | None = None
        # 串行化下单/平仓操作：单个 IB 连接被多个 await 处理器共享，
        # 防止并发下单交叉（OCA 组错乱、撤单撤到刚挂的保护单等）。
        self._order_lock = asyncio.Lock()
        # 成交等待 / 保护单确认超时（秒）。压短：下单持锁期间撤单/全平等"救命按钮"会排队，
        # 最坏持锁 = fill + protect。IOC 入场通常亚秒级，超时只是 gateway 挂死的兜底上限。
        self._fill_timeout_s = 2.0
        self._protect_timeout_s = 2.0

    # ---- 连接生命周期 ---------------------------------------------------

    def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    def is_live(self) -> bool:
        return self._settings.environment is Environment.LIVE

    async def connect(self) -> None:
        """连接 Gateway（按环境选 paper/live），并重订阅 watchlist。

        重连前**先断开已有连接**，否则新连接复用同一 clientId 会与旧连接冲突、连接超时
        （旧连接还占着 clientId）。

        进 ``_order_lock``：下单持锁期间绝不能换掉 ``self._ib``，否则进行中的撤单/
        紧急平仓会打到错误连接（裸仓风险）。
        """
        s = self._settings
        async with self._order_lock:
            # 先清旧连接，释放 clientId，避免重连时自我冲突。
            if self._ib is not None and self._ib.isConnected():
                with contextlib.suppress(Exception):
                    self._ib.disconnect()
            self._quotes.detach()
            self._ib = None
            self._adapter = None
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
                self._ib = ib
                # 未填真实账户时自动用 Gateway 探测到的账户做 scoping。
                account = resolve_account(s.ibkr_account, list(ib.managedAccounts()))
                self._account = account
                self._adapter = IbBrokerAdapter(ib, paper_account=account)
                self._last_error = None
                # 订阅账户盈亏（当日 / 已实现 / 浮动）；流式更新，snapshot 里读。
                with contextlib.suppress(Exception):
                    ib.reqPnL(account)
                # 行情：绑定连接 + 重订 watchlist
                self._quotes.attach(ib)
                await self._quotes.resubscribe_all()
                log.info("connected to %s gateway", s.environment.value)
            except Exception as exc:  # noqa: BLE001 — 连接失败不崩面板，错误进 last_error 展示
                self._last_error = _redact(f"{type(exc).__name__}: {exc}")
                log.warning("connect failed: %s", self._last_error)
                if ib.isConnected():
                    ib.disconnect()
                self._ib = None
                self._adapter = None

    async def disconnect(self) -> None:
        async with self._order_lock:
            if self._ib is not None and self._ib.isConnected():
                self._ib.disconnect()
            self._quotes.detach()
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
        self._last_error = _redact(f"IB {error_code}:{sym} {error_string}")
        log.warning("IB error %s: %s", error_code, _redact(error_string))

    # ---- 报价订阅（委托给独立 QuoteService）-----------------------------

    async def watch(self, symbol: str) -> None:
        await self._quotes.subscribe(symbol)

    def unwatch(self, symbol: str) -> None:
        self._quotes.unsubscribe(symbol)

    async def set_market_data_type(self, mdt: int) -> int:
        """运行时切换行情类型（实时/冻结/延迟/延迟冻结）。"""
        return await self._quotes.set_market_data_type(mdt)

    # ---- 状态快照 -------------------------------------------------------

    def _safe(self, fn: Any) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            self._last_error = _redact(f"{type(exc).__name__}: {exc}")
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
            "is_live": self.is_live(),
            "env_warning": self._env_warning,
            "session": session_label(),
            "is_rth": is_rth(),
            "market_data_type": self._quotes.market_data_type,
            "error": self._last_error,
            "account": account,
            "positions": positions,
            "orders": orders,
            "quotes": self._quotes.quotes(),
            "watchlist": self._quotes.watchlist(),
            "pnl": self._pnl() if connected else None,
        }

    def _pnl(self) -> dict[str, float | None] | None:
        """账户盈亏（当日/已实现/浮动）。读 ib.pnl() 流式订阅；未就绪/哨兵值返回 None。"""
        if self._ib is None:
            return None
        lst = self._safe(self._ib.pnl) or []
        for p in lst:
            return {
                "daily": _finite(getattr(p, "dailyPnL", None)),
                "realized": _finite(getattr(p, "realizedPnL", None)),
                "unrealized": _finite(getattr(p, "unrealizedPnL", None)),
            }
        return None

    async def trades_today(self, limit: int = 100) -> list[dict[str, Any]]:
        """今日订单/成交记录（最新在前）。

        先 ``reqCompletedOrdersAsync`` 向 IBKR 拉当日**已完成**订单（含其他客户端/重启前下的），
        再序列化 ``ib.trades()``（已完成 + 本 session 开放单）。on-demand 调用，不进 300ms 快照循环。
        """
        if not self.is_connected() or self._ib is None:
            return []
        with contextlib.suppress(Exception):
            await self._ib.reqCompletedOrdersAsync(apiOnly=False)
        with contextlib.suppress(Exception):
            await self._ib.reqAllOpenOrdersAsync()
        return self._serialize_trades(limit)

    def _serialize_trades(self, limit: int = 100) -> list[dict[str, Any]]:
        if self._ib is None:
            return []
        trades = self._safe(self._ib.trades) or []
        out: list[dict[str, Any]] = []
        for t in reversed(list(trades)):
            try:
                o, s = t.order, t.orderStatus
                # 已完成订单经 reqCompletedOrders 取回时，成交量/价常不在 orderStatus，
                # 而在 fills（成交回报）里。优先用 fills 算真实成交量 + 加权均价。
                fills = getattr(t, "fills", None) or []
                fill_qty = 0.0
                notional = 0.0
                fill_time = None
                for f in fills:
                    ex = getattr(f, "execution", None)
                    if ex is None:
                        continue
                    sh = float(getattr(ex, "shares", 0) or 0)
                    px = float(getattr(ex, "price", 0) or 0)
                    fill_qty += sh
                    notional += sh * px
                    fill_time = getattr(ex, "time", None)
                avg = (notional / fill_qty) if fill_qty else (
                    float(s.avgFillPrice) if s.avgFillPrice else None
                )
                filled = fill_qty or float(s.filled or 0)
                qty = float(o.totalQuantity or 0) or filled
                log = getattr(t, "log", None) or []
                t_obj = fill_time or (log[-1].time if log else None)
                ts = t_obj.isoformat() if t_obj is not None else None
                out.append(
                    {
                        "time": ts,
                        "symbol": t.contract.symbol,
                        "side": o.action,  # BUY / SELL
                        "order_type": o.orderType,
                        "quantity": qty,
                        "filled": filled,
                        "avg_fill_price": avg,
                        "status": s.status,
                    }
                )
            except Exception:  # noqa: BLE001 — 单条解析失败跳过，不拖垮整表
                continue
            if len(out) >= limit:
                break
        return out

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
        outside_rth: bool = False,
        stop_limit_offset_pct: Decimal | None = None,
    ) -> dict[str, Any]:
        """模式B：用 ib_async 原生 bracket 一次性发出 母单(限价) + 止盈(限价) + 止损。

        三条单经 ib.bracketOrder 自带 parentId + transmit 链，保护单零空窗。

        ``outside_rth=True``（盘前/盘后/夜盘）：三条单全部 `outsideRth=True`，且止损从普通 STP
        **转成 STP-LMT**（时段外普通 stop 不触发）。止损限价用 ``stop_limit_offset_pct`` 在触发价
        更深一侧留缓冲。
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
            stop_limit: Decimal | None = None
            if outside_rth:
                offset = stop_limit_offset_pct if stop_limit_offset_pct is not None else Decimal("0.005")
                stop_limit = stop_limit_price(stop_loss, offset, side=side)
                for order in bracket:
                    order.outsideRth = True
                # 普通 STP → STP-LMT（时段外可用）。auxPrice(触发价) 不变，补限价。
                bracket.stopLoss.orderType = "STP LMT"
                bracket.stopLoss.lmtPrice = float(stop_limit)
            # 保护腿一律 GTC（候选-3 推广到 RTH）：DAY 子单在收盘/session 边界过期 →
            # 母单成交后隔夜裸仓。母单保持 DAY（入场限价不该 resting 过夜）。
            bracket.takeProfit.tif = "GTC"
            bracket.stopLoss.tif = "GTC"
            trades = [ib.placeOrder(contract, order) for order in bracket]
            try:
                await self._verify_bracket_accepted(trades, timeout=self._protect_timeout_s)
            except Exception as exc:  # noqa: BLE001 — 任何未确认都 fail-closed
                # transmit 链只保证"一起提交"，不保证"全被接受"。子单被拒而母单 working
                # = 成交后缺腿保护（红线 #3）。撤掉全部三腿。
                self._cancel_trades(ib, trades)
                # 撤单后再读母单成交量：撤前/撤中母单可能已成交（对已成交单撤单是 no-op，
                # 但两条保护腿已被撤）→ 必须紧急平仓；未成交则安全拒单。
                parent_filled = int(trades[0].orderStatus.filled or 0)
                if parent_filled > 0:
                    pavg = float(trades[0].orderStatus.avgFillPrice or 0.0)
                    px = Decimal(str(pavg)) if math.isfinite(pavg) and pavg > 0 else entry_limit
                    await self._flatten_or_alert(
                        ib, contract, side, parent_filled, px, f"bracket 保护腿异常: {exc}"
                    )
                raise RuntimeError(f"bracket 未被 IB 接受，已撤全部三腿: {exc}") from exc
            return {
                "symbol": contract.symbol,
                "side": side.value,
                "quantity": quantity,
                "entry_limit": str(entry_limit),
                "take_profit": str(take_profit),
                "stop_loss": str(stop_loss),
                "stop_limit": str(stop_limit) if stop_limit is not None else None,
                "outside_rth": outside_rth,
                "parent_id": bracket.parent.orderId,
                "take_profit_id": bracket.takeProfit.orderId,
                "stop_loss_id": bracket.stopLoss.orderId,
            }

    async def _verify_bracket_accepted(
        self, trades: list[Any], timeout: float = 2.0, interval: float = 0.05
    ) -> None:
        """确认模式B三腿都被 IB 接受。

        母单是 resting 限价单：子单在母单成交前被 IB 以 PreSubmitted 持有，属正常。
        任一腿进入终态异常（被拒/被撤）或超时未确认 → 抛错（fail-closed，调用方撤腿）。
        """
        for _ in range(max(1, int(timeout / interval))):
            statuses = [t.orderStatus.status for t in trades]
            if any(s in _PROTECTION_BAD_STATES for s in statuses):
                raise RuntimeError(f"bracket 腿进入异常状态: {statuses}")
            if all(s in _PROTECTION_OK_STATES for s in statuses):
                return
            await asyncio.sleep(interval)
        statuses = [t.orderStatus.status for t in trades]
        if any(s in _PROTECTION_BAD_STATES for s in statuses) or not all(
            s in _PROTECTION_OK_STATES for s in statuses
        ):
            raise RuntimeError(f"bracket 未确认被接受（超时）: {statuses}")

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
                # best-effort 撤掉状态未知的入场单（IOC 理论不 resting，这是 gateway
                # 挂死的兜底）：消掉"稍后才成交→裸仓"的尾部风险，再大声抛错。
                with contextlib.suppress(Exception):
                    ib.cancelOrder(entry)
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
                await self._flatten_or_alert(
                    ib, contract, side, filled, Decimal("0"), f"成交价无效（{avg}），无法计算保护单"
                )

            fill_price = Decimal(str(avg))
            try:
                bracket = compute_bracket(fill_price, filled, take_profit, stop_loss, side=side)
            except ValueError as exc:
                # 【P0·审计 2026-06-10】入场已成交后保护价算不出（如 PROFIT_USD 在 IOC
                # 部分成交下每股距离 = value/filled 被放大到超过股价）→ 必须立即平仓。
                # 绝不能让异常裸冒泡变成"已拒单"响应而实际持仓裸奔（红线 #3）。
                await self._flatten_or_alert(
                    ib, contract, side, filled, fill_price, f"保护价计算失败: {exc}"
                )

            prot_trades: list[Any] = []
            try:
                group, tp_order, sl_order, prot_trades = self._place_oca_protection(
                    contract, side, filled, bracket, entry.orderId
                )
                await self._verify_protection_live(prot_trades, timeout=self._protect_timeout_s)
            except Exception as exc:  # noqa: BLE001 — 任何保护单失败都必须平仓
                # 先撤掉任何已挂出的保护单（避免遗留单腿在平仓后反向开新裸仓），再平仓。
                self._cancel_trades(ib, prot_trades)
                await self._flatten_or_alert(ib, contract, side, filled, fill_price, str(exc))

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

    def _cancel_trades(self, ib: Any, trades: list[Any]) -> None:
        """尽力撤掉给定 trade 的订单（用于保护单失败时清理遗留单腿）。

        用**下单时捕获的局部 ib 引用**而非 ``self._ib``：即使连接管理出现意外置换，
        撤单也必须打到挂单的那条连接上。
        """
        for t in trades:
            with contextlib.suppress(Exception):
                ib.cancelOrder(t.order)

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
        """市价全平：先撤所有挂单（含保护单，避免平仓后保护单又开新仓），再市价平掉每个持仓。

        每个平仓单等待终态并回报实际成交量（IOC 可能被整单撤掉）——绝不默认"已平"。
        """
        if not self.is_connected() or self._ib is None or self._adapter is None:
            raise RuntimeError("未连接 Gateway")
        ib = self._ib
        async with self._order_lock:
            self._adapter.cancel_all(None)
            await asyncio.sleep(0.3)
            # 对账：positions() 是 positionEvent 缓存，刚成交的仓位可能滞后。
            # 救命按钮必须先向 IB 强制拉一次最新持仓（best-effort，失败仍按缓存平）。
            with contextlib.suppress(Exception):
                await ib.reqPositionsAsync()
            closed: list[dict[str, Any]] = []
            for pos in self._adapter.list_positions():
                close_action = "SELL" if pos.side is Side.LONG else "BUY"
                contract = Stock(pos.symbol, "SMART", "USD")
                await ib.qualifyContractsAsync(contract)
                order = MarketOrder(close_action, pos.quantity)
                order.tif = "IOC"
                trade = ib.placeOrder(contract, order)
                await self._wait_done(trade, timeout=self._fill_timeout_s)
                flat_filled = int(trade.orderStatus.filled or 0)
                closed.append(
                    {
                        "symbol": pos.symbol,
                        "action": close_action,
                        "quantity": pos.quantity,
                        "filled": flat_filled,
                        "failed": flat_filled < pos.quantity,
                    }
                )
            log.warning("flatten_all: 平掉 %d 个持仓", len(closed))
            return {"flattened": closed}

    async def _flatten_or_alert(
        self,
        ib: Any,
        contract: Any,
        side: Side,
        filled: int,
        fill_price: Decimal,
        reason: str,
    ) -> NoReturn:
        """平仓并抛 ProtectionFailed。若**平仓下单本身失败**（断连等）= 裸仓未平，
        抛带 ``flatten.failed=True`` 的告警，让前端给出最强"立即手动平仓"提示。"""
        try:
            flatten = await self._emergency_flatten(ib, contract, side, filled)
        except Exception as flat_exc:  # noqa: BLE001 — 平仓失败本身要醒目上报
            log.critical("EMERGENCY FLATTEN FAILED → 可能裸仓: %s", flat_exc)
            raise ProtectionFailed(
                filled=filled,
                fill_price=fill_price,
                reason=f"{reason}；且紧急平仓下单失败：{flat_exc}",
                flatten={
                    "failed": True,
                    "quantity": filled,
                    "note": "紧急平仓下单失败，可能裸仓，请立即手动平仓！",
                },
            ) from flat_exc
        raise ProtectionFailed(
            filled=filled, fill_price=fill_price, reason=reason, flatten=flatten
        )

    async def _emergency_flatten(
        self, ib: Any, contract: Any, side: Side, quantity: int
    ) -> dict[str, Any]:
        """红线 #3：市价平掉刚成交的仓位，不留裸仓。直接按已确认成交量 ``quantity`` 平。

        为什么直接平 ``quantity`` 而**不**读 ``get_position`` 对账：能走到这里，上游
        ``_verify_protection_live`` 的 Filled-first 已把"任一保护腿成交=出场成功"分流返回，
        故两条调用路径（成交价无效 / 保护单挂失败且无腿成交）下持仓**必为满额 quantity**。
        而 ``ib.positions()`` 缓存由 positionEvent 推送，与成交事件是两条独立流、刚成交那几百
        毫秒常滞后——此刻信任缓存裸 0 会**漏平真实持仓 = 裸仓**（比直接平更危险）。

        平仓单等待终态并校验成交量：IOC 在停牌/LULD/流动性枯竭时会被整单撤掉，
        谎报"已平仓"比平仓失败更危险 → 未全平时返回 ``failed=True`` 强告警。
        """
        close_action = "SELL" if side is Side.LONG else "BUY"
        flat = MarketOrder(close_action, quantity)
        flat.tif = "IOC"
        trade = ib.placeOrder(contract, flat)
        log.error(
            "PROTECTION FAILED → 紧急平仓 %s %s x%s", contract.symbol, close_action, quantity
        )
        await self._wait_done(trade, timeout=self._fill_timeout_s)
        flat_filled = int(trade.orderStatus.filled or 0)
        result: dict[str, Any] = {
            "flatten_order_id": flat.orderId,
            "action": close_action,
            "quantity": quantity,
            "filled": flat_filled,
        }
        if flat_filled < quantity:
            result["failed"] = True
            result["note"] = (
                f"紧急平仓单仅成交 {flat_filled}/{quantity}（IOC 可能被撤），"
                "剩余仓位未平，请立即手动平仓！"
            )
        return result


__all__ = [
    "BrokerLike",
    "BrokerManager",
    "ProtectionFailed",
    "EntryUncertain",
    "resolve_account",
]
