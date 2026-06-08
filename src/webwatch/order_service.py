"""下单编排 —— 把 pricing 计算 + 下单前风控 + 序列化串起来（broker 之上的一层）。

M3 范围：限价买入的"计划"（纯函数，可测）+ 计划序列化。
真正下单的 IB 调用在 broker.place_limit_bracket（M3）。
风控目前只做单笔 notional 上限（最基本的钱安全）；完整风控接 scalper risk.manager 在 M5。

红线：本文件属下单链路，改动必须先改/加测试。金额一律 Decimal。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from scalper.strategy.base import Side

from webwatch.config import PanelConfig
from webwatch.pricing import (
    BracketPrices,
    NetEdge,
    Target,
    compute_bracket,
    min_tick,
    net_edge,
    round_to_tick,
)


class OrderRejected(Exception):
    """下单前风控/校验硬性拒绝（notional 超限、非法入参等）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class OrderPlan:
    """一笔限价括号单的完整计划（下单前预览 + 实际下单都用它）。"""

    symbol: str
    side: Side
    quantity: int
    entry_limit: Decimal
    notional: Decimal
    bracket: BracketPrices
    edge: NetEdge
    warnings: list[str]


def plan_limit_bracket(
    *,
    symbol: str,
    quantity: int,
    entry_limit: Decimal,
    take_profit: Target,
    stop_loss: Target,
    panel: PanelConfig,
    side: Side = Side.LONG,
) -> OrderPlan:
    """构建限价括号单计划：算 TP/SL 价 + 扣费净 edge + 风控校验 + 软告警。

    硬性拒绝（抛 OrderRejected）：空代码 / 非正数量或价 / notional 超单笔上限。
    软告警（warnings，不拒）：止盈<1tick 被顶高 / 扣费后净亏。
    """
    sym = symbol.strip().upper()
    if not sym:
        raise OrderRejected("代码为空")
    if quantity <= 0:
        raise OrderRejected(f"数量必须为正：{quantity}")
    if not entry_limit.is_finite() or entry_limit <= 0:
        raise OrderRejected(f"限价必须为正且有效：{entry_limit}")
    # 限价必须落在 IBKR 有效 tick 上，否则 IB 会拒单。
    entry_limit = round_to_tick(entry_limit, min_tick(entry_limit))

    notional = entry_limit * quantity
    if notional > panel.max_order_notional_usd:
        raise OrderRejected(
            f"单笔 notional ${notional} 超过上限 ${panel.max_order_notional_usd}"
        )

    try:
        bracket = compute_bracket(entry_limit, quantity, take_profit, stop_loss, side=side)
    except ValueError as exc:
        raise OrderRejected(str(exc)) from exc
    edge = net_edge(
        entry_limit,
        bracket.take_profit,
        quantity,
        side=side,
        commission_per_share=panel.commission_per_share_usd,
        min_commission=panel.commission_min_per_order_usd,
    )

    warnings: list[str] = []
    if bracket.tp_below_min_tick:
        warnings.append(
            f"止盈目标距离 < 1 个 tick，已顶到 {bracket.take_profit}"
            f"（有效幅度 {bracket.effective_tp_pct:.4%}，大于请求值）"
        )
    if not edge.profitable:
        warnings.append(
            f"按此止盈价扣费后净亏 ${edge.net_pnl}（手续费 ${edge.total_commission}）"
            f"，0.2% 覆盖不了成本"
        )

    return OrderPlan(
        symbol=sym,
        side=side,
        quantity=quantity,
        entry_limit=entry_limit,
        notional=notional,
        bracket=bracket,
        edge=edge,
        warnings=warnings,
    )


@dataclass(frozen=True)
class MarketOrderPlan:
    """市价买入计划。括号价是按参考价(ref_price)算的**预览**；真实价以成交价为准。"""

    symbol: str
    side: Side
    quantity: int
    ref_price: Decimal  # 仅用于下单前风控 + 预览，不是实际成交价
    est_notional: Decimal
    take_profit: Target
    stop_loss: Target
    preview_bracket: BracketPrices
    preview_edge: NetEdge
    warnings: list[str]


