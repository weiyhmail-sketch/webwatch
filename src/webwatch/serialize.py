"""把 scalper 数据类型序列化成 JSON-safe dict（纯函数，便于单测）。

约定：
- ``Decimal`` → ``str``（保精度，前端按需格式化）。
- ``float`` 价格直接给数字（仅展示用）。
- 账户号脱敏，不在前端暴露完整账号。
"""

from __future__ import annotations

from typing import Any

from scalper.execution.state_store import OrderSnapshot
from scalper.execution.types import AccountSummary, Position
from scalper.strategy.base import Side


def mask_account(account: str) -> str:
    """账户号脱敏：DU1234567 → DU***67。"""
    return f"{account[:2]}***{account[-2:]}" if len(account) >= 4 else "***"


def position_to_dict(p: Position) -> dict[str, Any]:
    """持仓 → dict，含未实现盈亏（若有 mark price）。"""
    unrealized: float | None = None
    if p.last_mark_price is not None:
        diff = (p.last_mark_price - p.avg_entry_price) * p.quantity
        if p.side is Side.SHORT:
            diff = -diff
        unrealized = round(diff, 2)
    return {
        "symbol": p.symbol,
        "side": p.side.value,
        "quantity": p.quantity,
        "avg_entry_price": p.avg_entry_price,
        "mark_price": p.last_mark_price,
        "unrealized_pnl": unrealized,
        "realized_pnl": str(p.realized_pnl),
    }


def account_to_dict(a: AccountSummary) -> dict[str, Any]:
    """账户摘要 → dict（账户号脱敏，金额转 str）。"""
    return {
        "account": mask_account(a.account),
        "net_liquidation": str(a.net_liquidation),
        "total_cash_value": str(a.total_cash_value),
        "buying_power": str(a.buying_power),
        "excess_liquidity": (
            str(a.excess_liquidity) if a.excess_liquidity is not None else None
        ),
        "currency": a.currency,
    }


def order_to_dict(o: OrderSnapshot) -> dict[str, Any]:
    """挂单快照 → dict。"""
    return {
        "order_id": str(o.order_id),
        "parent_entry_id": str(o.parent_entry_id) if o.parent_entry_id is not None else None,
        "symbol": o.symbol,
        "side": o.side.value,
        "order_type": o.order_type,
        "quantity": o.quantity,
        "limit_price": o.limit_price,
        "stop_price": o.stop_price,
        "status": o.status,
        "oca_group": o.oca_group,
    }


__all__ = ["mask_account", "position_to_dict", "account_to_dict", "order_to_dict"]
