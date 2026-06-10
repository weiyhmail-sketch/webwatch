"""radar_metrics 纯函数单测 —— 异动雷达的指标/事件/榜单/订阅预算全部在此定规格。

约定：时间戳是 monotonic 秒（测试用裸数字）；百分数单位（0.5 = 0.5%）。
"""

from __future__ import annotations

import json
import math
from collections import deque
from typing import Any

from webwatch.radar_metrics import (
    Sample,
    ScoreWeights,
    SymbolState,
    Thresholds,
    build_boards,
    compute_row,
    consecutive_direction,
    day_change_pct,
    detect_breaks,
    detect_events,
    f_or_none,
    minute_closes,
    passes_coarse_filters,
    pct_change_over,
    plan_subscriptions,
    rvol,
    spike_score,
    spread_pct,
    vol_burst,
    volume_over,
    vwap_dist_pct,
)

NOW = 10_000.0


def mk_samples(
    *pts: tuple[float, float] | tuple[float, float, float | None],
) -> deque[Sample]:
    """(秒前, 价格[, 累计量]) 列表 → 样本 deque（按时间升序）。"""
    out: deque[Sample] = deque(maxlen=420)
    for p in sorted(pts, key=lambda x: -x[0]):  # 秒前大的在前 = 时间早的在前
        ago, price = p[0], p[1]
        vol = p[2] if len(p) > 2 else None
        out.append(Sample(ts=NOW - ago, price=price, cum_volume=vol))
    return out


def steady_samples(
    seconds: int, *, start_price: float = 100.0, drift_per_s: float = 0.0,
    vol_per_s: float | None = None,
) -> deque[Sample]:
    """连续 1s 采样 seconds 秒：价格线性漂移，量线性累计。"""
    out: deque[Sample] = deque(maxlen=420)
    for i in range(seconds, -1, -1):  # i 秒前 → 现在
        t = seconds - i
        vol = None if vol_per_s is None else vol_per_s * t
        out.append(Sample(ts=NOW - i, price=start_price + drift_per_s * t, cum_volume=vol))
    return out


class TestFOrNone:
    def test_passthrough_and_nan(self) -> None:
        assert f_or_none(1.5) == 1.5
        assert f_or_none(None) is None
        assert f_or_none(float("nan")) is None
        assert f_or_none(float("inf")) is None
        assert f_or_none(1e16) is None  # IBKR 未就绪哨兵（极大值）
        assert f_or_none("abc") is None
        assert f_or_none(3) == 3.0


class TestPctChangeOver:
    def test_normal_1m_change(self) -> None:
        s = steady_samples(120, start_price=100.0, drift_per_s=0.01)  # 60s 涨 0.6
        got = pct_change_over(s, NOW, 60)
        assert got is not None
        # 60 秒前价 100.6 → 现价 101.2：+0.5964%
        assert math.isclose(got, (101.2 / 100.6 - 1) * 100, rel_tol=1e-9)

    def test_warmup_none(self) -> None:
        s = steady_samples(30)  # 只有 30s 历史，算不了 1 分钟
        assert pct_change_over(s, NOW, 60) is None

    def test_gap_tolerance(self) -> None:
        # 目标时刻 ±25%(15s) 内有样本就算；更远只能 None
        ok = mk_samples((70, 100.0), (5, 101.0))  # 70s 前样本距目标 60s 仅 10s → 可用
        got = pct_change_over(ok, NOW, 60)
        assert got is not None and math.isclose(got, 1.0, rel_tol=1e-9)
        bad = mk_samples((90, 100.0), (5, 101.0))  # 距目标 30s > 15s → 预热/断样
        assert pct_change_over(bad, NOW, 60) is None

    def test_empty_and_zero_price(self) -> None:
        assert pct_change_over(deque(), NOW, 60) is None
        s = mk_samples((60, 0.0), (0, 1.0))
        assert pct_change_over(s, NOW, 60) is None


