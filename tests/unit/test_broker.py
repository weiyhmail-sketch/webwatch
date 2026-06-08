"""broker 单测：纯函数 + place_limit_bracket（用 fake IB，无需 Gateway）。"""

from __future__ import annotations

from collections import namedtuple
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from scalper.execution.types import Position
from scalper.strategy.base import Side

from webwatch.broker import BrokerManager, EntryUncertain, ProtectionFailed, resolve_account
from webwatch.config import IBKRSettings, PanelConfig
from webwatch.pricing import Target


class TestResolveAccount:
    def test_configured_real_account_wins(self) -> None:
        assert resolve_account("DU1234567", ["DU9999999"]) == "DU1234567"

    def test_placeholder_falls_back_to_managed(self) -> None:
        assert resolve_account("DU0000000", ["DU7654321"]) == "DU7654321"

    def test_empty_falls_back_to_managed(self) -> None:
        assert resolve_account("", ["DU7654321"]) == "DU7654321"

    def test_no_managed_returns_configured(self) -> None:
        assert resolve_account("", []) == ""


# --- fake ib_async.IB（只实现 place_limit_bracket 用到的方法）---------------

_BO = namedtuple("_BO", ["parent", "takeProfit", "stopLoss"])


class _FakeOrder:
    def __init__(self, oid: int) -> None:
        self.orderId = oid


class _FakeTrade:
    def __init__(self, order: _FakeOrder) -> None:
        self.order = order


class _FakeContract:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol


class FakeIB:
    def __init__(self) -> None:
        self.placed: list[tuple[Any, int]] = []
        self.last_args: tuple[Any, ...] | None = None

    def isConnected(self) -> bool:  # noqa: N802 — 匹配 ib_async API
        return True

    async def qualifyContractsAsync(self, contract: Any) -> list[Any]:  # noqa: N802
        return [contract]

    def bracketOrder(  # noqa: N802
        self, action: str, qty: float, lp: float, tp: float, sl: float
    ) -> Any:
        self.last_args = (action, qty, lp, tp, sl)
        return _BO(_FakeOrder(101), _FakeOrder(102), _FakeOrder(103))

    def placeOrder(self, contract: Any, order: Any) -> Any:  # noqa: N802
        self.placed.append((contract.symbol, order.orderId))
        return _FakeTrade(order)


class TestPlaceLimitBracket:
    async def test_places_three_orders_with_native_bracket(self) -> None:
        bm = BrokerManager(IBKRSettings(), PanelConfig({}))
        fake = FakeIB()
        bm._ib = fake  # type: ignore[assignment]

        res = await bm.place_limit_bracket(
            symbol="aapl",
            side=Side.LONG,
            quantity=100,
            entry_limit=Decimal("100"),
            take_profit=Decimal("100.20"),
            stop_loss=Decimal("99.60"),
        )

        assert len(fake.placed) == 3  # 母单 + 止盈 + 止损 三单同发
        assert res["parent_id"] == 101
        assert res["take_profit_id"] == 102
        assert res["stop_loss_id"] == 103
        # IB 边界转 float，BUY 方向
        assert fake.last_args == ("BUY", 100, 100.0, 100.2, 99.6)

    async def test_raises_when_disconnected(self) -> None:
        bm = BrokerManager(IBKRSettings(), PanelConfig({}))
        try:
            await bm.place_limit_bracket(
                symbol="AAPL",
                side=Side.LONG,
                quantity=10,
                entry_limit=Decimal("100"),
                take_profit=Decimal("100.20"),
                stop_loss=Decimal("99.60"),
            )
        except RuntimeError as exc:
            assert "未连接" in str(exc)
        else:
            raise AssertionError("应在未连接时抛 RuntimeError")


# --- fake IB for 市价 + 保护单 -----------------------------------------------

_DONE = {"Filled", "Cancelled", "Inactive", "ApiCancelled"}


class _Status:
    def __init__(self, status: str, filled: int = 0, avg: float = 0.0) -> None:
        self.status = status
        self.filled = filled
        self.avgFillPrice = avg


class _Trade2:
    def __init__(self, order: Any, status: _Status) -> None:
        self.order = order
        self.orderStatus = status

    def isDone(self) -> bool:  # noqa: N802
        return self.orderStatus.status in _DONE


