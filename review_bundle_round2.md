# WebWatch Review Bundle — Round 2 复审（上轮 FAIL 后）

> 你是上一轮的同一位审阅员。上轮你给了 **FAIL**，核心阻塞 1 条 P0 + 数条 P1/P2。本轮请逐条核验
> 修复是否真关闭了阻塞项，并扫描本轮修复**自身**是否引入新 bug。

---

## §A 角色与任务

### A.1 角色
你是 WebWatch 上轮 review 的同一位审阅员。上轮 verdict=FAIL，阻塞：P0（OCA 兄弟腿成交被误判为失败→
紧急平仓开反向裸仓）+ P1a（cancel_all 绕过锁）+ P2（入场超时分类为 rejected）+ D4（账户读取失败静默放行）。

### A.2 项目背景（固定）
IBKR **手动**超短线下单面板。Python 3.12 / asyncio / FastAPI / `ib_async` / **paper 4002**（live 不启用）。
复用 scalper 执行层。工具链 pytest + mypy --strict + ruff。

### A.3 红线模块
`pricing.py` / `order_service.py` / `broker.py` / `risk.py` / `app.py` 下单分支。

### A.4 审查目标
逐条核验阻塞修复是否真到位 + 扫描本轮修复自身引入的新 bug。**第一红线**：成功止盈/止损后绝不留裸仓、
绝不反向开仓。

### A.5 反糊弄硬约束（一字不改）
1. 禁止措辞：「方向正确」「值得继续打磨」「在大部分情况下」「考虑」「建议进一步」「或许可以」
   「看起来不错」「质量不错」「整体合理」「有改善空间」。
2. 禁止推脱：「后续才做」「paper 不重要」「应该已处理」「按行业惯例」——用源码证明。
3. 每条 finding 必须 file:line + 代码片段。
4. 任一 P0 = verdict FAIL/PARTIAL。
5. 必须主动挖新盲区；找不到也要写分析过程。

### A.6 Verdict（五档）
✅ PASS / ⚠️ PARTIAL / 🚧 WORKAROUND / 🆕 NEW BUG / ❌ NOT FIXED

### A.7 输出格式
- §A 总评（PASS/FAIL + 一句话）
- §B 逐条阻塞修复对账（每条 verdict + file:line + 说明）
- §C NEW BUGS（对本 bundle §C 候选逐条 verdict + 你额外发现）
- §D 一致性扫描
- §E 准入结论（能否进 paper 验证 M6；切 live 前还缺什么）

### A.8 安全约束
不暴露 secrets/账户号；不建议改 paper 4002→live；不建议跳测试/降覆盖率。

---

## §B 上轮阻塞项 → 本轮修复对账（请逐条核验）

### BLOCKER-1【P0】OCA 兄弟腿成交被误判失败 → 紧急平仓开反向裸仓
**上轮判语**：止盈成交→OCA 撤止损(status=Cancelled)→`_verify_protection_live` 把 Cancelled 当 BAD→抛错→
`_emergency_flatten` 对已平仓位反向 SELL → 反向裸仓。高频成功路径。

**本轮修复（两处，纵深防御）**：
1. `_verify_protection_live`（broker.py）：**先判 Filled=成功**再判 BAD：
```python
if any(s == "Filled" for s in statuses):
    return  # 一腿成交=出场成功，兄弟腿被 OCA 撤是正常的
if any(s in _PROTECTION_BAD_STATES for s in statuses):
    raise RuntimeError(...)
```
2. `_emergency_flatten`（broker.py）：**按真实持仓对账**，flat 则 no-op：
```python
pos = self._adapter.get_position(contract.symbol)
actual_qty = pos.quantity if pos is not None else 0
if actual_qty <= 0:
    return {"flatten_order_id": None, ..., "quantity": 0, "note": "已无持仓，无需平仓"}
close_action = "SELL" if actual_side is Side.LONG else "BUY"
flat = MarketOrder(close_action, actual_qty)   # 用真实数量，非传入 quantity
```
**回归测试**：`test_oca_sibling_fill_is_success_not_flatten`（TP=Filled/SL=Cancelled → 仅 1 张 MKT，
无紧急平仓）；`test_emergency_flatten_noop_when_already_flat`（验证误判失败但真实已平 → 平仓 no-op，
仍仅 1 张 MKT）。fake 已升级为可分别设两腿状态（`tp_status`/`sl_status`），修补上轮覆盖盲区。
**请核验**：① Filled-first 是否真的先于 BAD 判断；② 对账后是否结构上不可能反向下单；③ 看 §C 候选-1
（部分成交时 Filled 短路是否仍安全）。

### BLOCKER-2【P1a】cancel_all_orders 绕过 _order_lock
**修复**：改 async 并入锁：
```python
async def cancel_all_orders(self) -> dict[str, Any]:
    ...
    async with self._order_lock:
        self._adapter.cancel_all(None)
        return {"cancelled": True}
```
app `cancel_all` 端点改 `await broker_.cancel_all_orders()`。**请核验**是否所有改 IB 状态的公开路径都在锁内
（仍剩 broadcast 的同步只读 snapshot 不在锁内——只读，是否可接受？见 §C 候选-3）。

