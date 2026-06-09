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
        assert current_session(_et(2026, 6, 14, 12, 0)) is Session.CLOSED  # 周日


class TestHelpers:
    def test_is_rth(self) -> None:
        assert is_rth(_et(2026, 6, 8, 11, 0)) is True
        assert is_rth(_et(2026, 6, 8, 5, 0)) is False
        assert is_rth(_et(2026, 6, 8, 22, 0)) is False

    def test_is_tradable_extended(self) -> None:
        assert is_tradable_extended(_et(2026, 6, 8, 5, 0)) is True  # 盘前
        assert is_tradable_extended(_et(2026, 6, 8, 22, 0)) is True  # 夜盘
        assert is_tradable_extended(_et(2026, 6, 13, 12, 0)) is False  # 周末
