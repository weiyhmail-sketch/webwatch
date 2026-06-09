"""价格计算核心 —— 纯函数，无 I/O、无副作用。

职责：
- 把止盈/止损「目标」解析成具体限价（支持多种表达方式，见 ``Target``）
- 取整到 IBKR 最小价位（tick）
- 低价股「目标距离 < 1 tick」告警
- 扣费净 edge 预估（让用户下单前看到该价位/手数下到底净不净赚）

止盈/止损目标支持三种表达：
1. 百分比      ``Target.pct(0.002)``        → +0.2%
2. 价格偏移    ``Target.price_offset(1)``    → 成交价 ±$1（每股）
3. 盈利金额    ``Target.profit_usd(100)``    → 总盈利 ±$100（按手数反推每股）

红线：金额一律 ``Decimal``，禁止裸 ``float``。改动本文件必须先改/加测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from enum import StrEnum

from scalper.strategy.base import Side

# IBKR / SEC Rule 612 最小价位变动（MPV）：≥$1 的美股按 $0.01，<$1 按 $0.0001。
_PENNY = Decimal("0.01")
_SUBPENNY = Decimal("0.0001")
_ONE_DOLLAR = Decimal("1")

# IBKR Pro Tiered 默认假设（与 scalper 一致；可在调用处覆盖）。
DEFAULT_COMMISSION_PER_SHARE = Decimal("0.0035")
DEFAULT_MIN_COMMISSION = Decimal("0.35")


def min_tick(price: Decimal) -> Decimal:
    """该价位的 IBKR 最小价位变动。"""
    return _SUBPENNY if price < _ONE_DOLLAR else _PENNY


def round_to_tick(
    price: Decimal,
    tick: Decimal,
    rounding: str = ROUND_HALF_UP,
) -> Decimal:
    """把 ``price`` 取整到最近的 ``tick`` 倍数（按 ``rounding`` 方向）。"""
    ticks = (price / tick).to_integral_value(rounding=rounding)
    return (ticks * tick).quantize(tick)


# --------------------------------------------------------------------------
# Target —— 止盈/止损目标的多种表达方式
# --------------------------------------------------------------------------


class TargetKind(StrEnum):
    """目标表达方式。"""

    PCT = "pct"  # 百分比（小数，如 0.002 = 0.2%）
    PRICE_OFFSET = "price_offset"  # 每股价格偏移（美元）
    PROFIT_USD = "profit_usd"  # 总盈/亏金额（美元，按手数反推每股）


@dataclass(frozen=True)
class Target:
    """一个止盈或止损目标。``value`` 必须 > 0（方向由 leg + side 决定）。"""

    kind: TargetKind
    value: Decimal

    @classmethod
    def pct(cls, value: Decimal) -> Target:
        return cls(TargetKind.PCT, value)

    @classmethod
    def price_offset(cls, value: Decimal) -> Target:
        return cls(TargetKind.PRICE_OFFSET, value)

    @classmethod
    def profit_usd(cls, value: Decimal) -> Target:
        return cls(TargetKind.PROFIT_USD, value)


def target_delta(entry: Decimal, quantity: int, target: Target) -> Decimal:
    """把目标解析成「每股价格距离」（恒为正）。

    - PCT：``entry * value``
    - PRICE_OFFSET：``value``
    - PROFIT_USD：``value / quantity``（总额按手数摊到每股，gross）
    """
    if not target.value.is_finite() or target.value <= 0:
        raise ValueError(f"target value must be a positive finite number, got {target.value}")
    if target.kind is TargetKind.PCT:
        return entry * target.value
    if target.kind is TargetKind.PRICE_OFFSET:
        return target.value
    # PROFIT_USD
    if quantity <= 0:
        raise ValueError(f"PROFIT_USD target needs positive quantity, got {quantity}")
    return target.value / Decimal(quantity)


@dataclass(frozen=True)
class BracketPrices:
    """一组括号价：入场 + 止盈 + 止损。"""

    entry: Decimal
    take_profit: Decimal
    stop_loss: Decimal
    tick: Decimal
    effective_tp_pct: Decimal
    # 告警：止盈目标对应距离 < 1 个 tick（常见于低价快速反弹股），
    # 此时止盈价被顶到 entry ± 1 tick，实际有效幅度大于请求值。
    tp_below_min_tick: bool


def compute_bracket(
    entry: Decimal,
    quantity: int,
    take_profit: Target,
    stop_loss: Target,
    *,
    side: Side = Side.LONG,
) -> BracketPrices:
    """从入场价 + 手数 + 止盈/止损目标算出括号价。

    取整方向保证"目标不缩水、止损不变紧"：
      - LONG：止盈向上取整（保证至少达标），止损向下取整（保证不比目标更紧）。
      - SHORT：对称反向。

    若止盈目标对应距离 < 1 个 tick（低价股），止盈价顶到 entry ± 1 tick，
    并置 ``tp_below_min_tick=True``。
    """
    if not entry.is_finite() or entry <= 0:
        raise ValueError(f"entry must be a positive finite number, got {entry}")
    tick = min_tick(entry)
    tp_delta = target_delta(entry, quantity, take_profit)
    sl_delta = target_delta(entry, quantity, stop_loss)
    tp_below = tp_delta < tick

    if side is Side.LONG:
        take_profit_price = round_to_tick(entry + tp_delta, tick, ROUND_UP)
        if take_profit_price <= entry:
            take_profit_price = (entry + tick).quantize(tick)
        stop_loss_price = round_to_tick(entry - sl_delta, tick, ROUND_DOWN)
    else:  # SHORT
        take_profit_price = round_to_tick(entry - tp_delta, tick, ROUND_DOWN)
        if take_profit_price >= entry:
            take_profit_price = (entry - tick).quantize(tick)
        stop_loss_price = round_to_tick(entry + sl_delta, tick, ROUND_UP)

    # 防御：目标过大（如低价股上 profit_usd 反推每股距离 > 入场价）会算出 <=0 的保护价。
    # 负/零保护价绝不能发到 IB（会被拒 → 裸仓风险），直接拒绝。
    if take_profit_price <= 0 or stop_loss_price <= 0:
        raise ValueError(
            f"目标过大导致保护价非正（TP={take_profit_price}, SL={stop_loss_price}），"
            f"请减小止盈/止损目标或提高入场价"
        )

    effective_tp_pct = abs(take_profit_price - entry) / entry
    return BracketPrices(
        entry=entry.quantize(tick),
        take_profit=take_profit_price,
        stop_loss=stop_loss_price,
        tick=tick,
        effective_tp_pct=effective_tp_pct,
        tp_below_min_tick=tp_below,
    )


@dataclass(frozen=True)
class NetEdge:
    """一个来回（买+卖）扣费后的净盈亏预估。"""

    gross_pnl: Decimal
    total_commission: Decimal
    total_fees: Decimal  # 监管费（SEC + TAF），仅卖出腿
    net_pnl: Decimal
    net_pnl_per_share: Decimal
    profitable: bool


def net_edge(
    entry: Decimal,
    exit_price: Decimal,
    quantity: int,
    *,
    side: Side = Side.LONG,
    commission_per_share: Decimal = DEFAULT_COMMISSION_PER_SHARE,
    min_commission: Decimal = DEFAULT_MIN_COMMISSION,
    sec_fee_rate: Decimal = Decimal("0"),
    taf_per_share: Decimal = Decimal("0"),
) -> NetEdge:
    """估算一个来回扣费后的净盈亏。

    - 佣金：买卖各 ``max(min_commission, per_share * qty)``。
    - 监管费（仅卖出腿）：``sec_fee_rate * 卖出金额 + taf_per_share * qty``。
      费率随时间变化，默认 0；面板可传入当期实际费率。
    """
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")
    qty = Decimal(quantity)

    if side is Side.LONG:
        gross_pnl = (exit_price - entry) * qty
        sell_price = exit_price
    else:
        gross_pnl = (entry - exit_price) * qty
        sell_price = entry

    leg_commission = max(min_commission, commission_per_share * qty)
    total_commission = leg_commission * 2

    sec_fee = sec_fee_rate * (sell_price * qty)
    taf_fee = taf_per_share * qty
    total_fees = sec_fee + taf_fee

    net_pnl = gross_pnl - total_commission - total_fees
    return NetEdge(
        gross_pnl=gross_pnl,
        total_commission=total_commission,
        total_fees=total_fees,
        net_pnl=net_pnl,
        net_pnl_per_share=net_pnl / qty,
        profitable=net_pnl > 0,
    )


def aggressive_limit(ref_price: Decimal, ticks: int, *, side: Side = Side.LONG) -> Decimal:
    """时段外把"市价"转成激进限价：买单贴卖一价上方 ``ticks`` 个 tick（卖单则下方）。

    时段外 IBKR 不收市价单；激进限价用略优于对手价的价格追求成交。
    """
    if not ref_price.is_finite() or ref_price <= 0:
        raise ValueError(f"ref_price must be positive finite, got {ref_price}")
    if ticks < 0:
        raise ValueError(f"ticks must be >= 0, got {ticks}")
    tick = min_tick(ref_price)
    offset = tick * ticks
    if side is Side.LONG:
        return round_to_tick(ref_price + offset, tick, ROUND_UP)
    return round_to_tick(ref_price - offset, tick, ROUND_DOWN)


def stop_limit_price(stop_price: Decimal, offset_pct: Decimal, *, side: Side = Side.LONG) -> Decimal:
    """止损限价单的保护限价：在止损触发价更"深"一侧留 ``offset_pct`` 缓冲，
    以便价格穿过触发价时仍能成交（代价：极端跳空可能不成交）。

    LONG 仓的止损是 SELL stop → 限价在触发价**下方**；SHORT 反之。
    """
    if not stop_price.is_finite() or stop_price <= 0:
        raise ValueError(f"stop_price must be positive finite, got {stop_price}")
    if not offset_pct.is_finite() or offset_pct < 0:
        raise ValueError(f"offset_pct must be >= 0 finite, got {offset_pct}")
    tick = min_tick(stop_price)
    if side is Side.LONG:
        limit = round_to_tick(stop_price * (Decimal(1) - offset_pct), tick, ROUND_DOWN)
    else:
        limit = round_to_tick(stop_price * (Decimal(1) + offset_pct), tick, ROUND_UP)
    if limit <= 0:
        raise ValueError(f"stop-limit 保护价非正（{limit}）")
    return limit


def shares_for_notional(notional_usd: Decimal, price: Decimal) -> int:
    """给定金额能买的整股数（向下取整）。"""
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")
    return int(notional_usd // price)


__all__ = [
    "BracketPrices",
    "NetEdge",
    "Target",
    "TargetKind",
    "min_tick",
    "round_to_tick",
    "target_delta",
    "compute_bracket",
    "net_edge",
    "aggressive_limit",
    "stop_limit_price",
    "shares_for_notional",
    "DEFAULT_COMMISSION_PER_SHARE",
    "DEFAULT_MIN_COMMISSION",
]
