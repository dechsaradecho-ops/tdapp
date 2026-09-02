"""Broker adapter pattern — MetaTrader5 / OANDA / Interactive Brokers.

The default PaperBroker executes against an in-memory simulated book so the whole
platform is runnable end-to-end without live accounts. Real adapters implement the
same Broker interface.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class OrderRequest:
    user_id: str
    asset: str
    direction: str          # BUY | SELL
    volume: float
    entry_price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]


@dataclass
class OrderResult:
    ok: bool
    broker_order_id: Optional[str] = None
    message: str = ""


@dataclass
class Position:
    ticket: str
    user_id: str
    asset: str
    direction: str
    volume: float
    entry_price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    current_price: float = 0.0


class Broker(ABC):
    """Common interface for all trading integrations."""

    @abstractmethod
    async def connect(self) -> bool: ...

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def close_position(self, ticket: str) -> OrderResult: ...

    @abstractmethod
    async def positions(self, user_id: str) -> list[Position]: ...

    @abstractmethod
    async def quote(self, asset: str) -> float: ...


class PaperBroker(Broker):
    """Simulated execution for development/demo."""

    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}
        self._seq = 0
        self._prices: dict[str, float] = {
            "EURUSD": 1.08500, "GBPUSD": 1.26500, "USDJPY": 149.500,
            "AUDUSD": 0.65200, "XAUUSD": 2400.00,
        }

    async def connect(self) -> bool:
        return True

    async def place_order(self, order: OrderRequest) -> OrderResult:
        self._seq += 1
        ticket = f"PAPER-{self._seq:06d}"
        self._positions[ticket] = Position(
            ticket=ticket, user_id=order.user_id, asset=order.asset,
            direction=order.direction, volume=order.volume,
            entry_price=order.entry_price, stop_loss=order.stop_loss,
            take_profit=order.take_profit, current_price=order.entry_price,
        )
        return OrderResult(ok=True, broker_order_id=ticket, message="opened (paper)")

    async def close_position(self, ticket: str) -> OrderResult:
        pos = self._positions.pop(ticket, None)
        return (
            OrderResult(ok=True, message=f"closed {ticket} pnl={self._approx_pnl(pos):.2f}")
            if pos else OrderResult(ok=False, message=f"unknown ticket {ticket}")
        )

    async def positions(self, user_id: str) -> list[Position]:
        return [p for p in self._positions.values() if p.user_id == user_id]

    async def quote(self, asset: str) -> float:
        return self._prices.get(asset.upper(), 0.0)

    @staticmethod
    def _approx_pnl(pos: Position) -> float:
        if pos.current_price == 0:
            return 0.0
        sign = 1 if pos.direction == "BUY" else -1
        return sign * (pos.current_price - pos.entry_price) * pos.volume
