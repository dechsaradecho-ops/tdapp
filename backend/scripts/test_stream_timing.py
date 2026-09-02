"""Measure time-to-first-chunk vs chunk cadence on the production stream."""
import asyncio
import time

import httpx

URL = "https://tdapp-api.onrender.com/api/chat/stream"


async def main() -> None:
    t0 = time.time()
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as c:
        async with c.stream(
            "POST", URL, json={"messages": [{"role": "user", "content": "สวัสดี ทดสอบ"}]}
        ) as r:
            print(f"[{time.time()-t0:6.1f}s] status={r.status_code} headers arrived")
            n = 0
            async for chunk in r.aiter_bytes():
                n += 1
                text = chunk.decode("utf-8", errors="replace")[:48].replace("\n", "\\n")
                print(f"[{time.time()-t0:6.1f}s] chunk#{n} ({len(chunk)}B): {text!r}")
            print(f"[{time.time()-t0:6.1f}s] done, {n} chunks")


asyncio.run(main())
