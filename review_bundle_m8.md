# WebWatch Review Bundle — M8 24h 交易 + 行情独立化（Day Review）

> 对抗式审查。下单链路核心上轮已 3 轮 PASS；本轮在其上加 **(1) 行情独立模块 quotes.py、
> (2) 24h 交易（盘前/盘后/夜盘）**——后者**改了下单链路**（新增 stop-limit 分支 + 市价转换），重点审。
> （注：切 live 前硬化 + M7 的改动另有 `review_bundle_harden_m7.md`，本包不重复。）

---

## §A 角色与任务

### A.1 角色
WebWatch 本轮 code reviewer。审 quotes 独立化 + 24h 交易。

### A.2 项目背景（固定）
IBKR **手动**超短线下单面板。Python 3.12 / asyncio / FastAPI / `ib_async` / paper 4002 / live 4001。
复用 scalper **执行层**（不含行情）。pytest + mypy --strict + ruff。默认 paper，live 二重互锁。

### A.3 红线模块
`pricing.py` / `order_service.py` / `broker.py` / `risk.py` / `app.py`(下单分支) / `session.py`(决定单型)。

### A.4 审查目标
审本轮改动正确性 + 新风险。**第一红线**：任何时段成交后绝不裸仓、绝不反向开仓；时段外保护单必须真有效。

### A.5 审查重点（本轮）
1. **时段外止损 STP→STP-LMT**（broker `place_limit_bracket(outside_rth=True)`）：
   - 三腿是否都 `outsideRth=True`？止损是否正确从 STP 变 STP-LMT（auxPrice=触发价不变、补 lmtPrice）？
   - `stop_limit_price` 的限价方向对不对（LONG 卖出止损：限价在触发价**下方**才能在下跌中成交）？
   - 复用 `ib.bracketOrder` 再 mutate 的做法：把返回的 StopOrder 设 `orderType="STP LMT"` + `lmtPrice`
     是否是合法、IBKR 能正确识别的 stop-limit？还是必须新建 `StopLimitOrder`？**请你判断这点**。
2. **市价时段外自动转激进限价**（app `order_market`）：
   - `aggressive_limit`（买=贴卖一价+N tick）方向/取整对不对？
   - 转换后是否仍**完整走风控 + notional 上限**（经 `plan_limit_bracket` 重算）？是否仍有 TP+SL 保护？
   - **潜在缺口**：风控 `_risk_for` 用的是 `ref_price`，但实际下单价是更高的 `aggressive`（贵 N tick）。
     live 上限/账户检查按 ref 而非 aggressive 算——会不会让"卡线"的单略微越限？（见 §C 候选-1）
3. **session 判定**（session.py）：
   - "判错只会不成交或用更保守单型，故安全" 这条推理成立吗？
   - 夜盘跨周边界（周日晚开始、周五早结束）被简化为周末 CLOSED，有无危险？
   - 不含节假日/半日历——半日（13:00 收）后到 16:00 被判 RTH，市价单会怎样（应只是不成交）？
4. **行情 quotes.py**：运行时切类型（cancel+resub）有无订阅泄漏/竞态；`marketDataType` 上报是否可靠。
5. **回归**：RTH 路径（市价 on-fill OCA、限价原生 bracket 普通 STP）本轮**未改**——确认没被波及。

### A.6 反糊弄硬约束（一字不改）
1. 禁止措辞：「方向正确」「值得继续打磨」「在大部分情况下」「考虑」「建议进一步」「或许可以」
   「看起来不错」「质量不错」「整体合理」「有改善空间」。
2. 禁止推脱：「后续才做」「paper 不重要」「应该已处理」「按行业惯例」——用源码证明。
3. 每条 finding 必须 file:line + 代码片段。
4. 任一 P0 = verdict FAIL/PARTIAL。
5. 必须主动挖新盲区；找不到也写分析过程。

### A.7 Verdict：✅ PASS / ⚠️ PARTIAL / 🚧 WORKAROUND / 🆕 NEW BUG / ❌ NOT FIXED
### A.8 输出：§A 总评 / §B 逐条改动对账 / §C NEW BUGS / §D 最终建议（能否进 paper 盘中验证）
### A.9 安全：不暴露 secrets/账户号；不建议改 paper 4002→live 端口；不建议跳测试/降覆盖率。

---

## §B 本轮改动清单（请逐条核验）

### 行情独立化（commit 2105c1f）
1. **quotes.py `QuoteService`**：ib_async `reqMktData` L1 tick 流（非 scalper 的 K线/HMDS）。
   subscribe/unsubscribe/resubscribe_all；运行时 `set_market_data_type`（cancel 旧 + reqMarketDataType + 重订）；
   `quotes()` 上报 bid/ask/last/close + 实际 `marketDataType`（实时/延迟）+ has_data。
2. **broker** 委托行情给 QuoteService（watch/unwatch/snapshot.quotes/market_data_type）；
   全 src 零 `scalper.data` 引用（已 grep 验证）。
3. app：`POST /api/market_data_type`；前端行情类型下拉 + 每行类型标注。

### 24h 交易（commit 9959fc3）
4. **session.py**：ET 时段判定（RTH/PRE/POST/OVERNIGHT/CLOSED），`is_rth`/`session_label`。
5. **pricing.py**：`aggressive_limit(ref, ticks, side)`、`stop_limit_price(stop, offset_pct, side)`（均纯函数 + 防御）。
6. **broker.place_limit_bracket(outside_rth, stop_limit_offset_pct)**：outside_rth=True 时三腿 `outsideRth=True`
   且止损 `STP→STP LMT`（lmtPrice=stop_limit_price）。RTH（outside_rth=False）路径不变。
7. **app**：
   - 限价单：`outside = extended_hours_enabled and not is_rth()` → 传入 place_limit_bracket。
   - 市价单：时段外 → `aggressive_limit` + `plan_limit_bracket` + `place_limit_bracket(outside_rth=True)`
     （返回 `converted_to_limit:true`）；RTH → 原 `place_market_with_protection`。
   - snapshot 出 `session`/`is_rth`。
8. config/前端：`extended_hours_enabled`(默认 true)/`aggressive_limit_ticks`(3)/`stop_limit_offset_pct`(0.005)；
   时段徽章 + 时段外市价 confirm 提示。

---

## §C 候选 NEW BUG（我自查的不确定点——请逐条 verdict）

### 候选-1：市价转换的风控用 ref_price，下单价是更高的 aggressive
`_risk_for`（live 上限 / 账户级 / max-loss）按 `ref_price` 算；实际入场 `aggressive = ref + N*tick`（贵一点）。
随后 `plan_limit_bracket(aggressive)` 会**重算通用 notional 上限**（用 aggressive），但 **live 首周上限 /
account-unavailable / max-loss 这几条没用 aggressive 重算**。N tick 通常很小（notional 差 ~0.03%），
但严格说卡线单可能略微越 live 上限。**请你判断**是否需要用 aggressive 重跑一次 `_risk_for`。

### 候选-2：STP→STP-LMT 用 mutate ib.bracketOrder 返回单是否合法
broker 把 `ib.bracketOrder` 返回的 StopOrder 直接 `orderType="STP LMT"; lmtPrice=...`（auxPrice 保留为触发价）。
我认为这等价于一个 stop-limit，但**没在真实 IBKR 上验证** ib_async 是否会正确序列化（vs 必须新建
`StopLimitOrder`）。若 IBKR 不认，止损单会被拒 → 时段外裸仓风险。**请你判断**，并建议是否改为显式
`StopLimitOrder` 更稳。（这是本轮最该盯的点。）

