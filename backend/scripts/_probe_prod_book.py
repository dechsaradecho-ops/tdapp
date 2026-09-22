"""Probe: replicate the monitor's exact rate fetch for the prod book."""
import asyncio
import sys

sys.path.insert(0, ".")

from app.services.execution import fetch_pnl_rates, pnl_conversion_rate  # noqa: E402
from app.integrations import quotes as q  # noqa: E402


async def main() -> None:
    assets = ["NZDUSD", "USDJPY", "GBPNZD", "EURCHF", "AUDNZD", "AUDCHF", "CADCHF"]
    seed = {"NZDUSD": 0.5741, "USDJPY": 156.989, "GBPNZD": 2.3281,
            "EURCHF": 0.9387, "AUDNZD": 1.2398, "AUDCHF": 0.5825, "CADCHF": 0.5834}
    rates = await fetch_pnl_rates(assets, seed=seed)
    print("rates keys:", sorted(rates))
    for a in assets:
        print(f"  {a:8s} -> rate={pnl_conversion_rate(a, rates)}")
    # what does the feed say about the legs?
    legs = ["CHFUSD", "USDCHF", "EURUSD", "AUDUSD", "CADUSD", "NZDUSD"]
    prices, fails = await q.fetch_spot_prices(legs)
    print("leg prices:", prices)
    for a in legs:
        print(f"  leg {a:8s} -> source={q.spot_source(a)}")


asyncio.run(main())
