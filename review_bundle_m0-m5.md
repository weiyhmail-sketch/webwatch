# WebWatch Review Bundle — M0–M5（下单链路首轮复核）

> 交给 ChatGPT / 另一个 Claude session 做对抗式 code review。读完按"输出格式"逐条给 verdict。

---

## §A 角色与任务

### A.1 角色设定
你是 WebWatch 首轮 code reviewer，重点审 **真金白银下单链路**。本轮是 M0–M5 全部代码 +
一轮自查后的加固。Claude Code 已自查并修了若干 P0，本 bundle 请你**独立复核**：既验证修复是否真到位，
也挖 Claude 没发现的新盲区。

### A.2 项目背景（固定）
- IBKR **手动**超短线下单可视化面板（不是全自动机器人）。Python 3.12 / 单进程 asyncio /
  FastAPI + 浏览器前端 / `ib_async` SDK / IBKR **paper 端口 4002**（live 4001 当前**不启用**）。
- 复用姊妹项目 scalper 的执行层（`ib_broker_adapter` 连接/适配、`oca_group`、`types`、`risk` 等）作为
  git 依赖；本项目不改 scalper。
- 工具链：`pytest` + `mypy --strict` + `ruff`。
- 核心流程：输入代码 → 市价/限价买入 → **买入同时挂止盈止损**。市价买入用**真实成交价**算 TP/SL。
  止盈目标支持三种：百分比 / 每股价格偏移 / 总盈利金额。

### A.3 红线模块清单（webwatch）
- `src/webwatch/pricing.py` — TP/SL 计算 + tick 取整 + 扣费净 edge（纯函数）
- `src/webwatch/order_service.py` — 下单计划 + 输入校验 + notional 上限
- `src/webwatch/broker.py` — IB 连接 + 下单（限价 bracket / 市价+保护单 / 撤单 / 全平）
- `src/webwatch/risk.py` — 下单前账户感知风控
- `src/webwatch/app.py` — FastAPI 端点（下单/风控/撤单/全平）的下单分支

### A.4 审查目标
审查本轮改动的正确性，以及是否引入新的安全风险。**第一优先级红线**：
绝不允许"入场已成交但保护单未挂成功"的裸仓存在（market 路径必须 fill→挂保护，失败则立即平仓）。

### A.5 审查重点（本轮）
1. **裸仓风险**：`place_market_with_protection` 的所有失败分支是否都走到"撤遗留保护单 + 平仓"？
   `_verify_protection_live` 会不会误判"已挂活"？`_wait_done` 超时是否被静默当未成交？
   `_emergency_flatten` 自身用 IOC 市价单且**未校验成交**——这是已知薄弱点，请重点评估其后果。
2. **保护价正确性**：`compute_bracket` 的 LONG/SHORT 取整方向、负价/非有限值防御、tick 取整。
3. **风控绕过**：NaN/Inf 是否还能绕过 notional 上限和 risk BLOCK？risk 在账户读取失败(None)时是否静默放行？
4. **并发**：单个 IB 连接被多个 await 处理器共享，`_order_lock` 是否覆盖所有下单/平仓路径？
5. **flatten_all**：撤单→sleep(0.3)→平仓 的竞态与 stale 持仓，IOC 平仓未校验成交。

### A.6 反糊弄硬约束（一字不改）
1. 禁止措辞：「方向正确」「值得继续打磨」「在大部分情况下」「考虑」「建议进一步」「或许可以」
   「看起来不错」「质量不错」「整体合理」「有改善空间」。要么 PASS 要么 FAIL，要么具体 P0/P1。
2. 禁止推脱：「这是后续才做的」「paper 阶段不重要」「Claude 应该已处理」「按行业惯例」——用源码证明。
3. 每条 finding 必须 file:line + 代码片段，没有引用默认作废。
4. 任何一条 P0 存在 = 整体 verdict 必须 FAIL/PARTIAL。
5. 必须主动挖新盲区；找不到也要说明分析过程，不得直接写"未发现"。

### A.7 Verdict 评分（五档）
✅ PASS（真修了，逻辑正确，测试覆盖核心保护属性）/ ⚠️ PARTIAL（留剩余风险）/
🚧 WORKAROUND（弱实现绕过原意）/ 🆕 NEW BUG（本轮引入新 bug）/ ❌ NOT FIXED（没改/改错）

### A.8 输出格式
- §A 总评（PASS/FAIL + 一句话原因）
- §B 逐条修复对账（对本 bundle §B 的每条修复给 verdict + file:line 引用 + 说明）
- §C NEW BUGS（本轮引入的新 bug + 你挖到的新盲区，尤其针对 A.5 重点）
- §D 已知限制评估（对本 bundle §B-2 列出的未修项，逐条给"可接受/必须修"判断）
- §E 最终建议（能否进入 paper 验证；切 live 前还缺什么）

### A.9 安全约束
- 不得暴露 secrets/账户号/token；不得建议把 paper 4002 改成 live 4001；不得建议跳过测试或降覆盖率。

---

## §B 本轮自查发现与修复对账

Claude Code 用 3 个独立 finder agent 对下单链路做了 high-recall 自查，发现并**已修复**下列问题。
请你独立复核每条是否真修到位（不要默认 Claude 说修了就修了）。

### B-1 已修复（请复核）

