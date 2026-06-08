"""M0 连接 smoke test：连上 IBKR Gateway 并打印账户摘要。

用法（先启动对应模式的 IB Gateway / TWS）：
    python scripts/check_connection.py            # 默认 paper(4002)
    python scripts/check_connection.py --env live # live(4001)，需 paper 验证后再用

成功标准：打印出 NetLiquidation / AvailableFunds 等账户字段，且账户号开头与环境匹配
（paper=DU，live=U）。这证明 webwatch 能复用 scalper 的连接层连上 Gateway。
"""

from __future__ import annotations

import argparse
import sys

from ib_async import IB
from scalper.execution.ib_broker_adapter import connect_live, connect_paper

from webwatch.config import Environment, load_settings

# 想看的账户字段（ib_async accountValues 的 tag）。
_WANT_TAGS = (
    "NetLiquidation",
    "TotalCashValue",
    "AvailableFunds",
    "BuyingPower",
    "ExcessLiquidity",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="IBKR 连接 smoke test")
    parser.add_argument(
        "--env",
        choices=[e.value for e in Environment],
        default=Environment.PAPER.value,
        help="运行环境（默认 paper）",
    )
    args = parser.parse_args()
    env = Environment(args.env)

    settings = load_settings(env)
    print(f"[webwatch] 连接配置: {settings.redacted()}")

    if settings.ibkr_account in ("", "DU0000000"):
        print(
            "[webwatch] ⚠️  未配置真实账户号。请先复制 "
            f"config/secrets.env.example → config/secrets.{env.value}.env 并填写。",
            file=sys.stderr,
        )

    ib = IB()
    try:
        if env is Environment.PAPER:
            connect_paper(ib, settings.ibkr_host, settings.ibkr_port, settings.ibkr_client_id)
        else:
            connect_live(
                ib,
                settings.ibkr_host,
                settings.ibkr_port,
                settings.ibkr_client_id,
                expected_account=settings.ibkr_account or None,
            )
        print(f"[webwatch] ✅ 已连接 {env.value} Gateway ({settings.ibkr_host}:{settings.ibkr_port})")

        managed = list(ib.managedAccounts())
        print(f"[webwatch] managedAccounts: {[a[:2] + '***' for a in managed]}")

        account = settings.ibkr_account or (managed[0] if managed else "")
        values = ib.accountValues(account=account)
        wanted = {v.tag: v.value for v in values if v.tag in _WANT_TAGS and v.currency in ("USD", "")}
        print("[webwatch] 账户摘要:")
        for tag in _WANT_TAGS:
            print(f"    {tag:<18} {wanted.get(tag, '<n/a>')}")
    except Exception as exc:  # noqa: BLE001 — smoke 脚本，打印友好错误即可
        print(f"[webwatch] ❌ 连接/查询失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if ib.isConnected():
            ib.disconnect()
            print("[webwatch] 已断开。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
