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
        description='사용 모델: E5'
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
