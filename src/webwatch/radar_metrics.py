"""异动雷达指标计算 —— 全部纯函数，零 IB 依赖（TDD 主战场）。

float 先例（同 quotes.py 模块说明）：本模块输出只用于**展示/排序/提醒**，
绝不进下单链路；下单价格永远由 pricing/order_service 以 Decimal 重算，
服务端也从不信任前端传价。

时间约定：样本时间戳用 ``time.monotonic()`` 秒（窗口计算稳健，不受系统时钟跳变影响）；
事件展示时间由服务层另配 UTC（存 UTC、ET 展示的项目约定不变）。
百分数单位：0.5 = 0.5%。
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any

# 一分钟桶取价的目标时刻容差（占窗口比例）：±25% 内有样本才算窗口有效，
# 否则视为预热中/断样（重连清窗后会短暂回到预热态）。
_WINDOW_TOLERANCE = 0.25
# 一交易日常规盘分钟数，vol_burst 的每分钟基线分母。
_MINUTES_PER_DAY = 390.0


@dataclass(frozen=True)
class Sample:
    """一次采样：monotonic 秒、最新价、当日累计成交量（股；未知 None）。"""

    ts: float
    price: float
    cum_volume: float | None


@dataclass
class SymbolState:
    """单标的的滚动状态（服务层每标的持有一个）。"""

    samples: deque[Sample] = field(default_factory=lambda: deque(maxlen=420))  # 7 分钟 @1s
    prev_day_high: float | None = None
    prev_day_low: float | None = None
    last_event_ts: dict[str, float] = field(default_factory=dict)  # 事件类型 → 上次触发
    last_halted: bool | None = None
    first_seen_ts: float = 0.0
    is_watch: bool = False


@dataclass(frozen=True)
class Thresholds:
    """事件触发阈值（来自 RadarConfig）。"""

    spike_1m_pct: float = 0.5
    sustained_5m_pct: float = 1.0
    sustained_min_candles: int = 3
    vol_burst_ratio: float = 8.0
    event_cooldown_s: float = 120.0


@dataclass(frozen=True)
class ScoreWeights:
    """综合异动分权重（来自 RadarConfig）。"""

    w_1m: float = 1.0
    w_5m: float = 0.5
    w_day: float = 0.1
    w_vol: float = 0.3


def f_or_none(value: Any) -> float | None:
    """转有限 float；None/NaN/Inf/IBKR 未就绪哨兵(极大值)/非数 → None。

    镜像 broker._finite 语义——雷达所有出口数值必须过这道闸，
    否则 NaN 进 WS JSON 会产出非法负载。
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f) or abs(f) > 1e15:
        return None
    return f


# --------------------------------------------------------------------------
# 滚动窗口
# --------------------------------------------------------------------------


def _sample_near(samples: Sequence[Sample], target_ts: float, tolerance_s: float) -> Sample | None:
    """取时间上最接近 target_ts 的样本；偏差超容差 → None（预热/断样）。"""
    if not samples:
        return None
    best = min(samples, key=lambda s: abs(s.ts - target_ts))
    if abs(best.ts - target_ts) > tolerance_s:
        return None
    return best


def pct_change_over(samples: Sequence[Sample], now: float, horizon_s: float) -> float | None:
    """近 horizon_s 秒涨跌幅（%）。窗口起点 ±25% 容差内无样本 → None。"""
    base = _sample_near(samples, now - horizon_s, _WINDOW_TOLERANCE * horizon_s)
    if base is None or base.price <= 0:
        return None
    latest = samples[-1]
    return (latest.price / base.price - 1.0) * 100.0


def volume_over(samples: Sequence[Sample], now: float, horizon_s: float) -> float | None:
    """近 horizon_s 秒成交量（累计量差）。任一端未知或倒退（日切重置）→ None。"""
    base = _sample_near(samples, now - horizon_s, _WINDOW_TOLERANCE * horizon_s)
    if base is None:
        return None
    latest = samples[-1]
    if base.cum_volume is None or latest.cum_volume is None:
        return None
    delta = latest.cum_volume - base.cum_volume
    return delta if delta >= 0 else None


def minute_closes(samples: Sequence[Sample], now: float, n: int) -> list[float]:
    """最近 n 个 1 分钟桶的收价（最旧在前）。从最近往回收集，遇空桶即停。"""
    closes: list[float] = []
    for i in range(n):
        lo, hi = now - 60.0 * (i + 1), now - 60.0 * i
        bucket_close: float | None = None
        for s in reversed(samples):  # 新→旧，找到该桶最后一笔即停
            if s.ts <= lo:
                break
            if s.ts <= hi:
                bucket_close = s.price
                break
        if bucket_close is None:
            break
        closes.append(bucket_close)
    closes.reverse()
    return closes