| # | 严重度 | 位置 | 问题 | 修复 |
|---|---|---|---|---|
| 1 | P0 | pricing.py / order_service.py | **NaN/Inf 绕过所有护栏**：`Decimal('nan')` 是合法 Decimal，`x<=0`、`notional>cap`、risk 的所有 `>` 比较对 NaN 全为 False → 带 NaN 价的单可直达 IB | 在 `compute_bracket`/`target_delta`/两个 plan 函数加 `is_finite()` 校验；非有限值直接拒绝 |
| 2 | P0 | pricing.py `compute_bracket` | **目标过大算出负/零保护价**（如 $5 股、10 股、profit_usd $100 → SL=5-10=-5），负价发到 IB 会被拒 → 裸仓 | 计算后校验 `tp_price>0 and sl_price>0`，否则抛 ValueError；plan 层转成 OrderRejected |
| 3 | P0 | broker.py `place_market_with_protection` | **入场单超时被静默当未成交**：`_wait_done` 超时后读 `filled=0` 返回 no-op；若实际已成交 = 裸仓且系统以为啥也没发生 | `_wait_done` 改返回 bool；超时 → 抛 RuntimeError 大声告警让用户查持仓 |
| 4 | P0 | broker.py `_verify_protection_live` | **误判保护单已挂活**：OK 状态集含 `PendingSubmit/ApiPending/ApiUpdate`（尚未上簿，可能随后被拒）→ 误判为已保护 | OK 集收紧为 `{PreSubmitted, Submitted, Filled}`；瞬态继续等，超时则 fail-closed→平仓 |
| 5 | P0 | broker.py `_place_oca_protection` | **单腿遗留**：止盈挂出后止损挂单抛错 → 调用方平仓但止盈仍 live → 平仓后可能反向开新裸仓 | 止损腿抛错时先撤止盈腿再抛；调用方 except 里 `_cancel_trades(prot_trades)` 撤掉两腿再平仓 |
| 6 | P1 | broker.py `place_market_with_protection` | **成交价 NaN/<=0** 但已成交：`avg<=0` 对 NaN 为 False → NaN 流入 compute_bracket | `math.isfinite(avg) and avg>0` 校验；有仓位但价无效 → 立即平仓 + ProtectionFailed |
| 7 | P1 | order_service.py `plan_limit_bracket` | **限价不在 tick 上**（如 10.005）→ IB 拒母单 | 下单前 `round_to_tick(entry_limit, min_tick(...))` |
| 8 | P1 | broker.py | **并发下单交叉**：单 IB 连接被多 await 处理器共享，无锁 | 加 `asyncio.Lock`，包住 place_limit_bracket / place_market_with_protection / flatten_all |

每条都新增了回归测试（见 §D）。当前 91 测试全绿。

### B-2 已知限制（**本轮未修**，请你判断"可接受 / 必须切 live 前修"）

1. **`_emergency_flatten` 用 IOC 市价单且未校验成交**：极端流动性差/闭市时 IOC 可能 0 成交，
   平仓"成功"返回但仓位仍在 = 仍可能裸仓。目前靠 ProtectionFailed 大声告警，但未验证平仓真成交、未重试。
2. **`flatten_all` 竞态**：cancel→sleep(0.3)→list_positions→IOC 平仓；0.3s 内保护单可能成交导致
   持仓 stale；IOC 平仓未校验成交。panic 按钮，best-effort。
3. **risk 的 max_loss 是毛损**（未计佣金/费）；空头按 entry×qty 估购买力，非真实做空保证金。
4. **risk 在账户读取失败时静默放行**：`risk_inputs()` 返回 (None,None) → 所有 BLOCK 跳过
   （仅 order_service 的 notional 上限仍生效）。是否该 fail-closed？
5. **ProtectionFailed 返回 HTTP 200**（带 `protection_failed:true`）；当前前端已专门处理并红字告警，
   但未来纯 API 调用方若只看 status code 会误判成功。
6. **market 路径风控用 ref_price 估算**（真实成交价可能更差）；用户已接受该设计。

---

## §C 钱相关模块完整源码

（见下方按文件分节。git 本轮刚初始化、尚无 commit；全部为新增文件。）

---

## §D 测试清单（与本轮相关）

下单链路测试文件：
- `tests/unit/test_pricing.py` — TP/SL 多模式、tick、低价股<1tick、负价/NaN 防御、净 edge
- `tests/unit/test_order_service.py` — 计划、notional 上限、NaN/Inf 拒绝、tick 取整、超大目标拒绝
- `tests/unit/test_risk.py` — max_loss(多/空)、购买力、PDT、无账户数据
- `tests/unit/test_broker.py` — 限价 bracket、市价 fill→OCA、未成交、保护单 Cancelled→平仓、
  保护单抛错→平仓、**止损腿抛错撤止盈腿**、**成交价无效→平仓**、**入场超时大声抛错**、撤单/全平/risk_inputs
- `tests/unit/test_app.py` — 预览/下单端点、风控拦截不下单、ProtectionFailed 上抛、撤单/全平端点
- `tests/unit/test_serialize.py` — 序列化

---

## §E Commit 历史
git 仓库本轮刚 `git init`，**尚无 commit**（用户尚未要求提交）。全部代码为工作区新增。

---

## §F 元信息
- 生成时间：2026-06-08 20:14:51 CST
- 分支：main（无 commit）
- 测试：**91 passed**（0 failed / 0 skipped）
- `mypy --strict`：通过；`ruff`：通过
- 覆盖率：TOTAL **81%**（pricing 95% / order_service 92% / risk 100% / serialize 100% /
  config 89% / app 83% / broker 66%——broker 未覆盖部分是 connect()/行情订阅/IB 事件等需活连接的路径，
  下单逻辑已覆盖）