class MarketFakeIB:
    """可配置成交/保护单结果的 fake IB。tp_status/sl_status 可分别设，模拟 OCA 非对称终态。"""

    def __init__(
        self,
        *,
        fill_price: float = 200.0,
        entry_filled: bool = True,
        entry_done: bool = True,
        tp_status: str = "Submitted",
        sl_status: str = "Submitted",
        protection_raises: bool = False,
        sl_raises: bool = False,
    ) -> None:
        self.fill_price = fill_price
        self.entry_filled = entry_filled
        self.entry_done = entry_done
        self.tp_status = tp_status
        self.sl_status = sl_status
        self.protection_raises = protection_raises
        self.sl_raises = sl_raises
        self.placed: list[Any] = []
        self.cancelled: list[int] = []
        self._id = 0
        self._entry_done = False

    def isConnected(self) -> bool:  # noqa: N802
        return True

    async def qualifyContractsAsync(self, contract: Any) -> list[Any]:  # noqa: N802
        return [contract]

    def cancelOrder(self, order: Any) -> None:  # noqa: N802
        self.cancelled.append(order.orderId)

    def placeOrder(self, contract: Any, order: Any) -> Any:  # noqa: N802
        self._id += 1
        order.orderId = self._id
        self.placed.append(order)
        if order.orderType == "MKT" and not self._entry_done:
            self._entry_done = True
            if not self.entry_done:  # 入场单卡在非终态 → 模拟超时
                return _Trade2(order, _Status("PreSubmitted", 0, 0.0))
            if self.entry_filled:
                return _Trade2(order, _Status("Filled", order.totalQuantity, self.fill_price))
            return _Trade2(order, _Status("Cancelled", 0, 0.0))
        if order.orderType == "MKT":  # 后续 MKT = 紧急平仓
            return _Trade2(order, _Status("Filled", order.totalQuantity, self.fill_price))
        if self.protection_raises:
            raise RuntimeError("broker rejected protection")
        if order.orderType == "STP":  # 止损腿
            if self.sl_raises:
                raise RuntimeError("broker rejected stop leg")
            return _Trade2(order, _Status(self.sl_status))
        return _Trade2(order, _Status(self.tp_status))  # LMT = 止盈腿


class MarketFakeAdapter:
    """fake 适配器：_emergency_flatten 用 get_position 对账真实持仓。"""

    def __init__(self, position_qty: int = 10, position_side: Side = Side.LONG) -> None:
        self.position_qty = position_qty
        self.position_side = position_side

    def get_position(self, symbol: str) -> Position | None:
        if self.position_qty <= 0:
            return None
        return _pos(symbol, self.position_side, self.position_qty)


def _bm_with(
    fake: MarketFakeIB, *, position_qty: int = 10, position_side: Side = Side.LONG
) -> BrokerManager:
    bm = BrokerManager(IBKRSettings(), PanelConfig({}))
    bm._ib = fake  # type: ignore[assignment]
    bm._adapter = MarketFakeAdapter(position_qty, position_side)  # type: ignore[assignment]
    return bm


