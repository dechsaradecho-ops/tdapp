"""Probe: does the Yahoo chart meta expose bid/ask per symbol?

If yes → paper fills can use the REAL spread from the API first and fall
back to the configured paper_spread only when bid/ask are missing (FX pairs
often omit them).
"""
import json

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SYMBOLS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "GC=F"]
KEYS = ("regularMarketPrice", "bid", "ask", "bidSize", "askSize",
        "regularMarketDayHigh", "regularMarketDayLow", "currency",
        "exchangeName", "fullExchangeName")


def main() -> int:
    with httpx.Client(follow_redirects=True, headers={"User-Agent": UA}) as c:
        for sym in SYMBOLS:
            try:
                r = c.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                          params={"interval": "1m", "range": "1d"}, timeout=10.0)
                meta = ((r.json().get("chart") or {}).get("result") or [{}])[0].get("meta", {})
                picked = {k: meta.get(k) for k in KEYS if k in meta}
                extra_bid_ask = {k: v for k, v in meta.items()
                                 if "bid" in k.lower() or "ask" in k.lower()}
                print(f"--- {sym} (status {r.status_code})")
                print(json.dumps(picked, indent=1))
                if extra_bid_ask:
                    print("bid/ask-ish keys:", json.dumps(extra_bid_ask))
                else:
                    print("bid/ask-ish keys: NONE")
            except Exception as exc:
                print(f"--- {sym}: ERROR {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
