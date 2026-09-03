"""``Venue`` implementation for Binance USDⓈ-M (demo/testnet) on top of :class:`BinanceRestClient`."""

from __future__ import annotations

import time
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from beidou_exchange.binance_usdm.rest_client import BinanceRestClient
from beidou_exchange.rules import format_decimal
from beidou_shared.binance_rules import parse_exchange_info
from beidou_shared.types import AccountState, InstrumentRules, OrderAck, OrderRequest, Position, Side, VenueError

RULES_TTL_SECONDS = 3600.0
ORDER_NOT_FOUND = -2013


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_order_ack(payload: dict[str, Any]) -> OrderAck:
    executed = Decimal(str(payload.get("executedQty", "0") or "0"))
    return OrderAck(
        symbol=str(payload.get("symbol", "")),
        client_order_id=str(payload.get("clientOrderId", "")),
        order_id=str(payload.get("orderId", "")),
        side=Side(str(payload.get("side", "BUY"))),
        status=str(payload.get("status", "")),
        executed_qty=executed,
        avg_price=_float(payload.get("avgPrice")),
        reduce_only=bool(payload.get("reduceOnly", False)),
        raw=dict(payload),
    )


def parse_position(payload: dict[str, Any]) -> Position | None:
    qty = _float(payload.get("positionAmt"))
    if qty == 0.0:
        return None
    return Position(
        symbol=str(payload.get("symbol", "")),
        qty=qty,
        entry_price=_float(payload.get("entryPrice")),
        mark_price=_float(payload.get("markPrice")),
        unrealized_pnl=_float(payload.get("unRealizedProfit")),
        leverage=int(_float(payload.get("leverage"), 0.0)),
    )


class BinanceUsdmVenue:
    def __init__(self, client: BinanceRestClient) -> None:
        self._client = client
        self._rules: dict[str, InstrumentRules] = {}
        self._rules_at = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def sync_clock(self) -> int:
        return await self._client.sync_clock()

    async def rules(self) -> dict[str, InstrumentRules]:
        if not self._rules or time.monotonic() - self._rules_at > RULES_TTL_SECONDS:
            payload = await self._client.get("/fapi/v1/exchangeInfo")
            self._rules = parse_exchange_info(payload)
            self._rules_at = time.monotonic()
        return dict(self._rules)

    async def hedge_mode(self) -> bool:
        payload = await self._client.get("/fapi/v1/positionSide/dual", signed=True)
        return bool(payload.get("dualSidePosition", False))

    async def account(self) -> AccountState:
        payload = await self._client.get("/fapi/v2/account", signed=True)
        positions: dict[str, Position] = {}
        for row in payload.get("positions", []):
            position = parse_position(row)
            if position is not None:
                positions[position.symbol] = position
        return AccountState(
            wallet_balance=_float(payload.get("totalWalletBalance")),
            available_balance=_float(payload.get("availableBalance")),
            equity=_float(payload.get("totalMarginBalance")),
            positions=positions,
            hedge_mode=False,
            can_trade=bool(payload.get("canTrade", True)),
        )

    async def positions(self) -> dict[str, Position]:
        payload = await self._client.get("/fapi/v2/positionRisk", signed=True)
        result: dict[str, Position] = {}
        for row in payload:
            position = parse_position(row)
            if position is not None:
                result[position.symbol] = position
        return result

    async def mark_prices(self, symbols: Sequence[str]) -> dict[str, float]:
        wanted = set(symbols)
        payload = await self._client.get("/fapi/v1/premiumIndex")
        return {str(row["symbol"]): _float(row.get("markPrice")) for row in payload if str(row.get("symbol")) in wanted}

    async def set_leverage(self, symbol: str, leverage: int) -> int:
        payload = await self._client.post("/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)})
        return int(_float(payload.get("leverage"), float(leverage)))

    async def place_order(self, request: OrderRequest) -> OrderAck:
        params: dict[str, Any] = {
            "symbol": request.symbol,
            "side": request.side.value,
            "type": request.order_type,
            "quantity": format_decimal(request.quantity),
            "newClientOrderId": request.client_order_id,
            "newOrderRespType": "RESULT",
        }
        if request.reduce_only:
            params["reduceOnly"] = "true"
        payload = await self._client.post("/fapi/v1/order", params)
        return parse_order_ack(payload)

    async def query_order(self, symbol: str, client_order_id: str) -> OrderAck | None:
        try:
            payload = await self._client.get(
                "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_order_id}, signed=True
            )
        except VenueError as exc:
            if exc.code == ORDER_NOT_FOUND:
                return None
            raise
        return parse_order_ack(payload)

    async def cancel_order(self, symbol: str, client_order_id: str) -> OrderAck | None:
        try:
            payload = await self._client.delete(
                "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_order_id}
            )
        except VenueError as exc:
            if exc.code == ORDER_NOT_FOUND:
                return None
            raise
        return parse_order_ack(payload)

    async def open_orders(self, symbol: str | None = None) -> list[OrderAck]:
        params = {"symbol": symbol} if symbol else {}
        payload = await self._client.get("/fapi/v1/openOrders", params, signed=True)
        return [parse_order_ack(row) for row in payload]

    async def income(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = int(start_ms)
        for _ in range(50):
            page = await self._client.get(
                "/fapi/v1/income", {"startTime": cursor, "endTime": int(end_ms), "limit": 1000}, signed=True
            )
            if not page:
                break
            rows.extend(page)
            last = int(page[-1].get("time", cursor))
            if len(page) < 1000 or last <= cursor:
                break
            cursor = last + 1
        return rows