### 候选-3：时段外订单的 tif / 夜盘持久性
bracketOrder 三腿默认 tif=""（DAY）。**夜盘（20:00–04:00）下 DAY 单是否能在夜盘 session 存活并成交？**
IBKR 夜盘可能需要特定 tif（如 GTC 或专门的 overnight tif）。我没设 tif。若 DAY 单在夜盘被当日清掉/不路由
到夜盘场所，则夜盘下单无效或保护单失效。**请你判断**夜盘对 tif/路由的要求，这关系到"夜盘真能交易吗"。

### 候选-4：session 不含节假日/半日历
半日（如某些节前 13:00 收）后 13:00–16:00 仍被判 RTH → 市价单（真·市价路径）发出。我的判断：市场已收，
IOC 市价不成交（安全），但**不会自动转激进限价**（因为以为是 RTH）。即半日盘后那段，市价按钮"看起来能点但不成交"。
请你判断是否可接受（paper 阶段），还是要接个最小节假日历。

### 分析过程（A.6#5）
本轮新增"时段→单型"的分支，故重点走了：① 时段外止损单型转换的合法性（候选-2，最高危）；
② 夜盘 tif/路由（候选-3）；③ 风控用价 vs 下单价的细微不一致（候选-1）；④ 时段判错的后果方向（候选-4）。
RTH 下单链路控制流未改，沿用上轮 PASS。

---

## §D 测试 / 元信息
- 新增：test_session.py（时段 10 例）、pricing aggressive/stop-limit（6 例）、
  broker outside_rth STP-LMT（1 例）、app 市价转换/限价 outsideRth（3 例）。
- 生成时间：2026-06-09 09:43 CST
- 测试：**131 passed**（0 failed / 0 skipped）；mypy --strict + ruff 通过。
- 覆盖率：TOTAL 87%（session 100% / pricing 93% / quotes 92% / app 82% / broker 79%——
  broker 未覆盖为 connect/IB 事件等需活连接路径）。
- commit：2105c1f（quotes）/ 9959fc3（M8）。baseline da2d4f6。

---

## §E 附：diff（git diff da2d4f6..HEAD, src+config）+ session.py / quotes.py 全文

