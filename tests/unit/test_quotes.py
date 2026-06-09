"""quotes 模块单测 —— 独立行情服务（fake IB，无需 Gateway、不依赖 scalper）。"""

from __future__ import annotations

from typing import Any

import pytest

from webwatch.quotes import QuoteService


class FakeTicker:
    def __init__(self, mdt: int) -> None:
        self.bid = 10.0
        self.ask = 10.10
        self.last = 10.05
        self.close = 9.90
        self.marketDataType = mdt


class FakeContract:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol.upper()


class FakeIBQ:
    def __init__(self) -> None:
        self.mdt = 0
        self.subscribed: list[str] = []
        self.cancelled: list[str] = []

    def isConnected(self) -> bool:  # noqa: N802
        return True

    def reqMarketDataType(self, t: int) -> None:  # noqa: N802
        self.mdt = t

    async def qualifyContractsAsync(self, contract: Any) -> list[Any]:  # noqa: N802
        return [contract]

    def reqMktData(self, contract: Any, *args: Any) -> FakeTicker:  # noqa: N802
        self.subscribed.append(contract.symbol)
        return FakeTicker(self.mdt)

    def cancelMktData(self, contract: Any) -> None:  # noqa: N802
        self.cancelled.append(contract.symbol)


# QuoteService 内部用 ib_async.Stock 构造 contract；用 monkeypatch 换成 FakeContract，
# 避免依赖真实 ib_async 合约构造。
@pytest.fixture(autouse=True)
def _patch_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("webwatch.quotes.Stock", lambda sym, exch, cur: FakeContract(sym))


class TestSubscribe:
    async def test_subscribe_before_connect_queues_then_resubscribes(self) -> None:
        qs = QuoteService(market_data_type=1)
        await qs.subscribe("aapl")  # 未连接 → 仅入 watchlist
        assert qs.watchlist() == ["AAPL"]
        assert qs.quotes()[0]["has_data"] is False

        ib = FakeIBQ()
        qs.attach(ib)
        await qs.resubscribe_all()
        q = qs.quotes()[0]
        assert q["symbol"] == "AAPL"
        assert q["bid"] == 10.0 and q["ask"] == 10.10 and q["last"] == 10.05
        assert q["data_type"] == "实时"
        assert q["delayed"] is False
        assert ib.subscribed == ["AAPL"]

    async def test_unsubscribe_cancels(self) -> None:
        qs = QuoteService()
        ib = FakeIBQ()
        qs.attach(ib)
        await qs.subscribe("tsla")
        qs.unsubscribe("tsla")
        assert qs.watchlist() == []
        assert ib.cancelled == ["TSLA"]


class TestMarketDataType:
    def test_attach_sets_type(self) -> None:
        qs = QuoteService(market_data_type=3)
        ib = FakeIBQ()
        qs.attach(ib)
        assert ib.mdt == 3

    def test_invalid_default_falls_back_to_realtime(self) -> None:
        assert QuoteService(market_data_type=99).market_data_type == 1

    async def test_switch_to_delayed_resubscribes(self) -> None:
        qs = QuoteService(market_data_type=1)
        ib = FakeIBQ()
        qs.attach(ib)
        await qs.subscribe("aapl")
        assert qs.quotes()[0]["delayed"] is False

        eff = await qs.set_market_data_type(3)  # 切延迟
        assert eff == 3
        assert ib.mdt == 3
        q = qs.quotes()[0]
        assert q["data_type"] == "延迟"
        assert q["delayed"] is True
        assert "AAPL" in ib.cancelled  # 切换时先撤再订

    async def test_invalid_type_rejected(self) -> None:
        qs = QuoteService()
        qs.attach(FakeIBQ())
        with pytest.raises(ValueError):
            await qs.set_market_data_type(7)


class TestDetach:
    async def test_detach_clears_tickers(self) -> None:
        qs = QuoteService()
        ib = FakeIBQ()
        qs.attach(ib)
        await qs.subscribe("aapl")
        qs.detach()
        assert qs.connected is False
        # watchlist 保留（重连后可重订），但行情数据已清
        assert qs.watchlist() == ["AAPL"]
        assert qs.quotes()[0]["has_data"] is False
