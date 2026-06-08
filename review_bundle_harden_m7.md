# WebWatch Review Bundle — 切 live 前硬化 + M7 live 闸门（Day Review）

> 交给 ChatGPT / 另一个 session 对抗式审查。上一轮下单链路已 PASS；本轮是在其之上的
> **3 个 commit**（硬化 + live 闸门 + 配置）。按"输出格式"逐条给 verdict。

---

## §A 角色与任务

### A.1 角色
你是 WebWatch 本轮 code reviewer。下单链路核心上轮已三轮复核 PASS；本轮审"切 live 前硬化"
与"M7 live 切换闸门"。

### A.2 项目背景（固定）
IBKR **手动**超短线下单面板。Python 3.12 / asyncio / FastAPI / `ib_async` / **paper 4002 / live 4001**。
复用 scalper 执行层。pytest + mypy --strict + ruff。默认 paper，live 需二重互锁。

### A.3 红线模块
`pricing.py` / `order_service.py` / `broker.py` / `risk.py` / `app.py` / `config.py`(环境解析)。

### A.4 审查目标
审本轮改动正确性 + 是否引入新风险，**尤其 live 闸门是否真的防住"误入 live / live 下放大单"**，
以及硬化是否真的关闭了上轮标注的"切 live 前"项。

### A.5 审查重点（本轮）
1. **live 二重互锁**：`config.resolve_environment` —— 只有 `WEBWATCH_ENV=live` 且
   `WEBWATCH_LIVE_CONFIRM=YES` 才 live；任一缺失必须回退 paper（安全方向）。有没有任何路径能
   绕过互锁误入 live？大小写 / 空白 / 意外值如何处理？
2. **live 首周上限**：`live_max_order_notional_usd`（$20000）+ 通用 `max_order_notional_usd`（$20000）。
   两者关系（effective = min）是否如预期？live 下超限是否真拒单？
3. **D4 fail-closed**：账户不可用时超 `account_unavailable_max_notional_usd`($1000) 的单是否真 BLOCK；
   `risk.assess` 的非有限值 BLOCK。
4. **裸仓显式告警**：`_flatten_or_alert` —— 紧急平仓下单本身失败时 `flatten.failed=True` 是否正确传到前端。
5. **压短锁超时**（fill/protect 2s/2s）：是否引入新的误超时→误平风险？
6. **回归**：上轮 PASS 的下单链路控制流（Filled-first、emergency 平 filled、OCA）有没有被本轮改动破坏？

### A.6 反糊弄硬约束（一字不改）
1. 禁止措辞：「方向正确」「值得继续打磨」「在大部分情况下」「考虑」「建议进一步」「或许可以」
   「看起来不错」「质量不错」「整体合理」「有改善空间」。
2. 禁止推脱：「后续才做」「paper 不重要」「应该已处理」「按行业惯例」——用源码证明。
3. 每条 finding 必须 file:line + 代码片段。
4. 任一 P0 = verdict FAIL/PARTIAL。
5. 必须主动挖新盲区；找不到也写分析过程。

### A.7 Verdict：✅ PASS / ⚠️ PARTIAL / 🚧 WORKAROUND / 🆕 NEW BUG / ❌ NOT FIXED
### A.8 输出：§A 总评 / §B 逐条 production 改动对账 / §C NEW BUGS / §D 最终建议（能否切 live）
### A.9 安全：不暴露 secrets/账户号；不建议改 paper 4002→live 端口；不建议跳测试/降覆盖率。

---

## §B 本轮改动清单（请逐条核验）

### commit 2cd8fcb —— 切 live 前硬化
1. **D4 fail-closed**（app `_risk_for`）：已连接但账户读不到（nav=None）时，notional >
   `account_unavailable_max_notional_usd`($1000) → BLOCK；之下 WARN。另 `risk.assess` 入口加
   非有限值（NaN/Inf）BLOCK（防御深度）。
2. **压短锁持有**（broker `__init__`）：`_fill_timeout_s` 5→2、`_protect_timeout_s` 2→2，
   最坏持锁 ~4s，减少撤单/全平排队（完整"紧急路径抢占锁"留后续）。
3. **裸仓显式告警**（broker `_flatten_or_alert`）：紧急平仓 `placeOrder` 本身失败 →
   抛 `ProtectionFailed(flatten={"failed":True,...})`；前端显示"⚠ 可能裸仓，立即手动平仓"。

