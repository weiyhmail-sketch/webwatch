# WebWatch Review Bundle — Round 3 复审（round-2 修复自身的 P0 已修）

> 你是同一位审阅员。round-2 你判 FAIL，唯一新阻塞是 candidate-2（`_emergency_flatten` 信任滞后的
> 持仓缓存裸 0 → 漏平裸仓）。本轮只动了这一处（+ 配套测试/前端文案）。请核验是否真关闭，并扫描新 bug。

---

## §A 角色与任务

### A.1 角色
WebWatch 同一位审阅员。round-2 verdict=FAIL，新阻塞 1 条 P0（candidate-2 漏平裸仓）；
其余 BLOCKER-2/3/4 你已判 PASS，candidate-1 你判非 bug。

### A.2 项目背景（固定）
IBKR **手动**超短线下单面板。Python 3.12 / asyncio / FastAPI / `ib_async` / **paper 4002**（live 不启用）。
复用 scalper 执行层。pytest + mypy --strict + ruff。

### A.3 审查目标
核验 candidate-2 是否真修 + 扫描本轮极小改动自身是否引入新 bug。第一红线：成交后绝不留裸仓、绝不反向开仓。

### A.4 反糊弄硬约束（一字不改）
1. 禁止措辞：「方向正确」「值得继续打磨」「在大部分情况下」「考虑」「建议进一步」「或许可以」
   「看起来不错」「质量不错」「整体合理」「有改善空间」。
2. 禁止推脱：「后续才做」「paper 不重要」「应该已处理」「按行业惯例」——用源码证明。
3. 每条 finding 必须 file:line + 代码片段。
4. 任一 P0 = verdict FAIL/PARTIAL。
5. 必须主动挖新盲区；找不到也写分析过程。

### A.5 Verdict：✅ PASS / ⚠️ PARTIAL / 🚧 WORKAROUND / 🆕 NEW BUG / ❌ NOT FIXED
### A.6 输出：§A 总评 / §B 阻塞修复对账 / §C NEW BUGS / §D 一致性扫描 / §E 准入结论
### A.7 安全：不暴露 secrets/账户号；不建议改 paper 4002→live；不建议跳测试/降覆盖率。

---

## §B 阻塞修复对账

### BLOCKER（round-2 P0 / candidate-2）`_emergency_flatten` 信任滞后持仓缓存 → 漏平裸仓
**round-2 判语**：`get_position`→`ib.positions()` 读 positionEvent 缓存，与成交事件独立；刚成交几百 ms 内
缓存裸 0 → 跳过平仓 → 谎报已平 → `filled` 股裸仓。且 `get_position` 无 try，`list_positions` 瞬断会抛
RecoverableError 致漏平。

**本轮修复**：紧急平仓**直接按已确认成交量 `quantity`(=filled) 平**，彻底不读持仓缓存（连带消除
get_position 抛异常的漏平路径）。

修复后 `_emergency_flatten`（broker.py）：
```python
def _emergency_flatten(self, contract, side, quantity):
    assert self._ib is not None
    close_action = "SELL" if side is Side.LONG else "BUY"
    flat = MarketOrder(close_action, quantity)
    flat.tif = "IOC"
    self._ib.placeOrder(contract, flat)
    log.error("PROTECTION FAILED → 紧急平仓 %s %s x%s", contract.symbol, close_action, quantity)
    return {"flatten_order_id": flat.orderId, "action": close_action, "quantity": quantity}
```

**正确性依据**（请你核验这条推理）：能走到 `_emergency_flatten` 只有两条路径，持仓都必为满额 `filled`：
1. **成交价无效**路径：`filled>0` 刚确认、**尚未挂任何保护单** → 仓位 100% = filled。
2. **保护失败 except** 路径：`_verify_protection_live` 的 **Filled-first** 已在上游把"任一保护腿 Filled=
   出场成功"`return` 掉；能落到 except 的只剩"无任何腿成交"（挂单抛错 / 全 BAD / 超时未确认）→ 仓位仍 = filled。
故直接平 `filled` 不会过量、不会反向；而信任缓存裸 0 才会漏平。这正是你 round-2 给的修法方向（择 b：直接平 filled）。

**回归测试**（替换了 round-2 那条把危险路径当正确的测试）：
`test_emergency_flatten_uses_filled_not_position_cache` —— `MarketFakeAdapter(position_qty=0)` 模拟
"缓存裸 0（滞后）"，但实际成交 10 股；断言**仍下出 10 股平仓单**（`flatten.quantity==10`、
平仓 MKT `totalQuantity==10`），证明不再因缓存裸 0 漏平。

**前端**：`protection_failed` 文案改为按 `flatten.quantity` 显示——`>0` 显示"已紧急平仓 N 股（单号 X）"，
否则显示"**未能确认平仓，请立即手动检查持仓！**"（消除 round-2 "已平仓 单号 null" 的虚假安心）。

---

## §C 候选 NEW BUG（本轮极小改动自身——逐条 verdict）

### 候选-1：`_cancel_trades` 与 `_emergency_flatten` 之间的窄竞态
except 路径里先 `_cancel_trades(prot_trades)`（best-effort 撤保护腿）再 `_emergency_flatten(filled)`。
若某保护腿在"撤单已发出但未生效"的极窄窗口内成交，持仓被该腿平掉一部分，随后 `_emergency_flatten`
仍按 `filled` 平 → 过量 → 反向开仓。
我的评估：窗口远小于 round-2 的缓存滞后（那是每次几百 ms；这是"撤单与成交在同一 tick 撞上"），且
能进 except 意味着 2s verify 窗口内**没有腿 Filled**，该腿恰在 cancel 后那一瞬成交概率极低。
倾向 paper 可接受、切 live 前用"平后对账"兜。**请你定**是真 P-级风险还是可接受残留。