### BLOCKER-3【P2】入场超时分类为 rejected
**修复**：新增 `EntryUncertain` 异常，超时抛它；app 单独处理返回 `entry_uncertain:true`（非 rejected）+
提示查持仓；前端 `showMarketResult` 加 `entry_uncertain` 分支醒目红字。**请核验**语义是否与 ProtectionFailed
一致（均"非 rejected + 可能有仓 + 醒目告警"）。

### BLOCKER-4【D4】风控在账户读取失败时静默放行
**修复**：app `_risk_for` 在"已连接但 nav 读不到"时**顶一条 WARN**到前端
（"账户级风控未生效，仅 notional 上限有效"）。另在 `risk.assess` 入口加**非有限值 BLOCK**（防御深度）。
**请判断**：WARN 是否足够，还是 BLOCK 类风控应在账户不可用时 fail-closed（拒单）？

---

## §C 候选 NEW BUG（本轮修复自身可能引入的问题——请逐条 verdict）

> 我对每处修复都做了回归分析，以下是我自己**不确定**、需要你重点验证的点（不是"未发现问题"）。

### 候选-1：`_verify_protection_live` 的 Filled 短路在**部分成交**下是否安全
TP 部分成交时 ib_async 的 `orderStatus.status` 在完全成交前通常仍是 `Submitted`/`PreSubmitted`（不是 Filled），
故 Filled 短路只在**完全成交**时触发，理论上安全。但若某版本/路径下部分成交即把 status 置 `Filled`
而 `filled < 下单量`，则会"出场成功"短路返回，留下一部分仓位（OCA 会按比例缩兄弟腿吗？）。
我没有在本环境验证 OCA 在部分成交下的兄弟腿数量调整行为。**请你判断**这是真风险还是 ib_async/IBKR 已保证。

### 候选-2：`_emergency_flatten` 对账依赖 `get_position` 即时刷新——**反向漏平**风险
对账修复消除了"对空仓反向下单"，但引入相反风险：刚成交后若 `get_position` 尚未反映新仓（ib_async
position 更新延迟）→ 读到 0 → **跳过本该执行的平仓** → 真有仓却没平 = 裸仓。
我假设"已确认 fill 后 position 立即可见"，但未验证 ib_async 的 position 更新与 execDetails 的时序。
**请你判断**这个时序假设是否成立；若不成立，应改为"对账失败/读到 0 但刚确认 fill 时，仍按 filled 平 + 平后校验"。

### 候选-3：`_order_lock` 让 cancel_all/flatten_all 可能被在途下单阻塞最长 ~7s
下单持锁期间（fill 超时 5s + verify 2s），用户点"撤单/全平"会等到在途下单结束才执行。
紧急止损场景下这 ~7s 延迟是否可接受？还是 cancel/flatten 应能抢占/打断在途下单？我倾向可接受
（单用户、且在途下单本身很快），但这是 trade-off，请你定。

### 候选-4（本轮自查已发现并修）：前端 `entry_uncertain` 未处理 → `pl.filled` 抛错
我加了 app 的 `entry_uncertain` 响应但最初漏了前端分支，会导致 `res.placement` 为 undefined 时
`pl.filled` 抛 JS 错误。**已修**（index.html `showMarketResult` 加 `entry_uncertain` 分支 +
`if (!pl) return` 兜底）。列出供你确认修法正确。

### 一致性说明（A.5#5）
本轮只动了 broker.py / risk.py / app.py / index.html，未动 pricing.py / order_service.py（上轮已 PASS）。
P0 修复改变了"验证→平仓"的控制流，故重点自查了控制流的两个反向风险（候选-1 部分成交、候选-2 漏平），
以及并发锁扩面后的响应性（候选-3）。

---

## §D 测试清单（本轮新增/修改）
- `test_broker.py`：升级 `MarketFakeIB`（分腿状态）+ 新增 `MarketFakeAdapter`（get_position 对账）；
  新增 `test_oca_sibling_fill_is_success_not_flatten`、`test_emergency_flatten_noop_when_already_flat`；
  `test_entry_timeout_raises_entry_uncertain`（改抛 EntryUncertain）；`test_cancel_all_orders` 改 async。
- `test_risk.py`：新增 `TestNonFiniteDefense`（NaN/Inf entry/stop → BLOCK）。
- `test_app.py`：`FakeBroker.cancel_all_orders` 改 async。

---

## §E 元信息
- 生成时间：2026-06-08 21:03:49 CST
- 分支：main（git 无 commit；全部为工作区代码）
- 测试：**95 passed**（0 failed / 0 skipped）
- mypy --strict：通过；ruff：通过
- 覆盖率：TOTAL 81%（risk 100% / pricing 95% / order_service 92% / app 82% / broker 67%——
  broker 未覆盖为 connect/行情/IB 事件等需活连接路径）

---