### commit 8b51f5c —— M7 live 闸门
4. **进 live 二重互锁**（config `RuntimeEnv` + `resolve_environment`）：`WEBWATCH_ENV=live` 且
   `WEBWATCH_LIVE_CONFIRM=YES` 才 live；缺一回退 paper + 返回警告。app lifespan 用它选环境。**已实测**：
   `WEBWATCH_ENV=live`（无 confirm）启动 → environment=paper、is_live=False、env_warning 提示。
5. **live 醒目标识**（broker snapshot 加 `is_live`/`env_warning`；前端红色脉冲横幅 + 互锁回退琥珀条）。
6. **live 首周上限**（app `_risk_for`）：`broker.is_live()` 且 notional >
   `live_max_order_notional_usd` → BLOCK。
7. **live 每笔确认**（前端）：confirm 弹窗加 "🔴 LIVE 实盘" 前缀。

### commit da2d4f6 —— 配置
8. `max_order_notional_usd` 5000→20000，使 live $20000 上限成为绑定约束（paper 也随之 20000）。

---

## §C 候选 NEW BUG（我自查的不确定点——请逐条 verdict）

### 候选-1：`resolve_environment` 用 `RuntimeEnv()`（pydantic-settings）读 env，大小写/别名健壮性
`env` 字段经 `env_prefix="WEBWATCH_"` 读 `WEBWATCH_ENV`，`case_sensitive=False`。我对 env 值做了
`.strip().lower()=="live"`、confirm 做 `.strip().upper()=="YES"`。请验证：是否存在 pydantic-settings
读取 `WEBWATCH_ENV`/`WEBWATCH_LIVE_CONFIRM` 的边界（如 `.env` 文件意外注入、嵌套 env）导致误判 live。

### 候选-2：互锁回退 paper 时是否会误连 live 端口
回退后 `load_settings(PAPER)` 读 `secrets.paper.env`（端口应 4002），且 `connect_paper_async` 防呆拒 4001。
请确认回退路径不可能用到 live 端口/账户。

### 候选-3：live 首周上限与通用上限的叠加语义
现在通用上限和 live 上限都 20000。live 下：plan 先按通用 20000 拒，_risk_for 再按 live 20000 拒
（effective=min=20000）。请确认没有"通用上限改大后 live 上限反而被绕过"的路径，以及 market 路径
用 `ref_price` 估 notional 与 live 上限的交互是否合理（真实成交价可能略高于 ref_price）。

### 候选-4：压短 fill 超时到 2s 是否让正常成交被误判 EntryUncertain
IOC 入场通常亚秒；但极端慢 gateway 下 2s 可能不够 → 误抛 EntryUncertain（语义是"状态未知，查持仓"，
非裸仓、安全方向）。请判断 2s 对 paper/live 是否过激进。

### 分析过程（A.6#5）
本轮新增的是"环境闸门 + 风控阈值"，故重点走了：① 误入 live 的所有可能输入路径（候选-1/2）；
② 上限叠加语义（候选-3）；③ 超时阈值变化的副作用（候选-4）。下单链路控制流本轮未改（仅超时常量），
Filled-first / 平 filled / OCA 逻辑沿用上轮 PASS。

---

## §D 测试 / 元信息
- 新增：`test_config.py`（互锁 4 例）、`_risk_for` live 上限 2 例、broker `is_live`/snapshot 2 例、
  `_flatten_or_alert` 裸仓告警、D4 fail-closed 2 例。
- 生成时间：2026-06-08 21:58:13 CST
- 测试：**106 passed**（0 failed / 0 skipped）；mypy --strict + ruff 通过。
- 覆盖率：TOTAL 82%（risk 100% / config 91% / app 82% / broker 70%——broker 未覆盖为
  connect/行情/IB 事件等需活连接路径）。
- commit：2cd8fcb（硬化）/ 8b51f5c（M7）/ da2d4f6（配置）。baseline b2ffaa0（上轮 PASS）。

---

## §E 附：本轮 diff（git diff b2ffaa0..HEAD）+ 关键模块全文