def consecutive_direction(closes: Sequence[float]) -> int:
    """末端连续同向根数（带符号：+3=连三根收涨）。平价打断；不足两根 → 0。"""
    if len(closes) < 2:
        return 0
    last_diff = closes[-1] - closes[-2]
    if last_diff == 0:
        return 0
    sign = 1 if last_diff > 0 else -1
    count = 0
    for i in range(len(closes) - 1, 0, -1):
        diff = closes[i] - closes[i - 1]
        if diff == 0 or (diff > 0) != (sign > 0):
            break
        count += 1
    return sign * count


# --------------------------------------------------------------------------
# 单值指标
# --------------------------------------------------------------------------


def spread_pct(bid: float | None, ask: float | None) -> float | None:
    """点差占比（%）。无效/交叉盘 → None。"""
    b, a = f_or_none(bid), f_or_none(ask)
    if b is None or a is None or b <= 0 or a <= 0 or a < b:
        return None
    mid = (a + b) / 2.0
    return (a - b) / mid * 100.0


def day_change_pct(last: float | None, prev_close: float | None) -> float | None:
    """日内涨跌幅（%，相对昨收）。"""
    l_, c = f_or_none(last), f_or_none(prev_close)
    if l_ is None or c is None or c <= 0:
        return None
    return (l_ / c - 1.0) * 100.0


def vwap_dist_pct(last: float | None, vwap: float | None) -> float | None:
    """现价相对 VWAP 的偏离（%）。"""
    l_, v = f_or_none(last), f_or_none(vwap)
    if l_ is None or v is None or v <= 0:
        return None
    return (l_ / v - 1.0) * 100.0


def rvol(volume_today: float | None, av_volume: float | None) -> float | None:
    """日量相对量（当日累计 / 90日均量）。纯比值，不做时段调整——
    日内"此刻强度"由 vol_burst 表达；本值是全日维度的背景参考。"""
    v, av = f_or_none(volume_today), f_or_none(av_volume)
    if v is None or av is None or av <= 0:
        return None
    return v / av


def vol_burst(vol_1m: float | None, av_volume: float | None) -> float | None:
    """1 分钟量爆倍数：近 1 分钟量 / (90日均量/390)。开收盘天然偏高（量呈 U 形），
    事件阈值默认 8× 兜底。"""
    v, av = f_or_none(vol_1m), f_or_none(av_volume)
    if v is None or av is None or av <= 0:
        return None
    return v / (av / _MINUTES_PER_DAY)


def detect_breaks(
    prev_high: float | None,
    prev_low: float | None,
    day_high: float | None,
    day_low: float | None,
    last: float | None,
) -> tuple[bool, bool, float | None, float | None]:
    """破日高/日低检测 + 新基线。首次观察只立基线不触发；
    触发要求"日高刷新 **且** 现价仍在基线上方"（正在突破，而非冲高回落）。"""
    hod_break = (
        prev_high is not None
        and day_high is not None
        and day_high > prev_high
        and last is not None
        and last >= prev_high
    )
    lod_break = (
        prev_low is not None
        and day_low is not None
        and day_low < prev_low
        and last is not None
        and last <= prev_low
    )
    highs = [v for v in (prev_high, day_high) if v is not None]
    lows = [v for v in (prev_low, day_low) if v is not None]
    new_high = max(highs) if highs else None
    new_low = min(lows) if lows else None
    return hod_break, lod_break, new_high, new_low


def spike_score(
    chg_1m: float | None,
    chg_5m: float | None,
    day_chg: float | None,
    burst: float | None,
    w: ScoreWeights,
) -> float | None:
    """综合异动分（带符号，正=拉升 负=下跌）：
    ``(w_1m·1m% + w_5m·5m% + w_day·日%) × clamp(1 + w_vol·log10(max(1,burst)), 1, 2.5)``。
    1m 与 5m 都预热中 → 退化为 ``w_day·日%``（新上榜标的立刻可排序）；连日%都没有 → None。"""
    if chg_1m is None and chg_5m is None:
        return w.w_day * day_chg if day_chg is not None else None
    base = (
        w.w_1m * (chg_1m or 0.0)
        + w.w_5m * (chg_5m or 0.0)
        + w.w_day * (day_chg or 0.0)
    )
    mult = 1.0
    if burst is not None and burst > 0:
        mult = min(max(1.0 + w.w_vol * math.log10(max(1.0, burst)), 1.0), 2.5)
    return base * mult


