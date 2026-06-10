"""broker 单测：纯函数 + place_limit_bracket（用 fake IB，无需 Gateway）。"""

from __future__ import annotations

import asyncio
from collections import namedtuple
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from scalper.execution.types import Position
from scalper.strategy.base import Side

from webwatch.broker import BrokerManager, EntryUncertain, ProtectionFailed, resolve_account
from webwatch.config import Environment, IBKRSettings, PanelConfig
from webwatch.pricing import Target


class _DiscFakeIB:
    def __init__(self) -> None:
        self.disconnected = False

    def isConnected(self) -> bool:  # noqa: N802
        return True

    def disconnect(self) -> None:
        self.disconnected = True


class TestConnectDisconnectsFirst:
    async def test_reconnect_disconnects_existing_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 重连前必须先断开旧连接，否则同一 clientId 自我冲突。
        async def boom(*a: object, **k: object) -> None:
            raise RuntimeError("no gateway")

        monkeypatch.setattr("webwatch.broker.connect_paper_async", boom)
        bm = BrokerManager(IBKRSettings(), PanelConfig({}))
        old = _DiscFakeIB()
        bm._ib = old  # type: ignore[assignment]
        await bm.connect()  # 应先断 old，再尝试新连接（新连接因 boom 失败）
        assert old.disconnected is True
        assert bm.is_connected() is False


class _FakeEvent:
    def __iadd__(self, handler: Any) -> _FakeEvent:
        return self


class _ConnFakeIB:
    """connect() 成功路径所需的最小 IB 面：事件挂接/账户探测/PnL/行情类型。"""

    def __init__(self) -> None:
        self.errorEvent = _FakeEvent()

    def isConnected(self) -> bool:  # noqa: N802
        return True

    def managedAccounts(self) -> list[str]:  # noqa: N802
        return ["DU111"]

    def reqPnL(self, account: str) -> None:  # noqa: N802
        return None

    def reqMarketDataType(self, t: int) -> None:  # noqa: N802
        return None

    def disconnect(self) -> None:
        return None


class _RadarSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.claim = False

    def attach(self, ib: Any) -> None:
        self.calls.append("attach")

    def detach(self) -> None:
        self.calls.append("detach")

    def start(self) -> None:
        self.calls.append("start")

    async def stop(self) -> None:
        self.calls.append("stop")

    def handles_error(self, req_id: int, code: int, msg: str, contract: Any = None) -> bool:
        self.calls.append(f"err:{code}")
        return self.claim

    async def on_market_data_type_changed(self) -> None:
        self.calls.append("mdt")

    def snapshot_dict(self) -> dict[str, Any]:
        return {"enabled": True, "spy": True}


class TestRadarWiring:
    """雷达接线（纯加法）：生命周期 / 错误认领 / 行情类型联动 / 快照键。"""

    def _bm(self) -> tuple[BrokerManager, _RadarSpy]:
        bm = BrokerManager(IBKRSettings(), PanelConfig({}))
        spy = _RadarSpy()
        bm._radar = spy  # type: ignore[assignment]
        return bm, spy

    async def test_disconnect_stops_and_detaches_radar(self) -> None:
        bm, spy = self._bm()
        await bm.disconnect()
        assert "stop" in spy.calls and "detach" in spy.calls

    async def test_failed_connect_detaches_radar_never_starts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(*a: object, **k: object) -> None:
            raise RuntimeError("no gateway")

        monkeypatch.setattr("webwatch.broker.connect_paper_async", boom)
        bm, spy = self._bm()
        await bm.connect()
        assert "start" not in spy.calls
        assert "detach" in spy.calls

    async def test_successful_connect_attaches_then_starts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def ok(*a: object, **k: object) -> None:
            return None

        monkeypatch.setattr("webwatch.broker.connect_paper_async", ok)
        monkeypatch.setattr("webwatch.broker.IB", _ConnFakeIB)
        monkeypatch.setattr(
            "webwatch.broker.IbBrokerAdapter",
            lambda ib, paper_account: SimpleNamespace(),
        )
        bm, spy = self._bm()
        await bm.connect()
        assert bm.is_connected() is True
        assert spy.calls[-2:] == ["attach", "start"]

    async def test_on_ib_error_defers_to_radar(self) -> None:
        bm, spy = self._bm()
        spy.claim = True
        bm._on_ib_error(7, 162, "scanner blocked", None)
        assert bm.snapshot()["error"] is None  # 雷达认领 → 不进全局错误横幅
        spy.claim = False
        bm._on_ib_error(8, 10197, "no market data", None)
        assert "10197" in (bm.snapshot()["error"] or "")

    async def test_set_market_data_type_notifies_radar(self) -> None:
        bm, spy = self._bm()
        await bm.set_market_data_type(3)
        assert "mdt" in spy.calls

    def test_snapshot_contains_radar_key(self) -> None:
        bm, _ = self._bm()
        assert bm.snapshot()["radar"] == {"enabled": True, "spy": True}


