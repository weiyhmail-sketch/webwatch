"""下单前风控 —— 纯函数，账户感知的硬/软检查。

设计取舍（money judgment，记录给后续 session）：
scalper 的 `risk.RiskManager`/`RiskState` 为**全自动 daemon** 设计，依赖 daily_pnl /
consecutive_stops / monthly_pnl / pending_entries / sector_map 等有状态跟踪，手动面板不维护
这些状态，强行构造 RiskState 易错（真金白银路径）。故本面板用**透明的逐单检查**：
- 单笔 notional 上限（在 order_service 已做）
- 单笔最大亏损 ≤ max_position_risk_pct × NAV（镜像 scalper MaxPositionRiskRule 的 0.5% 语义）
- 金额不超购买力
- PDT 提示（NAV < $25k 受日内交易限制）
手动面板里用户是决策者，风控做"硬拦截明显越界 + 软提示"，而非替代用户判断。

红线：金额一律 Decimal。改动本文件先改/加测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from scalper.strategy.base import Side

from webwatch.config import PanelConfig

BLOCK = "block"
WARN = "warn"

# IB 空头保证金的低价股下限：每股至少按 $2.50 计（IB 实际规则更复杂，这里取保守近似）。
# 多头不适用。
_SHORT_MIN_MARGIN_PER_SHARE = Decimal("2.50")


@dataclass(frozen=True)
class RiskFinding:
    code: str
    severity: str  # BLOCK | WARN
    message: str


def assess(
    *,
    entry: Decimal,
    stop_loss: Decimal,
    quantity: int,
    side: Side,
    nav: Decimal | None,
    buying_power: Decimal | None,
    panel: PanelConfig,
) -> list[RiskFinding]:
    """账户感知的下单前检查。BLOCK 应拒单，WARN 仅提示。"""
    findings: list[RiskFinding] = []
    # 防御深度：正常流里 plan 已挡掉非有限值，但本模块作为独立红线也必须自保——
    # 任何直接调用 assess() 的未来路径不能让 NaN/Inf 静默放行（NaN 的所有 `>` 比较为 False）。
    if not entry.is_finite() or not stop_loss.is_finite():
        return [RiskFinding("non_finite_price", BLOCK, f"价格非有限值（entry={entry}, stop={stop_loss}）")]
    # 同为防御深度：负/零数量使 notional 与 max_loss 全为负 → 所有 `>` 比较 False 静默放行。
    if quantity <= 0:
        return [RiskFinding("non_positive_quantity", BLOCK, f"数量必须为正：{quantity}")]
    notional = entry * quantity
    # 单笔最大亏损（多头：entry-stop；空头：stop-entry）
    max_loss = (entry - stop_loss) * quantity if side is Side.LONG else (stop_loss - entry) * quantity

    if nav is not None and panel.max_position_risk_pct > 0:
        limit = nav * panel.max_position_risk_pct
        if max_loss > limit:
            findings.append(
                RiskFinding(
                    "max_position_risk",
                    BLOCK,
                    f"单笔最大亏损 ${max_loss} 超过 {panel.max_position_risk_pct:.2%} NAV（${limit}）",
                )
            )

    # 购买力检查：多头按 notional；空头按保证金近似（低价股每股 $2.50 下限——
    # 按多头 notional 估会**低估**低价股空头保证金，漏拦超限单）。
    bp_required = notional
    if side is Side.SHORT:
        margin_base = entry if entry >= _SHORT_MIN_MARGIN_PER_SHARE else _SHORT_MIN_MARGIN_PER_SHARE
        bp_required = margin_base * quantity
        findings.append(
            RiskFinding(
                "short_margin_approx",
                WARN,
                f"空头保证金为近似估算（低价股按每股 ${_SHORT_MIN_MARGIN_PER_SHARE} 下限计 "
                f"${bp_required}），且未检查可借券(HTB)；实际以 IB 为准",
            )
        )
    if buying_power is not None and bp_required > buying_power:
        findings.append(
            RiskFinding(
                "buying_power",
                BLOCK,
                f"下单所需 ${bp_required}（{'空头保证金近似' if side is Side.SHORT else '金额'}）"
                f"超过购买力 ${buying_power}",
            )
        )

    if nav is not None and nav < panel.pdt_min_nav:
        findings.append(
            RiskFinding(
                "pdt",
                WARN,
                f"净值 ${nav} < ${panel.pdt_min_nav}，受 PDT 限制（5 日内最多 3 笔日内交易）",
            )
        )

    return findings


def finding_to_dict(f: RiskFinding) -> dict[str, str]:
    return {"code": f.code, "severity": f.severity, "message": f.message}


def blocks(findings: list[RiskFinding]) -> list[RiskFinding]:
    return [f for f in findings if f.severity == BLOCK]


__all__ = ["RiskFinding", "BLOCK", "WARN", "assess", "finding_to_dict", "blocks"]
