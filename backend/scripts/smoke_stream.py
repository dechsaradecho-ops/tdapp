"""Smoke-test /api/chat/stream in-process (ASGI, no server needed)."""
import asyncio
import sys

sys.path.insert(0, ".")

import httpx  # noqa: E402

from app.main import app  # noqa: E402
from app.services.database import Database  # noqa: E402


async def main() -> None:
    # ASGITransport skips lifespan — populate state manually (same as tests)
    app.state.db = Database()
    app.state.scheduler = None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        chunks: list[str] = []
        async with client.stream(
            "POST", "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "ตอบสั้น ๆ: ตลาดวันนี้เป็นไง"}]},
        ) as resp:
            print("status:", resp.status_code)
            async for piece in resp.aiter_text():
                chunks.append(piece)
                print("CHUNK:", repr(piece[:60]))
        full = "".join(chunks)
        print("total_chunks:", len(chunks), "len:", len(full))
        assert len(chunks) >= 1 and full.strip(), "empty stream"
        print("OK — streaming works")


asyncio.run(main())