class TestIsLiveAndSnapshot:
    def test_is_live_reflects_environment(self) -> None:
        paper = BrokerManager(IBKRSettings(environment=Environment.PAPER), PanelConfig({}))
        live = BrokerManager(IBKRSettings(environment=Environment.LIVE), PanelConfig({}))
        assert paper.is_live() is False
        assert live.is_live() is True

    def test_snapshot_exposes_env_warning_and_is_live(self) -> None:
        bm = BrokerManager(IBKRSettings(), PanelConfig({}), env_warning="已回退 paper")
        snap = bm.snapshot()
        assert snap["env_warning"] == "已回退 paper"
        assert snap["is_live"] is False
        assert snap["environment"] == "paper"


def _fill(shares: float, price: float, t: Any) -> Any:
    return SimpleNamespace(execution=SimpleNamespace(shares=shares, price=price, time=t))


class _TradeRec:
    def __init__(self, sym: str, action: str, otype: str, qty: float,
                 status: str, filled: float, avg: float, t: Any,
                 fills: list[Any] | None = None) -> None:
        self.contract = SimpleNamespace(symbol=sym)
        self.order = SimpleNamespace(action=action, orderType=otype, totalQuantity=qty)
        self.orderStatus = SimpleNamespace(status=status, filled=filled, avgFillPrice=avg)
        self.log = [SimpleNamespace(time=t)]
        self.fills = fills or []


class _TradesFakeIB:
    def __init__(self, trades: list[Any]) -> None:
        self._trades = trades

    def isConnected(self) -> bool:  # noqa: N802
        return True

    async def reqCompletedOrdersAsync(self, apiOnly: bool = False) -> list[Any]:  # noqa: N802
        return []

    async def reqAllOpenOrdersAsync(self) -> list[Any]:  # noqa: N802
        return []

    def trades(self) -> list[Any]:
        return self._trades