#### git diff da2d4f6..HEAD ####
```diff
diff --git a/config/panel.yaml b/config/panel.yaml
index 979ed9f..9c86222 100644
--- a/config/panel.yaml
+++ b/config/panel.yaml
@@ -22,6 +22,11 @@ bracket_mode: native
 # 超短线实盘必须用 1；若账户暂无实时行情订阅，开发期可设 3 看延迟报价。
 market_data_type: 1
 
+# 24 小时交易：允许盘前/盘后/夜盘下单。时段外自动：限价入场 + stop-limit 止损 + outsideRth。
+extended_hours_enabled: true
+aggressive_limit_ticks: 3        # 时段外"市价买入"转激进限价，贴对手价加的 tick 数
+stop_limit_offset_pct: 0.005     # 时段外 stop-limit 止损：保护限价相对触发价的缓冲
+
 risk:
   # 单笔 notional 上限（下单前风控会拒超限单；paper/live 通用）。
   # 设为 20000 以让下方 live 首周上限($20000)真正成为绑定约束。
diff --git a/src/webwatch/app.py b/src/webwatch/app.py
index 9c185b7..118fa34 100644
--- a/src/webwatch/app.py
+++ b/src/webwatch/app.py
@@ -29,10 +29,11 @@ from webwatch.order_service import (
     plan_market_order,
     plan_to_dict,
 )
-from webwatch.pricing import Target, TargetKind
+from webwatch.pricing import Target, TargetKind, aggressive_limit
 from webwatch.risk import BLOCK, WARN, RiskFinding, finding_to_dict
 from webwatch.risk import assess as risk_assess
 from webwatch.risk import blocks as risk_blocks
+from webwatch.session import is_rth
 
 log = logging.getLogger(__name__)
 
@@ -44,6 +45,10 @@ class WatchRequest(BaseModel):
     symbol: str
 
 
+class MarketDataTypeRequest(BaseModel):
+    market_data_type: int  # 1=实时 2=冻结 3=延迟 4=延迟冻结
+
+
 class LimitOrderRequest(BaseModel):
     symbol: str
     quantity: int
@@ -263,6 +268,15 @@ def create_app(broker: BrokerLike | None = None, *, auto_connect: bool = True) -
         broker_.unwatch(symbol)
         return JSONResponse(broker_.snapshot())
 
+    @app.post("/api/market_data_type")
+    async def set_market_data_type(request: Request, body: MarketDataTypeRequest) -> JSONResponse:
+        broker_: BrokerLike = request.app.state.broker
+        try:
+            mdt = await broker_.set_market_data_type(body.market_data_type)
+        except Exception as exc:  # noqa: BLE001
+            return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
+        return JSONResponse({"ok": True, "market_data_type": mdt})
+
     @app.post("/api/reconnect")
     async def reconnect(request: Request) -> JSONResponse:
         broker_: BrokerLike = request.app.state.broker
@@ -302,6 +316,7 @@ def create_app(broker: BrokerLike | None = None, *, auto_connect: bool = True) -
         blocked = _risk_block_response(findings)
         if blocked is not None:
             return blocked
+        outside = panel.extended_hours_enabled and not is_rth()
         try:
             placement = await broker_.place_limit_bracket(
                 symbol=plan.symbol,
@@ -310,6 +325,8 @@ def create_app(broker: BrokerLike | None = None, *, auto_connect: bool = True) -
                 entry_limit=plan.entry_limit,
                 take_profit=plan.bracket.take_profit,
                 stop_loss=plan.bracket.stop_loss,
+                outside_rth=outside,
+                stop_limit_offset_pct=panel.stop_limit_offset_pct,
             )
         except Exception as exc:  # noqa: BLE001 — 下单失败回报给前端，不崩
             return JSONResponse(
@@ -357,6 +374,50 @@ def create_app(broker: BrokerLike | None = None, *, auto_connect: bool = True) -
         blocked = _risk_block_response(findings)
         if blocked is not None:
             return blocked
+
+        # 时段外（盘前/盘后/夜盘）：IBKR 不收市价单 → 自动转"激进限价"bracket（stop-limit 止损）。
+        if panel.extended_hours_enabled and not is_rth():
+            try:
+                aggressive = aggressive_limit(
+                    plan.ref_price, panel.aggressive_limit_ticks, side=plan.side
+                )
+                lim = plan_limit_bracket(
+                    symbol=plan.symbol,
+                    quantity=plan.quantity,
+                    entry_limit=aggressive,
+                    take_profit=plan.take_profit,
+                    stop_loss=plan.stop_loss,
+                    panel=panel,
+                    side=plan.side,
+                )
+            except OrderRejected as exc:
+                return JSONResponse({"rejected": True, "reason": exc.reason}, status_code=400)
+            try:
+                placement = await broker_.place_limit_bracket(
+                    symbol=lim.symbol,
+                    side=lim.side,
+                    quantity=lim.quantity,
+                    entry_limit=lim.entry_limit,
+                    take_profit=lim.bracket.take_profit,
+                    stop_loss=lim.bracket.stop_loss,
+                    outside_rth=True,
+                    stop_limit_offset_pct=panel.stop_limit_offset_pct,
+                )
+            except Exception as exc:  # noqa: BLE001
+                return JSONResponse(
+                    {"rejected": True, "reason": f"下单失败: {exc}"}, status_code=502
+                )
+            return JSONResponse(
+                {
+                    "rejected": False,
+                    "converted_to_limit": True,
+                    "plan": plan_to_dict(lim),
+                    "placement": placement,
+                    "risk": [finding_to_dict(f) for f in findings],
+                }
+            )
+
+        # RTH：真·市价(IOC) + 成交后挂 OCA
         try:
             placement = await broker_.place_market_with_protection(
                 symbol=plan.symbol,
diff --git a/src/webwatch/broker.py b/src/webwatch/broker.py
index e04f165..f39fc48 100644
--- a/src/webwatch/broker.py
+++ b/src/webwatch/broker.py
@@ -18,7 +18,7 @@ import math
 from decimal import Decimal
 from typing import Any, NoReturn, Protocol, TypeVar
 
-from ib_async import IB, LimitOrder, MarketOrder, Stock, StopOrder, Ticker
+from ib_async import IB, LimitOrder, MarketOrder, Stock, StopOrder
 from scalper.execution.ib_broker_adapter import (
     IbBrokerAdapter,
     connect_live_async,
@@ -29,8 +29,10 @@ from scalper.execution.types import OrderId
 from scalper.strategy.base import Side
 
 from webwatch.config import Environment, IBKRSettings, PanelConfig
-from webwatch.pricing import BracketPrices, Target, compute_bracket
+from webwatch.pricing import BracketPrices, Target, compute_bracket, stop_limit_price
+from webwatch.quotes import QuoteService
 from webwatch.serialize import account_to_dict, order_to_dict, position_to_dict
+from webwatch.session import is_rth, session_label
 
 log = logging.getLogger(__name__)
 
@@ -92,6 +94,7 @@ class BrokerLike(Protocol):
     def is_live(self) -> bool: ...
     async def watch(self, symbol: str) -> None: ...
     def unwatch(self, symbol: str) -> None: ...
+    async def set_market_data_type(self, mdt: int) -> int: ...
     def snapshot(self) -> dict[str, Any]: ...
     async def place_limit_bracket(
         self,
@@ -102,6 +105,8 @@ class BrokerLike(Protocol):
         entry_limit: Decimal,
         take_profit: Decimal,
         stop_loss: Decimal,
+        outside_rth: bool = False,
+        stop_limit_offset_pct: Decimal | None = None,
     ) -> dict[str, Any]: ...
     async def place_market_with_protection(
         self,
@@ -117,15 +122,6 @@ class BrokerLike(Protocol):
     async def flatten_all(self) -> dict[str, Any]: ...
 
 
-def _num(value: float | None) -> float | None:
-    """把 ib_async ticker 的 nan/None 归一成 None（方便 JSON / 前端）。"""
-    if value is None:
-        return None
-    if isinstance(value, float) and math.isnan(value):
-        return None
-    return value
-
-
 class BrokerManager:
     """真实 IBKR 连接管理。"""
 
@@ -133,13 +129,11 @@ class BrokerManager:
         self, settings: IBKRSettings, panel: PanelConfig, *, env_warning: str | None = None
     ) -> None:
         self._settings = settings
-        self._market_data_type = panel.market_data_type
         self._env_warning = env_warning  # live 互锁回退等提示，前端醒目展示
         self._ib: IB | None = None
         self._adapter: IbBrokerAdapter | None = None
-        self._watchlist: list[str] = []  # 保持添加顺序
-        self._contracts: dict[str, Stock] = {}
-        self._tickers: dict[str, Ticker] = {}
+        # 行情独立模块（webwatch 自有，不依赖 scalper 行情代码）。
+        self._quotes = QuoteService(panel.market_data_type)
         self._last_error: str | None = None
         # 串行化下单/平仓操作：单个 IB 连接被多个 await 处理器共享，
         # 防止并发下单交叉（OCA 组错乱、撤单撤到刚挂的保护单等）。
@@ -173,15 +167,14 @@ class BrokerManager:
                     expected_account=s.ibkr_account or None,
                 )
             ib.errorEvent += self._on_ib_error
-            ib.reqMarketDataType(self._market_data_type)
             self._ib = ib
             # 未填真实账户时自动用 Gateway 探测到的账户做 scoping。
             account = resolve_account(s.ibkr_account, list(ib.managedAccounts()))
             self._adapter = IbBrokerAdapter(ib, paper_account=account)
             self._last_error = None
-            # 重新订阅之前 watch 的标的
-            for sym in list(self._watchlist):
-                await self._subscribe(sym)
+            # 行情：绑定连接 + 重订 watchlist
+            self._quotes.attach(ib)
+            await self._quotes.resubscribe_all()
             log.info("connected to %s gateway", s.environment.value)
         except Exception as exc:
             self._last_error = f"{type(exc).__name__}: {exc}"
@@ -194,6 +187,7 @@ class BrokerManager:
     async def disconnect(self) -> None:
         if self._ib is not None and self._ib.isConnected():
             self._ib.disconnect()
+        self._quotes.detach()
         self._ib = None
         self._adapter = None
 
@@ -211,57 +205,17 @@ class BrokerManager:
         self._last_error = f"IB {error_code}:{sym} {error_string}"
         log.warning("IB error %s: %s", error_code, error_string)
 
-    # ---- 报价订阅 -------------------------------------------------------
+    # ---- 报价订阅（委托给独立 QuoteService）-----------------------------
 
     async def watch(self, symbol: str) -> None:
-        sym = symbol.strip().upper()
-        if not sym:
-            return
-        if sym not in self._watchlist:
-            self._watchlist.append(sym)
-        if self.is_connected():
-            await self._subscribe(sym)
-
-    async def _subscribe(self, sym: str) -> None:
-        assert self._ib is not None
-        try:
-            contract = Stock(sym, "SMART", "USD")
-            await self._ib.qualifyContractsAsync(contract)
-            self._contracts[sym] = contract
-            self._tickers[sym] = self._ib.reqMktData(contract, "", False, False)
-        except Exception as exc:  # noqa: BLE001 — 单个标的订阅失败不应拖垮面板
-            self._last_error = f"watch {sym}: {type(exc).__name__}: {exc}"
-            log.warning(self._last_error)
+        await self._quotes.subscribe(symbol)
 
     def unwatch(self, symbol: str) -> None:
-        sym = symbol.strip().upper()
-        if sym in self._watchlist:
-            self._watchlist.remove(sym)
-        contract = self._contracts.pop(sym, None)
-        self._tickers.pop(sym, None)
-        if contract is not None and self._ib is not None and self._ib.isConnected():
-            try:
-                self._ib.cancelMktData(contract)
-            except Exception as exc:  # noqa: BLE001
-                log.warning("cancelMktData %s: %s", sym, exc)
-
-    def _quotes(self) -> list[dict[str, Any]]:
-        out: list[dict[str, Any]] = []
-        for sym in self._watchlist:
-            t = self._tickers.get(sym)
-            if t is None:
-                out.append({"symbol": sym, "bid": None, "ask": None, "last": None, "close": None})
-                continue
-            out.append(
-                {
-                    "symbol": sym,
-                    "bid": _num(t.bid),
-                    "ask": _num(t.ask),
-                    "last": _num(t.last),
-                    "close": _num(t.close),
-                }
-            )
-        return out
+        self._quotes.unsubscribe(symbol)
+
+    async def set_market_data_type(self, mdt: int) -> int:
+        """运行时切换行情类型（实时/冻结/延迟/延迟冻结）。"""
+        return await self._quotes.set_market_data_type(mdt)
 
     # ---- 状态快照 -------------------------------------------------------
 
@@ -289,13 +243,15 @@ class BrokerManager:
             "environment": self._settings.environment.value,
             "is_live": self.is_live(),
             "env_warning": self._env_warning,
-            "market_data_type": self._market_data_type,
+            "session": session_label(),
+            "is_rth": is_rth(),
+            "market_data_type": self._quotes.market_data_type,
             "error": self._last_error,
             "account": account,
             "positions": positions,
             "orders": orders,
-            "quotes": self._quotes(),
-            "watchlist": list(self._watchlist),
+            "quotes": self._quotes.quotes(),
+            "watchlist": self._quotes.watchlist(),
         }
 
     # ---- 下单（M3：限价 + 模式B 原生 bracket）---------------------------
@@ -309,11 +265,16 @@ class BrokerManager:
         entry_limit: Decimal,
         take_profit: Decimal,
         stop_loss: Decimal,
+        outside_rth: bool = False,
+        stop_limit_offset_pct: Decimal | None = None,
     ) -> dict[str, Any]:
-        """模式B：用 ib_async 原生 bracket 一次性发出 母单(限价) + 止盈(限价) + 止损(stop)。
+        """模式B：用 ib_async 原生 bracket 一次性发出 母单(限价) + 止盈(限价) + 止损。
 
         三条单经 ib.bracketOrder 自带 parentId + transmit 链，保护单零空窗。
-        ``transmit``：parent/TP=False、SL=True → 全部一起 transmit。
+
+        ``outside_rth=True``（盘前/盘后/夜盘）：三条单全部 `outsideRth=True`，且止损从普通 STP
+        **转成 STP-LMT**（时段外普通 stop 不触发）。止损限价用 ``stop_limit_offset_pct`` 在触发价
+        更深一侧留缓冲。
         """
         if not self.is_connected() or self._ib is None:
             raise RuntimeError("未连接 Gateway，无法下单")
@@ -330,6 +291,15 @@ class BrokerManager:
                 float(take_profit),
                 float(stop_loss),
             )
+            stop_limit: Decimal | None = None
+            if outside_rth:
+                offset = stop_limit_offset_pct if stop_limit_offset_pct is not None else Decimal("0.005")
+                stop_limit = stop_limit_price(stop_loss, offset, side=side)
+                for order in bracket:
+                    order.outsideRth = True
+                # 普通 STP → STP-LMT（时段外可用）。auxPrice(触发价) 不变，补限价。
+                bracket.stopLoss.orderType = "STP LMT"
+                bracket.stopLoss.lmtPrice = float(stop_limit)
             for order in bracket:
                 ib.placeOrder(contract, order)
             return {
@@ -339,6 +309,8 @@ class BrokerManager:
                 "entry_limit": str(entry_limit),
                 "take_profit": str(take_profit),
                 "stop_loss": str(stop_loss),
+                "stop_limit": str(stop_limit) if stop_limit is not None else None,
+                "outside_rth": outside_rth,
                 "parent_id": bracket.parent.orderId,
                 "take_profit_id": bracket.takeProfit.orderId,
                 "stop_loss_id": bracket.stopLoss.orderId,
diff --git a/src/webwatch/config.py b/src/webwatch/config.py
index 8d0ad70..ac14487 100644
--- a/src/webwatch/config.py
+++ b/src/webwatch/config.py
@@ -154,6 +154,12 @@ class PanelConfig:
         # IBKR 行情类型：1=实时(需订阅) 2=冻结 3=延迟 4=延迟冻结。
         # 超短线实盘用 1；无实时订阅时开发可设 3 看延迟报价。
         self.market_data_type = int(raw.get("market_data_type", 1))
+        # 24h 交易：允许盘前/盘后/夜盘下单（outsideRth）。时段外自动用限价入场 + stop-limit 止损。
+        self.extended_hours_enabled = bool(raw.get("extended_hours_enabled", True))
+        # 时段外"市价买入"转激进限价时，贴对手价加的 tick 数。
+        self.aggressive_limit_ticks = int(raw.get("aggressive_limit_ticks", 3))
+        # 时段外 stop-limit 止损：保护限价相对触发价的缓冲（向更深一侧）。
+        self.stop_limit_offset_pct = Decimal(str(raw.get("stop_limit_offset_pct", "0.005")))
         risk = raw.get("risk", {}) or {}
         self.max_order_notional_usd = Decimal(str(risk.get("max_order_notional_usd", "5000")))
         # 单笔最大亏损上限（占 NAV 比例），镜像 scalper 0.5% 语义。
diff --git a/src/webwatch/pricing.py b/src/webwatch/pricing.py
index ddd2315..5f56fa9 100644
--- a/src/webwatch/pricing.py
+++ b/src/webwatch/pricing.py
@@ -225,6 +225,42 @@ def net_edge(
     )
 
 
+def aggressive_limit(ref_price: Decimal, ticks: int, *, side: Side = Side.LONG) -> Decimal:
+    """时段外把"市价"转成激进限价：买单贴卖一价上方 ``ticks`` 个 tick（卖单则下方）。
+
+    时段外 IBKR 不收市价单；激进限价用略优于对手价的价格追求成交。
+    """
+    if not ref_price.is_finite() or ref_price <= 0:
+        raise ValueError(f"ref_price must be positive finite, got {ref_price}")
+    if ticks < 0:
+        raise ValueError(f"ticks must be >= 0, got {ticks}")
+    tick = min_tick(ref_price)
+    offset = tick * ticks
+    if side is Side.LONG:
+        return round_to_tick(ref_price + offset, tick, ROUND_UP)
+    return round_to_tick(ref_price - offset, tick, ROUND_DOWN)
+
+
+def stop_limit_price(stop_price: Decimal, offset_pct: Decimal, *, side: Side = Side.LONG) -> Decimal:
+    """止损限价单的保护限价：在止损触发价更"深"一侧留 ``offset_pct`` 缓冲，
+    以便价格穿过触发价时仍能成交（代价：极端跳空可能不成交）。
+
+    LONG 仓的止损是 SELL stop → 限价在触发价**下方**；SHORT 反之。
+    """
+    if not stop_price.is_finite() or stop_price <= 0:
+        raise ValueError(f"stop_price must be positive finite, got {stop_price}")
+    if not offset_pct.is_finite() or offset_pct < 0:
+        raise ValueError(f"offset_pct must be >= 0 finite, got {offset_pct}")
+    tick = min_tick(stop_price)
+    if side is Side.LONG:
+        limit = round_to_tick(stop_price * (Decimal(1) - offset_pct), tick, ROUND_DOWN)
+    else:
+        limit = round_to_tick(stop_price * (Decimal(1) + offset_pct), tick, ROUND_UP)
+    if limit <= 0:
+        raise ValueError(f"stop-limit 保护价非正（{limit}）")
+    return limit
+
+
 def shares_for_notional(notional_usd: Decimal, price: Decimal) -> int:
     """给定金额能买的整股数（向下取整）。"""
     if price <= 0:
@@ -242,6 +278,8 @@ __all__ = [
     "target_delta",
     "compute_bracket",
     "net_edge",
+    "aggressive_limit",
+    "stop_limit_price",
     "shares_for_notional",
     "DEFAULT_COMMISSION_PER_SHARE",
     "DEFAULT_MIN_COMMISSION",
diff --git a/src/webwatch/quotes.py b/src/webwatch/quotes.py
new file mode 100644
index 0000000..ea55205
--- /dev/null
+++ b/src/webwatch/quotes.py
@@ -0,0 +1,179 @@
+"""行情订阅 —— 独立模块，webwatch 自有实现，**完全不依赖 scalper 行情代码**。
+
+设计取舍（记录给后续 session）：
+- 用 ib_async `reqMktData` 的 **L1 逐笔 tick 流**，而非 scalper 的 `reqHistoricalData` /
+  `reqRealTimeBars`（5 秒 K 线）。后者受 HMDS 限流(162)/启动卡死(BUG-8) 困扰，且对"看实时报价
+  下手动单"来说粒度太粗、延迟大。逐笔 tick 才贴合超短线面板。
+- 行情类型（实时/延迟/冻结）可**运行时切换**：当账户实时行情被竞争会话占用（Error 10197）时，
+  用户可一键切到免费的延迟行情(type 3)继续看盘，不必重启。
+- 每个报价上报**实际拿到的行情类型**（ticker.marketDataType），前端据此显示"实时/延迟"，
+  避免"以为是实时其实是延迟"的误判。
+
+红线：不引入 scalper.data.*；金额展示用数字即可（非下单价，下单价在 pricing/order_service 用 Decimal）。
+"""
+
+from __future__ import annotations
+
+import contextlib
+import logging
+import math
+from typing import Any
+
+from ib_async import IB, Stock, Ticker
+
+log = logging.getLogger(__name__)
+
+# IBKR 行情类型码 → 中文名。1=实时 2=冻结 3=延迟 4=延迟冻结。
+MARKET_DATA_TYPE_NAMES: dict[int, str] = {1: "实时", 2: "冻结", 3: "延迟", 4: "延迟冻结"}
+_DELAYED_TYPES = frozenset({3, 4})
+_VALID_TYPES = frozenset({1, 2, 3, 4})
+
+
+def _num(value: float | None) -> float | None:
+    """ib_async ticker 的 nan/None → None（JSON / 前端友好）。"""
+    if value is None:
+        return None
+    if isinstance(value, float) and math.isnan(value):
+        return None
+    return value
+
+
+class QuoteService:
+    """管理 watchlist 的实时行情订阅。连接由外部注入（broker 持有同一 IB 实例）。"""
+
+    def __init__(self, market_data_type: int = 1) -> None:
+        self._mdt = market_data_type if market_data_type in _VALID_TYPES else 1
+        self._ib: IB | None = None
+        self._watchlist: list[str] = []  # 保持添加顺序
+        self._contracts: dict[str, Stock] = {}
+        self._tickers: dict[str, Ticker] = {}
+
+    # ---- 连接绑定 -------------------------------------------------------
+
+    def attach(self, ib: IB) -> None:
+        """连接建立后绑定 IB 并设置行情类型。"""
+        self._ib = ib
+        ib.reqMarketDataType(self._mdt)
+
+    def detach(self) -> None:
+        """断开时清理（不在断开的连接上发撤订阅）。"""
+        self._ib = None
+        self._tickers.clear()
+        self._contracts.clear()
+
+    @property
+    def connected(self) -> bool:
+        return self._ib is not None and self._ib.isConnected()
+
+    @property
+    def market_data_type(self) -> int:
+        return self._mdt
+
+    # ---- 订阅管理 -------------------------------------------------------
+
+    async def subscribe(self, symbol: str) -> None:
+        sym = symbol.strip().upper()
+        if not sym:
+            return
+        if sym not in self._watchlist:
+            self._watchlist.append(sym)
+        if self.connected:
+            await self._do_subscribe(sym)
+
+    async def _do_subscribe(self, sym: str) -> None:
+        assert self._ib is not None
+        try:
+            contract = Stock(sym, "SMART", "USD")
+            await self._ib.qualifyContractsAsync(contract)
+            self._contracts[sym] = contract
+            # snapshot=False → 持续 streaming；regulatorySnapshot=False → 不额外收费快照
+            self._tickers[sym] = self._ib.reqMktData(contract, "", False, False)
+        except Exception as exc:  # noqa: BLE001 — 单标的订阅失败不应拖垮整个面板
+            log.warning("subscribe %s failed: %s: %s", sym, type(exc).__name__, exc)
+
+    def unsubscribe(self, symbol: str) -> None:
+        sym = symbol.strip().upper()
+        if sym in self._watchlist:
+            self._watchlist.remove(sym)
+        contract = self._contracts.pop(sym, None)
+        self._tickers.pop(sym, None)
+        if contract is not None and self.connected:
+            assert self._ib is not None
+            try:
+                self._ib.cancelMktData(contract)
+            except Exception as exc:  # noqa: BLE001
+                log.warning("cancelMktData %s: %s", sym, exc)
+
+    async def resubscribe_all(self) -> None:
+        """重连后重订所有 watchlist 标的。"""
+        if not self.connected:
+            return
+        for sym in list(self._watchlist):
+            await self._do_subscribe(sym)
+
+    async def set_market_data_type(self, mdt: int) -> int:
+        """运行时切换行情类型（如实时被占用→切延迟）。返回生效类型。重订以立即刷新。"""
+        if mdt not in _VALID_TYPES:
+            raise ValueError(f"无效行情类型 {mdt}（应为 1/2/3/4）")
+        self._mdt = mdt
+        if self.connected:
+            assert self._ib is not None
+            self._ib.reqMarketDataType(mdt)
+            # 先撤再订，确保现有标的按新类型刷新
+            for sym in list(self._watchlist):
+                contract = self._contracts.get(sym)
+                if contract is not None:
+                    with contextlib.suppress(Exception):
+                        self._ib.cancelMktData(contract)
+            self._tickers.clear()
+            await self.resubscribe_all()
+        return self._mdt
+
+    # ---- 读取 -----------------------------------------------------------
+
+    def watchlist(self) -> list[str]:
+        return list(self._watchlist)
+
+    def quotes(self) -> list[dict[str, Any]]:
+        out: list[dict[str, Any]] = []
+        for sym in self._watchlist:
+            ticker = self._tickers.get(sym)
+            if ticker is None:
+                out.append(self._empty_quote(sym))
+                continue
+            mdt = getattr(ticker, "marketDataType", None)
+            bid, ask, last, close = (
+                _num(ticker.bid),
+                _num(ticker.ask),
+                _num(ticker.last),
+                _num(ticker.close),
+            )
+            out.append(
+                {
+                    "symbol": sym,
+                    "bid": bid,
+                    "ask": ask,
+                    "last": last,
+                    "close": close,
+                    "data_type": MARKET_DATA_TYPE_NAMES.get(mdt) if mdt else None,
+                    "delayed": mdt in _DELAYED_TYPES if mdt else None,
+                    "has_data": any(v is not None for v in (bid, ask, last)),
+                }
+            )
+        return out
+
+    @staticmethod
+    def _empty_quote(sym: str) -> dict[str, Any]:
+        return {
+            "symbol": sym,
+            "bid": None,
+            "ask": None,
+            "last": None,
+            "close": None,
+            "data_type": None,
+            "delayed": None,
+            "has_data": False,
+        }
+
+
+__all__ = ["QuoteService", "MARKET_DATA_TYPE_NAMES"]
diff --git a/src/webwatch/session.py b/src/webwatch/session.py
new file mode 100644
index 0000000..438f4b0
--- /dev/null
+++ b/src/webwatch/session.py
@@ -0,0 +1,98 @@
+"""美股交易时段判定 —— 独立模块，纯 stdlib（zoneinfo），不依赖 scalper / 行情。
+
+时段（美东 ET）：
+- 盘前 EXTENDED：04:00–09:30
+- 常规 RTH：09:30–16:00
+- 盘后 EXTENDED：16:00–20:00
+- 夜盘 OVERNIGHT：20:00–次日 04:00（IBKR Blue Ocean 通道，仅部分标的）
+- 休市 CLOSED：周末（粗略）
+
+**设计取舍**：不接入节假日/半日历（避免引第三方历法依赖、也避免历法错误带来的隐性风险）。
+判错的后果是**安全的**：
+- 误判为 RTH 实际休市 → 市价单不成交（IOC 撤），不会乱成交；
+- 误判为时段外 实际 RTH → 用限价+stop-limit+outsideRth，在 RTH 同样能正常工作。
+即两个方向出错都不会造成危险，只会"不成交"或"用更保守的单型"。
+
+判定只用于决定**订单构造**（市价 vs 限价、STP vs STP-LMT、是否 outsideRth），不做硬性时段拦截。
+"""
+
+from __future__ import annotations
+
+from datetime import datetime, time
+from enum import StrEnum
+from zoneinfo import ZoneInfo
+
+ET = ZoneInfo("America/New_York")
+
+_RTH_OPEN = time(9, 30)
+_RTH_CLOSE = time(16, 0)
+_PRE_OPEN = time(4, 0)
+_POST_CLOSE = time(20, 0)
+
+
+class Session(StrEnum):
+    RTH = "rth"  # 常规盘
+    PRE = "pre"  # 盘前
+    POST = "post"  # 盘后
+    OVERNIGHT = "overnight"  # 夜盘
+    CLOSED = "closed"  # 休市（周末）
+
+
+_SESSION_LABELS: dict[Session, str] = {
+    Session.RTH: "常规盘",
+    Session.PRE: "盘前",
+    Session.POST: "盘后",
+    Session.OVERNIGHT: "夜盘",
+    Session.CLOSED: "休市",
+}
+
+
+def now_et() -> datetime:
+    """当前美东时间（带时区）。"""
+    return datetime.now(ET)
+
+
+def current_session(dt: datetime | None = None) -> Session:
+    """判定 ``dt``（默认当前 ET）所处时段。周末粗略归 CLOSED（夜盘跨周边界不精细处理）。"""
+    dt = dt or now_et()
+    weekday = dt.weekday()  # 0=周一 … 6=周日
+    t = dt.time()
+    # 周六全天、周日 20:00 前：休市（周日晚的夜盘开始这里简化为仍 CLOSED，安全方向）。
+    if weekday == 5:
+        return Session.CLOSED
+    if weekday == 6:
+        return Session.CLOSED
+    # 周一到周五：
+    if _RTH_OPEN <= t < _RTH_CLOSE:
+        return Session.RTH
+    if _PRE_OPEN <= t < _RTH_OPEN:
+        return Session.PRE
+    if _RTH_CLOSE <= t < _POST_CLOSE:
+        return Session.POST
+    # 其余（20:00–次日 04:00）= 夜盘
+    return Session.OVERNIGHT
+
+
+def is_rth(dt: datetime | None = None) -> bool:
+    """是否常规交易时段（决定能否用市价单 / 普通 stop）。"""
+    return current_session(dt) is Session.RTH
+
+
+def is_tradable_extended(dt: datetime | None = None) -> bool:
+    """是否处于可下单的时段（RTH 或 盘前/盘后/夜盘）。周末 CLOSED 返回 False。"""
+    return current_session(dt) is not Session.CLOSED
+
+
+def session_label(dt: datetime | None = None) -> str:
+    return _SESSION_LABELS[current_session(dt)]
+
+
+__all__ = [
+    "Session",
+    "ET",
+    "now_et",
+    "current_session",
+    "is_rth",
+    "is_tradable_extended",
+    "session_label",
+]
diff --git a/src/webwatch/web/index.html b/src/webwatch/web/index.html
index 6c5e16b..ecac89b 100644
--- a/src/webwatch/web/index.html
+++ b/src/webwatch/web/index.html
@@ -58,6 +58,7 @@
 <header>
   <span id="env" class="badge paper">PAPER</span>
   <span><span id="dot" class="dot off"></span><span id="conn">未连接</span></span>
+  <span id="session" class="badge" style="background:#2a323d;color:var(--txt)"></span>
   <div class="acct" id="acct"></div>
   <span style="flex:1"></span>
   <span class="err" id="err"></span>
@@ -103,10 +104,18 @@
 
   <section class="card full">
     <h2>报价 (watchlist)</h2>
-    <div style="margin-bottom:10px;">
+    <div style="margin-bottom:10px; display:flex; align-items:center; gap:10px;">
       <input id="symInput" placeholder="代码 如 AAPL" />
       <button id="addBtn">添加</button>
-      <span class="mut" id="mdtype"></span>
+      <label class="mut" style="display:flex; align-items:center; gap:4px;">行情类型
+        <select id="mdtype">
+          <option value="1">实时</option>
+          <option value="2">冻结</option>
+          <option value="3">延迟</option>
+          <option value="4">延迟冻结</option>
+        </select>
+      </label>
+      <span class="mut" style="font-size:12px;">实时被占用(10197)时切「延迟」可看免费行情</span>
     </div>
     <table>
       <thead><tr><th>代码</th><th>买一</th><th>卖一</th><th>最新</th><th>昨收</th><th></th></tr></thead>
@@ -134,6 +143,8 @@
 <script>
 const $ = (id) => document.getElementById(id);
 let isLive = false;  // 由 render() 根据 state 更新；下单确认用
+let isRth = true;    // 是否常规盘；时段外市价会转激进限价
+let sessionLabel = '';
 const fmt = (v, d=2) => (v === null || v === undefined) ? '<span class="mut">—</span>' : Number(v).toFixed(d);
 const money = (v) => (v === null || v === undefined) ? '—' : Number(v).toLocaleString('en-US',{minimumFractionDigits:2, maximumFractionDigits:2});
 const sideCls = (s) => s === 'long' ? 'pos' : 'neg';
@@ -152,8 +163,14 @@ function render(st) {
   else if (st.env_warning) { bar.className = 'warn'; bar.textContent = '⚠ ' + st.env_warning; }
   else { bar.className = ''; bar.textContent = ''; }
   $('err').textContent = st.error ? ('⚠ ' + st.error) : '';
-  const mdt = {1:'实时',2:'冻结',3:'延迟',4:'延迟冻结'}[st.market_data_type] || st.market_data_type;
-  $('mdtype').textContent = '行情:' + mdt;
+  if (document.activeElement !== $('mdtype')) $('mdtype').value = String(st.market_data_type || 1);
+  // 交易时段
+  isRth = !!st.is_rth;
+  sessionLabel = st.session || '';
+  const sEl = $('session');
+  sEl.textContent = sessionLabel ? ('时段:' + sessionLabel) : '';
+  sEl.style.background = isRth ? 'var(--green)' : (sessionLabel === '休市' ? '#555' : 'var(--amber)');
+  sEl.style.color = isRth ? '#fff' : (sessionLabel === '休市' ? '#fff' : '#1a1a1a');
 
   // 账户
   const a = st.account;
@@ -165,10 +182,12 @@ function render(st) {
 
   // 报价
   const q = $('quotes');
-  q.innerHTML = (st.quotes && st.quotes.length) ? st.quotes.map(r => `
-    <tr><td>${r.symbol}</td><td>${fmt(r.bid)}</td><td>${fmt(r.ask)}</td>
+  q.innerHTML = (st.quotes && st.quotes.length) ? st.quotes.map(r => {
+    const tag = r.data_type ? `<span class="mut" style="font-size:11px"> ${r.data_type}</span>` : '';
+    return `<tr><td>${r.symbol}${tag}</td><td>${fmt(r.bid)}</td><td>${fmt(r.ask)}</td>
     <td>${fmt(r.last)}</td><td>${fmt(r.close)}</td>
-    <td class="x" onclick="unwatch('${r.symbol}')">✕</td></tr>`).join('')
+    <td class="x" onclick="unwatch('${r.symbol}')">✕</td></tr>`;
+  }).join('')
     : '<tr><td colspan="6" class="empty">还没有关注的标的</td></tr>';
 
   // 持仓
@@ -209,6 +228,10 @@ async function reconnect() {
 $('addBtn').onclick = addWatch;
 $('symInput').addEventListener('keydown', e => { if (e.key === 'Enter') addWatch(); });
 $('reconnect').onclick = reconnect;
+$('mdtype').addEventListener('change', async (e) => {
+  await fetch('/api/market_data_type', {method:'POST', headers:{'Content-Type':'application/json'},
+    body: JSON.stringify({market_data_type: Number(e.target.value)})});
+});
 
 // --- 下单 ---
 function orderBody() {
@@ -287,6 +310,15 @@ function showMarketResult(res) {
   if (res.rejected) { el.innerHTML = `<span class="neg">✕ 拒绝：${res.reason}</span>`; return; }
   const pl = res.placement;
   if (!pl) { el.innerHTML = `<span class="neg">✕ 异常响应，请检查持仓</span>`; return; }
+  // 时段外：市价已转激进限价 bracket（返回限价 bracket 形态）
+  if (res.converted_to_limit) {
+    const p = res.plan;
+    el.innerHTML = `<span class="pos">✓ 时段外已挂激进限价 bracket</span>（市价时段外不可用）—
+      限价 <b>${pl.entry_limit}</b> / 止盈 <b>${pl.take_profit}</b> /
+      止损 <b>${pl.stop_loss}</b>${pl.stop_limit ? ' (stop-limit '+pl.stop_limit+')' : ''}
+      <span class="mut">outsideRth · 单号 ${pl.parent_id}/${pl.take_profit_id}/${pl.stop_loss_id}</span>${riskHtml(res)}`;
+    return;
+  }
   if (pl.filled === 0) { el.innerHTML = `<span class="mut">市价单未成交（${pl.message||''}）</span>`; return; }
   el.innerHTML = `<span class="pos">✓ 市价成交</span> ${pl.filled} 股 @ <b>${pl.fill_price}</b> —
     止盈 <b>${pl.take_profit}</b> / 止损 <b>${pl.stop_loss}</b>
@@ -299,6 +331,7 @@ async function buyMarket() {
   let msg = (isLive ? '🔴🔴 LIVE 实盘下单！真金白银！🔴🔴\n\n' : '') +
     `确认【市价买入】${p.symbol} ${p.quantity} 股（约 @ ${p.ref_price}）\n` +
     `成交后按真实成交价挂止盈止损（预览：止盈 ${p.preview_take_profit} / 止损 ${p.preview_stop_loss}）`;
+  if (!isRth) msg += `\n\n⏰ 当前为${sessionLabel}（时段外）：市价不可用，将自动转为「激进限价 bracket」，止损用 stop-limit。`;
   if (p.warnings && p.warnings.length) msg += '\n\n⚠ ' + p.warnings.join('\n⚠ ');
   if (!confirm(msg)) return;
   showMarketResult(await postMarket('/api/order/market'));

```

#### `src/webwatch/session.py`（全文）####
```python
"""美股交易时段判定 —— 独立模块，纯 stdlib（zoneinfo），不依赖 scalper / 行情。

时段（美东 ET）：
- 盘前 EXTENDED：04:00–09:30
- 常规 RTH：09:30–16:00
- 盘后 EXTENDED：16:00–20:00
- 夜盘 OVERNIGHT：20:00–次日 04:00（IBKR Blue Ocean 通道，仅部分标的）
- 休市 CLOSED：周末（粗略）

**设计取舍**：不接入节假日/半日历（避免引第三方历法依赖、也避免历法错误带来的隐性风险）。
判错的后果是**安全的**：
- 误判为 RTH 实际休市 → 市价单不成交（IOC 撤），不会乱成交；
- 误判为时段外 实际 RTH → 用限价+stop-limit+outsideRth，在 RTH 同样能正常工作。
即两个方向出错都不会造成危险，只会"不成交"或"用更保守的单型"。

判定只用于决定**订单构造**（市价 vs 限价、STP vs STP-LMT、是否 outsideRth），不做硬性时段拦截。
"""

from __future__ import annotations

from datetime import datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)
_PRE_OPEN = time(4, 0)
_POST_CLOSE = time(20, 0)


class Session(StrEnum):
    RTH = "rth"  # 常规盘
    PRE = "pre"  # 盘前
    POST = "post"  # 盘后
    OVERNIGHT = "overnight"  # 夜盘
    CLOSED = "closed"  # 休市（周末）


_SESSION_LABELS: dict[Session, str] = {
    Session.RTH: "常规盘",
    Session.PRE: "盘前",
    Session.POST: "盘后",
    Session.OVERNIGHT: "夜盘",
    Session.CLOSED: "休市",
}


def now_et() -> datetime:
    """当前美东时间（带时区）。"""
    return datetime.now(ET)


def current_session(dt: datetime | None = None) -> Session:
    """判定 ``dt``（默认当前 ET）所处时段。周末粗略归 CLOSED（夜盘跨周边界不精细处理）。"""
    dt = dt or now_et()
    weekday = dt.weekday()  # 0=周一 … 6=周日
    t = dt.time()
    # 周六全天、周日 20:00 前：休市（周日晚的夜盘开始这里简化为仍 CLOSED，安全方向）。
    if weekday == 5:
        return Session.CLOSED
    if weekday == 6:
        return Session.CLOSED
    # 周一到周五：
    if _RTH_OPEN <= t < _RTH_CLOSE:
        return Session.RTH
    if _PRE_OPEN <= t < _RTH_OPEN:
        return Session.PRE
    if _RTH_CLOSE <= t < _POST_CLOSE:
        return Session.POST
    # 其余（20:00–次日 04:00）= 夜盘
    return Session.OVERNIGHT


def is_rth(dt: datetime | None = None) -> bool:
    """是否常规交易时段（决定能否用市价单 / 普通 stop）。"""
    return current_session(dt) is Session.RTH


def is_tradable_extended(dt: datetime | None = None) -> bool:
    """是否处于可下单的时段（RTH 或 盘前/盘后/夜盘）。周末 CLOSED 返回 False。"""
    return current_session(dt) is not Session.CLOSED


def session_label(dt: datetime | None = None) -> str:
    return _SESSION_LABELS[current_session(dt)]


__all__ = [
    "Session",
    "ET",
    "now_et",
    "current_session",
    "is_rth",
    "is_tradable_extended",
    "session_label",
]

```

#### `src/webwatch/quotes.py`（全文）####
```python
"""行情订阅 —— 独立模块，webwatch 自有实现，**完全不依赖 scalper 行情代码**。

设计取舍（记录给后续 session）：
- 用 ib_async `reqMktData` 的 **L1 逐笔 tick 流**，而非 scalper 的 `reqHistoricalData` /
  `reqRealTimeBars`（5 秒 K 线）。后者受 HMDS 限流(162)/启动卡死(BUG-8) 困扰，且对"看实时报价
  下手动单"来说粒度太粗、延迟大。逐笔 tick 才贴合超短线面板。
- 行情类型（实时/延迟/冻结）可**运行时切换**：当账户实时行情被竞争会话占用（Error 10197）时，
  用户可一键切到免费的延迟行情(type 3)继续看盘，不必重启。
- 每个报价上报**实际拿到的行情类型**（ticker.marketDataType），前端据此显示"实时/延迟"，
  避免"以为是实时其实是延迟"的误判。

红线：不引入 scalper.data.*；金额展示用数字即可（非下单价，下单价在 pricing/order_service 用 Decimal）。
"""

from __future__ import annotations

import contextlib
import logging
import math
from typing import Any

from ib_async import IB, Stock, Ticker

log = logging.getLogger(__name__)

# IBKR 行情类型码 → 中文名。1=实时 2=冻结 3=延迟 4=延迟冻结。
MARKET_DATA_TYPE_NAMES: dict[int, str] = {1: "实时", 2: "冻结", 3: "延迟", 4: "延迟冻结"}
_DELAYED_TYPES = frozenset({3, 4})
_VALID_TYPES = frozenset({1, 2, 3, 4})


def _num(value: float | None) -> float | None:
    """ib_async ticker 的 nan/None → None（JSON / 前端友好）。"""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


class QuoteService:
    """管理 watchlist 的实时行情订阅。连接由外部注入（broker 持有同一 IB 实例）。"""

    def __init__(self, market_data_type: int = 1) -> None:
        self._mdt = market_data_type if market_data_type in _VALID_TYPES else 1
        self._ib: IB | None = None
        self._watchlist: list[str] = []  # 保持添加顺序
        self._contracts: dict[str, Stock] = {}
        self._tickers: dict[str, Ticker] = {}

    # ---- 连接绑定 -------------------------------------------------------

    def attach(self, ib: IB) -> None:
        """连接建立后绑定 IB 并设置行情类型。"""
        self._ib = ib
        ib.reqMarketDataType(self._mdt)

    def detach(self) -> None:
        """断开时清理（不在断开的连接上发撤订阅）。"""
        self._ib = None
        self._tickers.clear()
        self._contracts.clear()

    @property
    def connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    @property
    def market_data_type(self) -> int:
        return self._mdt

    # ---- 订阅管理 -------------------------------------------------------

    async def subscribe(self, symbol: str) -> None:
        sym = symbol.strip().upper()
        if not sym:
            return
        if sym not in self._watchlist:
            self._watchlist.append(sym)
        if self.connected:
            await self._do_subscribe(sym)

    async def _do_subscribe(self, sym: str) -> None:
        assert self._ib is not None
        try:
            contract = Stock(sym, "SMART", "USD")
            await self._ib.qualifyContractsAsync(contract)
            self._contracts[sym] = contract
            # snapshot=False → 持续 streaming；regulatorySnapshot=False → 不额外收费快照
            self._tickers[sym] = self._ib.reqMktData(contract, "", False, False)
        except Exception as exc:  # noqa: BLE001 — 单标的订阅失败不应拖垮整个面板
            log.warning("subscribe %s failed: %s: %s", sym, type(exc).__name__, exc)

    def unsubscribe(self, symbol: str) -> None:
        sym = symbol.strip().upper()
        if sym in self._watchlist:
            self._watchlist.remove(sym)
        contract = self._contracts.pop(sym, None)
        self._tickers.pop(sym, None)
        if contract is not None and self.connected:
            assert self._ib is not None
            try:
                self._ib.cancelMktData(contract)
            except Exception as exc:  # noqa: BLE001
                log.warning("cancelMktData %s: %s", sym, exc)

    async def resubscribe_all(self) -> None:
        """重连后重订所有 watchlist 标的。"""
        if not self.connected:
            return
        for sym in list(self._watchlist):
            await self._do_subscribe(sym)

    async def set_market_data_type(self, mdt: int) -> int:
        """运行时切换行情类型（如实时被占用→切延迟）。返回生效类型。重订以立即刷新。"""
        if mdt not in _VALID_TYPES:
            raise ValueError(f"无效行情类型 {mdt}（应为 1/2/3/4）")
        self._mdt = mdt
        if self.connected:
            assert self._ib is not None
            self._ib.reqMarketDataType(mdt)
            # 先撤再订，确保现有标的按新类型刷新
            for sym in list(self._watchlist):
                contract = self._contracts.get(sym)
                if contract is not None:
                    with contextlib.suppress(Exception):
                        self._ib.cancelMktData(contract)
            self._tickers.clear()
            await self.resubscribe_all()
        return self._mdt

    # ---- 读取 -----------------------------------------------------------

    def watchlist(self) -> list[str]:
        return list(self._watchlist)

    def quotes(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for sym in self._watchlist:
            ticker = self._tickers.get(sym)
            if ticker is None:
                out.append(self._empty_quote(sym))
                continue
            mdt = getattr(ticker, "marketDataType", None)
            bid, ask, last, close = (
                _num(ticker.bid),
                _num(ticker.ask),
                _num(ticker.last),
                _num(ticker.close),
            )
            out.append(
                {
                    "symbol": sym,
                    "bid": bid,
                    "ask": ask,
                    "last": last,
                    "close": close,
                    "data_type": MARKET_DATA_TYPE_NAMES.get(mdt) if mdt else None,
                    "delayed": mdt in _DELAYED_TYPES if mdt else None,
                    "has_data": any(v is not None for v in (bid, ask, last)),
                }
            )
        return out

    @staticmethod
    def _empty_quote(sym: str) -> dict[str, Any]:
        return {
            "symbol": sym,
            "bid": None,
            "ask": None,
            "last": None,
            "close": None,
            "data_type": None,
            "delayed": None,
            "has_data": False,
        }


__all__ = ["QuoteService", "MARKET_DATA_TYPE_NAMES"]

```
