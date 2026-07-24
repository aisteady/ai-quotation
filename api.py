"""
对智能客服开放的同步推荐 HTTP API
================================

POST /api/v1/recommend
  Body: { query, extras, slots? }
  缺五要素 → ok=false + missing / clarify_question
  齐全 → ok=true + proposal_text + structured

启动：
  cd models/ai_quotation
  uv run python api.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from config import settings, validate_settings
from service import QuotationService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai_quotation.api")

app = FastAPI(title="AI Quotation Customer API", version="1.0.0")
_svc: QuotationService | None = None


class RecommendRequest(BaseModel):
    query: str = ""
    extras: str = ""
    slots: dict[str, Any] = Field(default_factory=dict)
    session_id: str = ""


def get_svc() -> QuotationService:
    global _svc
    if _svc is None:
        validate_settings(strict=False)
        # API 进程不启后台人审扫描，避免与 Streamlit 双开冲突
        _svc = QuotationService(start_harness_loop=False)
        logger.info("QuotationService ready for customer recommend API")
    return _svc


def _check_auth(authorization: str | None) -> None:
    token = (settings.customer_api_token or "").strip()
    if not token:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Authorization Bearer")
    got = authorization.split(" ", 1)[1].strip()
    if got != token:
        raise HTTPException(status_code=401, detail="Bearer token 无效")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "ai_quotation_customer_api"}


@app.post("/api/v1/recommend")
def recommend(
    body: RecommendRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_auth(authorization)
    svc = get_svc()
    result = svc.recommend_for_customer(
        query=body.query,
        extras=body.extras,
        slots=body.slots or None,
    )
    # 便于客服侧日志关联
    if body.session_id:
        result = {**result, "session_id": body.session_id}
    return result


def main() -> None:
    import uvicorn

    validate_settings(strict=False)
    host = settings.customer_api_host
    port = settings.customer_api_port
    logger.info("listening http://%s:%s/api/v1/recommend", host, port)
    uvicorn.run(
        "api:app",
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
