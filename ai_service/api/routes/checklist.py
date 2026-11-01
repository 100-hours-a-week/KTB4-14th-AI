"""백엔드에서 동기로 호출하는 여행 체크리스트 생성 API."""
import asyncio
from typing import Annotated

from fastapi import APIRouter, Body, Request

from ai_service.api.examples.checklist import CHECKLIST_REQUEST_EXAMPLE, CHECKLIST_RESPONSES
from ai_service.core.errors import ServiceUnavailable
from ai_service.schemas.checklist import ChecklistRequest, ChecklistResponse


def router(settings):
    routes = APIRouter()

    @routes.post(
        '/api/ai/v1/checklists', response_model=ChecklistResponse,
        responses=CHECKLIST_RESPONSES, tags=['V3'], summary='여행 체크리스트 생성',
        description=('여행 지역·도착/출발 일시·인원·동행 유형·선호·상세 일정으로 '
                     '체크리스트를 동기 생성합니다. 생성 모델은 OPENAI_MODEL 설정을 사용하며, '
                     '반환 결과의 DB 저장은 백엔드가 담당합니다.')
    )
    async def generate_checklist(
        body: Annotated[ChecklistRequest, Body(openapi_examples={
            'busan': {'summary': '부산 1박 2일 여행', 'value': CHECKLIST_REQUEST_EXAMPLE},
        })],
        request: Request,
    ):
        try:
            async with asyncio.timeout(settings.generation_timeout_seconds):
                return await request.app.state.checklist_generator.generate(body)
        except TimeoutError as exc:
            raise ServiceUnavailable(reason='checklist_timeout') from exc

    return routes