class TestVolumeOver:
    def test_delta(self) -> None:
        s = steady_samples(120, vol_per_s=1000.0)  # 每秒 1000 股
        got = volume_over(s, NOW, 60)
        assert got is not None and math.isclose(got, 60_000.0, rel_tol=1e-9)

    def test_none_propagation_and_negative(self) -> None:
        assert volume_over(mk_samples((60, 100.0, None), (0, 101.0, 5000.0)), NOW, 60) is None
        assert volume_over(mk_samples((60, 100.0, 5000.0), (0, 101.0, None)), NOW, 60) is None
        # 累计量倒退（日切/重置）→ None 而非负数
        assert volume_over(mk_samples((60, 100.0, 9000.0), (0, 101.0, 100.0)), NOW, 60) is None


class TestMinuteCloses:
    def test_complete_buckets_oldest_first(self) -> None:
        s = steady_samples(360, start_price=100.0, drift_per_s=0.01)
        closes = minute_closes(s, NOW, 6)
        assert len(closes) == 6
        assert closes == sorted(closes)  # 单调上涨 → 升序（最旧在前）
        assert math.isclose(closes[-1], 103.6, rel_tol=1e-9)  # 最近完整分钟的收价=现价

    def test_partial_history_stops_at_empty_bucket(self) -> None:
        s = steady_samples(150)  # 2.5 分钟历史 → 只有 2~3 个完整桶
        closes = minute_closes(s, NOW, 6)
        assert 2 <= len(closes) <= 3

    def test_empty(self) -> None:
        assert minute_closes(deque(), NOW, 6) == []


class TestConsecutiveDirection:
    def test_rising_falling_flat(self) -> None:
        assert consecutive_direction([1.0, 2.0, 3.0, 4.0]) == 3
        assert consecutive_direction([4.0, 3.0, 2.0, 1.0]) == -3
        assert consecutive_direction([1.0, 3.0, 2.0, 4.0]) == 1  # 只看末端连续段
        assert consecutive_direction([1.0, 2.0, 2.0, 3.0]) == 1  # 平价打断
        assert consecutive_direction([1.0, 1.0]) == 0
        assert consecutive_direction([1.0]) == 0
        assert consecutive_direction([]) == 0


class TestSpreadPct:
    def test_normal(self) -> None:
        got = spread_pct(99.95, 100.05)
        assert got is not None and math.isclose(got, 0.1 / 100.0 * 100, rel_tol=1e-9)

    def test_crossed_zero_none(self) -> None:
        assert spread_pct(100.05, 99.95) is None  # 交叉盘
        assert spread_pct(0.0, 1.0) is None
        assert spread_pct(None, 1.0) is None
        assert spread_pct(1.0, None) is None


class TestSimpleRatios:
    def test_day_change(self) -> None:
        got = day_change_pct(101.0, 100.0)
        assert got is not None and math.isclose(got, 1.0, rel_tol=1e-9)
        assert day_change_pct(101.0, None) is None
        assert day_change_pct(101.0, 0.0) is None

    def test_vwap_dist(self) -> None:
        got = vwap_dist_pct(102.0, 100.0)
        assert got is not None and math.isclose(got, 2.0, rel_tol=1e-9)
        assert vwap_dist_pct(None, 100.0) is None
        assert vwap_dist_pct(102.0, 0.0) is None

    def test_rvol_and_burst_none_paths(self) -> None:
        r = rvol(3_000_000.0, 1_000_000.0)
        assert r is not None and math.isclose(r, 3.0, rel_tol=1e-9)
        assert rvol(3_000_000.0, None) is None
        assert rvol(3_000_000.0, 0.0) is None
        b = vol_burst(50_000.0, 1_950_000.0)  # 基线 1950000/390=5000/分钟 → 10x
        assert b is not None and math.isclose(b, 10.0, rel_tol=1e-9)
        assert vol_burst(None, 1_950_000.0) is None
        assert vol_burst(50_000.0, None) is None