class TestTradesToday:
    async def test_serializes_newest_first(self) -> None:
        t1 = _TradeRec("AAPL", "BUY", "MKT", 10, "Filled", 10, 200.0,
                       pd.Timestamp("2026-06-09T10:00:00Z"))
        t2 = _TradeRec("AAPL", "SELL", "LMT", 10, "Cancelled", 0, 0.0,
                       pd.Timestamp("2026-06-09T10:01:00Z"))
        bm = BrokerManager(IBKRSettings(), PanelConfig({}))
        bm._ib = _TradesFakeIB([t1, t2])  # type: ignore[assignment]
        rows = await bm.trades_today()
        assert len(rows) == 2
        assert rows[0]["status"] == "Cancelled"  # 最新在前(t2)
        assert rows[1]["status"] == "Filled" and rows[1]["avg_fill_price"] == 200.0
        assert rows[0]["avg_fill_price"] is None

    async def test_filled_uses_fills_when_orderstatus_empty(self) -> None:
        # reqCompletedOrders 取回的已成交单：totalQuantity/filled/avg 为 0，真实成交在 fills 里
        ts = pd.Timestamp("2026-06-09T10:00:00Z")
        rec = _TradeRec("AAOI", "BUY", "MKT", 0, "Filled", 0, 0.0, ts,
                        fills=[_fill(60, 10.0, ts), _fill(40, 10.5, ts)])
        bm = BrokerManager(IBKRSettings(), PanelConfig({}))
        bm._ib = _TradesFakeIB([rec])  # type: ignore[assignment]
        rows = await bm.trades_today()
        assert rows[0]["filled"] == 100.0  # 60+40 来自 fills
        assert rows[0]["quantity"] == 100.0
        assert abs(rows[0]["avg_fill_price"] - 10.2) < 1e-9  # (60*10+40*10.5)/100


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
    """模式B fake：三腿状态可配置（验证 bracket 被 IB 接受 / 拒腿 / 母单先成交）。"""

    def __init__(
        self,
        *,
        parent_status: str = "Submitted",
        tp_status: str = "Submitted",
        sl_status: str = "Submitted",
        parent_filled: int = 0,
        parent_avg: float = 0.0,
    ) -> None:
        self.placed: list[tuple[Any, Any]] = []
        self.cancelled: list[int] = []
        self.last_args: tuple[Any, ...] | None = None
        self.last_bracket: Any = None
        self.parent_status = parent_status
        self.tp_status = tp_status
        self.sl_status = sl_status
        self.parent_filled = parent_filled
        self.parent_avg = parent_avg

    def isConnected(self) -> bool:  # noqa: N802 — 匹配 ib_async API
        return True

    async def qualifyContractsAsync(self, contract: Any) -> list[Any]:  # noqa: N802
        return [contract]

    def cancelOrder(self, order: Any) -> None:  # noqa: N802
        self.cancelled.append(order.orderId)

    def bracketOrder(  # noqa: N802
        self, action: str, qty: float, lp: float, tp: float, sl: float
    ) -> Any:
        self.last_args = (action, qty, lp, tp, sl)
        self.last_bracket = _BO(_FakeOrder(101), _FakeOrder(102), _FakeOrder(103))
        return self.last_bracket

    def placeOrder(self, contract: Any, order: Any) -> Any:  # noqa: N802
        self.placed.append((contract.symbol, order))
        oid = getattr(order, "orderId", 0)
        if oid == 101:
            return _Trade2(order, _Status(self.parent_status, self.parent_filled, self.parent_avg))
        if oid == 102:
            return _Trade2(order, _Status(self.tp_status))
        if oid == 103:
            return _Trade2(order, _Status(self.sl_status))
        # 其余（紧急平仓 MKT）：立即全量成交
        return _Trade2(order, _Status("Filled", getattr(order, "totalQuantity", 0), 100.0))


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
        assert res["outside_rth"] is False
        assert res["stop_limit"] is None

    async def test_outside_rth_uses_stop_limit_and_outside_flag(self) -> None:
        # 时段外：止损转 STP-LMT + 三单全部 outsideRth=True
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
            outside_rth=True,
            stop_limit_offset_pct=Decimal("0.005"),
        )
        assert res["outside_rth"] is True
        # 99.60 * 0.995 = 99.102 → 向下取整到分位 = 99.10
        assert res["stop_limit"] == "99.10"
        b = fake.last_bracket
        assert all(o.outsideRth for o in b)  # 三条腿都 outsideRth
        assert b.stopLoss.orderType == "STP LMT"
        assert b.stopLoss.lmtPrice == 99.10
        # 候选-3：保护腿用 GTC，避免 session 边界过期裸仓
        assert b.takeProfit.tif == "GTC"
        assert b.stopLoss.tif == "GTC"

    async def test_rth_bracket_children_use_gtc(self) -> None:
        # RTH 分支保护腿也必须 GTC：临近收盘成交后 DAY 子单收盘过期 → 隔夜裸仓。
        bm = BrokerManager(IBKRSettings(), PanelConfig({}))
        fake = FakeIB()
        bm._ib = fake  # type: ignore[assignment]
        await bm.place_limit_bracket(
            symbol="aapl", side=Side.LONG, quantity=100,
            entry_limit=Decimal("100"), take_profit=Decimal("100.20"), stop_loss=Decimal("99.60"),
        )
        b = fake.last_bracket
        assert b.takeProfit.tif == "GTC"
        assert b.stopLoss.tif == "GTC"

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


