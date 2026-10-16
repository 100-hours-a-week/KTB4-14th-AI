"""상태 확인과 API 문서 화면."""
from fastapi import APIRouter
from ai_service.api.swagger_ui import korean_swagger_html

router = APIRouter()

@router.get("/docs", include_in_schema=False)
async def swagger_docs():
    return korean_swagger_html(openapi_url="/openapi.json", title="Audigo AI API — API 문서")

@router.get("/health", tags=["system"], summary="서버 상태 확인", response_description="서버 정상")
async def health():
    return {"status": "ok", "mode": "live"}
