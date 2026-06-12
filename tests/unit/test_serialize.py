"""serialize 模块单测 —— 纯函数，用构造的 scalper 对象验证 JSON 形状。"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
from scalper.execution.state_store import OrderSnapshot
from scalper.execution.types import AccountSummary, Position
from scalper.strategy.base import Side

from webwatch.serialize import (
    account_to_dict,
    ib_trade_to_order_dict,
    mask_account,
    order_to_dict,
    position_to_dict,
)

TS = pd.Timestamp("2026-06-08T14:30:00Z")

# ib_async 未设置价格的哨兵（UNSET_DOUBLE）
_UNSET = 1.7976931348623157e308


def _ib_trade(
    *,
    sym: str = "AAPL",
    action: str = "BUY",
    otype: str = "LMT",
    qty: float = 100.0,
    lmt: float = _UNSET,
    aux: float = _UNSET,
    status: str = "Submitted",
    oca: str = "",
    order_id: int = 42,
    parent_id: int = 0,
) -> SimpleNamespace:
    """构造形如 ib_async Trade 的对象（只含序列化用到的字段）。"""
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=sym),
        order=SimpleNamespace(
            orderId=order_id,
            parentId=parent_id,
            action=action,
            orderType=otype,
            totalQuantity=qty,
            lmtPrice=lmt,
            auxPrice=aux,
            ocaGroup=oca,
        ),
        orderStatus=SimpleNamespace(status=status),
    )


class TestIbTradeToOrderDict:
    def test_stp_lmt_keeps_both_prices_and_type(self) -> None:
        # 修 bug 的核心场景：时段外止损(STP LMT)被 scalper 适配器静默跳过 →
        # 保护单挂着却不显示。webwatch 自有序列化必须原样保留。
        d = ib_trade_to_order_dict(
            _ib_trade(
                sym="AAOI", action="SELL", otype="STP LMT",
                lmt=99.10, aux=99.60, status="PreSubmitted",
                oca="scalper-oca-ww-934", order_id=936,
            )
        )
        assert d["order_type"] == "STP LMT"
        assert d["limit_price"] == 99.10
        assert d["stop_price"] == 99.60
        assert d["side"] == "short"  # SELL 腿
        assert d["oca_group"] == "scalper-oca-ww-934"
        assert d["status"] == "PreSubmitted"
        assert d["symbol"] == "AAOI"

    def test_lmt_unset_aux_is_none(self) -> None:
        d = ib_trade_to_order_dict(_ib_trade(otype="LMT", lmt=100.5))
        assert d["limit_price"] == 100.5
        assert d["stop_price"] is None
        assert d["side"] == "long"
        assert d["quantity"] == 100

    def test_mkt_both_prices_none(self) -> None:
        d = ib_trade_to_order_dict(_ib_trade(otype="MKT"))
        assert d["limit_price"] is None
        assert d["stop_price"] is None

    def test_parent_and_oca_empty_become_none(self) -> None:
        d = ib_trade_to_order_dict(_ib_trade(parent_id=0, oca=""))
        assert d["parent_entry_id"] is None
        assert d["oca_group"] is None

    def test_parent_id_kept_when_set(self) -> None:
        d = ib_trade_to_order_dict(_ib_trade(parent_id=934))
        assert d["parent_entry_id"] == "934"


class TestMaskAccount:
    def test_masks_middle(self) -> None:
        assert mask_account("DU1234567") == "DU***67"

    def test_short_account_fully_masked(self) -> None:
        assert mask_account("AB") == "***"


class TestPositionToDict:
    def test_long_unrealized_pnl(self) -> None:
        p = Position(
            symbol="AAPL",
            side=Side.LONG,
            quantity=10,
            avg_entry_price=100.0,
            open_ts=TS,
            entry_order_id="e1",  # type: ignore[arg-type]
            last_mark_price=101.0,
        )
        d = position_to_dict(p)
        assert d["symbol"] == "AAPL"
        assert d["side"] == "long"
        assert d["unrealized_pnl"] == 10.0
        assert d["realized_pnl"] == "0"

    def test_short_pnl_sign_inverted(self) -> None:
        p = Position(
            symbol="TSLA",
            side=Side.SHORT,
            quantity=5,
            avg_entry_price=200.0,
            open_ts=TS,
            entry_order_id="e2",  # type: ignore[arg-type]
            last_mark_price=198.0,
        )
        # 做空价格下跌 = 盈利
        assert position_to_dict(p)["unrealized_pnl"] == 10.0

    def test_no_mark_price_means_none_pnl(self) -> None:
        p = Position(
            symbol="MSFT",
            side=Side.LONG,
            quantity=3,
            avg_entry_price=300.0,
            open_ts=TS,
            entry_order_id="e3",  # type: ignore[arg-type]
        )
        assert position_to_dict(p)["unrealized_pnl"] is None


class TestAccountToDict:
    def test_masks_and_stringifies(self) -> None:
        a = AccountSummary(
            account="DU1234567",
            net_liquidation=Decimal("30000.50"),
            total_cash_value=Decimal("15000.00"),
            buying_power=Decimal("60000.00"),
            currency="USD",
            timestamp=TS,
            excess_liquidity=Decimal("12000.00"),
        )
        d = account_to_dict(a)
        assert d["account"] == "DU***67"
        assert d["net_liquidation"] == "30000.50"
        assert d["excess_liquidity"] == "12000.00"
        assert d["currency"] == "USD"

    def test_optional_excess_liquidity_none(self) -> None:
        a = AccountSummary(
            account="DU1234567",
            net_liquidation=Decimal("1"),
            total_cash_value=Decimal("1"),
            buying_power=Decimal("1"),
            currency="USD",
            timestamp=TS,
        )
        assert account_to_dict(a)["excess_liquidity"] is None


class TestOrderToDict:
    def test_fields(self) -> None:
        o = OrderSnapshot(
            order_id="o1",  # type: ignore[arg-type]
            parent_entry_id="e1",  # type: ignore[arg-type]
            symbol="AAPL",
            order_type="LMT",
            side=Side.LONG,
            quantity=100,
            limit_price=150.25,
            stop_price=None,
            status="Submitted",
            submitted_ts=TS,
            last_status_ts=TS,
            oca_group="scalper-oca-e1",
        )
        d = order_to_dict(o)
        assert d["order_id"] == "o1"
        assert d["order_type"] == "LMT"
        assert d["side"] == "long"
        assert d["limit_price"] == 150.25
        assert d["stop_price"] is None
        assert d["oca_group"] == "scalper-oca-e1"