---

## §C 源码（钱相关模块全文）

### `src/webwatch/pricing.py`

```python
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
    "shares_for_notional",
    "DEFAULT_COMMISSION_PER_SHARE",
    "DEFAULT_MIN_COMMISSION",
]

```

### `src/webwatch/order_service.py`

```python
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

```

### `src/webwatch/risk.py`

```python
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

    if buying_power is not None and notional > buying_power:
        findings.append(
            RiskFinding(
                "buying_power",
                BLOCK,
                f"下单金额 ${notional} 超过购买力 ${buying_power}",
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

```

### `src/webwatch/broker.py`

```python
"""券商连接管理 —— 持有单个持久 IB 连接，复用 scalper 的连接层与适配器。

M1 范围：只读（连接 / 账户 / 持仓 / 挂单 / 实时报价）。下单在 M3/M4 加。

关键：webwatch 自己 ``ib = IB()`` 后连接，再 ``IbBrokerAdapter(ib, ...)``，
因此持有**同一个 IB 实例**——模式B 的 ``ib.bracketOrder`` 可直接用（M3）。

设计为对断连**容错**：未连上时各读取方法返回空，``snapshot()`` 标记 connected=False，
前端据此显示状态，而非崩溃。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from decimal import Decimal
from typing import Any, Protocol, TypeVar

from ib_async import IB, LimitOrder, MarketOrder, Stock, StopOrder, Ticker
from scalper.execution.ib_broker_adapter import (
    IbBrokerAdapter,
    connect_live_async,
    connect_paper_async,
)
from scalper.execution.oca_group import DEFAULT_OCA_TYPE, make_oca_group
from scalper.execution.types import OrderId
from scalper.strategy.base import Side

from webwatch.config import Environment, IBKRSettings, PanelConfig
from webwatch.pricing import BracketPrices, Target, compute_bracket
from webwatch.serialize import account_to_dict, order_to_dict, position_to_dict

log = logging.getLogger(__name__)

T = TypeVar("T")

# secrets 里未填真实账户时的占位值（见 secrets.env.example）。
_PLACEHOLDER_ACCOUNTS = frozenset({"", "DU0000000"})

# IBKR 的"信息/状态"类 errorEvent 码（连接 OK、数据农场状态等），非真错误，不展示给用户。
# 202 = 委托单被取消：我们主动撤单时的正常回执（超短线撤单频繁），不当错误弹。
_BENIGN_IB_CODES = frozenset(
    {202, 1100, 1101, 1102, 2103, 2104, 2105, 2106, 2107, 2108, 2109, 2110, 2119, 2137, 2150, 2158, 2168, 2169}
)


# 保护单确认"已挂活"只认 IB 已接受上簿的状态。
# 不含 PendingSubmit/ApiPending/ApiUpdate —— 那些是"尚未上簿"，可能随后被拒，
# 误判为已挂活会留下没保护的裸仓。这些瞬态在 _verify 里继续等，超时则 fail-closed→平仓。
_PROTECTION_OK_STATES = frozenset({"PreSubmitted", "Submitted", "Filled"})
_PROTECTION_BAD_STATES = frozenset({"Cancelled", "ApiCancelled", "Inactive", "ValidationError"})


class ProtectionFailed(Exception):
    """红线 #3：入场已成交但保护单(TP/SL)未挂成功，已紧急平仓。"""

    def __init__(self, *, filled: int, fill_price: Decimal, reason: str, flatten: dict[str, Any]) -> None:
        super().__init__(f"保护单失败已平仓: {reason}")
        self.filled = filled
        self.fill_price = fill_price
        self.reason = reason
        self.flatten = flatten


def resolve_account(configured: str, managed: list[str]) -> str:
    """决定适配器用哪个账户做 scoping。

    已配置真实账户 → 用它；否则（空/占位符）回退到 Gateway 返回的
    managedAccounts 首个，让用户不填账户号面板也能正常显示。
    """
    if configured and configured not in _PLACEHOLDER_ACCOUNTS:
        return configured
    return managed[0] if managed else configured


class BrokerLike(Protocol):
    """app 依赖的最小券商接口（测试可注入 fake）。"""

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
    async def watch(self, symbol: str) -> None: ...
    def unwatch(self, symbol: str) -> None: ...
    def snapshot(self) -> dict[str, Any]: ...
    async def place_limit_bracket(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: int,
        entry_limit: Decimal,
        take_profit: Decimal,
        stop_loss: Decimal,
    ) -> dict[str, Any]: ...
    async def place_market_with_protection(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: int,
        take_profit: Target,
        stop_loss: Target,
    ) -> dict[str, Any]: ...
    def risk_inputs(self) -> tuple[Decimal | None, Decimal | None]: ...
    def cancel_all_orders(self) -> dict[str, Any]: ...
    async def flatten_all(self) -> dict[str, Any]: ...


def _num(value: float | None) -> float | None:
    """把 ib_async ticker 的 nan/None 归一成 None（方便 JSON / 前端）。"""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


class BrokerManager:
    """真实 IBKR 连接管理。"""

    def __init__(self, settings: IBKRSettings, panel: PanelConfig) -> None:
        self._settings = settings
        self._market_data_type = panel.market_data_type
        self._ib: IB | None = None
        self._adapter: IbBrokerAdapter | None = None
        self._watchlist: list[str] = []  # 保持添加顺序
        self._contracts: dict[str, Stock] = {}
        self._tickers: dict[str, Ticker] = {}
        self._last_error: str | None = None
        # 串行化下单/平仓操作：单个 IB 连接被多个 await 处理器共享，
        # 防止并发下单交叉（OCA 组错乱、撤单撤到刚挂的保护单等）。
        self._order_lock = asyncio.Lock()
        # 成交等待 / 保护单确认超时（秒）。测试可调小。
        self._fill_timeout_s = 5.0
        self._protect_timeout_s = 2.0

    # ---- 连接生命周期 ---------------------------------------------------

    def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    async def connect(self) -> None:
        """连接 Gateway（按环境选 paper/live），并重订阅 watchlist。"""
        s = self._settings
        ib = IB()
        try:
            if s.environment is Environment.PAPER:
                await connect_paper_async(ib, s.ibkr_host, s.ibkr_port, s.ibkr_client_id)
            else:
                await connect_live_async(
                    ib,
                    s.ibkr_host,
                    s.ibkr_port,
                    s.ibkr_client_id,
                    expected_account=s.ibkr_account or None,
                )
            ib.errorEvent += self._on_ib_error
            ib.reqMarketDataType(self._market_data_type)
            self._ib = ib
            # 未填真实账户时自动用 Gateway 探测到的账户做 scoping。
            account = resolve_account(s.ibkr_account, list(ib.managedAccounts()))
            self._adapter = IbBrokerAdapter(ib, paper_account=account)
            self._last_error = None
            # 重新订阅之前 watch 的标的
            for sym in list(self._watchlist):
                await self._subscribe(sym)
            log.info("connected to %s gateway", s.environment.value)
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            log.warning("connect failed: %s", self._last_error)
            if ib.isConnected():
                ib.disconnect()
            self._ib = None
            self._adapter = None

    async def disconnect(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()
        self._ib = None
        self._adapter = None

    def _on_ib_error(
        self,
        req_id: int,
        error_code: int,
        error_string: str,
        contract: Any = None,
    ) -> None:
        """把 IBKR 真错误（如 10197 无市场数据、354 未订阅）抓到 last_error 供前端展示。"""
        if error_code in _BENIGN_IB_CODES:
            return
        sym = f" [{contract.symbol}]" if contract is not None and hasattr(contract, "symbol") else ""
        self._last_error = f"IB {error_code}:{sym} {error_string}"
        log.warning("IB error %s: %s", error_code, error_string)

    # ---- 报价订阅 -------------------------------------------------------

    async def watch(self, symbol: str) -> None:
        sym = symbol.strip().upper()
        if not sym:
            return
        if sym not in self._watchlist:
            self._watchlist.append(sym)
        if self.is_connected():
            await self._subscribe(sym)

    async def _subscribe(self, sym: str) -> None:
        assert self._ib is not None
        try:
            contract = Stock(sym, "SMART", "USD")
            await self._ib.qualifyContractsAsync(contract)
            self._contracts[sym] = contract
            self._tickers[sym] = self._ib.reqMktData(contract, "", False, False)
        except Exception as exc:  # noqa: BLE001 — 单个标的订阅失败不应拖垮面板
            self._last_error = f"watch {sym}: {type(exc).__name__}: {exc}"
            log.warning(self._last_error)

    def unwatch(self, symbol: str) -> None:
        sym = symbol.strip().upper()
        if sym in self._watchlist:
            self._watchlist.remove(sym)
        contract = self._contracts.pop(sym, None)
        self._tickers.pop(sym, None)
        if contract is not None and self._ib is not None and self._ib.isConnected():
            try:
                self._ib.cancelMktData(contract)
            except Exception as exc:  # noqa: BLE001
                log.warning("cancelMktData %s: %s", sym, exc)

    def _quotes(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for sym in self._watchlist:
            t = self._tickers.get(sym)
            if t is None:
                out.append({"symbol": sym, "bid": None, "ask": None, "last": None, "close": None})
                continue
            out.append(
                {
                    "symbol": sym,
                    "bid": _num(t.bid),
                    "ask": _num(t.ask),
                    "last": _num(t.last),
                    "close": _num(t.close),
                }
            )
        return out

    # ---- 状态快照 -------------------------------------------------------

    def _safe(self, fn: Any) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            log.warning("read failed: %s", self._last_error)
            return None

    def snapshot(self) -> dict[str, Any]:
        """组装前端所需的完整只读状态。"""
        connected = self.is_connected()
        account: dict[str, Any] | None = None
        positions: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        if connected and self._adapter is not None:
            a = self._safe(self._adapter.account_summary)
            account = account_to_dict(a) if a is not None else None
            positions = [position_to_dict(p) for p in (self._safe(self._adapter.list_positions) or [])]
            orders = [order_to_dict(o) for o in (self._safe(self._adapter.list_open_orders) or [])]
        return {
            "connected": connected,
            "environment": self._settings.environment.value,
            "market_data_type": self._market_data_type,
            "error": self._last_error,
            "account": account,
            "positions": positions,
            "orders": orders,
            "quotes": self._quotes(),
            "watchlist": list(self._watchlist),
        }

    # ---- 下单（M3：限价 + 模式B 原生 bracket）---------------------------

    async def place_limit_bracket(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: int,
        entry_limit: Decimal,
        take_profit: Decimal,
        stop_loss: Decimal,
    ) -> dict[str, Any]:
        """模式B：用 ib_async 原生 bracket 一次性发出 母单(限价) + 止盈(限价) + 止损(stop)。

        三条单经 ib.bracketOrder 自带 parentId + transmit 链，保护单零空窗。
        ``transmit``：parent/TP=False、SL=True → 全部一起 transmit。
        """
        if not self.is_connected() or self._ib is None:
            raise RuntimeError("未连接 Gateway，无法下单")
        ib = self._ib
        action = "BUY" if side is Side.LONG else "SELL"
        contract = Stock(symbol.strip().upper(), "SMART", "USD")
        async with self._order_lock:
            await ib.qualifyContractsAsync(contract)
            # ib_async 用 float；内部仍以 Decimal 计算，仅在此 IB 边界转 float。
            bracket = ib.bracketOrder(
                action,
                quantity,
                float(entry_limit),
                float(take_profit),
                float(stop_loss),
            )
            for order in bracket:
                ib.placeOrder(contract, order)
            return {
                "symbol": contract.symbol,
                "side": side.value,
                "quantity": quantity,
                "entry_limit": str(entry_limit),
                "take_profit": str(take_profit),
                "stop_loss": str(stop_loss),
                "parent_id": bracket.parent.orderId,
                "take_profit_id": bracket.takeProfit.orderId,
                "stop_loss_id": bracket.stopLoss.orderId,
            }

    # ---- 下单（M4：市价 + 模式A 成交后精确挂 OCA）----------------------

    async def place_market_with_protection(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: int,
        take_profit: Target,
        stop_loss: Target,
    ) -> dict[str, Any]:
        """模式A：市价(IOC)买入 → 拿真实成交价算 TP/SL → 挂 OCA 兄弟单。

        红线 #3：若保护单未挂成功（异常 / 进入终态异常状态），**立即市价平仓** +
        抛 ``ProtectionFailed``，绝不留裸仓。
        """
        if not self.is_connected() or self._ib is None:
            raise RuntimeError("未连接 Gateway，无法下单")
        ib = self._ib
        action = "BUY" if side is Side.LONG else "SELL"
        contract = Stock(symbol.strip().upper(), "SMART", "USD")
        await ib.qualifyContractsAsync(contract)

        async with self._order_lock:
            entry = MarketOrder(action, quantity)
            entry.tif = "IOC"  # 与 scalper 一致：市价单不留挂单残余
            entry_trade = ib.placeOrder(contract, entry)
            done = await self._wait_done(entry_trade, timeout=self._fill_timeout_s)

            # 超时未达终态：入场单状态未知（可能已成交）。绝不静默当作未成交，
            # 否则真成交了却不挂保护单 = 裸仓。大声抛错让用户去查持仓/挂单。
            if not done:
                raise RuntimeError(
                    f"入场单超时未达终态（status={entry_trade.orderStatus.status}），"
                    f"状态未知，请立即检查持仓与挂单！"
                )

            filled = int(entry_trade.orderStatus.filled or 0)
            avg = float(entry_trade.orderStatus.avgFillPrice or 0.0)
            if filled <= 0:
                return {
                    "filled": 0,
                    "status": entry_trade.orderStatus.status,
                    "entry_order_id": entry.orderId,
                    "message": "市价单未成交（IOC 已撤）",
                }

            # 有成交但成交价无效（NaN/<=0）：有仓位却无法算保护单 → 立即平仓。
            if not math.isfinite(avg) or avg <= 0:
                flatten = self._emergency_flatten(contract, side, filled)
                raise ProtectionFailed(
                    filled=filled,
                    fill_price=Decimal("0"),
                    reason=f"成交价无效（{avg}），无法计算保护单",
                    flatten=flatten,
                )

            fill_price = Decimal(str(avg))
            bracket = compute_bracket(fill_price, filled, take_profit, stop_loss, side=side)

            prot_trades: list[Any] = []
            try:
                group, tp_order, sl_order, prot_trades = self._place_oca_protection(
                    contract, side, filled, bracket, entry.orderId
                )
                await self._verify_protection_live(prot_trades, timeout=self._protect_timeout_s)
            except Exception as exc:  # noqa: BLE001 — 任何保护单失败都必须平仓
                # 先撤掉任何已挂出的保护单（避免遗留单腿在平仓后反向开新裸仓），再平仓。
                self._cancel_trades(prot_trades)
                flatten = self._emergency_flatten(contract, side, filled)
                raise ProtectionFailed(
                    filled=filled, fill_price=fill_price, reason=str(exc), flatten=flatten
                ) from exc

            return {
                "filled": filled,
                "fill_price": str(fill_price),
                "symbol": contract.symbol,
                "side": side.value,
                "entry_order_id": entry.orderId,
                "take_profit": str(bracket.take_profit),
                "stop_loss": str(bracket.stop_loss),
                "oca_group": group,
                "take_profit_id": tp_order.orderId,
                "stop_loss_id": sl_order.orderId,
                "tp_below_min_tick": bracket.tp_below_min_tick,
            }

    async def _wait_done(self, trade: Any, timeout: float = 5.0, interval: float = 0.05) -> bool:
        """轮询等待订单到终态。返回 True=已达终态，False=超时未达终态。"""
        for _ in range(max(1, int(timeout / interval))):
            if trade.isDone():
                return True
            await asyncio.sleep(interval)
        return bool(trade.isDone())

    def _cancel_trades(self, trades: list[Any]) -> None:
        """尽力撤掉给定 trade 的订单（用于保护单失败时清理遗留单腿）。"""
        if self._ib is None:
            return
        for t in trades:
            with contextlib.suppress(Exception):
                self._ib.cancelOrder(t.order)

    def _place_oca_protection(
        self,
        contract: Any,
        side: Side,
        quantity: int,
        bracket: BracketPrices,
        entry_order_id: int,
    ) -> tuple[str, Any, Any, list[Any]]:
        """挂 OCA 兄弟单：止盈(限价) + 止损(stop)，一成交即撤另一个。

        若第二腿(止损)挂单抛错，先撤掉已挂出的第一腿(止盈)再抛，避免遗留单腿。
        """
        assert self._ib is not None
        ib = self._ib
        group = make_oca_group(OrderId(f"ww-{entry_order_id}"))
        close_action = "SELL" if side is Side.LONG else "BUY"

        tp_order = LimitOrder(close_action, quantity, float(bracket.take_profit))
        tp_order.ocaGroup = group
        tp_order.ocaType = DEFAULT_OCA_TYPE
        tp_order.tif = "GTC"

        sl_order = StopOrder(close_action, quantity, float(bracket.stop_loss))
        sl_order.ocaGroup = group
        sl_order.ocaType = DEFAULT_OCA_TYPE
        sl_order.tif = "GTC"

        tp_trade = ib.placeOrder(contract, tp_order)
        try:
            sl_trade = ib.placeOrder(contract, sl_order)
        except Exception:
            with contextlib.suppress(Exception):
                ib.cancelOrder(tp_order)
            raise
        return group, tp_order, sl_order, [tp_trade, sl_trade]

    async def _verify_protection_live(
        self, trades: list[Any], timeout: float = 2.0, interval: float = 0.05
    ) -> None:
        """确认两条保护单都挂活；任一进入终态异常或超时未确认 → 抛错（fail-closed）。"""
        for _ in range(max(1, int(timeout / interval))):
            statuses = [t.orderStatus.status for t in trades]
            if any(s in _PROTECTION_BAD_STATES for s in statuses):
                raise RuntimeError(f"保护单进入异常状态: {statuses}")
            if all(s in _PROTECTION_OK_STATES for s in statuses):
                return
            await asyncio.sleep(interval)
        statuses = [t.orderStatus.status for t in trades]
        if not all(s in _PROTECTION_OK_STATES for s in statuses):
            raise RuntimeError(f"保护单未确认挂出（超时）: {statuses}")

    # ---- 风控输入 / 撤单 / 全平（M5）-----------------------------------

    def risk_inputs(self) -> tuple[Decimal | None, Decimal | None]:
        """供下单前风控用的 (NAV, 购买力)；未连接或读取失败返回 (None, None)。"""
        if not self.is_connected() or self._adapter is None:
            return (None, None)
        summary = self._safe(self._adapter.account_summary)
        if summary is None:
            return (None, None)
        return (summary.net_liquidation, summary.buying_power)

    def cancel_all_orders(self) -> dict[str, Any]:
        """撤掉所有挂单（只撤单，不平仓）。"""
        if not self.is_connected() or self._adapter is None:
            raise RuntimeError("未连接 Gateway")
        self._adapter.cancel_all(None)
        return {"cancelled": True}

    async def flatten_all(self) -> dict[str, Any]:
        """市价全平：先撤所有挂单（含保护单，避免平仓后保护单又开新仓），再市价平掉每个持仓。"""
        if not self.is_connected() or self._ib is None or self._adapter is None:
            raise RuntimeError("未连接 Gateway")
        ib = self._ib
        async with self._order_lock:
            self._adapter.cancel_all(None)
            await asyncio.sleep(0.3)
            closed: list[dict[str, Any]] = []
            # 撤单传播后重读一次持仓，尽量用最新数量（降低 stale 数量风险）。
            for pos in self._adapter.list_positions():
                close_action = "SELL" if pos.side is Side.LONG else "BUY"
                contract = Stock(pos.symbol, "SMART", "USD")
                await ib.qualifyContractsAsync(contract)
                order = MarketOrder(close_action, pos.quantity)
                order.tif = "IOC"
                ib.placeOrder(contract, order)
                closed.append(
                    {"symbol": pos.symbol, "action": close_action, "quantity": pos.quantity}
                )
            log.warning("flatten_all: 平掉 %d 个持仓", len(closed))
            return {"flattened": closed}

    def _emergency_flatten(self, contract: Any, side: Side, quantity: int) -> dict[str, Any]:
        """红线 #3：市价平掉刚成交的仓位，不留裸仓。"""
        assert self._ib is not None
        close_action = "SELL" if side is Side.LONG else "BUY"
        flat = MarketOrder(close_action, quantity)
        flat.tif = "IOC"
        self._ib.placeOrder(contract, flat)
        log.error(
            "PROTECTION FAILED → 紧急平仓 %s %s x%s", contract.symbol, close_action, quantity
        )
        return {"flatten_order_id": flat.orderId, "action": close_action, "quantity": quantity}


__all__ = ["BrokerLike", "BrokerManager", "ProtectionFailed", "resolve_account"]

```