def passes_coarse_filters(
    price: float | None,
    dollar_vol_today: float | None,
    spr_pct: float | None,
    *,
    min_price: float,
    min_dollar_volume: float,
    max_spread_pct: float,
    is_watch: bool,
) -> bool:
    """服务端粗滤（自选豁免）：价格/成交额未知或不达标 → 拦；
    点差未知（盘外 bid/ask 缺失）→ 放行但行内显示 —。"""
    if is_watch:
        return True
    if price is None or price < min_price:
        return False
    if dollar_vol_today is None or dollar_vol_today < min_dollar_volume:
        return False
    return not (spr_pct is not None and spr_pct > max_spread_pct)


# --------------------------------------------------------------------------
# 行组装 / 事件 / 榜单 / 订阅预算
# --------------------------------------------------------------------------


def compute_row(
    symbol: str,
    state: SymbolState,
    now: float,
    *,
    bid: float | None,
    ask: float | None,
    last: float | None,
    close: float | None,
    day_high: float | None,
    day_low: float | None,
    vwap: float | None,
    volume_shares: float | None,
    av_volume: float | None,
    halted_raw: float | None,
    delayed: bool,
    watch: bool,
    weights: ScoreWeights | None = None,
) -> dict[str, Any]:
    """把一只标的的原始 ticker 值 + 滚动窗口算成雷达行。所有数值出口已 NaN 清洗。"""
    w = weights if weights is not None else ScoreWeights()
    b, a = f_or_none(bid), f_or_none(ask)
    l_ = f_or_none(last)
    hi, lo = f_or_none(day_high), f_or_none(day_low)
    vol = f_or_none(volume_shares)
    av = f_or_none(av_volume)
    halted_f = f_or_none(halted_raw)
    halted: bool | None = None if halted_f is None else halted_f >= 1.0

    chg_1m = pct_change_over(state.samples, now, 60.0)
    chg_5m = pct_change_over(state.samples, now, 300.0)
    vol_1m = volume_over(state.samples, now, 60.0)
    consec = consecutive_direction(minute_closes(state.samples, now, 6))
    burst = vol_burst(vol_1m, av)
    day_chg = day_change_pct(l_, close)
    dollar_vol = l_ * vol if (l_ is not None and vol is not None) else None

    hod_dist = (hi - l_) / l_ * 100.0 if (hi is not None and l_ is not None and l_ > 0) else None
    lod_dist = (l_ - lo) / l_ * 100.0 if (lo is not None and l_ is not None and l_ > 0) else None

    return {
        "symbol": symbol,
        "watch": watch,
        "last": l_,
        "bid": b,
        "ask": a,
        "spread_pct": spread_pct(b, a),
        "day_chg_pct": day_chg,
        "chg_1m_pct": chg_1m,
        "chg_5m_pct": chg_5m,
        "vol_1m": f_or_none(vol_1m),
        "rvol": rvol(vol, av),
        "vol_burst": f_or_none(burst),
        "vwap_dist_pct": vwap_dist_pct(l_, vwap),
        "hod_dist_pct": f_or_none(hod_dist),
        "lod_dist_pct": f_or_none(lod_dist),
        "consec": consec,
        "dollar_vol_today": f_or_none(dollar_vol),
        "halted": halted,
        "delayed": delayed,
        "score": f_or_none(spike_score(chg_1m, chg_5m, day_chg, burst, w)),
        "warmup_1m": chg_1m is None,
        "warmup_5m": chg_5m is None,
        "day_high": hi,
        "day_low": lo,
    }


