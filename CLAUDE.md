# WebWatch — Claude Code 协作说明

## 项目本质

IBKR 手动超短线下单面板（**可视化操作面板**）。**真金白银**：输入代码 → 市价/限价买入 →
买入同时自动挂止盈止损。目标抓快速反弹超短线，买入涨 +0.2%（覆盖手续费）即卖。

姊妹项目：[scalper](https://github.com/weiyhmail-sketch/scalper)（全自动 ORB/VWAP 机器人）。
本项目**复用其执行层**（`ib_broker_adapter` / `oca_group` / `types` / `config` / `risk` / `kill_switch`），
作为 git 依赖装入，**不在 scalper 仓库里改动**。完整方案见 plan 文件。

## 开工首读

- **[STATUS.md](STATUS.md)** — 跨 session 进度交接，**每个 session 开工第一句话 `@STATUS.md`**。
  里程碑状态、已完成细节、待用户拍板事项、复用资产速查、下一步都在里面。
  每轮结束必须更新它。

## 红线（任何情况都不可越过）

1. **绝不**未经用户明确批准就连 live(4001) 或对 live 账户下单。默认 paper(4002)。
   切 live 必须：双重确认 + live 醒目标识 + 首周小手数上限。
2. **绝不**改动下单链路核心（`pricing.py` 的 TP/SL 计算与 tick 取整、`order_service.py`、
   `broker.py` 的下单分支）而不附带新增/调整测试。强制 TDD（对标 scalper risk/execution 红线）。
3. **绝不**让保护单缺失：任何"入场已成交但 TP/SL 未挂成功"的分支，必须立即市价平仓 + 告警，
   不允许裸仓存在。
4. **绝不**降低测试覆盖或跳过测试来声称全绿。
5. secrets（`config/secrets.paper.env` / `secrets.live.env`）**绝不进 git、绝不打印**到日志/报错。

## 代码风格（沿用 scalper）

- 类型注解必填，`mypy --strict` 通过；`ruff` 无 warning
- 金额/价格/比例一律 `decimal.Decimal`，**禁止裸 `float`**
- 禁止 `print`（脚本除外）、禁止散落 `os.getenv`（走 `config.py` 的 pydantic-settings）
- 时间戳 UTC 存、ET 展示

## 默认开发循环

1. 改下单/计算相关代码前，先读对应测试
2. **先写/改测试，再改实现**（pricing / order_service / broker 强制 TDD）
3. 跑 `pytest` 必须全绿，`mypy --strict` + `ruff` 全过
4. 每轮结束生成 review bundle 交 ChatGPT 复核（见下"三方分工"）

## 常用命令

| 命令 | 用途 |
|---|---|
| `uv sync` | 安装依赖（首次会拉 scalper git 依赖，较重） |
| `uv run python scripts/check_connection.py` | M0 连接 smoke test（默认 paper） |
| `uv run pytest` | 跑全部测试 |
| `uv run pytest -m "not integration"` | 只跑单元测试（不连 Gateway） |
| `uv run mypy src && uv run ruff check` | 类型 + lint |
| `uv run uvicorn webwatch.app:app --reload` | 起面板（M1 后可用） |

## 何时停下来问用户

- 要从 paper(4002) 切到 live(4001)
- 要改 TP/SL 计算公式、tick 取整逻辑、或任何下单分支
- 要放宽单笔 notional 上限 / 取消下单前风控检查
- 要新增第二券商或第二行情源
- 出现任何"看起来能跑通但不太确定"的情况

## 三方分工（沿用 scalper 约定）

| 谁 | 负责 |
|---|---|
| 用户 | 决策、验证、运行命令、持有密钥、批准 live |
| Claude Code | 规划、架构、写代码、code review、调试、所有金钱相关判断 |
| 协助 AI（ChatGPT 等） | 通用工具教学（IB Gateway 安装、SSH key、Homebrew、防火墙等）+ 下单链路复核 |

判断转交标准：「换一个项目还一样做吗？」是 → 通用 → 转交。
**绝不**让协助 AI 接触 secrets / 账户号；**绝不**让其直接合并本项目下单链路代码。
