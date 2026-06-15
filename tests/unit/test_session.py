"""session 模块单测 —— 美股时段判定（固定 ET 时间，确定性）。"""

from __future__ import annotations

from datetime import datetime

from webwatch.session import ET, Session, current_session, is_rth, is_tradable_extended


def _et(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


# 2026-06-08 是周一，2026-06-13 是周六。
class TestCurrentSession:
    def test_rth(self) -> None:
        assert current_session(_et(2026, 6, 8, 10, 0)) is Session.RTH
        assert current_session(_et(2026, 6, 8, 9, 30)) is Session.RTH
        assert current_session(_et(2026, 6, 8, 15, 59)) is Session.RTH

    def test_pre_market(self) -> None:
        assert current_session(_et(2026, 6, 8, 5, 0)) is Session.PRE
        assert current_session(_et(2026, 6, 8, 9, 29)) is Session.PRE

    def test_post_market(self) -> None:
        assert current_session(_et(2026, 6, 8, 16, 0)) is Session.POST
        assert current_session(_et(2026, 6, 8, 19, 59)) is Session.POST

    def test_overnight(self) -> None:
        assert current_session(_et(2026, 6, 8, 22, 0)) is Session.OVERNIGHT
        assert current_session(_et(2026, 6, 8, 2, 0)) is Session.OVERNIGHT

    def test_weekend_closed(self) -> None:
        assert current_session(_et(2026, 6, 13, 12, 0)) is Session.CLOSED  # 周六
        assert current_session(_et(2026, 6, 14, 12, 0)) is Session.CLOSED  # 周日午间


# 夜盘周边界：IBKR 夜盘(Blue Ocean)周日 20:00 ET 开盘，跑到周五 20:00 ET 收盘。
# 2026-06-12 周五，06-13 周六，06-14 周日。
class TestOvernightWeekBoundary:
    def test_sunday_evening_opens_overnight(self) -> None:
        # 周日 20:00 起夜盘开市（此前是本周第一个可交易时段）
        assert current_session(_et(2026, 6, 14, 20, 0)) is Session.OVERNIGHT
        assert current_session(_et(2026, 6, 14, 22, 40)) is Session.OVERNIGHT
        assert current_session(_et(2026, 6, 14, 23, 59)) is Session.OVERNIGHT

    def test_sunday_before_2000_still_closed(self) -> None:
        assert current_session(_et(2026, 6, 14, 19, 59)) is Session.CLOSED
        assert current_session(_et(2026, 6, 14, 3, 0)) is Session.CLOSED

    def test_monday_early_morning_overnight(self) -> None:
        # 周一 00:00–04:00 是周日夜盘的延续，仍可交易
        assert current_session(_et(2026, 6, 15, 2, 0)) is Session.OVERNIGHT

    def test_friday_evening_closed(self) -> None:
        # 周五 20:00 后夜盘不开（进入周末），应休市
        assert current_session(_et(2026, 6, 12, 20, 0)) is Session.CLOSED
        assert current_session(_et(2026, 6, 12, 23, 0)) is Session.CLOSED

    def test_friday_early_morning_overnight(self) -> None:
        # 周五 00:00–04:00 是周四夜盘的延续，仍可交易
        assert current_session(_et(2026, 6, 12, 2, 0)) is Session.OVERNIGHT

    def test_saturday_early_morning_closed(self) -> None:
        # 周六凌晨：周五晚已无夜盘，正确休市
        assert current_session(_et(2026, 6, 13, 2, 0)) is Session.CLOSED

    def test_sunday_evening_tradable(self) -> None:
        assert is_tradable_extended(_et(2026, 6, 14, 22, 0)) is True
        assert is_tradable_extended(_et(2026, 6, 12, 21, 0)) is False  # 周五晚不可交易


class TestHelpers:
    def test_is_rth(self) -> None:
        assert is_rth(_et(2026, 6, 8, 11, 0)) is True
        assert is_rth(_et(2026, 6, 8, 5, 0)) is False
        assert is_rth(_et(2026, 6, 8, 22, 0)) is False

    def test_is_tradable_extended(self) -> None:
        assert is_tradable_extended(_et(2026, 6, 8, 5, 0)) is True  # 盘前
        assert is_tradable_extended(_et(2026, 6, 8, 22, 0)) is True  # 夜盘
        assert is_tradable_extended(_et(2026, 6, 13, 12, 0)) is False  # 周末
