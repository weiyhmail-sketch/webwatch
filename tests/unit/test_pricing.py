"""pricing 模块单测 —— 最易出 bug 的计算核心（符号 / tick 取整 / 低价股 / 扣费净 edge）。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from scalper.strategy.base import Side

from webwatch.pricing import (
    BracketPrices,
    NetEdge,
    Target,
    aggressive_limit,
    compute_bracket,
    min_tick,
    net_edge,
    round_to_tick,
    shares_for_notional,
    stop_limit_price,
    target_delta,
)


class TestMinTick:
    def test_at_and_above_one_dollar_is_penny(self) -> None:
        assert min_tick(Decimal("1.00")) == Decimal("0.01")
        assert min_tick(Decimal("1.01")) == Decimal("0.01")
        assert min_tick(Decimal("500")) == Decimal("0.01")

    def test_below_one_dollar_is_subpenny(self) -> None:
        assert min_tick(Decimal("0.99")) == Decimal("0.0001")
        assert min_tick(Decimal("0.5")) == Decimal("0.0001")


class TestRoundToTick:
    def test_rounds_to_penny(self) -> None:
        assert round_to_tick(Decimal("100.204"), Decimal("0.01")) == Decimal("100.20")
        assert round_to_tick(Decimal("100.206"), Decimal("0.01")) == Decimal("100.21")

    def test_result_is_decimal_not_float(self) -> None:
        assert isinstance(round_to_tick(Decimal("10.0"), Decimal("0.01")), Decimal)


class TestTargetDelta:
    def test_pct(self) -> None:
        assert target_delta(Decimal("100"), 100, Target.pct(Decimal("0.002"))) == Decimal("0.200")

    def test_price_offset_ignores_entry_and_qty(self) -> None:
        assert target_delta(Decimal("100"), 100, Target.price_offset(Decimal("1"))) == Decimal("1")

    def test_profit_usd_divides_by_quantity(self) -> None:
        assert target_delta(Decimal("100"), 100, Target.profit_usd(Decimal("100"))) == Decimal("1")
        assert target_delta(Decimal("50"), 200, Target.profit_usd(Decimal("100"))) == Decimal("0.5")

    def test_profit_usd_requires_quantity(self) -> None:
        with pytest.raises(ValueError):
            target_delta(Decimal("100"), 0, Target.profit_usd(Decimal("100")))

    def test_nonpositive_value_rejected(self) -> None:
        with pytest.raises(ValueError):
            target_delta(Decimal("100"), 100, Target.pct(Decimal("0")))

    def test_nan_value_rejected(self) -> None:
        with pytest.raises(ValueError):
            target_delta(Decimal("100"), 100, Target.pct(Decimal("nan")))


class TestComputeBracketGuards:
    def test_nan_entry_rejected(self) -> None:
        with pytest.raises(ValueError):
            compute_bracket(
                Decimal("nan"), 100, Target.pct(Decimal("0.002")), Target.pct(Decimal("0.004"))
            )

    def test_infinite_entry_rejected(self) -> None:
        with pytest.raises(ValueError):
            compute_bracket(
                Decimal("inf"), 100, Target.pct(Decimal("0.002")), Target.pct(Decimal("0.004"))
            )

    def test_oversized_target_yields_negative_price_rejected(self) -> None:
        # 低价股 $5、10 股、止损目标盈亏 $100 → 每股 $10 距离 → SL = 5-10 = -5 → 必须拒绝
        with pytest.raises(ValueError):
            compute_bracket(
                Decimal("5"), 10, Target.pct(Decimal("0.002")), Target.profit_usd(Decimal("100"))
            )


class TestComputeBracketPct:
    def test_normal_case_signs_and_values(self) -> None:
        b = compute_bracket(
            Decimal("100"), 100, Target.pct(Decimal("0.002")), Target.pct(Decimal("0.004"))
        )
        assert isinstance(b, BracketPrices)
        assert b.take_profit > b.entry > b.stop_loss  # LONG: TP 上、SL 下
        assert b.take_profit == Decimal("100.20")
        assert b.stop_loss == Decimal("99.60")
        assert b.tp_below_min_tick is False

    def test_tp_rounds_up_to_guarantee_target(self) -> None:
        # entry 33.33, +0.2% = 33.39666 → 向上取整到分位 = 33.40
        b = compute_bracket(
            Decimal("33.33"), 100, Target.pct(Decimal("0.002")), Target.pct(Decimal("0.004"))
        )
        assert b.take_profit == Decimal("33.40")

    def test_low_price_below_one_tick_warns_and_bumps(self) -> None:
        # $4 股，0.2% = $0.008 < 1 tick($0.01) → 告警，TP 顶到 entry+1tick
        b = compute_bracket(
            Decimal("4.00"), 100, Target.pct(Decimal("0.002")), Target.pct(Decimal("0.004"))
        )
        assert b.tp_below_min_tick is True
        assert b.take_profit == Decimal("4.01")
        assert b.effective_tp_pct > Decimal("0.002")

    def test_short_inverts_tp_sl(self) -> None:
        b = compute_bracket(
            Decimal("100"),
            100,
            Target.pct(Decimal("0.002")),
            Target.pct(Decimal("0.004")),
            side=Side.SHORT,
        )
        assert b.take_profit < b.entry < b.stop_loss  # SHORT: TP 下、SL 上
        assert b.take_profit == Decimal("99.80")
        assert b.stop_loss == Decimal("100.40")


class TestComputeBracketPriceOffset:
    def test_dollar_offset_long(self) -> None:
        # 成交价 +$1 止盈，-$2 止损
        b = compute_bracket(
            Decimal("100"), 100, Target.price_offset(Decimal("1")), Target.price_offset(Decimal("2"))
        )
        assert b.take_profit == Decimal("101.00")
        assert b.stop_loss == Decimal("98.00")

    def test_offset_below_tick_warns(self) -> None:
        b = compute_bracket(
            Decimal("4.00"),
            100,
            Target.price_offset(Decimal("0.005")),  # < 1 tick
            Target.price_offset(Decimal("0.02")),
        )
        assert b.tp_below_min_tick is True
        assert b.take_profit == Decimal("4.01")


class TestComputeBracketProfitUsd:
    def test_total_profit_target_long(self) -> None:
        # 100 股，盈利 $100 即止盈 → 每股 +$1 → 101.00；亏 $200 止损 → 每股 -$2 → 98.00
        b = compute_bracket(
            Decimal("100"),
            100,
            Target.profit_usd(Decimal("100")),
            Target.profit_usd(Decimal("200")),
        )
        assert b.take_profit == Decimal("101.00")
        assert b.stop_loss == Decimal("98.00")

    def test_profit_target_scales_with_quantity(self) -> None:
        # 200 股，盈利 $100 → 每股 +$0.50 → 50.50
        b = compute_bracket(
            Decimal("50"),
            200,
            Target.profit_usd(Decimal("100")),
            Target.profit_usd(Decimal("100")),
        )
        assert b.take_profit == Decimal("50.50")
        assert b.stop_loss == Decimal("49.50")


class TestNetEdge:
    def test_profitable_after_commission(self) -> None:
        e = net_edge(Decimal("100"), Decimal("100.20"), 100)
        assert isinstance(e, NetEdge)
        assert e.gross_pnl == Decimal("20.00")
        assert e.total_commission == Decimal("0.70")  # 2 × max(0.35, 0.0035*100)
        assert e.net_pnl == Decimal("19.30")
        assert e.profitable is True

    def test_thin_edge_eaten_by_fees_is_unprofitable(self) -> None:
        e = net_edge(Decimal("4.00"), Decimal("4.01"), 100)
        assert e.gross_pnl == Decimal("1.00")
        assert e.net_pnl < e.gross_pnl
        e2 = net_edge(Decimal("4.00"), Decimal("4.01"), 10)
        assert e2.profitable is False
        assert e2.net_pnl < 0

    def test_regulatory_fees_applied_on_sell(self) -> None:
        no_fee = net_edge(Decimal("100"), Decimal("100.20"), 100)
        with_fee = net_edge(
            Decimal("100"),
            Decimal("100.20"),
            100,
            sec_fee_rate=Decimal("0.0000278"),
            taf_per_share=Decimal("0.000166"),
        )
        assert with_fee.total_fees > 0
        assert with_fee.net_pnl < no_fee.net_pnl

    def test_all_outputs_are_decimal(self) -> None:
        e = net_edge(Decimal("100"), Decimal("100.20"), 100)
        for val in (e.gross_pnl, e.total_commission, e.total_fees, e.net_pnl, e.net_pnl_per_share):
            assert isinstance(val, Decimal)


class TestAggressiveLimit:
    def test_long_adds_ticks_above_ref(self) -> None:
        # $100、+3 tick($0.01) → 100.03
        assert aggressive_limit(Decimal("100"), 3, side=Side.LONG) == Decimal("100.03")

    def test_short_subtracts_ticks(self) -> None:
        assert aggressive_limit(Decimal("100"), 3, side=Side.SHORT) == Decimal("99.97")

    def test_rejects_nonpositive(self) -> None:
        with pytest.raises(ValueError):
            aggressive_limit(Decimal("0"), 3)


class TestStopLimitPrice:
    def test_long_limit_below_stop(self) -> None:
        # LONG 卖出止损：限价在触发价下方 0.5% → 100*0.995=99.50
        assert stop_limit_price(Decimal("100"), Decimal("0.005"), side=Side.LONG) == Decimal("99.50")

    def test_short_limit_above_stop(self) -> None:
        assert stop_limit_price(Decimal("100"), Decimal("0.005"), side=Side.SHORT) == Decimal("100.50")

    def test_rejects_negative_offset(self) -> None:
        with pytest.raises(ValueError):
            stop_limit_price(Decimal("100"), Decimal("-0.1"))


class TestSharesForNotional:
    def test_floors_to_whole_shares(self) -> None:
        assert shares_for_notional(Decimal("2000"), Decimal("33.33")) == 60
        assert shares_for_notional(Decimal("1000"), Decimal("100")) == 10

    def test_zero_when_price_exceeds_notional(self) -> None:
        assert shares_for_notional(Decimal("50"), Decimal("100")) == 0

    def test_rejects_nonpositive_price(self) -> None:
        with pytest.raises(ValueError):
            shares_for_notional(Decimal("1000"), Decimal("0"))
