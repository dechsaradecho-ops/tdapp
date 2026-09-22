"""Probe: run the real monitor_snapshot path for a CHF cross and print uPnL."""
import asyncio
import sys

sys.path.insert(0, ".")

from app.services import execution as ex  # noqa: E402
from app.integrations import quotes as q  # noqa: E402


async def main() -> None:
    # Fake the spot feed exactly like prod: CHFUSD daily, USDCHF/EURUSD spot
    async def fake_spot(assets):
        return ({"CHFUSD": 1.2174, "USDCHF": 0.8194, "EURUSD": 1.1465,
                 "AUDUSD": 0.7115, "CADUSD": 0.7131,
                 "EURCHF": 0.939, "AUDCHF": 0.5826, "CADCHF": 0.5836}, {})

    def src(a):
        return "daily" if a in ("CHFUSD", "CADUSD") else "spot"

    q.fetch_spot_prices = fake_spot
    q.spot_source = src

    rates = await ex.fetch_pnl_rates(
        ["EURCHF", "AUDCHF", "CADCHF"],
        seed={"EURCHF": 0.939, "AUDCHF": 0.5826, "CADCHF": 0.5836})
    print("rates:", {k: round(v, 5) for k, v in sorted(rates.items())})
    for a, entry, mark in [("EURCHF", 0.9403, 0.9387),
                           ("AUDCHF", 0.58413, 0.5825),
                           ("CADCHF", 0.58485, 0.5834)]:
        from types import SimpleNamespace
        u = ex.PaperBrokerPnl.compute(SimpleNamespace(
            direction="BUY", current_price=mark, entry_price=entry,
            volume=0.03, asset=a), rates=rates)
        print(f"  {a}: rate={ex.pnl_conversion_rate(a, rates)} uPnL={u}")


asyncio.run(main())
