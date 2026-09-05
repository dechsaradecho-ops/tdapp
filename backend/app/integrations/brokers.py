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

    async def all_positions(self) -> list[Position]:
        """Every open position regardless of user (position-guard loop)."""
        return []

    async def mark_price(self, ticket: str) -> float:
        """Current price for an open ticket (0.0 when unavailable)."""
        return 0.0

    async def modify_stop_loss(self, ticket: str, stop_loss: float) -> OrderResult:
        """Move the SL of an open position (breakeven / trailing stop).

        Default implementation: unsupported (real adapters override).
        """
        return OrderResult(ok=False, message="modify_stop_loss not supported")

    async def partial_close(self, ticket: str, volume: float) -> OrderResult:
        """Close part of an open position (TP1 partial take-profit).

        Default implementation: unsupported (real adapters override).
        """
        return OrderResult(ok=False, message="partial_close not supported")


class PaperBroker(Broker):
    """Simulated execution for development/demo."""

    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}
        self.closed_trades: list[dict] = []  # journal for paper-trading analytics
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
        if pos is None:
            return OrderResult(ok=False, message=f"unknown ticket {ticket}")
        pos.current_price = pos.current_price or pos.entry_price
        pnl = self._approx_pnl(pos)
        self.closed_trades.append({
            "ticket": ticket, "asset": pos.asset, "direction": pos.direction,
            "volume": pos.volume, "entry_price": pos.entry_price,
            "exit_price": pos.current_price, "pnl": round(pnl, 2),
        })
        return OrderResult(ok=True, message=f"closed {ticket} pnl={pnl:.2f}")

    async def positions(self, user_id: str) -> list[Position]:
        return [p for p in self._positions.values() if p.user_id == user_id]

    async def all_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def mark_price(self, ticket: str) -> float:
        pos = self._positions.get(ticket)
        return pos.current_price if pos else 0.0

    async def quote(self, asset: str) -> float:
        return self._prices.get(asset.upper(), 0.0)

    async def modify_stop_loss(self, ticket: str, stop_loss: float) -> OrderResult:
        """Move the SL of an open paper position (breakeven / trailing)."""
        pos = self._positions.get(ticket)
        if pos is None:
            return OrderResult(ok=False, message=f"unknown ticket {ticket}")
        pos.stop_loss = float(stop_loss)
        return OrderResult(ok=True, broker_order_id=ticket,
                           message=f"SL moved to {stop_loss:g}")

    async def partial_close(self, ticket: str, volume: float) -> OrderResult:
        """Close part of a paper position; the remainder keeps its ticket.

        The closed slice is journaled into closed_trades with a suffixed
        ticket (PAPER-xxxxxx#1) so analytics can tell partial exits apart.
        """
        pos = self._positions.get(ticket)
        if pos is None:
            return OrderResult(ok=False, message=f"unknown ticket {ticket}")
        volume = round(float(volume), 2)
        if volume <= 0 or volume >= pos.volume:
            return OrderResult(ok=False,
                               message=f"invalid partial volume {volume:g} "
                                       f"(position {pos.volume:g})")
        closed_vol = volume
        pos.volume = round(pos.volume - closed_vol, 2)
        # PnL of the closed slice only: sign × (price − entry) × slice × contract
        sign = 1 if pos.direction == "BUY" else -1
        asset = str(getattr(pos, "asset", "") or "").upper()
        contract = PaperBroker.CONTRACT_SIZES.get(asset, 100_000.0)
        pnl = sign * (pos.current_price - pos.entry_price) * closed_vol * contract
        self.closed_trades.append({
            "ticket": f"{ticket}#partial", "asset": pos.asset,
            "direction": pos.direction, "volume": closed_vol,
            "entry_price": pos.entry_price, "exit_price": pos.current_price,
            "pnl": round(pnl, 2),
        })
        return OrderResult(ok=True, broker_order_id=ticket,
                           message=f"closed {closed_vol:g} lots, "
                                   f"{pos.volume:g} lots remain")

    async def set_quote(self, asset: str, price: float) -> None:
        """Feed a live price into the paper book (used by tests + position guard)."""
        self._prices[asset.upper()] = price

    async def refresh_prices(self) -> None:
        """Tick paper prices with a small random walk so SL/TP can trigger.

        Live feeds replace this when a real broker adapter lands; kept here so
        the paper book behaves like a market instead of frozen prices.
        """
        import random
        for asset in self._prices:
            self._prices[asset] *= 1 + random.uniform(-0.0005, 0.0005)
        for pos in self._positions.values():
            if asset := pos.asset.upper():
                if asset in self._prices:
                    pos.current_price = self._prices[asset]

    # Contract multiplier per asset: XAUUSD trades 100 oz per standard lot;
    # FX pairs default to 100,000 units. PnL is reported in dollars — without
    # this, a 0.01-lot FX position with a 100-pip move showed 0.10 instead
    # of 100.00 (monitor page PnL stuck near 0.00).
    CONTRACT_SIZES = {"XAUUSD": 100.0}

    @staticmethod
    def _approx_pnl(pos: Position) -> float:
        if pos.current_price == 0:
            return 0.0
        sign = 1 if pos.direction == "BUY" else -1
        asset = str(getattr(pos, "asset", "") or "").upper()
        contract = PaperBroker.CONTRACT_SIZES.get(asset, 100_000.0)
        return sign * (pos.current_price - pos.entry_price) * pos.volume * contract
