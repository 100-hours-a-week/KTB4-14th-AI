"""체크리스트 문구를 생성·검증하고 초기 상태와 정렬 순서를 부여한다."""
import json

from pydantic import ValidationError

from ai_service.clients.openai import OpenAIClient
from ai_service.core.errors import ChecklistGenerationFailed, GenerationFailed, InvalidModelOutput
from ai_service.prompts.checklist import CHECKLIST_RETRY_PROMPT, CHECKLIST_SYSTEM_PROMPT
from ai_service.schemas.checklist import (
    ChecklistItem, ChecklistRequest, ChecklistResponse, GeneratedChecklist,
)


class ChecklistGenerator:
    def __init__(self, openai: OpenAIClient):
        self.openai = openai

    async def generate(self, request: ChecklistRequest) -> ChecklistResponse:
        messages = [
            {'role': 'system', 'content': CHECKLIST_SYSTEM_PROMPT},
            {'role': 'user', 'content': json.dumps(
                request.model_dump(mode='json', exclude={'travel_plan_id'}), ensure_ascii=False)},
        ]
        schema = GeneratedChecklist.model_json_schema()
        for attempt in range(2):
            try:
                raw = await self.openai.complete(messages, schema, 'audigo_checklist')
                generated = GeneratedChecklist.model_validate_json(raw)
            except GenerationFailed as exc:
                raise ChecklistGenerationFailed(reason='checklist_model_refused') from exc
            except (InvalidModelOutput, ValidationError):
                if attempt == 0:
                    messages.append({'role': 'user', 'content': CHECKLIST_RETRY_PROMPT})
                continue
            return ChecklistResponse(
                travel_plan_id=request.travel_plan_id,
                title=generated.title,
                items=[ChecklistItem(content=item.content, sort_order=index)
                       for index, item in enumerate(generated.items, start=1)],
            )
        raise ChecklistGenerationFailed(reason='checklist_output_invalid')
