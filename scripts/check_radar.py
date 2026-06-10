"""异动雷达前置探测：扫描器权限 + generic tick 字段 + 成交量单位核实。

用法（先启动 paper Gateway，最好在美股盘中跑——盘外扫描器可能返回空）：
    uv run python scripts/check_radar.py
    uv run python scripts/check_radar.py --mover TSLA   # 指定第二只探测标的

依次探测并打印：
1. reqScannerParameters 里是否含雷达要用的 scan code
   （TOP_PERC_GAIN / TOP_PERC_LOSE / HOT_BY_VOLUME）。
2. 实跑一次 TOP_PERC_GAIN（STK / STK.US.MAJOR / 10 行）——能拿到代码列表
   说明扫描器权限 OK；只见错误码说明账户无扫描器/行情权限。
3. 对 AAPL + 一只活跃股订阅 generic tick "233,165,293,294,295,595" 流 10 秒，
   打印 last/volume/rtVolume/avVolume/vwap/halted/volumeRate3Min/marketDataType。

人工核对要点（决定 config/panel.yaml 的 radar 配置）：
- **成交量单位**：盘中 AAPL 当日 volume 应为 4000万–6000万（股）。若只有 ~50万，
  说明 Gateway 按 lots(×100) 报量 → panel.yaml 设 radar.volume_lot_multiplier: 100。
- **595 可用性**：若 errorEvent 报 595/volumeRate 相关错误，把它从
  radar.generic_ticks 里去掉（只影响"即时量速"排序，非必需）。
- **扫描器无权限**：雷达会自动退化为"仅自选"模式，面板有状态横幅，不阻塞使用。
"""

from __future__ import annotations

import argparse
import math
import sys
from typing import Any

from ib_async import IB, ScannerSubscription, Stock
from scalper.execution.ib_broker_adapter import connect_paper

from webwatch.config import Environment, load_settings

_SCAN_CODES = ("TOP_PERC_GAIN", "TOP_PERC_LOSE", "HOT_BY_VOLUME")
_GENERIC_TICKS = "233,165,293,294,295,595"
# 连接/农场状态类信息码，探测时不当错误打印。
_BENIGN = frozenset({2104, 2106, 2107, 2108, 2119, 2158})


def _fmt(v: Any) -> str:
    if v is None:
        return "<none>"
    if isinstance(v, float) and math.isnan(v):
        return "<nan>"
    return str(v)


def _on_error(req_id: int, code: int, msg: str, contract: Any = None) -> None:
    if code in _BENIGN:
        return
    sym = f" [{contract.symbol}]" if contract is not None and hasattr(contract, "symbol") else ""
    print(f"[ib-error] reqId={req_id} code={code}{sym} {msg}", file=sys.stderr)


def probe_scanner_params(ib: IB) -> None:
    print("\n== 1/3 扫描器参数（权限与可用 scan code）==")
    try:
        xml = ib.reqScannerParameters()
    except Exception as exc:  # noqa: BLE001 — 探测脚本，打印后继续其余探测
        print(f"  ❌ reqScannerParameters 失败: {type(exc).__name__}: {exc}")
        return
    print(f"  参数 XML 长度: {len(xml)}")
    for code in _SCAN_CODES:
        mark = "✅" if code in xml else "❌ 不在参数表"
        print(f"  {code:<16} {mark}")


def probe_scan_once(ib: IB) -> None:
    print("\n== 2/3 实跑 TOP_PERC_GAIN（STK.US.MAJOR，10 行）==")
    sub = ScannerSubscription(
        numberOfRows=10,
        instrument="STK",
        locationCode="STK.US.MAJOR",
        scanCode="TOP_PERC_GAIN",
        abovePrice=1.0,
        aboveVolume=100_000,
    )
    data = ib.reqScannerSubscription(sub)
    ib.sleep(8)  # 等服务端推一轮结果
    rows = list(data)
    ib.cancelScannerSubscription(data)
    if not rows:
        print("  ❌ 空结果。若上方有 ib-error（如权限/订阅类错误码）→ 账户无扫描器权限；")
        print("     若无错误且当前为盘外时段 → 盘中再试一次。")
        return
    print(f"  ✅ 收到 {len(rows)} 行：")
    for sd in rows:
        c = sd.contractDetails.contract
        print(f"    #{sd.rank:<3} {c.symbol:<6} ({c.primaryExchange or c.exchange})")


def probe_ticks(ib: IB, symbols: list[str]) -> None:
    print(f"\n== 3/3 generic tick 探测 {symbols}（{_GENERIC_TICKS}，流 10s）==")
    tickers = []
    for sym in symbols:
        contract = Stock(sym, "SMART", "USD")
        ib.qualifyContracts(contract)
        tickers.append((sym, ib.reqMktData(contract, _GENERIC_TICKS, False, False)))
    ib.sleep(10)
    for sym, t in tickers:
        print(f"  {sym}:")
        print(f"    marketDataType  = {_fmt(t.marketDataType)}  (1实时 2冻结 3延迟 4延迟冻结)")
        print(f"    last/bid/ask    = {_fmt(t.last)} / {_fmt(t.bid)} / {_fmt(t.ask)}")
        print(f"    volume(当日)    = {_fmt(t.volume)}   ← AAPL 盘中应为 4000万-6000万(股)级")
        print(f"    rtVolume        = {_fmt(t.rtVolume)} ← tick233 当日累计(股)，与上行对照定单位")
        print(f"    avVolume(90日)  = {_fmt(t.avVolume)}")
        print(f"    vwap            = {_fmt(t.vwap)}")
        print(f"    halted          = {_fmt(t.halted)}  (0正常 1/2停牌 nan未知)")
        print(
            f"    volumeRate 3/5/10min = {_fmt(t.volumeRate3Min)} / "
            f"{_fmt(t.volumeRate5Min)} / {_fmt(t.volumeRate10Min)}  ← 595，报错可去掉"
        )
    for _, t in tickers:
        if t.contract is not None:
            ib.cancelMktData(t.contract)


def main() -> int:
    parser = argparse.ArgumentParser(description="异动雷达前置探测（默认 paper）")
    parser.add_argument("--mover", default="TSLA", help="第二只探测标的（默认 TSLA）")
    args = parser.parse_args()

    settings = load_settings(Environment.PAPER)
    print(f"[webwatch] 连接配置: {settings.redacted()}")
    ib = IB()
    ib.errorEvent += _on_error
    try:
        connect_paper(ib, settings.ibkr_host, settings.ibkr_port, settings.ibkr_client_id + 1)
        print(f"[webwatch] ✅ 已连接 paper Gateway ({settings.ibkr_host}:{settings.ibkr_port})")
        probe_scanner_params(ib)
        probe_scan_once(ib)
        probe_ticks(ib, ["AAPL", args.mover.strip().upper()])
        print("\n[webwatch] 探测完成。按模块 docstring 的「人工核对要点」检查上方输出。")
    except Exception as exc:  # noqa: BLE001 — smoke 脚本，打印友好错误即可
        print(f"[webwatch] ❌ 探测失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if ib.isConnected():
            ib.disconnect()
            print("[webwatch] 已断开。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
