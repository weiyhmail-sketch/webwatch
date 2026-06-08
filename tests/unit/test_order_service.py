"""order_service 单测 —— 下单计划（风控拒绝 / 价格 / 净 edge / 告警）。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from scalper.strategy.base import Side

from webwatch.config import PanelConfig
from webwatch.order_service import (
    OrderRejected,
    market_plan_to_dict,
    plan_limit_bracket,
    plan_market_order,
    plan_to_dict,
)
from webwatch.pricing import Target

PANEL = PanelConfig({})  # 默认：notional 上限 5000，佣金 0.0035/0.35


def _plan(**kw):  # type: ignore[no-untyped-def]
    base = dict(
        symbol="aapl",
        quantity=100,
        entry_limit=Decimal("50"),
        take_profit=Target.pct(Decimal("0.002")),
        stop_loss=Target.pct(Decimal("0.004")),
        panel=PANEL,
        side=Side.LONG,
    )
    base.update(kw)
    return plan_limit_bracket(**base)  # type: ignore[arg-type]


class TestPlanHappyPath:
    def test_prices_and_normalization(self) -> None:
        p = _plan()
        assert p.symbol == "AAPL"
        assert p.notional == Decimal("5000")  # 50 * 100 == 上限，允许
        assert p.bracket.take_profit == Decimal("50.10")
        assert p.bracket.stop_loss == Decimal("49.80")
        assert p.edge.profitable is True
        assert p.warnings == []

    def test_profit_usd_target(self) -> None:
        p = _plan(take_profit=Target.profit_usd(Decimal("100")))  # 100 股赚 $100 → +$1
        assert p.bracket.take_profit == Decimal("51.00")


class TestPlanRejections:
    def test_notional_cap(self) -> None:
        with pytest.raises(OrderRejected, match="notional"):
            _plan(entry_limit=Decimal("100"))  # 100*100=10000 > 5000

    def test_empty_symbol(self) -> None:
        with pytest.raises(OrderRejected):
            _plan(symbol="   ")

    def test_nonpositive_quantity(self) -> None:
        with pytest.raises(OrderRejected):
            _plan(quantity=0)

    def test_nonpositive_price(self) -> None:
        with pytest.raises(OrderRejected):
            _plan(entry_limit=Decimal("0"))

    def test_nan_price_rejected(self) -> None:
        # 关键安全：NaN 不能绕过校验（Decimal('nan')<=0 为 False）
        with pytest.raises(OrderRejected):
            _plan(entry_limit=Decimal("nan"))

    def test_infinite_price_rejected(self) -> None:
        with pytest.raises(OrderRejected):
            _plan(entry_limit=Decimal("inf"))

    def test_oversized_target_rejected(self) -> None:
        # 目标过大算出负保护价 → compute_bracket 抛错 → 转成 OrderRejected（不 500）
        with pytest.raises(OrderRejected):
            _plan(entry_limit=Decimal("5"), quantity=10, stop_loss=Target.profit_usd(Decimal("100")))


class TestPlanTickRounding:
    def test_entry_rounded_to_tick(self) -> None:
        # 用户输入非 tick 限价 10.005 → 取整到 10.01（避免 IB 拒单）
        p = _plan(entry_limit=Decimal("10.005"))
        assert p.entry_limit == Decimal("10.01")


class TestPlanWarnings:
    def test_tp_below_tick_warns_not_rejects(self) -> None:
        p = _plan(entry_limit=Decimal("4"), quantity=100)  # 0.2% of $4 = $0.008 < 1 tick
        assert p.bracket.tp_below_min_tick is True
        assert any("tick" in w for w in p.warnings)

    def test_unprofitable_warns(self) -> None:
        p = _plan(entry_limit=Decimal("4"), quantity=10)  # gross $0.10 < 佣金 $0.70
        assert p.edge.profitable is False
        assert any("净亏" in w for w in p.warnings)


class TestPlanToDict:
    def test_shape(self) -> None:
        d = plan_to_dict(_plan())
        assert d["symbol"] == "AAPL"
        assert d["take_profit"] == "50.10"
        assert d["stop_loss"] == "49.80"
        assert d["profitable"] is True
        assert isinstance(d["warnings"], list)


def _mplan(**kw):  # type: ignore[no-untyped-def]
    base = dict(
        symbol="aapl",
        quantity=10,
        ref_price=Decimal("200"),
        take_profit=Target.pct(Decimal("0.002")),
        stop_loss=Target.pct(Decimal("0.004")),
        panel=PANEL,
        side=Side.LONG,
    )
    base.update(kw)
    return plan_market_order(**base)  # type: ignore[arg-type]


class TestPlanMarketOrder:
    def test_preview_bracket_from_ref_price(self) -> None:
        p = _mplan()
        assert p.est_notional == Decimal("2000")
        assert p.preview_bracket.take_profit == Decimal("200.40")
        assert p.preview_bracket.stop_loss == Decimal("199.20")
        # targets 保留下来，供成交后按真实价重算
        assert p.take_profit == Target.pct(Decimal("0.002"))

    def test_notional_cap_uses_ref_price(self) -> None:
        with pytest.raises(OrderRejected, match="notional"):
            _mplan(ref_price=Decimal("100"), quantity=100)  # 10000 > 5000

    def test_to_dict_marks_preview(self) -> None:
        d = market_plan_to_dict(_mplan())
        assert d["preview_take_profit"] == "200.40"
        assert d["ref_price"] == "200"