class TestDetectBreaks:
    def test_first_observation_sets_baseline_no_fire(self) -> None:
        hod, lod, nh, nl = detect_breaks(None, None, 105.0, 95.0, 100.0)
        assert hod is False and lod is False
        assert nh == 105.0 and nl == 95.0

    def test_hod_fires_then_baseline_updates(self) -> None:
        # 已有基线 105 → 日高推到 106 且现价 ≥ 基线 → 触发，新基线 106
        hod, lod, nh, nl = detect_breaks(105.0, 95.0, 106.0, 95.0, 105.8)
        assert hod is True and lod is False
        assert nh == 106.0 and nl == 95.0
        # 用新基线再判：日高未再创新 → 不触发
        hod2, _, nh2, _ = detect_breaks(nh, nl, 106.0, 95.0, 106.0)
        assert hod2 is False and nh2 == 106.0

    def test_lod_break(self) -> None:
        _, lod, _, nl = detect_breaks(105.0, 95.0, 105.0, 94.0, 94.2)
        assert lod is True and nl == 94.0

    def test_break_requires_price_at_level(self) -> None:
        # 日高刷新但现价已回落到基线下 → 不算"正在突破"
        hod, _, _, _ = detect_breaks(105.0, 95.0, 106.0, 95.0, 104.0)
        assert hod is False


class TestSpikeScore:
    W = ScoreWeights(w_1m=1.0, w_5m=0.5, w_day=0.1, w_vol=0.3)

    def test_signed_and_volume_multiplier(self) -> None:
        up = spike_score(0.6, 1.2, 5.0, 10.0, self.W)
        assert up is not None and up > 0
        # base = 0.6 + 0.6 + 0.5 = 1.7；mult = 1 + 0.3*log10(10) = 1.3
        assert math.isclose(up, 1.7 * 1.3, rel_tol=1e-9)
        down = spike_score(-0.6, -1.2, -5.0, 10.0, self.W)
        assert down is not None and math.isclose(down, -1.7 * 1.3, rel_tol=1e-9)

    def test_multiplier_clamped(self) -> None:
        got = spike_score(1.0, 0.0, 0.0, 1e9, self.W)  # 巨量 → mult 封顶 2.5
        assert got is not None and math.isclose(got, 1.0 * 2.5, rel_tol=1e-9)
        floor = spike_score(1.0, 0.0, 0.0, 0.001, self.W)  # burst<1 → mult 保底 1
        assert floor is not None and math.isclose(floor, 1.0, rel_tol=1e-9)

    def test_both_warmup_falls_back_to_day(self) -> None:
        got = spike_score(None, None, 8.0, None, self.W)
        assert got is not None and math.isclose(got, 0.8, rel_tol=1e-9)
        assert spike_score(None, None, None, None, self.W) is None

    def test_partial_none_treated_as_zero(self) -> None:
        got = spike_score(0.5, None, None, None, self.W)
        assert got is not None and math.isclose(got, 0.5, rel_tol=1e-9)


class TestCoarseFilters:
    KW: dict[str, Any] = {
        "min_price": 1.0, "min_dollar_volume": 2_000_000.0, "max_spread_pct": 0.5,
    }

    def test_pass_and_each_gate(self) -> None:
        ok = passes_coarse_filters(10.0, 5_000_000.0, 0.1, is_watch=False, **self.KW)
        assert ok is True
        assert passes_coarse_filters(0.5, 5e6, 0.1, is_watch=False, **self.KW) is False  # 价格
        assert passes_coarse_filters(10.0, 1e6, 0.1, is_watch=False, **self.KW) is False  # 成交额
        assert passes_coarse_filters(10.0, 5e6, 0.9, is_watch=False, **self.KW) is False  # 点差
        assert passes_coarse_filters(None, 5e6, 0.1, is_watch=False, **self.KW) is False
        assert passes_coarse_filters(10.0, None, 0.1, is_watch=False, **self.KW) is False

    def test_spread_unknown_passes(self) -> None:
        # 盘外 bid/ask 缺失 → 点差未知不一票否决（行内显示 —）
        assert passes_coarse_filters(10.0, 5e6, None, is_watch=False, **self.KW) is True

    def test_watch_exempt(self) -> None:
        assert passes_coarse_filters(0.5, None, 9.9, is_watch=True, **self.KW) is True


