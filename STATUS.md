# STATUS — WebWatch 进度交接

> **每个 session 开工第一句话：`@STATUS.md`。** 本文件是跨 session 交接的单一真相源。
> 协作约定见 [CLAUDE.md](CLAUDE.md)，完整方案见 plan 文件
> `~/.claude/plans/ibkr-api-jaunty-sprout.md`。

最后更新：**2026-06-10**（全项目审计 → P0/P1 全修：裸仓 P0、Web 安全 3×P0、模式B验证、
平仓成交验证、SHORT 保证金、tick 边界；182 单测全绿）

---

## 项目一句话

IBKR 手动超短线下单**可视化面板**：输入代码 → 市价/限价买入 → 同时自动挂止盈止损。
抓快速反弹超短线（买入达目标即卖）。**真金白银**。复用姊妹项目
[scalper](https://github.com/weiyhmail-sketch/scalper) 经 745 测试验证的 IBKR 执行层，不重造轮子。

---

## 里程碑总览

| M | 内容 | 状态 |
|---|---|---|
| M0 | 脚手架 + 复用 scalper 执行层 + 连 paper Gateway | ✅ 代码完成（连接待用户在本机验证）|
| M1 | 报价 + 持仓只读面板（FastAPI + 前端 + WS）| ✅ 完成，36 单测全绿（真实报价待 Gateway 验证）|
| M2 | pricing 模块（TP/SL 计算 + tick 取整 + 扣费净 edge）| ✅ 完成，24 单测全绿 |
| M3 | 限价买入 + 模式B 原生 bracket（`ib.bracketOrder`）| ✅ 完成，55 单测 + paper 实测三单同发 |
| M4 | 市价买入 + 模式A 成交后精确挂（OCA 兄弟单）| ✅ 代码完成，66 单测（含保护失败→平仓）；活盘 fill 待 M6 验证 |
| M5 | 风控 + 撤单/全平 + 热键 | ✅ 完成，80 单测全绿 |
| M6 | paper 验证 1–2 天 | ⏳ 待用户盘中实操（需行情权限）|
| M7 | 切 live 闸门（二重互锁 + 醒目标识 + 首周上限 + 每笔确认）| ✅ 代码完成，106 单测；实际切 live 待 M6 通过 + 用户批准 |

---

## 全项目审计 + 修复（2026-06-10，本轮）

3 个审计代理（钱链路/Web 安全/工程质量）全面审计后按 TDD 修复全部 P0/P1 + 主要 P2：

**P0（已修，均有回归测试）**：
- **裸仓 P0**：`place_market_with_protection` 中按真实成交价重算的 `compute_bracket` 在
  flatten 保护 try 块**之外**——PROFIT_USD 目标 + IOC 部分成交时每股距离 = value/filled
  被放大 → ValueError 裸冒泡 → 前端显示"已拒单"**实际裸仓**。已移入保护（失败→紧急平仓）。
- **Web 安全 3×P0**：下单端点零认证零 CORS 防护（drive-by CSRF 下单）、`/ws` 不校验 Origin
  （任意网页可读账户推送）、LAN 直连。已加 `_local_only_guard` middleware：API 全量 + 写方法
  校验 Origin/Host 在本机白名单（127.0.0.1/localhost/::1），WS accept 前校验 Origin；
  即使误用 `--host 0.0.0.0`，经 LAN IP 访问 Host 非本机仍 403。

**P1（已修）**：
- **模式B 挂出后验证**（`_verify_bracket_accepted`）：子单被拒→撤三腿；母单已成交→紧急平仓
  （app 限价/转限价路径透传 `protection_failed`，前端醒目展示）。
- **紧急平仓/全平验证成交**：`_emergency_flatten`/`flatten_all` 等终态并校验 filled，
  IOC 被撤（停牌/LULD）→ `failed=True`"仅成交 x/y 立即手动平仓"，不再谎报已平。
  `flatten_all` 平仓前先 `reqPositionsAsync` 对账（缓存滞后漏平）。
- **前端 XSS**：`esc()` 转义所有 innerHTML 后端字段（symbol/order_type/status/reason 来自
  IBKR 数据流不可信）。
- **SHORT 保证金**：risk 空头购买力按 max(entry, $2.50)/股 近似（低价股按多头 notional 会
  **低估**漏拦）+ 常驻 WARN（近似估算/未查可借券）。

**P2/防御（已修）**：跨 $1 tick 边界按腿价自身价位取整（entry<$1 而保护价≥$1 的亚分价 IB 必拒）；
模式B RTH 保护腿也 GTC（收盘过期隔夜裸仓）；connect/disconnect 进 `_order_lock`（下单中重连
换连接）；`_cancel_trades`/`_emergency_flatten` 用下单时局部 ib 引用；EntryUncertain 抛错前
best-effort 撤入场单；risk 负数量 BLOCK；单笔绝对股数上限 `max_order_shares`(10万股，仙股第二道闸)；
错误文本账户号脱敏 `_redact`。

**测试缺口补齐**：SHORT 模式A 全链路（SELL 入场/BUY 保护/BUY 紧急平仓）、SHORT off-tick 取整
方向、保护腿 PendingSubmit 超时、部分成交按 filled 挂保护。**145 → 182 单测全绿**。

**工程固化**：ruff 加 `T20`(禁 print)/`DTZ`(禁 naive datetime)/`BLE`(裸捕获须 noqa 注明)；
mypy 改按模块 override（不再全局 ignore_missing_imports）；`.coverage` 移出 git。

## 做空下单 + 脏代码标的修复（2026-06-10）

- **报价标的删不掉（已修）**：根因是脏字符（引号）混进 watchlist 代码 `A'A'O'I`，前端 ✕ 按钮
  用内联 `onclick` 字符串拼接被单引号截断。双层修复：① 后端 `quotes.py` 新增 `sanitize_symbol()`
  在 subscribe/unsubscribe 入口清洗（只留字母/数字/`.`/`-` 转大写）；② 前端报价行改 `data-sym`/`data-x`
  属性 + `#quotes` 事件委托（不再拼 onclick，任意字符都删得掉、消除注入面）。提交 `81b57d1`。
- **做空下单（已加）**：后端下单链路（pricing/broker/order_service/app/risk）**早已全程支持 `side`**，
  仅前端硬编码 `side:'long'`。本次纯前端加 **做多/做空段控**（做多·买入=红 / 做空·卖空=绿，红涨绿跌），
  切换联动按钮文字+配色+标题；`orderBody/marketBody` 用 `currentSide`；确认框动词随方向变。
  **补齐 app 端点 SHORT 端到端单测**（limit/market/preview 各一），堵住"UI 开放 SHORT 但后端路径没测"的缺口。
  paper 实测预览：SHORT entry 50 → 止盈 49.90(下方) / 止损 50.20(上方)，方向正确；LONG 对照正常。
- **145 单测全绿**，mypy --strict + ruff 通过。

## UI 重构 + 易用性 + 订单记录（2026-06-09，paper 实测）

paper 实测后按用户反馈做了一批前端/易用性改进（后端下单逻辑未动）：
- **UI 重新设计**：深色专业风 + **红涨绿跌**(A股惯例，涨/盈/买=红) + **左右分栏**(左下单票常驻/右看盘)
  + **大号行情**(选中标的) + **点报价行→自动填单选中** + **快捷手数 100/500/1000** + 大红买入按钮。
- **% 输入改百分数**：止盈止损 % 框填 0.2 = 0.2%(前端 ÷100)，修掉"0.2 被当 20%"的危险坑。
- **今日订单/成交记录**：新增 `GET /api/trades`(reqCompletedOrders + fills 取真实成交价) + 右栏卡片。
- **重连 bug 修复**：`connect()` 重连前先断开旧连接，避免同 clientId 自我冲突超时(实测触发过)。
- **行情实时化**：用户关竞争会话 + 上述重连修复 + 干净重启 → 实时盘口正常(10197 解除)。
- **paper 实盘链路验证成功**：AAOI 市价买 171.61 → 止盈卖 172.05(+0.26%)，止损 OCA 自动撤销。
- **137 单测全绿**，mypy --strict + ruff 通过。提交：ui/ux/feat/broker-fix 多个 commit 在 main。

## M8 复核（PARTIAL，无 P0）→ 已修（2026-06-09）

复核方判 **PARTIAL，无 P0**。候选-2（STP→STP-LMT mutate，本轮最高危）经复核方**实测与原生
StopLimitOrder 字段逐一相同**、IBKR 能正确识别 → 非 bug。已修复其余：
- **独立-H（P1，进 paper 前必修）**：app 只判 is_rth、不拦 CLOSED → 周末单挂成 resting 过周末。
  已接 `is_tradable_extended()`：休市（周末/全休市）**拒单**（app `_closed_response`）。
- **候选-3（开夜盘前必结）**：时段外保护腿用 DAY tif，跨 session 边界过期→裸仓。已改 **GTC**
  （broker：outside_rth 时 TP/SL tif=GTC）。夜盘 SMART 路由能否到 Blue Ocean **仍需盘中实测**
  （路由失败=不成交，安全降级；GTC 已消除边界过期裸仓）。
- **候选-1 + 独立-I（切 live 前）**：市价转激进限价后，**按实际下单价(aggressive)重跑风控**+block，
  使"被风控价=被下单价"；响应 risk findings 也用 aggressive。
- 候选-4（节假日历）：按复核方建议 paper 阶段暂不做（判错只是"市价可点不成交"，安全）。
- **133 单测全绿**（+CLOSED 拒单/GTC 断言），mypy --strict + ruff 通过。
- 复核结论：**可进 paper 盘中（RTH+盘前盘后）验证**；夜盘 routing 与候选-1 在切 live 前再确认。

## M8 — 24 小时交易（盘前/盘后/夜盘）（2026-06-08，按用户要求）

用户要 24h 都能交易。IBKR 硬规则：时段外不收市价单 & 普通 STP 不触发，只收限价 + stop-limit + outsideRth。
按用户决策实现（完整 24h 含夜盘 / 止损用 stop-limit / 市价转激进限价）：
- [session.py](src/webwatch/session.py)：纯 stdlib(zoneinfo) 判 ET 时段（RTH/盘前/盘后/夜盘/休市）。
  不接节假日历——判错只会"不成交"或"用更保守单型"，安全。
- [pricing.py](src/webwatch/pricing.py)：`aggressive_limit`（市价转激进限价）+ `stop_limit_price`（止损限价缓冲）。
- [broker.py](src/webwatch/broker.py)::`place_limit_bracket(outside_rth=True)` → 三腿全 `outsideRth=True`，
  止损从 STP **转 STP-LMT**（复用 ib.bracketOrder 再 mutate）。
- [app.py](src/webwatch/app.py)：限价单时段外自动带 outsideRth；**市价单时段外自动转激进限价 bracket**
  （`converted_to_limit`）；snapshot 暴露 `session`/`is_rth`。
- [前端](src/webwatch/web/index.html)：顶部时段徽章（RTH 绿/时段外琥珀/休市灰）；行情类型下拉；
  市价 confirm 在时段外提示"将转激进限价 + stop-limit"。
- config：`extended_hours_enabled`(默认 true)、`aggressive_limit_ticks`(3)、`stop_limit_offset_pct`(0.005)。
- 17 新测试（session/pricing/broker outside_rth/app 转换）。**131 单测全绿**，mypy --strict + ruff 通过。
- ⚠️ **caveats**（用户已知）：夜盘仅部分标的可交易、流动性薄点差宽（+0.2% 易被吃）；
  stop-limit 极端跳空可能不成交；session 不含节假日历。

## 行情模块重做为独立 quotes.py（2026-06-08，按用户要求）

用户指出不要依赖 scalper 行情代码。**事实确认**：webwatch 从未 import scalper 行情模块
（无 realtime_bar_subscriber/historical_loader/tick_validator）——一直是自有 `reqMktData`。
本轮把行情从 broker.py **抽成独立 [quotes.py](src/webwatch/quotes.py)（`QuoteService`）**，并增强：
- 用 ib_async `reqMktData` **L1 逐笔 tick 流**（非 scalper 的 5 秒 K 线 + HMDS 限流/卡死那套）。
- **运行时切换行情类型**（实时/冻结/延迟/延迟冻结）：实时被占用(10197)时一键切免费延迟行情，无需重启。
  `POST /api/market_data_type`；前端下拉。
- 每个报价上报**实际行情类型**（ticker.marketDataType）→ 前端显示"实时/延迟"，防误判。
- broker.py 仅委托（watch/unwatch/set_market_data_type/snapshot.quotes）；行情完全独立于 scalper。
- 14 单测覆盖（[test_quotes.py](tests/unit/test_quotes.py)）。**当前 114 单测全绿**。

## ✅ 下单链路复核 PASS（2026-06-08，round 3 通过）

外部复核方 round-3 判 **PASS**。下单链路（pricing/order_service/broker/risk/app）经
**自查 8 个（5 P0）+ 外审 2 轮各 1 个 P0**，全部修复 + 回归测试，**95 单测全绿**，mypy --strict + ruff 通过。
裸仓红线 #3 的四类失效（反向开仓 / 漏平 / 误判保护失败 / 入场超时静默）已逐一堵死。

**切 live 前硬化清单**（三轮复核累积）：
- [x] **1. D4 fail-closed**：账户不可用时，超 `account_unavailable_max_notional_usd`($1000) 的单直接拒，
  之下仅 WARN（app `_risk_for`）。
- [x] **2. 压短锁持有**：fill/protect 超时 5s/2s → **2s/2s**，最坏持锁 ~4s，减少撤单/全平排队
  （完整"紧急路径抢占入场锁"留作后续；当前 IOC 亚秒、影响小）。
- [x] **3. 裸仓显式告警**：紧急平仓下单本身失败 → `_flatten_or_alert` 抛 `flatten.failed=True`，
  前端显示"⚠ 可能裸仓，立即手动平仓"。
- [x] **4. 做空保证金近似**（2026-06-10 审计修复）：空头购买力按 max(entry, $2.50)/股 估
  + 常驻 WARN（近似/未查可借券 HTB）。完整 IB 分档规则与 shortability 查询留切 live 前评估。
- [x] **5. 紧急平仓对账**（2026-06-10 审计修复）：`_emergency_flatten`/`flatten_all` 等终态
  并校验 filled，未平→`failed=True` 强告警；`flatten_all` 先 `reqPositionsAsync` 对账。
- [ ] 6. EntryUncertain 路径自动对账平仓（已加 best-effort 撤入场单；完整对账靠用户手动查，已醒目提示）。
- [x] **M7 红线 #1 live 切换**（已实现）：
  - 进 live 二重互锁：`WEBWATCH_ENV=live` **且** `WEBWATCH_LIVE_CONFIRM=YES`，缺一回退 paper（已实测）。
  - live 醒目标识：顶部红色脉冲横幅 + env 徽章红；互锁回退时琥珀色警告条。
  - live 首周单笔上限：`live_max_order_notional_usd`（$20000，仅 live 生效，超限拒单）。
  - live 每笔下单 confirm 弹窗加 "🔴 LIVE 实盘" 前缀。
  - 通用 `max_order_notional_usd` 已调到 **$20000**（commit da2d4f6），与 live 上限对齐
    → 实际每笔上限 $20000（paper/live 通用）。要更保守可下调它。

### 如何切 live（M6 paper 验证通过 + 用户明确批准后）
```bash
cp config/secrets.env.example config/secrets.live.env   # 填 live 账户号(U 开头)，端口 4001
WEBWATCH_ENV=live WEBWATCH_LIVE_CONFIRM=YES uv run uvicorn webwatch.app:app
```
缺 `WEBWATCH_LIVE_CONFIRM=YES` 会自动回退 paper（安全）。

硬化后 **98 单测全绿**，mypy --strict + ruff 通过。

## 复审 round 3（2026-06-08）—— round-2 修复自身的新 P0，已修

round-2 的复核又发现 1 个 P0（我在 round-2 复审包 §C 候选-2 自己标了"不确定"，复核确认是真的）：
- **P0（已修）**：round-2 把 `_emergency_flatten` 改成"读 `get_position` 对账、flat 则 no-op"，
  但 `get_position`→`ib.positions()` 读的是 **positionEvent 缓存**，与成交事件是两条独立流；
  入场刚成交那几百毫秒缓存常滞后→读到 0→**跳过平仓**→谎报已平，真实持仓裸仓（红线 #3 另一方向）。
  **修复**：紧急平仓**直接按已确认成交量 `filled` 平**，不依赖持仓缓存。
  依据复核确认的关键事实：上游 `_verify_protection_live` 的 Filled-first 已把"任一保护腿成交=成功"
  分流返回，故能走到紧急平仓时持仓**必为满额 filled**，直接平最安全。
  新增回归测试模拟"缓存裸 0 但实际成交 10"→断言仍平 10。前端 protection_failed 文案也改为
  平仓量为 0 时显示"未能确认平仓，请手动检查"。
- 复核确认：候选-1（部分成交）非 bug；BLOCKER-2/3/4 全 PASS。
- **切 live 前仍需**（复核标注，非 paper 阻塞）：D4 升级为账户不可用时大单 fail-closed、
  flatten_all/cancel_all 紧急路径不竞争入场锁（或压缩 fill 超时）、EntryUncertain 路径评估自动对账、
  紧急平仓 placeOrder 失败的裸仓显式告警。

## 复审 round 2（2026-06-08）—— ChatGPT 判 FAIL，已修 P0

外部复核（ChatGPT）发现 1 个 **P0 裸仓 bug**（自查漏掉）+ 若干 P1/P2，已修：
- **P0（已修）**：止盈成交后 OCA 撤兄弟止损腿（status=Cancelled），`_verify_protection_live`
  把这个**成功路径**误判为"保护单异常"→ 紧急平仓对已平仓位反向下单 = 开反向裸仓。
  这恰是"+0.2% 快速止盈"高频路径。修复：① 任一保护腿 `Filled` 即判出场成功返回；
  ② `_emergency_flatten` 改为**按真实持仓对账**（`get_position`，flat 则 no-op），
  结构上杜绝"对空仓反向下单"。加了 `TP=Filled/SL=Cancelled` 与 `flat→no-op` 回归测试
  （旧 fake 用单一状态给两腿，无法表达非对称终态，故漏测）。
- **P1a（已修）**：`cancel_all_orders` 改 async 并纳入 `_order_lock`，避免撤掉别处刚挂的保护单。
- **P2（已修）**：入场超时改抛 `EntryUncertain`，app 返回 `entry_uncertain:true`（非 rejected），
  前端醒目提示查持仓，不再被"rejected"语义吞掉。
- **D4（已修）**：连接但账户读不到时，风控不再静默放行——顶一条 WARN 到前端。
- **防御深度（已修）**：`risk.assess` 自身加非有限值 BLOCK（不依赖上游）。
- **切 live 前仍需做**（复核 §D/§C 标注，已记入下方待办）：flatten_all 平后对账+重试、
  verify 超时改对账而非盲平、_emergency_flatten 平后校验成交、真实做空保证金、API 文档约束。
- **当前 95 单测全绿**，mypy --strict + ruff 全过。

## 下单链路自查 + 加固（2026-06-08，复核包已出）

3 个 finder agent 对钱链路做 high-recall 自查，发现并**已修 8 个真问题（5 个 P0 裸仓/护栏绕过类）**，
全部加回归测试，**91 测试全绿**。详见 [review_bundle_m0-m5.md](review_bundle_m0-m5.md)（已生成，待 ChatGPT/另 session 复核）。
- 已修：NaN/Inf 绕过护栏、目标过大算出负保护价、入场超时静默当未成交、保护单瞬态误判已挂活、
  保护单单腿遗留、成交价无效未平仓、限价未取整到 tick、并发下单无锁。
- **未修(已知限制，待复核判断)**：_emergency_flatten IOC 未校验成交、flatten_all 竞态、
  risk max_loss 毛损/空头保证金、risk 账户读取失败静默放行、ProtectionFailed 返回 200。

## 已完成细节

### M0 — 脚手架（代码✅ / 真实连接待验证）
- [pyproject.toml](pyproject.toml)：scalper pin 到 commit `1f335cbc`（git ssh 依赖）；
  `allow-direct-references=true`。Python 3.12 venv（uv），vectorbt 等重依赖构建成功。
- [src/webwatch/config.py](src/webwatch/config.py)：pydantic-settings，paper(4002)/live(4001)
  secrets 严格分离，默认 paper；账户号脱敏 `redacted()`；`PanelConfig` 读 panel.yaml。
- [config/panel.yaml](config/panel.yaml)、[config/secrets.env.example](config/secrets.env.example)
- [scripts/check_connection.py](scripts/check_connection.py)：连接 smoke test（默认 paper）。
- [CLAUDE.md](CLAUDE.md)、[README.md](README.md)
- 验证：scalper 复用模块 import 干净；`IB.bracketOrder` 可用；ruff + mypy --strict 全过。
- ⚠️ **真实 Gateway 连接尚未验证**——需用户配 secrets + 起 Gateway 跑 check_connection（见下）。

### M5 — 风控 + 撤单/全平 + 热键（✅ 80 单测全绿）
- [risk.py](src/webwatch/risk.py)：下单前账户感知风控（纯函数）—— 单笔最大亏损 ≤ 0.5% NAV(BLOCK)、
  金额≤购买力(BLOCK)、NAV<$25k PDT 提示(WARN)。[test_risk.py](tests/unit/test_risk.py)。
  **决策**：未直接复用 scalper `RiskManager`（其 RiskState 依赖 daily_pnl/consecutive_stops 等
  daemon 有状态跟踪，手动面板不维护，强行构造易错）；改用透明逐单检查。详见 risk.py 模块 docstring。
- [broker.py](src/webwatch/broker.py)：`risk_inputs()`(NAV/购买力)、`cancel_all_orders()`(只撤单)、
  `flatten_all()`(**先撤所有挂单含保护单 → 再市价平所有持仓**，避免平仓后保护单又开新仓)。
- [app.py](src/webwatch/app.py)：下单前跑风控，BLOCK→400 不下单，WARN 随响应返回；
  `POST /api/cancel_all` + `/api/flatten_all`。
- [web/index.html](src/webwatch/web/index.html)：顶栏「撤全部挂单」「市价全平」(红, confirm)；
  **键盘热键 b=市价 / l=限价 / f=全平 / c=撤单**(输入框聚焦时不触发)；风控 WARN 显示在下单结果区。

### M4 — 市价买入 + 模式A 成交后精确挂（✅ 代码完成，活盘 fill 待验证）
- [broker.py](src/webwatch/broker.py)::`place_market_with_protection` —— 市价(IOC)买入 →
  `_wait_done` 等成交 → 用 `orderStatus.avgFillPrice` 真实成交价 `compute_bracket` →
  `_place_oca_protection` 挂 TP(限价)+SL(stop) OCA 兄弟单 → `_verify_protection_live` 确认挂活。
  **红线 #3**：保护单异常/超时 → `_emergency_flatten` 立即市价平仓 + 抛 `ProtectionFailed`。
- [order_service.py](src/webwatch/order_service.py)::`plan_market_order` —— 用参考价(ref_price)
  做 notional 风控 + 预览括号价（实际按成交价重算）。
- [app.py](src/webwatch/app.py)：`POST /api/order/market/preview` + `/api/order/market`；
  `ProtectionFailed` 时返回 `protection_failed:True` + 平仓信息，前端醒目红字告警。
- [web/index.html](src/webwatch/web/index.html)：新增「市价买入」按钮（橙色），confirm 弹窗 +
  保护单失败醒目提示。
- 测试：[test_broker.py](tests/unit/test_broker.py) 用 fake IB 覆盖 fill→OCA / 未成交 /
  保护单 Cancelled→平仓 / 保护单 placeOrder 抛错→平仓 四条路径。
- ⚠️ **活盘 fill→挂 OCA 路径待 M6 盘中验证**（市场关闭时市价 IOC 不成交，无法验证 fill 分支）。

### M3 — 限价买入 + 模式B 原生 bracket（✅ paper 实测通过）
- [src/webwatch/order_service.py](src/webwatch/order_service.py)：`plan_limit_bracket` 纯函数 ——
  算 TP/SL 价 + 扣费净 edge + 下单前校验（单笔 notional 上限）+ 软告警（<1tick / 净亏）；
  硬性拒绝抛 `OrderRejected`。[test_order_service.py](tests/unit/test_order_service.py)。
- [broker.py](src/webwatch/broker.py)::`place_limit_bracket` —— 用 `ib.bracketOrder` 一次发出
  母单(限价)+止盈(限价)+止损(stop)，自带 parentId/transmit 链，**保护单零空窗**。Decimal 仅在 IB
  边界转 float。[test_broker.py](tests/unit/test_broker.py) 用 fake IB 验证三单同发。
- [app.py](src/webwatch/app.py)：`POST /api/order/preview`（预览，不下单）+ `POST /api/order/limit`
  （下单）。服务端**总是重新计算**价格，不信任前端传的价。风控拒单返回 400 且不下单。
- [web/index.html](src/webwatch/web/index.html)：下单表单（代码/数量/限价/止盈止损模式+值/预览/买入），
  买入前 confirm 弹窗显示 TP/SL/净盈亏/告警。
- **paper 实测**（2026-06-08）：AAPL 买入限价$50 + TP$50.10 + SL$49.80 三单同时挂出，结构正确
  （limit-long-50 / limit-short-50.10 / stop-short-49.80），`cancel_all` 清零。

### M1 — 报价/持仓只读面板（✅ 36 单测全绿）
- [src/webwatch/serialize.py](src/webwatch/serialize.py)：scalper 对象 → JSON-safe dict（账户号脱敏、
  Decimal→str、持仓未实现盈亏）。纯函数，[test_serialize.py](tests/unit/test_serialize.py) 8 测。
- [src/webwatch/broker.py](src/webwatch/broker.py)：`BrokerManager` —— 持有单个 IB 连接，
  async connect（paper/live）、账户/持仓/挂单读取、watchlist + 实时报价订阅、`snapshot()` 全状态；
  **对断连容错**（未连上返回空、记录 error，不崩）。`BrokerLike` Protocol 供测试注入 fake。
- [src/webwatch/app.py](src/webwatch/app.py)：FastAPI —— `GET /`、`GET /api/state`、
  `POST/DELETE /api/watch`、`POST /api/reconnect`、`WS /ws`（~300ms 推送）。
  `create_app(broker, auto_connect)` 可注入 fake，[test_app.py](tests/unit/test_app.py) 4 测。
- [src/webwatch/web/index.html](src/webwatch/web/index.html)：单页前端 —— 环境徽章(paper绿/live红)、
  连接状态、账户摘要、报价表(加/删标的)、持仓表(浮盈着色)、挂单表；WS 自动重连。
- 验证：`uvicorn webwatch.app:app` 起得来，无 Gateway 时优雅显示"未连接"+友好错误；
  watchlist 持久化（连上后自动订阅）。⚠️ 真实报价/持仓待 Gateway 验证。
- 行情类型 `market_data_type`（panel.yaml）：1实时/2冻结/3延迟/4延迟冻结，默认 1。

### M2 — pricing 计算核心（✅ 24 单测全绿）
- [src/webwatch/pricing.py](src/webwatch/pricing.py)、[tests/unit/test_pricing.py](tests/unit/test_pricing.py)
- **止盈/止损目标多模式**（`Target`）：
  - `pct` 百分比（+0.2%/+0.3%/+0.5%）
  - `price_offset` 每股价格偏移（成交价 +$1/+$2）
  - `profit_usd` 总盈/亏金额（盈利 $100/$200 即止盈，按手数反推每股）
- tick 取整（≥$1→$0.01，<$1→$0.0001）；止盈向上取整保证不缩水；
  **低价股目标<1tick 告警**（`tp_below_min_tick`）。
- `net_edge`：扣佣金/监管费的净盈亏预估（服务"盈利高于手续费"诉求）。
- `shares_for_notional`：按金额算整股数。

---

## 关键决策（已锁定，勿擅自推翻）

1. **新仓库** webwatch + 把 scalper 当 git 依赖复用（不在 scalper 仓库改动）。
2. **本地 Web 面板**：FastAPI 后端 + 浏览器前端。
3. **两种挂单模式都做、可切换**：限价→原生 bracket（模式B，`ib.bracketOrder`）；
   市价→成交后精确挂 OCA 兄弟单（模式A）。
4. **live 安全闸**：代码支持 paper/live 切换，**默认 paper**，paper 验证 1–2 天后再切 live。
5. **止盈/止损支持 pct / price_offset / profit_usd 三种表达**（用户 2026-06-08 追加）。

---

## 待用户拍板 / 待办

- [x] **paper 连接已实测**（2026-06-08）：账户/持仓链路 OK，净值 ~$1.02M。
- [ ] **IBKR 行情权限**：reqMktData 报 `Error 10197（关联真实账户登录期间无市场数据）`，
  所有报价 nan。属 IBKR 账户层问题（竞争会话 / paper 未共享行情订阅），非代码问题。
  已交另一个 Claude session 处理（关竞争会话 / 官网开 paper 行情共享 / 盘中再测）。
  **不阻塞开发**（下单链路不依赖行情）。代码已把 IB 真错误抓到面板 `error` 字段。
- [ ] **决策：`profit_usd` 目标算 gross 还是 net？** 当前实现为 **gross**（浮盈达标即触发，
  与 IBKR 浮盈显示一致）；net（扣完来回佣金净赚）待用户确认是否要改。
- [ ] **建议 ChatGPT 复核 `pricing.py`**（符号/取整/扣费），按"Claude 开发+ChatGPT 复核"流程。
  可用 `review-bundle-generator` skill 打包。
- [ ] **下一步：M6 盘中验证**（需美股开盘 + 行情权限）：市价买入真实成交 → 自动挂 OCA；
  限价 bracket 已验证；撤单/全平/热键在 paper 上点一遍。代码已就绪，等盘中。
- [ ] **M7 切 live 闸门**：双重确认 + live 醒目标识 + 首周小手数上限，用户明确批准后切 4001。
- [ ] 全部下单代码（M2–M5）建议过一轮 ChatGPT/另 session 复核（pricing/order_service/broker/risk）。

---

## 复用的 scalper 资产速查

| 用途 | 导入路径 | 备注 |
|---|---|---|
| 连接 | `scalper.execution.ib_broker_adapter.connect_paper` / `connect_paper_async` / `connect_live` | 同步版给脚本；**async 版给 FastAPI 事件循环**；防呆拒错配端口 |
| 适配器 | `scalper.execution.ib_broker_adapter.IbBrokerAdapter` | ⚠️ 类名是 **Ib**BrokerAdapter（非 IBBrokerAdapter）；`__init__(ib, *, paper_account, on_disconnect=None, on_reconnect=None)` |
| 下单接口 | `scalper.execution.broker_adapter.BrokerAdapter`（Protocol）| place_order/cancel/positions/fills/account_summary |
| 订单类型 | `scalper.execution.types`（Order/OrderId/OrderType/Position/Fill/AccountSummary）| 金额全 Decimal |
| OCA 分组 | `scalper.execution.oca_group.make_oca_group` / `DEFAULT_OCA_TYPE`（=1 CANCEL_WITH_BLOCK）| 模式A 的 TP/SL 兄弟单 |
| 原生 bracket | `ib_async.IB.bracketOrder(action, qty, limitPrice, takeProfitPrice, stopLossPrice)` | 模式B；webwatch 自己持有 IB 实例直接调，不改 scalper |
| 方向枚举 | `scalper.strategy.base.Side`（LONG/SHORT）| |
| 风控 | `scalper.risk.manager` / `scalper.risk.rules` | M5 下单前检查 |

**关键事实**：webwatch 自己 `ib = IB()` 后调 `connect_paper(ib, ...)` 再 `IbBrokerAdapter(ib, ...)`，
所以 webwatch **持有同一个 IB 实例**，模式B 可直接 `ib.bracketOrder(...)`，无需动 scalper。

---

## 常用命令

```bash
uv sync                                          # 安装依赖（首次较重）
uv run python scripts/check_connection.py        # 连接 smoke（默认 paper）
uv run pytest                                     # 全部测试
uv run pytest -m "not integration"               # 仅单元（不连 Gateway）
uv run mypy src && uv run ruff check src tests    # 类型 + lint
uv run uvicorn webwatch.app:app --reload         # 起面板（M1 后可用）
```

---

## 下一个 session 怎么接

1. 读本文件 + [CLAUDE.md](CLAUDE.md)。
2. 确认"待用户拍板/待办"里连接是否已验证、profit_usd gross/net 是否已定。
3. 按里程碑表挑下一个 ⏳，遵守红线（下单/计算改动先写测试）。
4. 改下单链路/计算前先读对应测试；完成后跑 pytest + mypy + ruff 全绿。
5. 每轮结束更新本 STATUS.md 的里程碑表与"已完成细节"。