class TestLimitBracketVerification:
    """模式B 挂出后验证：transmit 链只保证一起提交，不保证全被接受（P1 修复回归）。"""

    def _bm(self, fake: FakeIB) -> BrokerManager:
        bm = BrokerManager(IBKRSettings(), PanelConfig({}))
        bm._ib = fake  # type: ignore[assignment]
        bm._protect_timeout_s = 0.2  # type: ignore[attr-defined]
        return bm

    async def _place(self, bm: BrokerManager, quantity: int = 100) -> dict[str, Any]:
        return await bm.place_limit_bracket(
            symbol="aapl", side=Side.LONG, quantity=quantity,
            entry_limit=Decimal("100"), take_profit=Decimal("100.20"), stop_loss=Decimal("99.60"),
        )

    async def test_child_rejected_cancels_all_legs(self) -> None:
        # 子单（止损腿）被 IB 拒 → 撤掉全部三腿 + 抛错，绝不留"母单 working 无保护"。
        fake = FakeIB(sl_status="Cancelled")
        with pytest.raises(RuntimeError, match="bracket"):
            await self._place(self._bm(fake))
        assert {101, 102, 103} <= set(fake.cancelled)

    async def test_child_rejected_after_parent_fill_flattens(self) -> None:
        # 母单已成交但保护腿被拒 → 红线 #3：必须紧急平仓 + ProtectionFailed。
        fake = FakeIB(parent_status="Filled", parent_filled=100, parent_avg=100.0,
                      sl_status="Cancelled")
        with pytest.raises(ProtectionFailed) as ei:
            await self._place(self._bm(fake))
        flats = [o for _, o in fake.placed if getattr(o, "orderType", "") == "MKT"]
        assert flats and flats[0].totalQuantity == 100  # 按母单成交量平
        assert ei.value.flatten["action"] == "SELL"

    async def test_pending_timeout_cancels_all_legs(self) -> None:
        # 全部腿停在 PendingSubmit 超时 → fail-closed：撤三腿 + 抛错（母单未成交，无裸仓）。
        fake = FakeIB(parent_status="PendingSubmit", tp_status="PendingSubmit",
                      sl_status="PendingSubmit")
        with pytest.raises(RuntimeError, match="超时"):
            await self._place(self._bm(fake))
        assert {101, 102, 103} <= set(fake.cancelled)

    async def test_accepted_bracket_returns_normally(self) -> None:
        fake = FakeIB()  # 三腿都 Submitted
        res = await self._place(self._bm(fake))
        assert res["parent_id"] == 101
        assert fake.cancelled == []


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
        entry_fill_qty: int | None = None,
        tp_status: str = "Submitted",
        sl_status: str = "Submitted",
        protection_raises: bool = False,
        sl_raises: bool = False,
        flatten_raises: bool = False,
        flatten_ioc_cancels: bool = False,
    ) -> None:
        self.fill_price = fill_price
        self.entry_filled = entry_filled
        self.entry_done = entry_done
        self.entry_fill_qty = entry_fill_qty  # None=全量成交；设值=IOC 部分成交
        self.tp_status = tp_status
        self.sl_status = sl_status
        self.protection_raises = protection_raises
        self.sl_raises = sl_raises
        self.flatten_raises = flatten_raises
        self.flatten_ioc_cancels = flatten_ioc_cancels  # 紧急平仓 IOC 被整单撤掉（停牌/无流动性）
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
                qty = self.entry_fill_qty if self.entry_fill_qty is not None else order.totalQuantity
                return _Trade2(order, _Status("Filled", qty, self.fill_price))
            return _Trade2(order, _Status("Cancelled", 0, 0.0))
        if order.orderType == "MKT":  # 后续 MKT = 紧急平仓
            if self.flatten_raises:
                raise RuntimeError("broker rejected flatten")
            if self.flatten_ioc_cancels:
                return _Trade2(order, _Status("Cancelled", 0, 0.0))
            return _Trade2(order, _Status("Filled", order.totalQuantity, self.fill_price))
        if self.protection_raises:
            raise RuntimeError("broker rejected protection")
        if order.orderType == "STP":  # 止损腿
            if self.sl_raises:
                raise RuntimeError("broker rejected stop leg")
            return _Trade2(order, _Status(self.sl_status))
        return _Trade2(order, _Status(self.tp_status))  # LMT = 止盈腿


