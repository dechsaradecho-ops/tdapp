"""AI endpoints.

POST /api/ai/explain → narrative for a trade/signal (uses the configured model)
POST /api/ai/test    → one-line round trip for the Settings page "ทดสอบ" button
                       (optionally with the model/base URL typed in the form,
                       so a new gateway can be verified BEFORE saving it)
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.core.ai_config import (describe_ai_config, get_ai_overrides,
                                set_ai_overrides)
from app.integrations.ai_provider import build_context_block, get_ai_provider

router = APIRouter()


class ExplainRequest(BaseModel):
    asset: str
    direction: str
    entry: float
    stop_loss: float
    take_profit: float
    opportunity_score: float
    regime: str = "bull_trend"


@router.post("/explain")
async def explain(payload: ExplainRequest, request: Request) -> dict:
    provider = get_ai_provider()
    context = build_context_block(payload.model_dump())
    reply = await provider.chat([{
        "role": "user",
        "content": (
            f"Explain this {payload.direction} {payload.asset} proposal with 3-5 numbered reasons, "
            f"referencing trend/momentum/volatility/news. End with a one-line risk caveat.\n{context}"
        ),
    }])
    return {"explanation": reply}


class AITestRequest(BaseModel):
    """Optional one-off override for the Settings page test button.

    The Settings page sends the values CURRENTLY TYPED IN THE FORM (not yet
    saved), so a user can verify a new model/URL before committing it. Leaving
    them out tests whatever the runtime is already using.
    """

    model: str | None = None
    base_url: str | None = None


@router.post("/test")
async def test_ai(payload: AITestRequest | None = None) -> dict:
    """POST /api/ai/test — one-line round trip so the Settings page can prove
    the configured model/base URL really answers.

    Returns 200 even when the provider fails; `ok: false` + `error` carry the
    failure so the UI can show it inline instead of a 500 toast. The reply text
    itself is echoed back (truncated) because that is the fastest way to spot
    "wrong model" (e.g. a gateway answering with a different model name).
    """
    payload = payload or AITestRequest()
    saved = get_ai_overrides()
    model = (payload.model or "").strip()
    base_url = (payload.base_url or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        return {
            "ok": False,
            "provider": "n/a",
            "error": "base URL ต้องขึ้นต้นด้วย http:// หรือ https://",
            "info": describe_ai_config(),
        }
    if model or base_url:
        # Temporarily apply the form values so the provider built below uses
        # them; restored in `finally` so a failed test never leaves the running
        # process on an unsaved config.
        set_ai_overrides(url=base_url or saved["url"], model=model or saved["model"])
    try:
        provider = get_ai_provider()
        reply = await provider.chat([{
            "role": "user",
            "content": "ตอบสั้น ๆ ว่า 'พร้อมใช้งาน' แล้วบอกชื่อโมเดลที่คุณใช้",
        }], temperature=0.1)
        ok = not reply.lstrip().startswith("[AI ERROR]")
        out: dict = {
            "ok": ok,
            "provider": provider.name,
            "model": getattr(provider, "model", "") or "-",
            "base_url": getattr(provider, "base_url", "") or "-",
            "reply": reply[:500],
            "info": describe_ai_config(),
        }
        if not ok:
            out["error"] = reply[:500]
        return out
    except Exception as exc:                      # never 500 the test button
        return {"ok": False, "provider": "error",
                "error": f"{exc.__class__.__name__}: {exc}",
                "info": describe_ai_config()}
    finally:
        if model or base_url:
            set_ai_overrides(url=saved["url"], model=saved["model"])