def detect_events(
    state: SymbolState,
    row: Mapping[str, Any],
    th: Thresholds,
    now: float,
) -> list[dict[str, Any]]:
    """按行指标产出事件（无 id/ts，服务层补）。

    确定性但**有状态**：维护 (类型→上次触发) 冷却、停牌跃迁、破高破低基线。
    基线/停牌状态**每轮都更新**，冷却只抑制事件发射。
    """
    events: list[dict[str, Any]] = []

    def fire(etype: str, direction: str, value: float | None) -> None:
        last_ts = state.last_event_ts.get(etype)
        if last_ts is not None and now - last_ts < th.event_cooldown_s:
            return
        state.last_event_ts[etype] = now
        events.append(
            {
                "symbol": row["symbol"],
                "type": etype,
                "dir": direction,
                "value": value,
                "price": row.get("last"),
            }
        )

    # 停牌/复牌跃迁（None=未知：不触发也不覆盖已知状态）
    halted = row.get("halted")
    if halted is not None:
        prev = state.last_halted
        if halted and prev is not True:
            fire("halted", "flat", None)
        elif not halted and prev is True:
            fire("resumed", "flat", None)
        state.last_halted = halted

    chg_1m = row.get("chg_1m_pct")
    if chg_1m is not None and abs(chg_1m) >= th.spike_1m_pct:
        fire("spike_1m", "up" if chg_1m > 0 else "down", chg_1m)

    chg_5m = row.get("chg_5m_pct")
    consec = int(row.get("consec") or 0)
    if (
        chg_5m is not None
        and abs(consec) >= th.sustained_min_candles
        and abs(chg_5m) >= th.sustained_5m_pct
        and (consec > 0) == (chg_5m > 0)
    ):
        fire("sustained_5m", "up" if chg_5m > 0 else "down", chg_5m)

    hod, lod, new_high, new_low = detect_breaks(
        state.prev_day_high, state.prev_day_low, row.get("day_high"), row.get("day_low"), row.get("last")
    )
    if hod:
        fire("hod_break", "up", row.get("day_high"))
    if lod:
        fire("lod_break", "down", row.get("day_low"))
    state.prev_day_high = new_high
    state.prev_day_low = new_low

    burst = row.get("vol_burst")
    if (
        burst is not None
        and burst >= th.vol_burst_ratio
        and chg_1m is not None
        and abs(chg_1m) >= 0.15  # 防大宗成交无价动误报
    ):
        fire("vol_burst", "up" if chg_1m > 0 else "down", burst)

    return events


def build_boards(
    rows: Sequence[Mapping[str, Any]],
    board_size: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按 score 分流排序截断：(急拉榜, 急跌榜)。score None/0 不上榜；同分按代码（确定性）。"""
    up = sorted(
        (dict(r) for r in rows if r.get("score") is not None and r["score"] > 0),
        key=lambda r: (-r["score"], r["symbol"]),
    )
    down = sorted(
        (dict(r) for r in rows if r.get("score") is not None and r["score"] < 0),
        key=lambda r: (r["score"], r["symbol"]),
    )
    return up[:board_size], down[:board_size]


def plan_subscriptions(
    current: Mapping[str, float],
    scan_ranked: Sequence[str],
    protected: AbstractSet[str],
    *,
    now: float,
    cap: int,
    evict_after_s: float,
    max_evictions: int,
) -> tuple[list[str], list[str]]:
    """订阅预算计划：(新订列表, 撤订列表)。

    - 候选 = 扫描榜名次序里既不在订、也不受保护（自选走 QuoteService，不归雷达订）的代码
    - 空位不够时，从「不在本轮扫描 ∧ 非保护 ∧ 失联超 evict_after_s」里
      按最旧优先（同龄字典序）驱逐，每轮至多 max_evictions 只
    - 空扫描（如盘外/无权限）绝不驱逐——避免一次坏扫描清空雷达
    """
    scan_set = set(scan_ranked)
    seen: set[str] = set()
    candidates: list[str] = []
    for sym in scan_ranked:
        if sym in current or sym in protected or sym in seen:
            continue
        seen.add(sym)
        candidates.append(sym)
    if not candidates:
        return [], []

    free = max(0, cap - len(current))
    if len(candidates) <= free:
        return candidates, []

    pool = sorted(
        (
            sym
            for sym, last_seen in current.items()
            if sym not in scan_set and sym not in protected and now - last_seen > evict_after_s
        ),
        key=lambda sym: (current[sym], sym),
    )
    n_evict = min(len(candidates) - free, max_evictions, len(pool))
    to_unsub = pool[:n_evict]
    to_sub = candidates[: free + n_evict]
    return to_sub, to_unsub


__all__ = [
    "Sample",
    "SymbolState",
    "Thresholds",
    "ScoreWeights",
    "f_or_none",
    "pct_change_over",
    "volume_over",
    "minute_closes",
    "consecutive_direction",
    "spread_pct",
    "day_change_pct",
    "vwap_dist_pct",
    "rvol",
    "vol_burst",
    "detect_breaks",
    "spike_score",
    "passes_coarse_filters",
    "compute_row",
    "detect_events",
    "build_boards",
    "plan_subscriptions",
]