def _th(**kw: Any) -> Thresholds:
    base = {
        "spike_1m_pct": 0.5, "sustained_5m_pct": 1.0, "sustained_min_candles": 3,
        "vol_burst_ratio": 8.0, "event_cooldown_s": 120.0,
    }
    base.update(kw)
    return Thresholds(**base)


def _row(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": "TST", "last": 100.0, "chg_1m_pct": 0.0, "chg_5m_pct": 0.0,
        "consec": 0, "vol_burst": 1.0, "halted": False, "day_high": 101.0, "day_low": 99.0,
    }
    base.update(kw)
    return base


class TestDetectEvents:
    def test_spike_1m_up_down(self) -> None:
        st = SymbolState()
        ev = detect_events(st, _row(chg_1m_pct=0.6), _th(), NOW)
        assert [(e["type"], e["dir"]) for e in ev] == [("spike_1m", "up")]
        st2 = SymbolState()
        ev2 = detect_events(st2, _row(chg_1m_pct=-0.7), _th(), NOW)
        assert [(e["type"], e["dir"]) for e in ev2] == [("spike_1m", "down")]
        assert ev2[0]["value"] == -0.7 and ev2[0]["price"] == 100.0

    def test_cooldown_suppresses_then_expires(self) -> None:
        st = SymbolState()
        assert len(detect_events(st, _row(chg_1m_pct=0.6), _th(), NOW)) == 1
        # 冷却内再触发 → 抑制
        assert detect_events(st, _row(chg_1m_pct=0.9), _th(), NOW + 30) == []
        # 冷却过 → 再触发
        assert len(detect_events(st, _row(chg_1m_pct=0.9), _th(), NOW + 121)) == 1

    def test_sustained_needs_both_candles_and_move(self) -> None:
        st = SymbolState()
        assert detect_events(st, _row(consec=3, chg_5m_pct=0.5), _th(), NOW) == []  # 幅度不够
        assert detect_events(st, _row(consec=2, chg_5m_pct=1.5), _th(), NOW + 200) == []  # 根数不够
        ev = detect_events(st, _row(consec=-3, chg_5m_pct=-1.5), _th(), NOW + 400)
        assert [(e["type"], e["dir"]) for e in ev] == [("sustained_5m", "down")]

    def test_hod_break_event_and_baseline(self) -> None:
        st = SymbolState()
        # 首观：只立基线
        assert detect_events(st, _row(day_high=105.0, day_low=95.0, last=100.0), _th(), NOW) == []
        assert st.prev_day_high == 105.0 and st.prev_day_low == 95.0
        # 创新高且价在位 → hod_break
        ev = detect_events(st, _row(day_high=106.0, day_low=95.0, last=105.5), _th(), NOW + 10)
        assert [(e["type"], e["dir"]) for e in ev] == [("hod_break", "up")]
        assert st.prev_day_high == 106.0
        # 冷却内继续新高 → 基线仍更新但不发事件
        assert detect_events(st, _row(day_high=107.0, day_low=95.0, last=107.0), _th(), NOW + 20) == []
        assert st.prev_day_high == 107.0

    def test_vol_burst_needs_price_move_guard(self) -> None:
        st = SymbolState()
        assert detect_events(st, _row(vol_burst=9.0, chg_1m_pct=0.05), _th(), NOW) == []  # 大宗无价动
        ev = detect_events(st, _row(vol_burst=9.0, chg_1m_pct=0.2), _th(), NOW + 1)
        assert [(e["type"], e["dir"]) for e in ev] == [("vol_burst", "up")]

    def test_halted_transitions(self) -> None:
        st = SymbolState()
        ev = detect_events(st, _row(halted=True), _th(), NOW)  # 首观即停牌 → 告知
        assert [(e["type"], e["dir"]) for e in ev] == [("halted", "flat")]
        assert detect_events(st, _row(halted=True), _th(), NOW + 1) == []  # 状态没变
        ev2 = detect_events(st, _row(halted=False), _th(), NOW + 2)
        assert [(e["type"], e["dir"]) for e in ev2] == [("resumed", "flat")]
        # 未知(None) 不触发也不清已知状态
        assert detect_events(st, _row(halted=None), _th(), NOW + 3) == []
        assert st.last_halted is False

    def test_multiple_events_one_cycle(self) -> None:
        st = SymbolState()
        st.prev_day_high, st.prev_day_low = 100.5, 99.0
        ev = detect_events(
            st, _row(chg_1m_pct=0.8, consec=3, chg_5m_pct=1.5, day_high=101.0, last=100.8),
            _th(), NOW,
        )
        assert {e["type"] for e in ev} == {"spike_1m", "sustained_5m", "hod_break"}


