"""FastAPI 面板后端 —— M1 只读：报价 / 持仓 / 挂单 / 账户，WebSocket 推送。

启动：``uv run uvicorn webwatch.app:app --reload``（默认 paper，best-effort 连接）。
下单路由在 M3/M4 加。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from scalper.strategy.base import Side

from webwatch.broker import BrokerLike, BrokerManager, EntryUncertain, ProtectionFailed
from webwatch.config import PanelConfig, load_panel_config, load_settings, resolve_environment
from webwatch.order_service import (
    MarketOrderPlan,
    OrderRejected,
    market_plan_to_dict,
    plan_limit_bracket,
    plan_market_order,
    plan_to_dict,
)
from webwatch.pricing import Target, TargetKind
from webwatch.risk import BLOCK, WARN, RiskFinding, finding_to_dict
from webwatch.risk import assess as risk_assess
from webwatch.risk import blocks as risk_blocks

log = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent / "web"
BROADCAST_INTERVAL_S = 0.3  # 报价/状态推送间隔（秒）


class WatchRequest(BaseModel):
    symbol: str


class MarketDataTypeRequest(BaseModel):
    market_data_type: int  # 1=实时 2=冻结 3=延迟 4=延迟冻结


class LimitOrderRequest(BaseModel):
    symbol: str
    quantity: int
    entry_limit: str  # Decimal 字符串，避免 float 精度
    tp_kind: str = "pct"
    tp_value: str
    sl_kind: str = "pct"
    sl_value: str
    side: str = "long"


class MarketOrderRequest(BaseModel):
    symbol: str
    quantity: int
    ref_price: str  # 当前参考价（前端报价），用于下单前风控 + 预览
    tp_kind: str = "pct"
    tp_value: str
    sl_kind: str = "pct"
    sl_value: str
    side: str = "long"


def _market_plan_from_request(body: MarketOrderRequest, panel: PanelConfig) -> MarketOrderPlan:
    try:
        ref = Decimal(body.ref_price)
        take_profit = Target(TargetKind(body.tp_kind), Decimal(body.tp_value))
        stop_loss = Target(TargetKind(body.sl_kind), Decimal(body.sl_value))
        side = Side(body.side)
    except (InvalidOperation, ValueError) as exc:
        raise OrderRejected(f"参数非法: {exc}") from exc
    return plan_market_order(
        symbol=body.symbol,
        quantity=body.quantity,
        ref_price=ref,
        take_profit=take_profit,
        stop_loss=stop_loss,
        panel=panel,
        side=side,
    )


def _risk_for(
    broker_: BrokerLike,
    entry: Decimal,
    stop_loss: Decimal,
    quantity: int,
    side: Side,
    panel: PanelConfig,
) -> Any:
    """取账户 NAV/购买力，跑下单前风控，返回 findings 列表。"""
    nav, buying_power = broker_.risk_inputs()
    findings = risk_assess(
        entry=entry,
        stop_loss=stop_loss,
        quantity=quantity,
        side=side,
        nav=nav,
        buying_power=buying_power,
        panel=panel,
    )
    # live 首周单笔 notional 上限（仅 live 生效）——把验证期规模封住。
    if broker_.is_live():
        notional = entry * quantity
        if notional > panel.live_max_order_notional_usd:
            findings = [
                RiskFinding(
                    "live_order_cap",
                    BLOCK,
                    f"LIVE 首周单笔上限 ${panel.live_max_order_notional_usd}，"
                    f"本单 ${notional} 超限，已拒单",
                ),
                *findings,
            ]
    # 已连接但读不到账户数据 → 账户级风控（最大亏损/购买力）失效。
    # fail-closed：超过"盲飞上限"的单直接 BLOCK；之下仅 WARN。
    if broker_.is_connected() and nav is None:
        notional = entry * quantity
        if notional > panel.account_unavailable_max_notional_usd:
            extra = RiskFinding(
                "account_unavailable_block",
                BLOCK,
                f"账户数据不可用且金额 ${notional} 超过盲飞上限 "
                f"${panel.account_unavailable_max_notional_usd}，已拒单（账户级风控失效时不放大单）",
            )
        else:
            extra = RiskFinding(
                "account_unavailable",
                WARN,
                "账户数据不可用：单笔最大亏损/购买力检查未生效，仅 notional 上限仍有效，请谨慎下单",
            )
        findings = [extra, *findings]
    return findings


def _risk_block_response(findings: Any) -> JSONResponse | None:
    """有 BLOCK 级风控则返回 400 响应，否则 None。"""
    blocking = risk_blocks(findings)
    if blocking:
        return JSONResponse(
            {
                "rejected": True,
                "reason": "风控拒绝: " + "；".join(f.message for f in blocking),
                "risk": [finding_to_dict(f) for f in findings],
            },
            status_code=400,
        )
    return None


def _plan_from_request(body: LimitOrderRequest, panel: PanelConfig) -> Any:
    """把请求解析成 OrderPlan（参数非法或风控拒绝抛 OrderRejected）。"""
    try:
        entry = Decimal(body.entry_limit)
        take_profit = Target(TargetKind(body.tp_kind), Decimal(body.tp_value))
        stop_loss = Target(TargetKind(body.sl_kind), Decimal(body.sl_value))
        side = Side(body.side)
    except (InvalidOperation, ValueError) as exc:
        raise OrderRejected(f"参数非法: {exc}") from exc
    return plan_limit_bracket(
        symbol=body.symbol,
        quantity=body.quantity,
        entry_limit=entry,
        take_profit=take_profit,
        stop_loss=stop_loss,
        panel=panel,
        side=side,
    )


class ConnectionManager:
    """WebSocket 连接池 + 广播。"""

    def __init__(self) -> None:
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.active.discard(ws)

    async def broadcast(self, data: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.active):
            try:
                await ws.send_json(data)
            except Exception:  # noqa: BLE001 — 单个连接发送失败就剔除
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


async def _broadcast_loop(broker: BrokerLike, manager: ConnectionManager) -> None:
    while True:
        await asyncio.sleep(BROADCAST_INTERVAL_S)
        if not manager.active:
            continue
        try:
            state = broker.snapshot()
        except Exception:  # noqa: BLE001
            log.exception("snapshot failed in broadcast loop")
            continue
        await manager.broadcast(state)


def create_app(broker: BrokerLike | None = None, *, auto_connect: bool = True) -> FastAPI:
    """构建 app。测试可注入 fake broker 并关掉 auto_connect。"""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        panel = load_panel_config()
        if broker is not None:
            b: BrokerLike = broker
        else:
            eff_env, env_warning = resolve_environment()  # live 二重互锁
            if eff_env.value == "live":
                log.warning("⚠️ 启动于 LIVE 实盘环境（端口 4001）")
            b = BrokerManager(load_settings(eff_env), panel, env_warning=env_warning)
        app.state.broker = b
        app.state.panel = panel
        if auto_connect:
            with contextlib.suppress(Exception):
                await b.connect()
        manager = ConnectionManager()
        app.state.ws_manager = manager
        task = asyncio.create_task(_broadcast_loop(b, manager))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            with contextlib.suppress(Exception):
                await b.disconnect()

    app = FastAPI(title="WebWatch 下单面板", lifespan=lifespan)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_WEB_DIR / "index.html")

    @app.get("/api/state")
    async def state(request: Request) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        return JSONResponse(broker_.snapshot())

    @app.post("/api/watch")
    async def add_watch(request: Request, body: WatchRequest) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        await broker_.watch(body.symbol)
        return JSONResponse(broker_.snapshot())

    @app.delete("/api/watch/{symbol}")
    async def remove_watch(request: Request, symbol: str) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        broker_.unwatch(symbol)
        return JSONResponse(broker_.snapshot())

    @app.post("/api/market_data_type")
    async def set_market_data_type(request: Request, body: MarketDataTypeRequest) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        try:
            mdt = await broker_.set_market_data_type(body.market_data_type)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "market_data_type": mdt})

    @app.post("/api/reconnect")
    async def reconnect(request: Request) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        await broker_.connect()
        return JSONResponse({"connected": broker_.is_connected()})

    @app.post("/api/order/preview")
    async def order_preview(request: Request, body: LimitOrderRequest) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        panel: PanelConfig = request.app.state.panel
        try:
            plan = _plan_from_request(body, panel)
        except OrderRejected as exc:
            return JSONResponse({"rejected": True, "reason": exc.reason})
        findings = _risk_for(
            broker_, plan.entry_limit, plan.bracket.stop_loss, plan.quantity, plan.side, panel
        )
        return JSONResponse(
            {
                "rejected": False,
                "plan": plan_to_dict(plan),
                "risk": [finding_to_dict(f) for f in findings],
            }
        )

    @app.post("/api/order/limit")
    async def order_limit(request: Request, body: LimitOrderRequest) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        panel: PanelConfig = request.app.state.panel
        try:
            plan = _plan_from_request(body, panel)
        except OrderRejected as exc:
            return JSONResponse({"rejected": True, "reason": exc.reason}, status_code=400)
        findings = _risk_for(
            broker_, plan.entry_limit, plan.bracket.stop_loss, plan.quantity, plan.side, panel
        )
        blocked = _risk_block_response(findings)
        if blocked is not None:
            return blocked
        try:
            placement = await broker_.place_limit_bracket(
                symbol=plan.symbol,
                side=plan.side,
                quantity=plan.quantity,
                entry_limit=plan.entry_limit,
                take_profit=plan.bracket.take_profit,
                stop_loss=plan.bracket.stop_loss,
            )
        except Exception as exc:  # noqa: BLE001 — 下单失败回报给前端，不崩
            return JSONResponse(
                {"rejected": True, "reason": f"下单失败: {exc}"}, status_code=502
            )
        return JSONResponse(
            {
                "rejected": False,
                "plan": plan_to_dict(plan),
                "placement": placement,
                "risk": [finding_to_dict(f) for f in findings],
            }
        )

    @app.post("/api/order/market/preview")
    async def market_preview(request: Request, body: MarketOrderRequest) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        panel: PanelConfig = request.app.state.panel
        try:
            plan = _market_plan_from_request(body, panel)
        except OrderRejected as exc:
            return JSONResponse({"rejected": True, "reason": exc.reason})
        findings = _risk_for(
            broker_, plan.ref_price, plan.preview_bracket.stop_loss, plan.quantity, plan.side, panel
        )
        return JSONResponse(
            {
                "rejected": False,
                "plan": market_plan_to_dict(plan),
                "risk": [finding_to_dict(f) for f in findings],
            }
        )

    @app.post("/api/order/market")
    async def order_market(request: Request, body: MarketOrderRequest) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        panel: PanelConfig = request.app.state.panel
        try:
            plan = _market_plan_from_request(body, panel)
        except OrderRejected as exc:
            return JSONResponse({"rejected": True, "reason": exc.reason}, status_code=400)
        findings = _risk_for(
            broker_, plan.ref_price, plan.preview_bracket.stop_loss, plan.quantity, plan.side, panel
        )
        blocked = _risk_block_response(findings)
        if blocked is not None:
            return blocked
        try:
            placement = await broker_.place_market_with_protection(
                symbol=plan.symbol,
                side=plan.side,
                quantity=plan.quantity,
                take_profit=plan.take_profit,
                stop_loss=plan.stop_loss,
            )
        except ProtectionFailed as exc:
            # 红线：已成交但保护单失败，已紧急平仓 —— 醒目告知用户
            return JSONResponse(
                {
                    "rejected": False,
                    "protection_failed": True,
                    "reason": exc.reason,
                    "filled": exc.filled,
                    "fill_price": str(exc.fill_price),
                    "flatten": exc.flatten,
                }
            )
        except EntryUncertain as exc:
            # 入场超时、状态未知（可能已成交）。绝不当作"已拒绝"，醒目提示查持仓。
            return JSONResponse(
                {
                    "rejected": False,
                    "entry_uncertain": True,
                    "reason": str(exc),
                }
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"rejected": True, "reason": f"下单失败: {exc}"}, status_code=502
            )
        return JSONResponse(
            {
                "rejected": False,
                "plan": market_plan_to_dict(plan),
                "placement": placement,
                "risk": [finding_to_dict(f) for f in findings],
            }
        )

    @app.post("/api/cancel_all")
    async def cancel_all(request: Request) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        try:
            result = await broker_.cancel_all_orders()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "reason": str(exc)}, status_code=502)
        return JSONResponse({"ok": True, **result})

    @app.post("/api/flatten_all")
    async def flatten_all(request: Request) -> JSONResponse:
        broker_: BrokerLike = request.app.state.broker
        try:
            result = await broker_.flatten_all()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "reason": str(exc)}, status_code=502)
        return JSONResponse({"ok": True, **result})

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        manager: ConnectionManager = websocket.app.state.ws_manager
        broker_: BrokerLike = websocket.app.state.broker
        await manager.connect(websocket)
        try:
            await websocket.send_json(broker_.snapshot())
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(websocket)
        except Exception:  # noqa: BLE001
            manager.disconnect(websocket)

    return app


app = create_app()