def plan_market_order(
    *,
    symbol: str,
    quantity: int,
    ref_price: Decimal,
    take_profit: Target,
    stop_loss: Target,
    panel: PanelConfig,
    side: Side = Side.LONG,
) -> MarketOrderPlan:
    """市价买入计划：用参考价做风控(notional 上限) + 预览括号价/净 edge。

    市价单成交价未知，故用 ref_price（前端当前报价）估算 notional 做下单前风控，
    并给出"若按 ref_price 成交"的止盈止损预览。实际下单后按真实成交价重算。
    """
    sym = symbol.strip().upper()
    if not sym:
        raise OrderRejected("代码为空")
    if quantity <= 0:
        raise OrderRejected(f"数量必须为正：{quantity}")
    if not ref_price.is_finite() or ref_price <= 0:
        raise OrderRejected(f"参考价必须为正且有效：{ref_price}")

    est_notional = ref_price * quantity
    if est_notional > panel.max_order_notional_usd:
        raise OrderRejected(
            f"估算 notional ${est_notional}（{quantity}×${ref_price}）"
            f"超过上限 ${panel.max_order_notional_usd}"
        )

    try:
        bracket = compute_bracket(ref_price, quantity, take_profit, stop_loss, side=side)
    except ValueError as exc:
        raise OrderRejected(str(exc)) from exc
    edge = net_edge(
        ref_price,
        bracket.take_profit,
        quantity,
        side=side,
        commission_per_share=panel.commission_per_share_usd,
        min_commission=panel.commission_min_per_order_usd,
    )
    warnings: list[str] = []
    if bracket.tp_below_min_tick:
        warnings.append(f"止盈目标距离 < 1 个 tick，预览顶到 {bracket.take_profit}")
    if not edge.profitable:
        warnings.append(f"按参考价预览扣费后净亏 ${edge.net_pnl}")

    return MarketOrderPlan(
        symbol=sym,
        side=side,
        quantity=quantity,
        ref_price=ref_price,
        est_notional=est_notional,
        take_profit=take_profit,
        stop_loss=stop_loss,
        preview_bracket=bracket,
        preview_edge=edge,
        warnings=warnings,
    )


def market_plan_to_dict(plan: MarketOrderPlan) -> dict[str, Any]:
    """市价计划 → JSON-safe dict（价格标注为按参考价的预览）。"""
    return {
        "symbol": plan.symbol,
        "side": plan.side.value,
        "quantity": plan.quantity,
        "ref_price": str(plan.ref_price),
        "est_notional": str(plan.est_notional),
        "preview_take_profit": str(plan.preview_bracket.take_profit),
        "preview_stop_loss": str(plan.preview_bracket.stop_loss),
        "tp_below_min_tick": plan.preview_bracket.tp_below_min_tick,
        "preview_net_pnl": str(plan.preview_edge.net_pnl),
        "profitable": plan.preview_edge.profitable,
        "warnings": plan.warnings,
    }


def plan_to_dict(plan: OrderPlan) -> dict[str, Any]:
    """计划 → JSON-safe dict（前端预览用）。金额转 str 保精度。"""
    return {
        "symbol": plan.symbol,
        "side": plan.side.value,
        "quantity": plan.quantity,
        "entry_limit": str(plan.entry_limit),
        "notional": str(plan.notional),
        "take_profit": str(plan.bracket.take_profit),
        "stop_loss": str(plan.bracket.stop_loss),
        "tick": str(plan.bracket.tick),
        "effective_tp_pct": str(plan.bracket.effective_tp_pct),
        "tp_below_min_tick": plan.bracket.tp_below_min_tick,
        "net_pnl": str(plan.edge.net_pnl),
        "gross_pnl": str(plan.edge.gross_pnl),
        "total_commission": str(plan.edge.total_commission),
        "profitable": plan.edge.profitable,
        "warnings": plan.warnings,
    }


__all__ = [
    "OrderRejected",
    "OrderPlan",
    "MarketOrderPlan",
    "plan_limit_bracket",
    "plan_market_order",
    "plan_to_dict",
    "market_plan_to_dict",
]