### 候选-2：`_emergency_flatten` 的 `placeOrder` 失败 = 裸仓但仅 502
紧急平仓的 `placeOrder` 若抛错（断连等），异常冒泡到 app 通用 except → 502 "下单失败"，
此时入场已成交、保护未挂、平仓也失败 = 裸仓，但前端只显示"下单失败"，无"裸仓！手动平"强提示。
我的评估：罕见（刚成功下入场单，紧接着 placeOrder 抛错的窗口很小），列为**切 live 前**硬化项
（紧急平仓失败应显式抛"裸仓未平"告警态，类似 EntryUncertain）。请你判断是否 paper 也必须修。

### 候选-3：测试侧 `MarketFakeAdapter.get_position` / `_bm_with(position_qty=...)` 现已成死代码
`_emergency_flatten` 不再读 `get_position`，故测试里的 `MarketFakeAdapter.get_position` 与
`position_qty` 参数对市价路径不再生效（仅 `test_emergency_flatten_uses_filled_not_position_cache`
靠它表达"缓存=0"的语义，但代码已不读它，该断言实际只验证了"按 filled 平"）。非生产 bug，
但提示测试语义与实现已解耦——请确认这不影响你对"漏平已修"的信心（核心断言是"缓存=0 时仍平 filled"，
而代码根本不看缓存 → 结构上保证不漏平，比对账更强）。

### 分析过程（A.4#5）
本轮只改 1 个函数（删除缓存依赖）+ 1 测试 + 1 前端文案。新风险只可能来自"直接平 filled 是否会过量"，
故逐条走了到达 `_emergency_flatten` 的两条路径证明持仓恒为 filled（§B），再找"filled 与真实持仓何时会
不一致"——唯一来源是 cancel→flatten 间的保护腿成交窄竞态（候选-1）。

---

## §D 测试 / 元信息
- 新增/改：`test_emergency_flatten_uses_filled_not_position_cache`（替换 round-2 的 noop 测试）。
- 生成时间：2026-06-08 21:16:31 CST；分支 main（无 commit）。
- 测试：**95 passed**（0 failed / 0 skipped）；mypy --strict + ruff 通过；覆盖率 TOTAL ~81%。

---

## §E 附：相关源码
（仅 `_emergency_flatten` 改动；`_verify_protection_live` 的 Filled-first 是本修复的正确性前提，一并附。
完整 broker.py 见 round-2 bundle §F，本轮除 `_emergency_flatten` 外未变。）

#### place_market_with_protection（含两条 emergency 调用路径）####
```python
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

```

#### _verify_protection_live（Filled-first，本修复的正确性前提）####
```python
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

```

#### _emergency_flatten（本轮修复）####
```python
    def _emergency_flatten(self, contract: Any, side: Side, quantity: int) -> dict[str, Any]:
        """红线 #3：市价平掉刚成交的仓位，不留裸仓。直接按已确认成交量 ``quantity`` 平。

        为什么直接平 ``quantity`` 而**不**读 ``get_position`` 对账：能走到这里，上游
        ``_verify_protection_live`` 的 Filled-first 已把"任一保护腿成交=出场成功"分流返回，
        故两条调用路径（成交价无效 / 保护单挂失败且无腿成交）下持仓**必为满额 quantity**。
        而 ``ib.positions()`` 缓存由 positionEvent 推送，与成交事件是两条独立流、刚成交那几百
        毫秒常滞后——此刻信任缓存裸 0 会**漏平真实持仓 = 裸仓**（比直接平更危险）。
        """
        assert self._ib is not None
        close_action = "SELL" if side is Side.LONG else "BUY"
        flat = MarketOrder(close_action, quantity)
        flat.tif = "IOC"
        self._ib.placeOrder(contract, flat)
        log.error(
            "PROTECTION FAILED → 紧急平仓 %s %s x%s", contract.symbol, close_action, quantity
        )
        return {"flatten_order_id": flat.orderId, "action": close_action, "quantity": quantity}


```

#### 新回归测试 ####
```python

    async def test_emergency_flatten_uses_filled_not_position_cache(self) -> None:
        # 【round-2 P0】紧急平仓必须按已确认成交量平，绝不能因 positions() 缓存滞后(裸 0)
        # 而跳过平仓 → 漏平裸仓。position_qty=0 模拟"刚成交、持仓缓存尚未刷新"。
        fake = MarketFakeIB(tp_status="Cancelled", sl_status="Cancelled")
        bm = _bm_with(fake, position_qty=0)  # 缓存读到 0（滞后），但实际刚成交 10 股
        with pytest.raises(ProtectionFailed) as ei:
            await bm.place_market_with_protection(
                symbol="aapl", side=Side.LONG, quantity=10,
                take_profit=Target.pct(Decimal("0.002")), stop_loss=Target.pct(Decimal("0.004")),
            )
        # 必须按成交量 10 平，而非信任缓存裸 0 跳过
        assert ei.value.flatten["quantity"] == 10
        assert ei.value.flatten["action"] == "SELL"
        flatten_mkts = [o for o in fake.placed if o.orderType == "MKT"][1:]  # 排除入场
        assert flatten_mkts and flatten_mkts[0].totalQuantity == 10

```