## §F 本轮改动模块完整源码（broker.py / risk.py / app.py）
（pricing.py / order_service.py 本轮未改，见上轮 bundle。前端 index.html 仅加 entry_uncertain 分支。）

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


class EntryUncertain(Exception):
    """入场单超时未达终态：状态未知（可能已成交=裸仓）。需用户立即查持仓，不可当作"未成交"。"""

    def __init__(self, status: str) -> None:
        super().__init__(f"入场单超时未达终态（status={status}），状态未知，请立即检查持仓与挂单！")
        self.status = status


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
    async def cancel_all_orders(self) -> dict[str, Any]: ...
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
                raise EntryUncertain(entry_trade.orderStatus.status)

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
        """确认保护单挂活。任一进入终态异常或超时未确认 → 抛错（fail-closed）。

        **关键**：任一保护腿 ``Filled`` 表示出场已发生（OCA 正常成交即撤兄弟腿，
        兄弟腿会变 Cancelled —— 这是成功路径，绝不能误判为失败去平仓，否则对已平仓位
        反向开裸仓）。故先判 Filled=成功，再判 BAD。
        """
        for _ in range(max(1, int(timeout / interval))):
            statuses = [t.orderStatus.status for t in trades]
            if any(s == "Filled" for s in statuses):
                return  # 一腿成交 = 出场成功；兄弟腿被 OCA 撤掉是正常的
            if any(s in _PROTECTION_BAD_STATES for s in statuses):
                raise RuntimeError(f"保护单进入异常状态: {statuses}")
            if all(s in _PROTECTION_OK_STATES for s in statuses):
                return
            await asyncio.sleep(interval)
        statuses = [t.orderStatus.status for t in trades]
        if any(s == "Filled" for s in statuses):
            return
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

    async def cancel_all_orders(self) -> dict[str, Any]:
        """撤掉所有挂单（只撤单，不平仓）。进 _order_lock，避免撤掉别的处理器刚挂出的保护单。"""
        if not self.is_connected() or self._adapter is None:
            raise RuntimeError("未连接 Gateway")
        async with self._order_lock:
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
        """红线 #3：市价平掉仓位，不留裸仓。

        **必须按真实持仓平**（不盲信传入 quantity）：若保护单其实已成交、仓位已平，
        再按 quantity 同向下单会凭空开出反向裸仓。读 ``get_position`` 对账，flat 则不下单。
        """
        assert self._ib is not None and self._adapter is not None
        pos = self._adapter.get_position(contract.symbol)
        actual_qty = pos.quantity if pos is not None else 0
        actual_side = pos.side if pos is not None else side
        if actual_qty <= 0:
            log.warning("emergency flatten: %s 已无持仓，跳过下单", contract.symbol)
            return {"flatten_order_id": None, "action": None, "quantity": 0, "note": "已无持仓，无需平仓"}
        close_action = "SELL" if actual_side is Side.LONG else "BUY"
        flat = MarketOrder(close_action, actual_qty)
        flat.tif = "IOC"
        self._ib.placeOrder(contract, flat)
        log.error(
            "PROTECTION FAILED → 紧急平仓 %s %s x%s（按真实持仓）",
            contract.symbol,
            close_action,
            actual_qty,
        )
        return {"flatten_order_id": flat.orderId, "action": close_action, "quantity": actual_qty}


__all__ = [
    "BrokerLike",
    "BrokerManager",
    "ProtectionFailed",
    "EntryUncertain",
    "resolve_account",
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
    # 防御深度：正常流里 plan 已挡掉非有限值，但本模块作为独立红线也必须自保——
    # 任何直接调用 assess() 的未来路径不能让 NaN/Inf 静默放行（NaN 的所有 `>` 比较为 False）。
    if not entry.is_finite() or not stop_loss.is_finite():
        return [RiskFinding("non_finite_price", BLOCK, f"价格非有限值（entry={entry}, stop={stop_loss}）")]
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

from webwatch.broker import BrokerLike, BrokerManager, EntryUncertain, ProtectionFailed
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
from webwatch.risk import WARN, RiskFinding, finding_to_dict
from webwatch.risk import assess as risk_assess
from webwatch.risk import blocks as risk_blocks

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
    findings = risk_assess(
        entry=entry,
        stop_loss=stop_loss,
        quantity=quantity,
        side=side,
        nav=nav,
        buying_power=buying_power,
        panel=panel,
    )
    # 已连接但读不到账户数据 → 账户级风控（最大亏损/购买力）静默失效，必须显式告警。
    if broker_.is_connected() and nav is None:
        findings = [
            RiskFinding(
                "account_unavailable",
                WARN,
                "账户数据不可用：单笔最大亏损/购买力检查未生效，仅 notional 上限仍有效，请谨慎下单",
            ),
            *findings,
        ]
    return findings


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
        except EntryUncertain as exc:
            # 入场超时、状态未知（可能已成交）。绝不当作"已拒绝"，醒目提示查持仓。
            return JSONResponse(
                {
                    "rejected": False,
                    "entry_uncertain": True,
                    "reason": str(exc),
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
            result = await broker_.cancel_all_orders()
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