class TestComputeRow:
    def test_full_row_realtime(self) -> None:
        st = SymbolState()
        st.samples = steady_samples(360, start_price=100.0, drift_per_s=0.01, vol_per_s=5000.0)
        row = compute_row(
            "ABCD", st, NOW,
            bid=103.55, ask=103.65, last=103.6, close=100.0,
            day_high=103.7, day_low=99.5, vwap=101.0,
            volume_shares=1_800_000.0, av_volume=10_000_000.0,
            halted_raw=0.0, delayed=False, watch=False,
        )
        assert row["symbol"] == "ABCD" and row["watch"] is False
        assert row["last"] == 103.6 and row["halted"] is False and row["delayed"] is False
        assert row["warmup_1m"] is False and row["warmup_5m"] is False
        assert row["chg_1m_pct"] is not None and row["chg_5m_pct"] is not None
        assert row["consec"] == 5  # 单调上涨 6 个完整桶 → 末端连续 +5
        assert row["dollar_vol_today"] is not None
        assert math.isclose(row["dollar_vol_today"], 103.6 * 1_800_000.0, rel_tol=1e-9)
        assert row["rvol"] is not None and math.isclose(row["rvol"], 0.18, rel_tol=1e-9)
        assert row["score"] is not None and row["score"] > 0
        assert row["hod_dist_pct"] is not None and row["hod_dist_pct"] >= 0
        # 全行可 JSON 序列化且无 NaN
        json.dumps(row, allow_nan=False)

    def test_nan_inputs_sanitized_and_warmup(self) -> None:
        nan = float("nan")
        st = SymbolState()  # 无样本 → 1m/5m 预热
        row = compute_row(
            "NEWB", st, NOW,
            bid=nan, ask=nan, last=12.0, close=10.0,
            day_high=nan, day_low=nan, vwap=nan,
            volume_shares=nan, av_volume=nan,
            halted_raw=nan, delayed=False, watch=False,
        )
        assert row["bid"] is None and row["spread_pct"] is None
        assert row["halted"] is None
        assert row["warmup_1m"] is True and row["warmup_5m"] is True
        # 双预热 → score 退化为 day_chg 加权（day_chg=+20%）
        assert row["score"] is not None and row["score"] > 0
        json.dumps(row, allow_nan=False)


