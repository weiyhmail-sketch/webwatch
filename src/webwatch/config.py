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
        # 24h 交易：允许盘前/盘后/夜盘下单（outsideRth）。时段外自动用限价入场 + stop-limit 止损。
        self.extended_hours_enabled = bool(raw.get("extended_hours_enabled", True))
        # 时段外"市价买入"转激进限价时，贴对手价加的 tick 数。
        self.aggressive_limit_ticks = int(raw.get("aggressive_limit_ticks", 3))
        # 时段外 stop-limit 止损：保护限价相对触发价的缓冲（向更深一侧）。
        self.stop_limit_offset_pct = Decimal(str(raw.get("stop_limit_offset_pct", "0.005")))
        risk = raw.get("risk", {}) or {}
        self.max_order_notional_usd = Decimal(str(risk.get("max_order_notional_usd", "5000")))
        # 单笔绝对股数上限（第二道闸）：仙股下 notional 上限拦不住超大手数。
        self.max_order_shares = int(risk.get("max_order_shares", 100_000))
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