class TestMarketWithProtection:
    async def test_fill_then_oca_protection(self) -> None:
        fake = MarketFakeIB(fill_price=200.0)
        bm = _bm_with(fake)
        res = await bm.place_market_with_protection(
            symbol="aapl",
            side=Side.LONG,
            quantity=10,
            take_profit=Target.pct(Decimal("0.002")),
            stop_loss=Target.pct(Decimal("0.004")),
        )
        assert res["filled"] == 10
        assert Decimal(res["fill_price"]) == Decimal("200")
        assert res["take_profit"] == "200.40"  # 200 * 1.002
        assert res["stop_loss"] == "199.20"  # 200 * 0.996
        assert res["oca_group"].startswith("scalper-oca-ww-")
        assert len(fake.placed) == 3  # 市价入场 + TP + SL

    async def test_not_filled_returns_without_protection(self) -> None:
        fake = MarketFakeIB(entry_filled=False)
        bm = _bm_with(fake)
        res = await bm.place_market_with_protection(
            symbol="aapl",
            side=Side.LONG,
            quantity=10,
            take_profit=Target.pct(Decimal("0.002")),
            stop_loss=Target.pct(Decimal("0.004")),
        )
        assert res["filled"] == 0
        assert len(fake.placed) == 1  # 只有未成交的市价单，无保护单

    async def test_oca_sibling_fill_is_success_not_flatten(self) -> None:
        # 【P0 回归】止盈成交 → 止损被 OCA 撤(Cancelled)，是成功路径，
        # 绝不能误判失败去平仓（否则对已平仓位反向开裸仓）。
        fake = MarketFakeIB(tp_status="Filled", sl_status="Cancelled")
        bm = _bm_with(fake)
        res = await bm.place_market_with_protection(
            symbol="aapl", side=Side.LONG, quantity=10,
            take_profit=Target.pct(Decimal("0.002")), stop_loss=Target.pct(Decimal("0.004")),
        )
        assert res["filled"] == 10
        # 仅入场一张 MKT，绝无紧急平仓 MKT（否则就是反向裸仓）
        assert sum(1 for o in fake.placed if o.orderType == "MKT") == 1

    async def test_emergency_flatten_uses_filled_not_position_cache(self) -> None:
        # 【round-2 P0】紧急平仓必须按已确认成交量平，绝不能因 positions() 缓存滞后(裸 0)
        # 而跳过平仓 → 漏平裸仓。position_qty=0 模拟"刚成交、持仓缓存尚未刷新"。
        fake = MarketFakeIB(tp_status="Cancelled", sl_status="Cancelled")
        bm = _bm_with(fake, position_qty=0)  # 缓存读到 0（滞后），但实际刚成交 10 股
        with pytest.raises(ProtectionFailed) as ei:
            await bm.place_market_with_protection(
                symbol="aapl", side=Side.LONG, quantity=10,
                take_profit=Target.pct(Decimal("0.002")), stop_loss=Target.pct(Decimal("0.004")),
            )
        # 必须按成交量 10 平，而非信任缓存裸 0 跳过
        assert ei.value.flatten["quantity"] == 10
        assert ei.value.flatten["action"] == "SELL"
        flatten_mkts = [o for o in fake.placed if o.orderType == "MKT"][1:]  # 排除入场
        assert flatten_mkts and flatten_mkts[0].totalQuantity == 10

    async def test_protection_cancelled_triggers_flatten(self) -> None:
        # 两腿都异常终态（非 OCA 正常）+ 仍有真实持仓 → 立即平仓 + ProtectionFailed
        fake = MarketFakeIB(tp_status="Cancelled", sl_status="Cancelled")
        bm = _bm_with(fake, position_qty=10)
        with pytest.raises(ProtectionFailed) as ei:
            await bm.place_market_with_protection(
                symbol="aapl",
                side=Side.LONG,
                quantity=10,
                take_profit=Target.pct(Decimal("0.002")),
                stop_loss=Target.pct(Decimal("0.004")),
            )
        assert ei.value.filled == 10
        assert ei.value.flatten["action"] == "SELL"  # 多头平仓 = 卖出
        assert sum(1 for o in fake.placed if o.orderType == "MKT") == 2  # 入场 + 平仓

    async def test_protection_place_raises_triggers_flatten(self) -> None:
        fake = MarketFakeIB(protection_raises=True)
        bm = _bm_with(fake, position_qty=10)
        with pytest.raises(ProtectionFailed):
            await bm.place_market_with_protection(
                symbol="aapl",
                side=Side.LONG,
                quantity=10,
                take_profit=Target.pct(Decimal("0.002")),
                stop_loss=Target.pct(Decimal("0.004")),
            )
        assert sum(1 for o in fake.placed if o.orderType == "MKT") == 2

    async def test_sl_leg_raise_cancels_tp_leg_then_flatten(self) -> None:
        # 止盈挂出后止损挂单抛错 → 必须撤掉止盈遗留腿 + 平仓，否则裸仓/反向开仓
        fake = MarketFakeIB(sl_raises=True)
        bm = _bm_with(fake, position_qty=10)
        with pytest.raises(ProtectionFailed):
            await bm.place_market_with_protection(
                symbol="aapl", side=Side.LONG, quantity=10,
                take_profit=Target.pct(Decimal("0.002")), stop_loss=Target.pct(Decimal("0.004")),
            )
        lmt_ids = [o.orderId for o in fake.placed if o.orderType == "LMT"]
        assert lmt_ids and lmt_ids[0] in fake.cancelled  # 止盈腿被撤
        assert sum(1 for o in fake.placed if o.orderType == "MKT") == 2  # 入场 + 平仓

    async def test_invalid_fill_price_triggers_flatten(self) -> None:
        # 有成交但成交价 NaN → 无法算保护单 → 立即平仓 + ProtectionFailed
        fake = MarketFakeIB(fill_price=float("nan"))
        bm = _bm_with(fake, position_qty=10)
        with pytest.raises(ProtectionFailed) as ei:
            await bm.place_market_with_protection(
                symbol="aapl", side=Side.LONG, quantity=10,
                take_profit=Target.pct(Decimal("0.002")), stop_loss=Target.pct(Decimal("0.004")),
            )
        assert "成交价无效" in ei.value.reason
        assert sum(1 for o in fake.placed if o.orderType == "MKT") == 2  # 入场 + 平仓

    async def test_entry_timeout_raises_entry_uncertain(self) -> None:
        # 入场单超时未达终态 → 抛 EntryUncertain（可能已成交=裸仓），不静默当未成交
        fake = MarketFakeIB(entry_done=False)
        bm = _bm_with(fake)
        bm._fill_timeout_s = 0.1  # type: ignore[attr-defined]
        with pytest.raises(EntryUncertain, match="超时"):
            await bm.place_market_with_protection(
                symbol="aapl", side=Side.LONG, quantity=10,
                take_profit=Target.pct(Decimal("0.002")), stop_loss=Target.pct(Decimal("0.004")),
            )