#### git diff b2ffaa0..HEAD（src/ + panel.yaml）####
```diff
diff --git a/config/panel.yaml b/config/panel.yaml
index 96d6645..979ed9f 100644
--- a/config/panel.yaml
+++ b/config/panel.yaml
@@ -23,13 +23,17 @@ bracket_mode: native
 market_data_type: 1
 
 risk:
-  # 单笔 notional 上限（下单前风控会拒超限单）。
-  # live 首周建议设小，例如 500，验证稳定后再放宽。
-  max_order_notional_usd: 5000
+  # 单笔 notional 上限（下单前风控会拒超限单；paper/live 通用）。
+  # 设为 20000 以让下方 live 首周上限($20000)真正成为绑定约束。
+  max_order_notional_usd: 20000
   # 单笔最大亏损 ≤ 此比例 × NAV（镜像 scalper 0.5%）。超限拒单。
   max_position_risk_pct: 0.005
   # 净值低于此值时受 PDT 限制（仅提示，不拒单）。
   pdt_min_nav: 25000
+  # 账户数据不可用（账户级风控失效）时，超过此金额的单直接拒；之下仅告警。盲飞封小额。
+  account_unavailable_max_notional_usd: 1000
+  # live 首周单笔 notional 上限（仅 live 生效）。验证期压规模，稳定后再放宽。
+  live_max_order_notional_usd: 20000
 
 commission:
   per_share_usd: 0.0035       # IBKR Pro Tiered 假设（与 scalper 一致）
diff --git a/src/webwatch/app.py b/src/webwatch/app.py
index 4439d30..9c185b7 100644
--- a/src/webwatch/app.py
+++ b/src/webwatch/app.py
@@ -20,7 +20,7 @@ from pydantic import BaseModel
 from scalper.strategy.base import Side
 
 from webwatch.broker import BrokerLike, BrokerManager, EntryUncertain, ProtectionFailed
-from webwatch.config import Environment, PanelConfig, load_panel_config, load_settings
+from webwatch.config import PanelConfig, load_panel_config, load_settings, resolve_environment
 from webwatch.order_service import (
     MarketOrderPlan,
     OrderRejected,
@@ -30,7 +30,7 @@ from webwatch.order_service import (
     plan_to_dict,
 )
 from webwatch.pricing import Target, TargetKind
-from webwatch.risk import WARN, RiskFinding, finding_to_dict
+from webwatch.risk import BLOCK, WARN, RiskFinding, finding_to_dict
 from webwatch.risk import assess as risk_assess
 from webwatch.risk import blocks as risk_blocks
 
@@ -104,16 +104,37 @@ def _risk_for(
         buying_power=buying_power,
         panel=panel,
     )
-    # 已连接但读不到账户数据 → 账户级风控（最大亏损/购买力）静默失效，必须显式告警。
+    # live 首周单笔 notional 上限（仅 live 生效）——把验证期规模封住。
+    if broker_.is_live():
+        notional = entry * quantity
+        if notional > panel.live_max_order_notional_usd:
+            findings = [
+                RiskFinding(
+                    "live_order_cap",
+                    BLOCK,
+                    f"LIVE 首周单笔上限 ${panel.live_max_order_notional_usd}，"
+                    f"本单 ${notional} 超限，已拒单",
+                ),
+                *findings,
+            ]
+    # 已连接但读不到账户数据 → 账户级风控（最大亏损/购买力）失效。
+    # fail-closed：超过"盲飞上限"的单直接 BLOCK；之下仅 WARN。
     if broker_.is_connected() and nav is None:
-        findings = [
-            RiskFinding(
+        notional = entry * quantity
+        if notional > panel.account_unavailable_max_notional_usd:
+            extra = RiskFinding(
+                "account_unavailable_block",
+                BLOCK,
+                f"账户数据不可用且金额 ${notional} 超过盲飞上限 "
+                f"${panel.account_unavailable_max_notional_usd}，已拒单（账户级风控失效时不放大单）",
+            )
+        else:
+            extra = RiskFinding(
                 "account_unavailable",
                 WARN,
                 "账户数据不可用：单笔最大亏损/购买力检查未生效，仅 notional 上限仍有效，请谨慎下单",
-            ),
-            *findings,
-        ]
+            )
+        findings = [extra, *findings]
     return findings
 
 
@@ -195,9 +216,13 @@ def create_app(broker: BrokerLike | None = None, *, auto_connect: bool = True) -
     @contextlib.asynccontextmanager
     async def lifespan(app: FastAPI) -> AsyncIterator[None]:
         panel = load_panel_config()
-        b: BrokerLike = broker if broker is not None else BrokerManager(
-            load_settings(Environment.PAPER), panel
-        )
+        if broker is not None:
+            b: BrokerLike = broker
+        else:
+            eff_env, env_warning = resolve_environment()  # live 二重互锁
+            if eff_env.value == "live":
+                log.warning("⚠️ 启动于 LIVE 实盘环境（端口 4001）")
+            b = BrokerManager(load_settings(eff_env), panel, env_warning=env_warning)
         app.state.broker = b
         app.state.panel = panel
         if auto_connect:
diff --git a/src/webwatch/broker.py b/src/webwatch/broker.py
index 3bfd184..e04f165 100644
--- a/src/webwatch/broker.py
+++ b/src/webwatch/broker.py
@@ -16,7 +16,7 @@ import contextlib
 import logging
 import math
 from decimal import Decimal
-from typing import Any, Protocol, TypeVar
+from typing import Any, NoReturn, Protocol, TypeVar
 
 from ib_async import IB, LimitOrder, MarketOrder, Stock, StopOrder, Ticker
 from scalper.execution.ib_broker_adapter import (
@@ -89,6 +89,7 @@ class BrokerLike(Protocol):
     async def connect(self) -> None: ...
     async def disconnect(self) -> None: ...
     def is_connected(self) -> bool: ...
+    def is_live(self) -> bool: ...
     async def watch(self, symbol: str) -> None: ...
     def unwatch(self, symbol: str) -> None: ...
     def snapshot(self) -> dict[str, Any]: ...
@@ -128,9 +129,12 @@ def _num(value: float | None) -> float | None:
 class BrokerManager:
     """真实 IBKR 连接管理。"""
 
-    def __init__(self, settings: IBKRSettings, panel: PanelConfig) -> None:
+    def __init__(
+        self, settings: IBKRSettings, panel: PanelConfig, *, env_warning: str | None = None
+    ) -> None:
         self._settings = settings
         self._market_data_type = panel.market_data_type
+        self._env_warning = env_warning  # live 互锁回退等提示，前端醒目展示
         self._ib: IB | None = None
         self._adapter: IbBrokerAdapter | None = None
         self._watchlist: list[str] = []  # 保持添加顺序
@@ -140,8 +144,9 @@ class BrokerManager:
         # 串行化下单/平仓操作：单个 IB 连接被多个 await 处理器共享，
         # 防止并发下单交叉（OCA 组错乱、撤单撤到刚挂的保护单等）。
         self._order_lock = asyncio.Lock()
-        # 成交等待 / 保护单确认超时（秒）。测试可调小。
-        self._fill_timeout_s = 5.0
+        # 成交等待 / 保护单确认超时（秒）。压短：下单持锁期间撤单/全平等"救命按钮"会排队，
+        # 最坏持锁 = fill + protect。IOC 入场通常亚秒级，超时只是 gateway 挂死的兜底上限。
+        self._fill_timeout_s = 2.0
         self._protect_timeout_s = 2.0
 
     # ---- 连接生命周期 ---------------------------------------------------
@@ -149,6 +154,9 @@ class BrokerManager:
     def is_connected(self) -> bool:
         return self._ib is not None and self._ib.isConnected()
 
+    def is_live(self) -> bool:
+        return self._settings.environment is Environment.LIVE
+
     async def connect(self) -> None:
         """连接 Gateway（按环境选 paper/live），并重订阅 watchlist。"""
         s = self._settings
@@ -279,6 +287,8 @@ class BrokerManager:
         return {
             "connected": connected,
             "environment": self._settings.environment.value,
+            "is_live": self.is_live(),
+            "env_warning": self._env_warning,
             "market_data_type": self._market_data_type,
             "error": self._last_error,
             "account": account,
@@ -380,12 +390,8 @@ class BrokerManager:
 
             # 有成交但成交价无效（NaN/<=0）：有仓位却无法算保护单 → 立即平仓。
             if not math.isfinite(avg) or avg <= 0:
-                flatten = self._emergency_flatten(contract, side, filled)
-                raise ProtectionFailed(
-                    filled=filled,
-                    fill_price=Decimal("0"),
-                    reason=f"成交价无效（{avg}），无法计算保护单",
-                    flatten=flatten,
+                self._flatten_or_alert(
+                    contract, side, filled, Decimal("0"), f"成交价无效（{avg}），无法计算保护单"
                 )
 
             fill_price = Decimal(str(avg))
@@ -400,10 +406,7 @@ class BrokerManager:
             except Exception as exc:  # noqa: BLE001 — 任何保护单失败都必须平仓
                 # 先撤掉任何已挂出的保护单（避免遗留单腿在平仓后反向开新裸仓），再平仓。
                 self._cancel_trades(prot_trades)
-                flatten = self._emergency_flatten(contract, side, filled)
-                raise ProtectionFailed(
-                    filled=filled, fill_price=fill_price, reason=str(exc), flatten=flatten
-                ) from exc
+                self._flatten_or_alert(contract, side, filled, fill_price, str(exc))
 
             return {
                 "filled": filled,
@@ -537,6 +540,34 @@ class BrokerManager:
             log.warning("flatten_all: 平掉 %d 个持仓", len(closed))
             return {"flattened": closed}
 
+    def _flatten_or_alert(
+        self,
+        contract: Any,
+        side: Side,
+        filled: int,
+        fill_price: Decimal,
+        reason: str,
+    ) -> NoReturn:
+        """平仓并抛 ProtectionFailed。若**平仓下单本身失败**（断连等）= 裸仓未平，
+        抛带 ``flatten.failed=True`` 的告警，让前端给出最强"立即手动平仓"提示。"""
+        try:
+            flatten = self._emergency_flatten(contract, side, filled)
+        except Exception as flat_exc:  # noqa: BLE001 — 平仓失败本身要醒目上报
+            log.critical("EMERGENCY FLATTEN FAILED → 可能裸仓: %s", flat_exc)
+            raise ProtectionFailed(
+                filled=filled,
+                fill_price=fill_price,
+                reason=f"{reason}；且紧急平仓下单失败：{flat_exc}",
+                flatten={
+                    "failed": True,
+                    "quantity": filled,
+                    "note": "紧急平仓下单失败，可能裸仓，请立即手动平仓！",
+                },
+            ) from flat_exc
+        raise ProtectionFailed(
+            filled=filled, fill_price=fill_price, reason=reason, flatten=flatten
+        )
+
     def _emergency_flatten(self, contract: Any, side: Side, quantity: int) -> dict[str, Any]:
         """红线 #3：市价平掉刚成交的仓位，不留裸仓。直接按已确认成交量 ``quantity`` 平。
 
diff --git a/src/webwatch/config.py b/src/webwatch/config.py
index 6afd7ef..8d0ad70 100644
--- a/src/webwatch/config.py
+++ b/src/webwatch/config.py
@@ -69,6 +69,36 @@ class IBKRSettings(BaseSettings):
         }
 
 
+class RuntimeEnv(BaseSettings):
+    """运行时环境选择（M7 live 二重互锁）。仅从环境变量读，不进 secrets 文件。"""
+
+    model_config = SettingsConfigDict(
+        env_prefix="WEBWATCH_", extra="ignore", case_sensitive=False
+    )
+
+    env: str = "paper"  # WEBWATCH_ENV：paper | live
+    live_confirm: str = ""  # WEBWATCH_LIVE_CONFIRM：必须 =YES 才允许 live
+
+
+def resolve_environment() -> tuple[Environment, str | None]:
+    """解析有效运行环境，带 live 二重互锁。
+
+    要进 live 必须同时 ``WEBWATCH_ENV=live`` 且 ``WEBWATCH_LIVE_CONFIRM=YES``；
+    任一缺失则**回退 paper**（安全方向）并返回警告字串供前端醒目展示。
+    返回 ``(有效环境, 警告或 None)``。
+    """
+    r = RuntimeEnv()
+    requested = (r.env or "paper").strip().lower()
+    if requested == "live":
+        if r.live_confirm.strip().upper() == "YES":
+            return Environment.LIVE, None
+        return (
+            Environment.PAPER,
+            "请求 WEBWATCH_ENV=live 但未设 WEBWATCH_LIVE_CONFIRM=YES → 已回退 paper（live 二重互锁）",
+        )
+    return Environment.PAPER, None
+
+
 def secrets_path(environment: Environment, config_dir: Path = CONFIG_DIR) -> Path:
     """该环境对应的 secrets 文件路径。"""
     return config_dir / f"secrets.{environment.value}.env"
@@ -130,6 +160,15 @@ class PanelConfig:
         self.max_position_risk_pct = Decimal(str(risk.get("max_position_risk_pct", "0.005")))
         # PDT 提示阈值：净值低于此值受日内交易限制。
         self.pdt_min_nav = Decimal(str(risk.get("pdt_min_nav", "25000")))
+        # 账户数据不可用（账户级风控失效）时，超过此 notional 的单直接拒（fail-closed）；
+        # 之下仅 WARN。盲飞时把规模封死在小额，保护真金白银。
+        self.account_unavailable_max_notional_usd = Decimal(
+            str(risk.get("account_unavailable_max_notional_usd", "1000"))
+        )
+        # live 首周单笔 notional 上限（仅 live 生效）。验证期把规模压住，稳定后再放宽。
+        self.live_max_order_notional_usd = Decimal(
+            str(risk.get("live_max_order_notional_usd", "20000"))
+        )
         comm = raw.get("commission", {}) or {}
         self.commission_per_share_usd = Decimal(str(comm.get("per_share_usd", "0.0035")))
         self.commission_min_per_order_usd = Decimal(str(comm.get("min_per_order_usd", "0.35")))
diff --git a/src/webwatch/web/index.html b/src/webwatch/web/index.html
index 20a123f..6c5e16b 100644
--- a/src/webwatch/web/index.html
+++ b/src/webwatch/web/index.html
@@ -40,6 +40,11 @@
   .err { color:var(--amber); font-size:12px; }
   .x { color:var(--muted); cursor:pointer; }
   .empty { color:var(--muted); padding:8px; }
+  #livebar { display:none; padding:8px 16px; font-weight:700; text-align:center; }
+  #livebar.live { display:block; background:var(--red); color:#fff;
+                  letter-spacing:1px; animation:pulse 1.5s infinite; }
+  #livebar.warn { display:block; background:var(--amber); color:#1a1a1a; }
+  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.7} }
   .orderform { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
   .orderform label { color:var(--muted); display:flex; align-items:center; gap:4px; }
   .orderform select { background:#0f1419; border:1px solid var(--line); color:var(--txt);
@@ -60,6 +65,7 @@
   <button id="flattenBtn" style="background:var(--red)" title="f">市价全平</button>
   <button class="ghost" id="reconnect">重连</button>
 </header>
+<div id="livebar"></div>
 
 <main>
   <section class="card full">
@@ -127,6 +133,7 @@
 
 <script>
 const $ = (id) => document.getElementById(id);
+let isLive = false;  // 由 render() 根据 state 更新；下单确认用
 const fmt = (v, d=2) => (v === null || v === undefined) ? '<span class="mut">—</span>' : Number(v).toFixed(d);
 const money = (v) => (v === null || v === undefined) ? '—' : Number(v).toLocaleString('en-US',{minimumFractionDigits:2, maximumFractionDigits:2});
 const sideCls = (s) => s === 'long' ? 'pos' : 'neg';
@@ -138,6 +145,12 @@ function render(st) {
   env.className = 'badge ' + (st.environment === 'live' ? 'live' : 'paper');
   $('dot').className = 'dot ' + (st.connected ? 'on' : 'off');
   $('conn').textContent = st.connected ? '已连接' : '未连接';
+  // LIVE 醒目横幅 / 互锁回退警告
+  isLive = !!st.is_live;
+  const bar = $('livebar');
+  if (isLive) { bar.className = 'live'; bar.textContent = '🔴 LIVE 实盘 — 真金白银，每笔下单都会用真实资金成交'; }
+  else if (st.env_warning) { bar.className = 'warn'; bar.textContent = '⚠ ' + st.env_warning; }
+  else { bar.className = ''; bar.textContent = ''; }
   $('err').textContent = st.error ? ('⚠ ' + st.error) : '';
   const mdt = {1:'实时',2:'冻结',3:'延迟',4:'延迟冻结'}[st.market_data_type] || st.market_data_type;
   $('mdtype').textContent = '行情:' + mdt;
@@ -232,7 +245,8 @@ async function buyLimit() {
   const pr = await postOrder('/api/order/preview');
   if (pr.rejected) { showOrderResult(pr, false); return; }
   const p = pr.plan;
-  let msg = `确认限价买入 ${p.symbol} ${p.quantity} 股 @ ${p.entry_limit}\n止盈 ${p.take_profit} / 止损 ${p.stop_loss}\n扣费净盈亏 ${p.net_pnl}`;
+  let msg = (isLive ? '🔴🔴 LIVE 实盘下单！真金白银！🔴🔴\n\n' : '') +
+    `确认限价买入 ${p.symbol} ${p.quantity} 股 @ ${p.entry_limit}\n止盈 ${p.take_profit} / 止损 ${p.stop_loss}\n扣费净盈亏 ${p.net_pnl}`;
   if (p.warnings && p.warnings.length) msg += '\n\n⚠ ' + p.warnings.join('\n⚠ ');
   if (!confirm(msg)) return;
   showOrderResult(await postOrder('/api/order/limit'), true);
@@ -258,9 +272,11 @@ function showMarketResult(res) {
   const el = $('orderResult');
   if (res.protection_failed) {
     const f = res.flatten || {};
-    const flatMsg = (f.quantity && f.quantity > 0)
-      ? `已紧急平仓 ${f.quantity} 股（单号 ${f.flatten_order_id}）`
-      : `<b>未能确认平仓，请立即手动检查持仓！</b>`;
+    const flatMsg = f.failed
+      ? `<b>⚠ 紧急平仓下单也失败，可能裸仓——立即手动平仓！</b>`
+      : (f.flatten_order_id && f.quantity > 0)
+        ? `已紧急平仓 ${f.quantity} 股（单号 ${f.flatten_order_id}）`
+        : `<b>未能确认平仓，请立即手动检查持仓！</b>`;
     el.innerHTML = `<span class="neg">⚠ 保护单失败！</span> 成交 ${res.filled} 股 @ ${res.fill_price} — ${flatMsg}。原因：${res.reason}`;
     return;
   }
@@ -280,7 +296,8 @@ async function buyMarket() {
   const pr = await postMarket('/api/order/market/preview');
   if (pr.rejected) { showOrderResult(pr, false); return; }
   const p = pr.plan;
-  let msg = `确认【市价买入】${p.symbol} ${p.quantity} 股（约 @ ${p.ref_price}）\n` +
+  let msg = (isLive ? '🔴🔴 LIVE 实盘下单！真金白银！🔴🔴\n\n' : '') +
+    `确认【市价买入】${p.symbol} ${p.quantity} 股（约 @ ${p.ref_price}）\n` +
     `成交后按真实成交价挂止盈止损（预览：止盈 ${p.preview_take_profit} / 止损 ${p.preview_stop_loss}）`;
   if (p.warnings && p.warnings.length) msg += '\n\n⚠ ' + p.warnings.join('\n⚠ ');
   if (!confirm(msg)) return;

```

