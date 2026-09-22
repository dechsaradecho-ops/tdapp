"""Probe: what if USDCHF is daily on prod? Does EURCHF still resolve?"""
import asyncio
import sys

sys.path.insert(0, ".")

from app.services import execution as ex  # noqa: E402
from app.integrations import quotes as q  # noqa: E402


async def main() -> None:
    # Simulate prod: CHFUSD daily, USDCHF daily, EURUSD spot
    async def fake_spot(assets):
        print("  fetch called with:", sorted(assets))
        return ({"CHFUSD": 1.2174, "USDCHF": 0.8192, "EURUSD": 1.1464,
                 "AUDUSD": 0.7115, "CADUSD": 0.7131}, {})

    def src(a):
        return "daily" if a in ("CHFUSD", "USDCHF", "CADUSD") else "spot"

    q.fetch_spot_prices = fake_spot
    q.spot_source = src

    rates = await ex.fetch_pnl_rates(["EURCHF", "AUDCHF", "CADCHF"],
                                     seed={"EURCHF": 0.9387, "AUDCHF": 0.5825,
                                           "CADCHF": 0.5834})
    print("rates:", sorted(rates))
    for a in ["EURCHF", "AUDCHF", "CADCHF"]:
        print(f"  {a} -> {ex.pnl_conversion_rate(a, rates)}")


asyncio.run(main())