class MarketFakeAdapter:
    """fake 适配器（提供 get_position 供持仓读取；紧急平仓本身按 filled 平、不读它）。"""

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

    async def test_flatten_order_failure_raises_naked_alert(self) -> None:
        # 切 live 前硬化：紧急平仓下单本身失败 → flatten.failed 标记，前端给"裸仓立即手动平"强提示
        fake = MarketFakeIB(tp_status="Cancelled", sl_status="Cancelled", flatten_raises=True)
        bm = _bm_with(fake)
        with pytest.raises(ProtectionFailed) as ei:
            await bm.place_market_with_protection(
                symbol="aapl", side=Side.LONG, quantity=10,
                take_profit=Target.pct(Decimal("0.002")), stop_loss=Target.pct(Decimal("0.004")),
            )
        assert ei.value.flatten.get("failed") is True
        assert "手动平仓" in ei.value.flatten["note"]

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
        # best-effort：抛错前尝试撤掉状态未知的入场单，消掉"稍后才成交"的尾部风险
        assert fake.placed[0].orderId in fake.cancelled

    async def test_partial_fill_places_protection_for_filled_qty(self) -> None:
        # IOC 部分成交：保护单必须按实际成交量挂，不能按请求数量
        fake = MarketFakeIB(fill_price=200.0, entry_fill_qty=60)
        bm = _bm_with(fake)
        res = await bm.place_market_with_protection(
            symbol="aapl", side=Side.LONG, quantity=100,
            take_profit=Target.pct(Decimal("0.002")), stop_loss=Target.pct(Decimal("0.004")),
        )
        assert res["filled"] == 60
        legs = [o for o in fake.placed if o.orderType in ("LMT", "STP")]
        assert legs and all(o.totalQuantity == 60 for o in legs)

    async def test_partial_fill_bracket_error_flattens_not_leaks(self) -> None:
        # 【P0 回归·审计 2026-06-10】PROFIT_USD 止损 + IOC 部分成交：每股距离 = value/filled
        # 被放大 → compute_bracket 抛 ValueError。此刻入场已成交，必须紧急平仓 + ProtectionFailed，
        # 绝不能让异常裸冒泡变成"已拒单"（实际裸仓、无平仓无告警）。
        # 100 股 @$5 止损 $400：plan 时每股 $4 合法；只成交 10 股 → 每股 $40 → SL 负 → ValueError。
        fake = MarketFakeIB(fill_price=5.0, entry_fill_qty=10)
        bm = _bm_with(fake)
        with pytest.raises(ProtectionFailed) as ei:
            await bm.place_market_with_protection(
                symbol="pny", side=Side.LONG, quantity=100,
                take_profit=Target.pct(Decimal("0.002")),
                stop_loss=Target.profit_usd(Decimal("400")),
            )
        assert ei.value.filled == 10
        mkts = [o for o in fake.placed if o.orderType == "MKT"]
        assert len(mkts) == 2 and mkts[1].totalQuantity == 10  # 入场 + 按实际成交量紧急平仓

    async def test_short_entry_sell_protection_buy(self) -> None:
        # SHORT 全链路：入场 SELL，OCA 两腿 BUY 回补，TP 在下方 / SL 在上方
        fake = MarketFakeIB(fill_price=50.0)
        bm = _bm_with(fake)
        res = await bm.place_market_with_protection(
            symbol="aapl", side=Side.SHORT, quantity=10,
            take_profit=Target.pct(Decimal("0.002")), stop_loss=Target.pct(Decimal("0.004")),
        )
        assert fake.placed[0].action == "SELL"
        legs = [o for o in fake.placed if o.orderType in ("LMT", "STP")]
        assert legs and all(o.action == "BUY" for o in legs)
        assert Decimal(res["take_profit"]) < Decimal("50") < Decimal(res["stop_loss"])

    async def test_short_protection_failure_flattens_with_buy(self) -> None:
        # SHORT 保护失败 → 紧急平仓方向必须是 BUY 回补
        fake = MarketFakeIB(fill_price=50.0, tp_status="Cancelled", sl_status="Cancelled")
        bm = _bm_with(fake)
        with pytest.raises(ProtectionFailed) as ei:
            await bm.place_market_with_protection(
                symbol="aapl", side=Side.SHORT, quantity=10,
                take_profit=Target.pct(Decimal("0.002")), stop_loss=Target.pct(Decimal("0.004")),
            )
        assert ei.value.flatten["action"] == "BUY"
        mkts = [o for o in fake.placed if o.orderType == "MKT"]
        assert mkts[1].action == "BUY"

    async def test_protection_stuck_pending_times_out_and_flattens(self) -> None:
        # 保护腿卡在 PendingSubmit 超时（未确认上簿）→ fail-closed：撤两腿 + 平仓
        fake = MarketFakeIB(tp_status="PendingSubmit", sl_status="PendingSubmit")
        bm = _bm_with(fake)
        bm._protect_timeout_s = 0.1  # type: ignore[attr-defined]
        with pytest.raises(ProtectionFailed, match="超时"):
            await bm.place_market_with_protection(
                symbol="aapl", side=Side.LONG, quantity=10,
                take_profit=Target.pct(Decimal("0.002")), stop_loss=Target.pct(Decimal("0.004")),
            )
        assert len(fake.cancelled) == 2  # 两条保护腿先撤
        assert sum(1 for o in fake.placed if o.orderType == "MKT") == 2  # 入场 + 平仓

    async def test_flatten_ioc_unfilled_reports_failed_not_lies(self) -> None:
        # 【P1 回归】紧急平仓 IOC 被整单撤掉（停牌/LULD/无流动性）→ 仓位仍在。
        # 绝不能谎报"已平仓"——必须 flatten.failed=True + 醒目手动平仓提示。
        fake = MarketFakeIB(tp_status="Cancelled", sl_status="Cancelled", flatten_ioc_cancels=True)
        bm = _bm_with(fake)
        with pytest.raises(ProtectionFailed) as ei:
            await bm.place_market_with_protection(
                symbol="aapl", side=Side.LONG, quantity=10,
                take_profit=Target.pct(Decimal("0.002")), stop_loss=Target.pct(Decimal("0.004")),
            )
        assert ei.value.flatten.get("failed") is True
        assert "手动" in ei.value.flatten["note"]
        assert ei.value.flatten["filled"] == 0  # 实际平掉 0 股


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

    async def test_flatten_all_reports_actual_fills(self) -> None:
        # 全平必须回报每个平仓单的实际成交结果（IOC 可能被撤），不得默认"已平"
        adapter = FakeAdapter(positions=[_pos("AAPL", Side.LONG, 10)])
        fake = MarketFakeIB()
        bm = BrokerManager(IBKRSettings(), PanelConfig({}))
        bm._ib = fake  # type: ignore[assignment]
        bm._adapter = adapter  # type: ignore[assignment]
        result = await bm.flatten_all()
        row = result["flattened"][0]
        assert row["filled"] == 10
        assert row["failed"] is False

    async def test_flatten_all_flags_unfilled_close(self) -> None:
        # 平仓 IOC 整单被撤 → failed=True，前端可醒目提示"未平干净"
        adapter = FakeAdapter(positions=[_pos("AAPL", Side.LONG, 10)])
        fake = MarketFakeIB(flatten_ioc_cancels=True)
        fake._entry_done = True  # 没有入场单：第一张 MKT 就是平仓单
        bm = BrokerManager(IBKRSettings(), PanelConfig({}))
        bm._ib = fake  # type: ignore[assignment]
        bm._adapter = adapter  # type: ignore[assignment]
        result = await bm.flatten_all()
        row = result["flattened"][0]
        assert row["filled"] == 0
        assert row["failed"] is True


class TestErrorRedaction:
    def test_ib_error_masks_account_number(self) -> None:
        # 红线 #5：错误文本可能携带账户号（U/DU+数字），进 last_error 前必须脱敏
        bm = BrokerManager(IBKRSettings(), PanelConfig({}))
        bm._on_ib_error(1, 321, "Order rejected for account DU1234567 (U7654321)", None)
        assert bm._last_error is not None
        assert "DU1234567" not in bm._last_error
        assert "U7654321" not in bm._last_error
        assert "DU***" in bm._last_error


class TestConnectRespectsOrderLock:
    async def test_connect_waits_for_order_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 下单持锁期间 /api/reconnect 不得换掉 self._ib（撤单/紧急平仓会打到错误连接）
        async def fake_connect(*a: object, **k: object) -> None:
            return None

        monkeypatch.setattr("webwatch.broker.connect_paper_async", fake_connect)
        bm = BrokerManager(IBKRSettings(), PanelConfig({}))
        await bm._order_lock.acquire()
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(bm.connect(), 0.1)
        finally:
            bm._order_lock.release()