class FakeAdapter:
    def __init__(self, positions: list[Any] | None = None) -> None:
        self.positions = positions or []
        self.cancel_calls: list[Any] = []

    def cancel_all(self, symbol: Any = None) -> None:
        self.cancel_calls.append(symbol)

    def list_positions(self) -> list[Any]:
        return self.positions

    def account_summary(self) -> Any:
        return SimpleNamespace(
            net_liquidation=Decimal("100000"), buying_power=Decimal("400000")
        )


def _pos(symbol: str, side: Side, qty: int) -> Position:
    return Position(
        symbol=symbol,
        side=side,
        quantity=qty,
        avg_entry_price=100.0,
        open_ts=pd.Timestamp("2026-06-08T14:30:00Z"),
        entry_order_id="e1",  # type: ignore[arg-type]
    )


class TestRiskInputsCancelFlatten:
    def test_risk_inputs(self) -> None:
        bm = BrokerManager(IBKRSettings(), PanelConfig({}))
        bm._ib = MarketFakeIB()  # type: ignore[assignment]
        bm._adapter = FakeAdapter()  # type: ignore[assignment]
        nav, bp = bm.risk_inputs()
        assert nav == Decimal("100000")
        assert bp == Decimal("400000")

    def test_risk_inputs_disconnected(self) -> None:
        bm = BrokerManager(IBKRSettings(), PanelConfig({}))
        assert bm.risk_inputs() == (None, None)

    async def test_cancel_all_orders(self) -> None:
        bm = BrokerManager(IBKRSettings(), PanelConfig({}))
        adapter = FakeAdapter()
        bm._ib = MarketFakeIB()  # type: ignore[assignment]
        bm._adapter = adapter  # type: ignore[assignment]
        assert await bm.cancel_all_orders() == {"cancelled": True}
        assert adapter.cancel_calls == [None]

    async def test_flatten_all_cancels_then_closes(self) -> None:
        adapter = FakeAdapter(positions=[_pos("AAPL", Side.LONG, 10), _pos("TSLA", Side.SHORT, 5)])
        fake = MarketFakeIB()
        bm = BrokerManager(IBKRSettings(), PanelConfig({}))
        bm._ib = fake  # type: ignore[assignment]
        bm._adapter = adapter  # type: ignore[assignment]
        result = await bm.flatten_all()
        assert adapter.cancel_calls == [None]  # 先撤所有挂单
        assert len(result["flattened"]) == 2
        # 多头平仓 = SELL，空头平仓 = BUY
        actions = {r["symbol"]: r["action"] for r in result["flattened"]}
        assert actions == {"AAPL": "SELL", "TSLA": "BUY"}
        assert sum(1 for o in fake.placed if o.orderType == "MKT") == 2