#### `src/webwatch/config.py`（全文）####
```python
"""WebWatch 配置 — IBKR 连接设置 + 面板默认参数。

红线：
- paper(4002) / live(4001) secrets 严格分离到不同 env 文件，默认 paper。
- secrets 绝不进 git、绝不打印（见 .gitignore 与 redact）。
- 金额一律 ``Decimal``，禁止裸 ``float``。
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

from webwatch.pricing import Target, TargetKind

# 仓库根目录：.../webwatch（本文件在 src/webwatch/config.py，上溯三层）
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


class Environment(StrEnum):
    """运行环境。"""

    PAPER = "paper"
    LIVE = "live"


# 每个环境的标准端口（防呆：scalper connect_paper/connect_live 也会拒绝错配端口）。
DEFAULT_PORTS: dict[Environment, int] = {
    Environment.PAPER: 4002,
    Environment.LIVE: 4001,
}


class IBKRSettings(BaseSettings):
    """IBKR 连接 secrets。从 ``config/secrets.<env>.env`` 加载。

    字段全部可经环境变量 ``WEBWATCH_*`` 覆盖（如 ``WEBWATCH_IBKR_PORT``）。
    """

    model_config = SettingsConfigDict(
        env_prefix="WEBWATCH_",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = Environment.PAPER
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = DEFAULT_PORTS[Environment.PAPER]
    ibkr_client_id: int = 11
    # 账户号：paper 以 DU 开头，live 以 U 开头。connect_live 会校验非 DU。
    ibkr_account: str = ""

    def redacted(self) -> dict[str, Any]:
        """可安全打印的视图——账户号脱敏，绝不泄露完整账号。"""
        acct = self.ibkr_account
        masked = f"{acct[:2]}***{acct[-2:]}" if len(acct) >= 4 else "***"
        return {
            "environment": self.environment.value,
            "ibkr_host": self.ibkr_host,
            "ibkr_port": self.ibkr_port,
            "ibkr_client_id": self.ibkr_client_id,
            "ibkr_account": masked,
        }


class RuntimeEnv(BaseSettings):
    """运行时环境选择（M7 live 二重互锁）。仅从环境变量读，不进 secrets 文件。"""

    model_config = SettingsConfigDict(
        env_prefix="WEBWATCH_", extra="ignore", case_sensitive=False
    )

    env: str = "paper"  # WEBWATCH_ENV：paper | live
    live_confirm: str = ""  # WEBWATCH_LIVE_CONFIRM：必须 =YES 才允许 live


def resolve_environment() -> tuple[Environment, str | None]:
    """解析有效运行环境，带 live 二重互锁。

    要进 live 必须同时 ``WEBWATCH_ENV=live`` 且 ``WEBWATCH_LIVE_CONFIRM=YES``；
    任一缺失则**回退 paper**（安全方向）并返回警告字串供前端醒目展示。
    返回 ``(有效环境, 警告或 None)``。
    """
    r = RuntimeEnv()
    requested = (r.env or "paper").strip().lower()
    if requested == "live":
        if r.live_confirm.strip().upper() == "YES":
            return Environment.LIVE, None
        return (
            Environment.PAPER,
            "请求 WEBWATCH_ENV=live 但未设 WEBWATCH_LIVE_CONFIRM=YES → 已回退 paper（live 二重互锁）",
        )
    return Environment.PAPER, None


def secrets_path(environment: Environment, config_dir: Path = CONFIG_DIR) -> Path:
    """该环境对应的 secrets 文件路径。"""
    return config_dir / f"secrets.{environment.value}.env"


def load_settings(
    environment: Environment = Environment.PAPER,
    config_dir: Path = CONFIG_DIR,
) -> IBKRSettings:
    """加载指定环境的连接设置。

    优先级：进程环境变量 > 对应 env 文件 > 类默认值。
    若该环境的 secrets 文件不存在，则仅用环境变量/默认值（首次跑会提示去复制 example）。
    """
    env_file = secrets_path(environment, config_dir)
    settings = IBKRSettings(
        _env_file=str(env_file) if env_file.exists() else None,  # type: ignore[call-arg]
        environment=environment,
    )
    return settings


# --------------------------------------------------------------------------
# 面板默认参数（非 secrets，可进 git）—— 从 config/panel.yaml 加载
# --------------------------------------------------------------------------


class BracketMode(StrEnum):
    """挂单模式。"""

    NATIVE = "native"  # 原生 parent+TP+SL 同时发出（限价默认，零空窗）
    ON_FILL = "on_fill"  # 成交后用真实成交价精确挂 OCA 兄弟单（市价默认）


def _parse_target(raw: Any, default_kind: str, default_value: str) -> Target:
    """解析 panel.yaml 里的 {kind, value} 目标块；缺失则用默认（pct 模式）。"""
    block = raw if isinstance(raw, dict) else {}
    kind = TargetKind(block.get("kind", default_kind))
    value = Decimal(str(block.get("value", default_value)))
    return Target(kind=kind, value=value)


class PanelConfig:
    """面板默认参数。金额/比例一律 ``Decimal``。"""

    def __init__(self, raw: dict[str, Any]) -> None:
        # 止盈/止损默认目标，支持 pct / price_offset / profit_usd 三种表达。
        self.default_take_profit = _parse_target(raw.get("take_profit"), "pct", "0.002")
        self.default_stop_loss = _parse_target(raw.get("stop_loss"), "pct", "0.004")
        self.default_quantity = int(raw.get("default_quantity", 100))
        self.default_notional_usd = Decimal(str(raw.get("default_notional_usd", "2000")))
        self.default_bracket_mode = BracketMode(raw.get("bracket_mode", "native"))
        # IBKR 行情类型：1=实时(需订阅) 2=冻结 3=延迟 4=延迟冻结。
        # 超短线实盘用 1；无实时订阅时开发可设 3 看延迟报价。
        self.market_data_type = int(raw.get("market_data_type", 1))
        risk = raw.get("risk", {}) or {}
        self.max_order_notional_usd = Decimal(str(risk.get("max_order_notional_usd", "5000")))
        # 单笔最大亏损上限（占 NAV 比例），镜像 scalper 0.5% 语义。
        self.max_position_risk_pct = Decimal(str(risk.get("max_position_risk_pct", "0.005")))
        # PDT 提示阈值：净值低于此值受日内交易限制。
        self.pdt_min_nav = Decimal(str(risk.get("pdt_min_nav", "25000")))
        # 账户数据不可用（账户级风控失效）时，超过此 notional 的单直接拒（fail-closed）；
        # 之下仅 WARN。盲飞时把规模封死在小额，保护真金白银。
        self.account_unavailable_max_notional_usd = Decimal(
            str(risk.get("account_unavailable_max_notional_usd", "1000"))
        )
        # live 首周单笔 notional 上限（仅 live 生效）。验证期把规模压住，稳定后再放宽。
        self.live_max_order_notional_usd = Decimal(
            str(risk.get("live_max_order_notional_usd", "20000"))
        )
        comm = raw.get("commission", {}) or {}
        self.commission_per_share_usd = Decimal(str(comm.get("per_share_usd", "0.0035")))
        self.commission_min_per_order_usd = Decimal(str(comm.get("min_per_order_usd", "0.35")))
        self.hotkeys: dict[str, str] = dict(raw.get("hotkeys", {}) or {})


def load_panel_config(config_dir: Path = CONFIG_DIR) -> PanelConfig:
    """加载 config/panel.yaml；文件缺失时用内置默认值。"""
    path = config_dir / "panel.yaml"
    raw: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded
    return PanelConfig(raw)


__all__ = [
    "Environment",
    "DEFAULT_PORTS",
    "IBKRSettings",
    "BracketMode",
    "PanelConfig",
    "secrets_path",
    "load_settings",
    "load_panel_config",
    "REPO_ROOT",
    "CONFIG_DIR",
]

```
