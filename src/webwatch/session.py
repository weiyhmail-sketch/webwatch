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
    """判定 ``dt``（默认当前 ET）所处时段。

    夜盘(Blue Ocean)周开市窗口：**周日 20:00 ET 开盘 → 周五 20:00 ET 收盘**，
    其间连续运行（工作日内嵌盘前/RTH/盘后）。故周末边界需精细处理：
    - 周六全天休市；
    - 周日 20:00 前休市，20:00 起夜盘开市（本周首个可交易时段）；
    - 周五 20:00 后休市（夜盘不延续到周末），但周五凌晨 00:00–04:00 是周四夜盘延续，仍可交易。
    """
    dt = dt or now_et()
    weekday = dt.weekday()  # 0=周一 … 6=周日
    t = dt.time()
    # 周六全天休市。
    if weekday == 5:
        return Session.CLOSED
    # 周日：20:00 前休市；20:00 起夜盘开市。
    if weekday == 6:
        return Session.OVERNIGHT if t >= _POST_CLOSE else Session.CLOSED
    # 周一到周五：
    if _RTH_OPEN <= t < _RTH_CLOSE:
        return Session.RTH
    if _PRE_OPEN <= t < _RTH_OPEN:
        return Session.PRE
    if _RTH_CLOSE <= t < _POST_CLOSE:
        return Session.POST
    # 其余（20:00–次日 04:00）= 夜盘；但周五 20:00 后进入周末，夜盘不开 → 休市。
    if weekday == 4 and t >= _POST_CLOSE:
        return Session.CLOSED
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
