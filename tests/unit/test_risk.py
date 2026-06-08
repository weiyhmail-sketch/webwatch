"""risk 模块单测 —— 下单前账户感知风控。"""

from __future__ import annotations

from decimal import Decimal

from scalper.strategy.base import Side

from webwatch.config import PanelConfig
from webwatch.risk import BLOCK, WARN, assess, blocks

PANEL = PanelConfig({})  # max_position_risk_pct 0.005, pdt_min_nav 25000


def _assess(**kw):  # type: ignore[no-untyped-def]
    base = dict(
        entry=Decimal("100"),
        stop_loss=Decimal("99.80"),
        quantity=100,
        side=Side.LONG,
        nav=Decimal("1000000"),
        buying_power=Decimal("4000000"),
        panel=PANEL,
    )
    base.update(kw)
    return assess(**base)  # type: ignore[arg-type]


class TestMaxPositionRisk:
    def test_blocks_when_loss_exceeds_pct_nav(self) -> None:
        # 单笔最大亏损 (100-90)*100=1000；NAV 100000 * 0.5% = 500 → 拦截
        f = _assess(entry=Decimal("100"), stop_loss=Decimal("90"), quantity=100, nav=Decimal("100000"))
        assert any(x.code == "max_position_risk" and x.severity == BLOCK for x in f)

    def test_ok_when_within_limit(self) -> None:
        f = _assess(stop_loss=Decimal("99.80"), nav=Decimal("100000"))
        assert not [x for x in f if x.code == "max_position_risk"]

    def test_short_side_loss_direction(self) -> None:
        # 做空：止损在上方，(110-100)*100=1000 > 500 → 拦截
        f = _assess(
            entry=Decimal("100"), stop_loss=Decimal("110"), quantity=100,
            side=Side.SHORT, nav=Decimal("100000"),
        )
        assert any(x.code == "max_position_risk" for x in f)


class TestBuyingPower:
    def test_blocks_when_notional_exceeds_bp(self) -> None:
        f = _assess(entry=Decimal("100"), stop_loss=Decimal("99.9"), quantity=100,
                    nav=Decimal("1000000"), buying_power=Decimal("5000"))
        assert any(x.code == "buying_power" and x.severity == BLOCK for x in f)


class TestPdt:
    def test_warns_below_min_nav(self) -> None:
        f = _assess(nav=Decimal("20000"))
        pdt = [x for x in f if x.code == "pdt"]
        assert pdt and pdt[0].severity == WARN
        # 仅提示，不算 block
        assert not any(x.code == "pdt" for x in blocks(f))


class TestNoAccountData:
    def test_no_nav_no_findings(self) -> None:
        assert _assess(nav=None, buying_power=None) == []


class TestNonFiniteDefense:
    def test_nan_price_blocks(self) -> None:
        f = _assess(entry=Decimal("nan"))
        assert any(x.code == "non_finite_price" and x.severity == BLOCK for x in f)

    def test_inf_stop_blocks(self) -> None:
        f = _assess(stop_loss=Decimal("inf"))
        assert any(x.code == "non_finite_price" and x.severity == BLOCK for x in f)
