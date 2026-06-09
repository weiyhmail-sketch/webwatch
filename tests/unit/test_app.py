"""app 路由/WS 测试 —— 注入 fake broker，无需 Gateway。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from scalper.strategy.base import Side

from webwatch.app import _risk_for, create_app
from webwatch.broker import ProtectionFailed
from webwatch.config import PanelConfig
from webwatch.pricing import Target
from webwatch.risk import BLOCK


class FakeBroker:
    """实现 BrokerLike 的最小假券商。"""

    def __init__(
        self,
        *,
        protection_fails: bool = False,
        nav: Decimal | None = Decimal("1000000"),
        buying_power: Decimal | None = Decimal("4000000"),
        live: bool = False,
    ) -> None:
        self.connected = True
        self.live = live
        self.watchlist: list[str] = []
        self.placed: list[dict[str, Any]] = []
        self.market_orders: list[dict[str, Any]] = []
        self.protection_fails = protection_fails
        self.nav = nav
        self.buying_power = buying_power
        self.cancelled = False
        self.flattened = False

    def risk_inputs(self) -> tuple[Decimal | None, Decimal | None]:
        return (self.nav, self.buying_power)

    async def cancel_all_orders(self) -> dict[str, Any]:
        self.cancelled = True
        return {"cancelled": True}

    async def flatten_all(self) -> dict[str, Any]:
        self.flattened = True
        return {"flattened": [{"symbol": "AAPL", "action": "SELL", "quantity": 10}]}

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    def is_live(self) -> bool:
        return self.live

    async def watch(self, symbol: str) -> None:
        s = symbol.strip().upper()
        if s and s not in self.watchlist:
            self.watchlist.append(s)

    async def set_market_data_type(self, mdt: int) -> int:
        if mdt not in (1, 2, 3, 4):
            raise ValueError("bad type")
        self.mdt = mdt
        return mdt

    def unwatch(self, symbol: str) -> None:
        s = symbol.strip().upper()
        if s in self.watchlist:
            self.watchlist.remove(s)

    async def place_limit_bracket(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: int,
        entry_limit: Decimal,
        take_profit: Decimal,
        stop_loss: Decimal,
        outside_rth: bool = False,
        stop_limit_offset_pct: Decimal | None = None,
    ) -> dict[str, Any]:
        rec = {
            "symbol": symbol,
            "side": side.value,
            "quantity": quantity,
            "entry_limit": str(entry_limit),
            "take_profit": str(take_profit),
            "stop_loss": str(stop_loss),
            "outside_rth": outside_rth,
            "stop_limit": "x" if outside_rth else None,
            "parent_id": 1,
            "take_profit_id": 2,
            "stop_loss_id": 3,
        }
        self.placed.append(rec)
        return rec

    async def place_market_with_protection(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: int,
        take_profit: Target,
        stop_loss: Target,
    ) -> dict[str, Any]:
        if self.protection_fails:
            raise ProtectionFailed(
                filled=quantity,
                fill_price=Decimal("200"),
                reason="保护单被拒",
                flatten={"flatten_order_id": 9, "action": "SELL", "quantity": quantity},
            )
        rec = {
            "filled": quantity,
            "fill_price": "200",
            "symbol": symbol,
            "take_profit": "200.40",
            "stop_loss": "199.20",
            "oca_group": "scalper-oca-ww-1",
        }
        self.market_orders.append(rec)
        return rec

    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "environment": "paper",
            "market_data_type": 1,
            "error": None,
            "account": {"account": "DU***67", "net_liquidation": "30000.50",
                        "total_cash_value": "15000.00", "buying_power": "60000.00",
                        "excess_liquidity": None, "currency": "USD"},
            "positions": [],
            "orders": [],
            "quotes": [{"symbol": s, "bid": None, "ask": None, "last": None, "close": None}
                       for s in self.watchlist],
            "watchlist": list(self.watchlist),
        }


@pytest.fixture(autouse=True)
def _tradable(monkeypatch: Any) -> None:
    """默认把"当前可交易"固定为 True，使下单测试不受真实时钟(周末)影响。
    需要测休市的用例自行覆盖。"""
    monkeypatch.setattr("webwatch.app.is_tradable_extended", lambda: True)


def _client() -> TestClient:
    return TestClient(create_app(FakeBroker(), auto_connect=False))


def test_index_served() -> None:
    with _client() as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "WebWatch" in r.text


def test_state_endpoint() -> None:
    with _client() as c:
        r = c.get("/api/state")
        assert r.status_code == 200
        body = r.json()
        assert body["connected"] is True
        assert body["environment"] == "paper"
        assert body["account"]["account"] == "DU***67"


def test_watch_then_unwatch() -> None:
    with _client() as c:
        r = c.post("/api/watch", json={"symbol": "aapl"})
        assert r.status_code == 200
        assert "AAPL" in r.json()["watchlist"]

        r2 = c.delete("/api/watch/AAPL")
        assert "AAPL" not in r2.json()["watchlist"]


def test_websocket_pushes_initial_snapshot() -> None:
    with _client() as c, c.websocket_connect("/ws") as ws:
        data = ws.receive_json()
        assert data["environment"] == "paper"
        assert "quotes" in data


def _order_body(**kw: Any) -> dict[str, Any]:
    body = {
        "symbol": "aapl",
        "quantity": 10,
        "entry_limit": "50",
        "tp_kind": "pct",
        "tp_value": "0.002",
        "sl_kind": "pct",
        "sl_value": "0.004",
        "side": "long",
    }
    body.update(kw)
    return body


def test_order_preview_ok() -> None:
    with _client() as c:
        r = c.post("/api/order/preview", json=_order_body())
        assert r.status_code == 200
        body = r.json()
        assert body["rejected"] is False
        assert body["plan"]["take_profit"] == "50.10"


def test_order_preview_notional_rejected() -> None:
    with _client() as c:
        # 300 * 100 = 30000 > 上限 20000
        r = c.post("/api/order/preview", json=_order_body(quantity=300, entry_limit="100"))
        body = r.json()
        assert body["rejected"] is True
        assert "notional" in body["reason"]


def test_order_limit_places_bracket() -> None:
    fake = FakeBroker()
    with TestClient(create_app(fake, auto_connect=False)) as c:
        r = c.post("/api/order/limit", json=_order_body())
        assert r.status_code == 200
        body = r.json()
        assert body["rejected"] is False
        assert body["placement"]["parent_id"] == 1
        assert len(fake.placed) == 1
        assert fake.placed[0]["take_profit"] == "50.10"


def test_order_limit_rejected_does_not_place() -> None:
    fake = FakeBroker()
    with TestClient(create_app(fake, auto_connect=False)) as c:
        r = c.post("/api/order/limit", json=_order_body(quantity=300, entry_limit="100"))
        assert r.status_code == 400
        assert r.json()["rejected"] is True
        assert len(fake.placed) == 0  # 风控拒单不下单


def _market_body(**kw: Any) -> dict[str, Any]:
    body = {
        "symbol": "aapl",
        "quantity": 10,
        "ref_price": "200",
        "tp_kind": "pct",
        "tp_value": "0.002",
        "sl_kind": "pct",
        "sl_value": "0.004",
        "side": "long",
    }
    body.update(kw)
    return body


def test_market_preview_ok() -> None:
    with _client() as c:
        r = c.post("/api/order/market/preview", json=_market_body())
        body = r.json()
        assert body["rejected"] is False
        assert body["plan"]["preview_take_profit"] == "200.40"


def test_market_order_places_with_protection(monkeypatch: Any) -> None:
    monkeypatch.setattr("webwatch.app.is_rth", lambda: True)  # 固定 RTH 走市价路径
    fake = FakeBroker()
    with TestClient(create_app(fake, auto_connect=False)) as c:
        r = c.post("/api/order/market", json=_market_body())
        body = r.json()
        assert body["rejected"] is False
        assert body["placement"]["filled"] == 10
        assert len(fake.market_orders) == 1


def test_market_order_protection_failed_surfaced(monkeypatch: Any) -> None:
    monkeypatch.setattr("webwatch.app.is_rth", lambda: True)
    fake = FakeBroker(protection_fails=True)
    with TestClient(create_app(fake, auto_connect=False)) as c:
        r = c.post("/api/order/market", json=_market_body())
        body = r.json()
        assert body["protection_failed"] is True
        assert body["flatten"]["action"] == "SELL"


def test_market_order_notional_rejected_does_not_place() -> None:
    fake = FakeBroker()
    with TestClient(create_app(fake, auto_connect=False)) as c:
        r = c.post("/api/order/market", json=_market_body(quantity=300, ref_price="100"))
        assert r.status_code == 400
        assert len(fake.market_orders) == 0


def test_order_limit_risk_block_does_not_place() -> None:
    # NAV 很小 → 单笔最大亏损超 0.5% NAV → 风控拦截
    fake = FakeBroker(nav=Decimal("1000"))
    with TestClient(create_app(fake, auto_connect=False)) as c:
        r = c.post("/api/order/limit", json=_order_body(quantity=100, entry_limit="50"))
        assert r.status_code == 400
        assert "风控" in r.json()["reason"]
        assert len(fake.placed) == 0


def test_preview_includes_risk_warnings() -> None:
    # NAV < 25k → PDT 警告（不拦截）
    fake = FakeBroker(nav=Decimal("20000"))
    with TestClient(create_app(fake, auto_connect=False)) as c:
        r = c.post("/api/order/preview", json=_order_body())
        body = r.json()
        assert body["rejected"] is False
        assert any(f["code"] == "pdt" for f in body["risk"])


def test_account_unavailable_blocks_large_order() -> None:
    # 账户数据不可用 + 大额单 → fail-closed 拒单（盲飞不放大单）
    fake = FakeBroker(nav=None, buying_power=None)
    with TestClient(create_app(fake, auto_connect=False)) as c:
        r = c.post("/api/order/limit", json=_order_body(quantity=100, entry_limit="50"))  # 5000>1000
        assert r.status_code == 400
        assert "盲飞" in r.json()["reason"]
        assert len(fake.placed) == 0


def test_account_unavailable_warns_small_order() -> None:
    # 账户数据不可用 + 小额单 → 仅 WARN，仍可下单
    fake = FakeBroker(nav=None, buying_power=None)
    with TestClient(create_app(fake, auto_connect=False)) as c:
        r = c.post("/api/order/limit", json=_order_body(quantity=10, entry_limit="50"))  # 500<1000
        body = r.json()
        assert body["rejected"] is False
        assert any(f["code"] == "account_unavailable" for f in body["risk"])
        assert len(fake.placed) == 1


def test_live_order_cap_blocks_oversized() -> None:
    # live 下单笔 notional 超 live 上限(默认 20000) → BLOCK
    fake = FakeBroker(live=True)
    findings = _risk_for(fake, Decimal("300"), Decimal("299"), 100, Side.LONG, PanelConfig({}))
    assert any(f.code == "live_order_cap" and f.severity == BLOCK for f in findings)


def test_paper_has_no_live_cap() -> None:
    fake = FakeBroker(live=False)
    findings = _risk_for(fake, Decimal("300"), Decimal("299"), 100, Side.LONG, PanelConfig({}))
    assert not any(f.code == "live_order_cap" for f in findings)


def test_market_order_outside_rth_converts_to_limit(monkeypatch: Any) -> None:
    # 时段外：市价买入自动转激进限价 bracket（outside_rth=True）
    monkeypatch.setattr("webwatch.app.is_rth", lambda: False)
    fake = FakeBroker()
    with TestClient(create_app(fake, auto_connect=False)) as c:
        r = c.post("/api/order/market", json=_market_body())
        body = r.json()
        assert body["rejected"] is False
        assert body["converted_to_limit"] is True
        assert len(fake.placed) == 1  # 走了 place_limit_bracket
        assert fake.placed[0]["outside_rth"] is True
        assert len(fake.market_orders) == 0  # 没走真·市价


def test_market_order_rth_uses_market_path(monkeypatch: Any) -> None:
    monkeypatch.setattr("webwatch.app.is_rth", lambda: True)
    fake = FakeBroker()
    with TestClient(create_app(fake, auto_connect=False)) as c:
        r = c.post("/api/order/market", json=_market_body())
        body = r.json()
        assert body.get("converted_to_limit") is None
        assert len(fake.market_orders) == 1  # 真·市价路径


def test_limit_order_outside_rth_passes_flag(monkeypatch: Any) -> None:
    monkeypatch.setattr("webwatch.app.is_rth", lambda: False)
    fake = FakeBroker()
    with TestClient(create_app(fake, auto_connect=False)) as c:
        r = c.post("/api/order/limit", json=_order_body())
        assert r.json()["rejected"] is False
        assert fake.placed[0]["outside_rth"] is True


def test_closed_session_rejects_limit(monkeypatch: Any) -> None:
    # 休市（周末）→ 拒单，不挂 resting 过周末
    monkeypatch.setattr("webwatch.app.is_tradable_extended", lambda: False)
    fake = FakeBroker()
    with TestClient(create_app(fake, auto_connect=False)) as c:
        r = c.post("/api/order/limit", json=_order_body())
        assert r.status_code == 400
        assert "休市" in r.json()["reason"]
        assert len(fake.placed) == 0


def test_closed_session_rejects_market(monkeypatch: Any) -> None:
    monkeypatch.setattr("webwatch.app.is_tradable_extended", lambda: False)
    fake = FakeBroker()
    with TestClient(create_app(fake, auto_connect=False)) as c:
        r = c.post("/api/order/market", json=_market_body())
        assert r.status_code == 400
        assert len(fake.placed) == 0 and len(fake.market_orders) == 0


def test_market_data_type_endpoint() -> None:
    fake = FakeBroker()
    with TestClient(create_app(fake, auto_connect=False)) as c:
        r = c.post("/api/market_data_type", json={"market_data_type": 3})
        assert r.json() == {"ok": True, "market_data_type": 3}
        bad = c.post("/api/market_data_type", json={"market_data_type": 9})
        assert bad.status_code == 400


def test_cancel_all_endpoint() -> None:
    fake = FakeBroker()
    with TestClient(create_app(fake, auto_connect=False)) as c:
        r = c.post("/api/cancel_all")
        assert r.json()["ok"] is True
        assert fake.cancelled is True


def test_flatten_all_endpoint() -> None:
    fake = FakeBroker()
    with TestClient(create_app(fake, auto_connect=False)) as c:
        r = c.post("/api/flatten_all")
        body = r.json()
        assert body["ok"] is True
        assert len(body["flattened"]) == 1
        assert fake.flattened is True