### `src/webwatch/app.py`

```python
"""FastAPI 面板后端 —— M1 只读：报价 / 持仓 / 挂单 / 账户，WebSocket 推送。

启动：``uv run uvicorn webwatch.app:app --reload``（默认 paper，best-effort 连接）。
下单路由在 M3/M4 加。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from scalper.strategy.base import Side

from webwatch.broker import BrokerLike, BrokerManager, ProtectionFailed
from webwatch.config import Environment, PanelConfig, load_panel_config, load_settings
from webwatch.order_service import (
    MarketOrderPlan,
    OrderRejected,
    market_plan_to_dict,
    plan_limit_bracket,
    plan_market_order,
    plan_to_dict,
)
from webwatch.pricing import Target, TargetKind
from webwatch.risk import assess as risk_assess
from webwatch.risk import blocks as risk_blocks
from webwatch.risk import finding_to_dict

log = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent / "web"
BROADCAST_INTERVAL_S = 0.3  # 报价/状态推送间隔（秒）


class WatchRequest(BaseModel):
    symbol: str


class LimitOrderRequest(BaseModel):
    symbol: str
    quantity: int
    entry_limit: str  # Decimal 字符串，避免 float 精度
    tp_kind: str = "pct"
    tp_value: str
    sl_kind: str = "pct"
    sl_value: str
    side: str = "long"


class MarketOrderRequest(BaseModel):
    symbol: str
    quantity: int
    ref_price: str  # 当前参考价（前端报价），用于下单前风控 + 预览
    tp_kind: str = "pct"
    tp_value: str
    sl_kind: str = "pct"
    sl_value: str
    side: str = "long"


def _market_plan_from_request(body: MarketOrderRequest, panel: PanelConfig) -> MarketOrderPlan:
    try:
        ref = Decimal(body.ref_price)
        take_profit = Target(TargetKind(body.tp_kind), Decimal(body.tp_value))
        stop_loss = Target(TargetKind(body.sl_kind), Decimal(body.sl_value))
        side = Side(body.side)
    except (InvalidOperation, ValueError) as exc:
        raise OrderRejected(f"参数非法: {exc}") from exc
    return plan_market_order(
        symbol=body.symbol,
        quantity=body.quantity,
        ref_price=ref,
        take_profit=take_profit,
        stop_loss=stop_loss,
        panel=panel,
        side=side,
    )


def _risk_for(
    broker_: BrokerLike,
    entry: Decimal,
    stop_loss: Decimal,
    quantity: int,
    side: Side,
    panel: PanelConfig,
) -> Any:
    """取账户 NAV/购买力，跑下单前风控，返回 findings 列表。"""
    nav, buying_power = broker_.risk_inputs()
    return risk_assess(
        entry=entry,
        stop_loss=stop_loss,
        quantity=quantity,
        side=side,
        nav=nav,
        buying_power=buying_power,
        panel=panel,
    )


def _risk_block_response(findings: Any) -> JSONResponse | None:
    """有 BLOCK 级风控则返回 400 响应，否则 None。"""
    blocking = risk_blocks(findings)
    if blocking:
        return JSONResponse(
            {
                "rejected": True,
                "reason": "风控拒绝: " + "；".join(f.message for f in blocking),
                "risk": [finding_to_dict(f) for f in findings],
            },
            status_code=400,
        )
    return None


def _plan_from_request(body: LimitOrderRequest, panel: PanelConfig) -> Any:
    """把请求解析成 OrderPlan（参数非法或风控拒绝抛 OrderRejected）。"""
    try:
        entry = Decimal(body.entry_limit)
        take_profit = Target(TargetKind(body.tp_kind), Decimal(body.tp_value))
        stop_loss = Target(TargetKind(body.sl_kind), Decimal(body.sl_value))
        side = Side(body.side)
    except (InvalidOperation, ValueError) as exc:
        raise OrderRejected(f"参数非法: {exc}") from exc
    return plan_limit_bracket(
        symbol=body.symbol,
        quantity=body.quantity,
        entry_limit=entry,
        take_profit=take_profit,
        stop_loss=stop_loss,
        panel=panel,
        side=side,
    )


class ConnectionManager:
    """WebSocket 连接池 + 广播。"""

    def __init__(self) -> None:
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.active.discard(ws)

    async def broadcast(self, data: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.active):
            try:
                await ws.send_json(data)
            except Exception:  # noqa: BLE001 — 单个连接发送失败就剔除
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


async def _broadcast_loop(broker: BrokerLike, manager: ConnectionManager) -> None:
    while True:
        await asyncio.sleep(BROADCAST_INTERVAL_S)
        if not manager.active:
            continue
        try:
            state = broker.snapshot()
        except Exception:  # noqa: BLE001
            log.exception("snapshot failed in broadcast loop")
            continue
        await manager.broadcast(state)


def create_app(broker: BrokerLike | None = None, *, auto_connect: bool = True) -> FastAPI:
    """构建 app。测试可注入 fake broker 并关掉 auto_connect。"""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        panel = load_panel_config()
        b: BrokerLike = broker if broker is not None else BrokerManager(
            load_settings(Environment.PAPER), panel
        )
        app.state.broker = b
        app.state.panel = panel
        if auto_connect:
            with contextlib.suppress(Exception):
                await b.connect()
        manager = ConnectionManager()
        app.state.ws_manager = manager
        task = asyncio.create_task(_broadcast_loop(b, manager))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            with contextlib.suppress(Exception):
                await b.disconnect()

    app = FastAPI(title="WebWatch 下单面板", lifespan=lifespan)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_WEB_DIR / "index.html")

    @app.get("/api/state")
    async def state(request: Request) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        return JSONResponse(broker_.snapshot())

    @app.post("/api/watch")
    async def add_watch(request: Request, body: WatchRequest) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        await broker_.watch(body.symbol)
        return JSONResponse(broker_.snapshot())

    @app.delete("/api/watch/{symbol}")
    async def remove_watch(request: Request, symbol: str) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        broker_.unwatch(symbol)
        return JSONResponse(broker_.snapshot())

    @app.post("/api/reconnect")
    async def reconnect(request: Request) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        await broker_.connect()
        return JSONResponse({"connected": broker_.is_connected()})

    @app.post("/api/order/preview")
    async def order_preview(request: Request, body: LimitOrderRequest) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        panel: PanelConfig = request.app.state.panel
        try:
            plan = _plan_from_request(body, panel)
        except OrderRejected as exc:
            return JSONResponse({"rejected": True, "reason": exc.reason})
        findings = _risk_for(
            broker_, plan.entry_limit, plan.bracket.stop_loss, plan.quantity, plan.side, panel
        )
        return JSONResponse(
            {
                "rejected": False,
                "plan": plan_to_dict(plan),
                "risk": [finding_to_dict(f) for f in findings],
            }
        )

    @app.post("/api/order/limit")
    async def order_limit(request: Request, body: LimitOrderRequest) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        panel: PanelConfig = request.app.state.panel
        try:
            plan = _plan_from_request(body, panel)
        except OrderRejected as exc:
            return JSONResponse({"rejected": True, "reason": exc.reason}, status_code=400)
        findings = _risk_for(
            broker_, plan.entry_limit, plan.bracket.stop_loss, plan.quantity, plan.side, panel
        )
        blocked = _risk_block_response(findings)
        if blocked is not None:
            return blocked
        try:
            placement = await broker_.place_limit_bracket(
                symbol=plan.symbol,
                side=plan.side,
                quantity=plan.quantity,
                entry_limit=plan.entry_limit,
                take_profit=plan.bracket.take_profit,
                stop_loss=plan.bracket.stop_loss,
            )
        except Exception as exc:  # noqa: BLE001 — 下单失败回报给前端，不崩
            return JSONResponse(
                {"rejected": True, "reason": f"下单失败: {exc}"}, status_code=502
            )
        return JSONResponse(
            {
                "rejected": False,
                "plan": plan_to_dict(plan),
                "placement": placement,
                "risk": [finding_to_dict(f) for f in findings],
            }
        )

    @app.post("/api/order/market/preview")
    async def market_preview(request: Request, body: MarketOrderRequest) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        panel: PanelConfig = request.app.state.panel
        try:
            plan = _market_plan_from_request(body, panel)
        except OrderRejected as exc:
            return JSONResponse({"rejected": True, "reason": exc.reason})
        findings = _risk_for(
            broker_, plan.ref_price, plan.preview_bracket.stop_loss, plan.quantity, plan.side, panel
        )
        return JSONResponse(
            {
                "rejected": False,
                "plan": market_plan_to_dict(plan),
                "risk": [finding_to_dict(f) for f in findings],
            }
        )

    @app.post("/api/order/market")
    async def order_market(request: Request, body: MarketOrderRequest) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        panel: PanelConfig = request.app.state.panel
        try:
            plan = _market_plan_from_request(body, panel)
        except OrderRejected as exc:
            return JSONResponse({"rejected": True, "reason": exc.reason}, status_code=400)
        findings = _risk_for(
            broker_, plan.ref_price, plan.preview_bracket.stop_loss, plan.quantity, plan.side, panel
        )
        blocked = _risk_block_response(findings)
        if blocked is not None:
            return blocked
        try:
            placement = await broker_.place_market_with_protection(
                symbol=plan.symbol,
                side=plan.side,
                quantity=plan.quantity,
                take_profit=plan.take_profit,
                stop_loss=plan.stop_loss,
            )
        except ProtectionFailed as exc:
            # 红线：已成交但保护单失败，已紧急平仓 —— 醒目告知用户
            return JSONResponse(
                {
                    "rejected": False,
                    "protection_failed": True,
                    "reason": exc.reason,
                    "filled": exc.filled,
                    "fill_price": str(exc.fill_price),
                    "flatten": exc.flatten,
                }
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"rejected": True, "reason": f"下单失败: {exc}"}, status_code=502
            )
        return JSONResponse(
            {
                "rejected": False,
                "plan": market_plan_to_dict(plan),
                "placement": placement,
                "risk": [finding_to_dict(f) for f in findings],
            }
        )

    @app.post("/api/cancel_all")
    async def cancel_all(request: Request) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        try:
            result = broker_.cancel_all_orders()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "reason": str(exc)}, status_code=502)
        return JSONResponse({"ok": True, **result})

    @app.post("/api/flatten_all")
    async def flatten_all(request: Request) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        try:
            result = await broker_.flatten_all()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "reason": str(exc)}, status_code=502)
        return JSONResponse({"ok": True, **result})

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        manager: ConnectionManager = websocket.app.state.ws_manager
        broker_: BrokerLike = websocket.app.state.broker
        await manager.connect(websocket)
        try:
            await websocket.send_json(broker_.snapshot())
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(websocket)
        except Exception:  # noqa: BLE001
            manager.disconnect(websocket)

    return app


app = create_app()

```