class TestBuildBoards:
    def _rows(self) -> list[dict[str, Any]]:
        return [
            {"symbol": "A", "score": 3.0}, {"symbol": "B", "score": 1.0},
            {"symbol": "C", "score": -2.0}, {"symbol": "D", "score": None},
            {"symbol": "E", "score": -0.5}, {"symbol": "F", "score": 0.0},
            {"symbol": "G", "score": 2.0},
        ]

    def test_split_sort_cap(self) -> None:
        up, down = build_boards(self._rows(), board_size=2)
        assert [r["symbol"] for r in up] == ["A", "G"]  # 降序截断
        assert [r["symbol"] for r in down] == ["C", "E"]  # 最负的在前
        # None 与 0 分不上榜
        up_all, down_all = build_boards(self._rows(), board_size=10)
        names = {r["symbol"] for r in up_all} | {r["symbol"] for r in down_all}
        assert "D" not in names and "F" not in names

    def test_tie_breaks_by_symbol(self) -> None:
        rows = [{"symbol": "Z", "score": 1.0}, {"symbol": "A", "score": 1.0}]
        up, _ = build_boards(rows, board_size=2)
        assert [r["symbol"] for r in up] == ["A", "Z"]


class TestPlanSubscriptions:
    def test_fills_free_slots_in_rank_order(self) -> None:
        sub, unsub = plan_subscriptions(
            current={}, scan_ranked=["A", "B", "C"], protected=set(),
            now=NOW, cap=2, evict_after_s=180, max_evictions=5,
        )
        assert sub == ["A", "B"] and unsub == []

    def test_skips_current_and_protected(self) -> None:
        sub, _ = plan_subscriptions(
            current={"A": NOW}, scan_ranked=["A", "WATCH1", "B"], protected={"WATCH1"},
            now=NOW, cap=5, evict_after_s=180, max_evictions=5,
        )
        assert sub == ["B"]

    def test_eviction_only_stale_and_unprotected(self) -> None:
        current = {
            "OLD1": NOW - 400,   # 失联超 180s → 可驱逐
            "OLD2": NOW - 300,   # 可驱逐
            "FRESH": NOW - 10,   # 刚见过 → 不可
            "BOARD": NOW - 999,  # 在榜保护
        }
        sub, unsub = plan_subscriptions(
            current=current, scan_ranked=["N1", "N2", "N3"], protected={"BOARD"},
            now=NOW, cap=4, evict_after_s=180, max_evictions=5,
        )
        # 0 空位 → 驱逐最旧的两只换两只新
        assert unsub == ["OLD1", "OLD2"]
        assert sub == ["N1", "N2"]

    def test_max_evictions_per_cycle(self) -> None:
        current = {f"OLD{i}": NOW - 1000 - i for i in range(6)}
        sub, unsub = plan_subscriptions(
            current=current, scan_ranked=[f"N{i}" for i in range(6)], protected=set(),
            now=NOW, cap=6, evict_after_s=180, max_evictions=2,
        )
        assert len(unsub) == 2 and len(sub) == 2

    def test_eviction_order_deterministic(self) -> None:
        # 同龄 → 字典序
        current = {"ZZ": NOW - 500, "AA": NOW - 500, "MM": NOW - 600}
        _, unsub = plan_subscriptions(
            current=current, scan_ranked=["N1", "N2", "N3"], protected=set(),
            now=NOW, cap=3, evict_after_s=180, max_evictions=3,
        )
        assert unsub == ["MM", "AA", "ZZ"]  # 最旧优先，同龄字典序

    def test_empty_scan_no_eviction(self) -> None:
        sub, unsub = plan_subscriptions(
            current={"A": NOW - 999}, scan_ranked=[], protected=set(),
            now=NOW, cap=1, evict_after_s=180, max_evictions=5,
        )
        assert sub == [] and unsub == []

    def test_cap_already_full_no_stale(self) -> None:
        sub, unsub = plan_subscriptions(
            current={"A": NOW, "B": NOW}, scan_ranked=["C"], protected=set(),
            now=NOW, cap=2, evict_after_s=180, max_evictions=5,
        )
        assert sub == [] and unsub == []
