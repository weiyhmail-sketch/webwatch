# WebWatch — IBKR 手动超短线下单面板

通过 IBKR API 连接的**可视化手动下单面板**：输入股票代码 → 市价/限价买入 →
买入同时自动挂止盈止损（bracket）。市价买入时按给定涨跌比例自动算出限价。
面向快速反弹超短线（买入 +0.2% 即卖，盈利覆盖手续费）。

复用姊妹项目 [scalper](https://github.com/weiyhmail-sketch/scalper) 经 745 测试验证的
IBKR 执行层，不重造轮子。**默认 paper(4002)，paper 验证后再切 live(4001)。**

## 快速开始

```bash
# 1. 安装依赖（首次较重，会拉 scalper git 依赖；需对 scalper 私仓的 ssh 访问权）
uv sync

# 2. 配置连接（secrets 不会进 git）
cp config/secrets.env.example config/secrets.paper.env
# 编辑 secrets.paper.env，填入 paper 账户号（DU 开头）；端口保持 4002

# 3. 启动 IB Gateway（paper 模式），然后跑连接 smoke test
uv run python scripts/check_connection.py

# 4. 起面板（默认 paper；无 Gateway 也能开，会显示"未连接"）
uv run uvicorn webwatch.app:app --reload
# 浏览器打开 http://127.0.0.1:8000
```

看到账户摘要（NetLiquidation 等）即连接成功。

## 状态

- [x] M0 脚手架 + 连接 paper Gateway
- [x] M2 pricing 模块（TP/SL 多模式 + tick 取整 + 扣费净 edge，24 单测全绿）
- [x] M1 报价 + 持仓只读面板（FastAPI + WebSocket，36 单测全绿）
- [x] M3 限价买入 + 原生 bracket（paper 实测三单同发）
- [x] M4 市价买入 + 成交后精确挂 + 保护单失败立即平仓（活盘验证待 M6）
- [x] M5 风控 + 撤单/全平 + 热键（80 单测全绿）
- [ ] M6 paper 验证 1–2 天（待盘中实操）
- [x] M7 切 live 闸门（二重互锁 + 红色醒目标识 + 首周上限 + 每笔确认）
- [x] 行情独立模块 quotes.py（零 scalper 行情依赖 + 运行时切实时/延迟）
- [x] M8 24h 交易（盘前/盘后/夜盘：outsideRth + 限价入场 + stop-limit 止损 + 市价转激进限价，131 单测全绿）

开发约定见 [CLAUDE.md](CLAUDE.md)。
