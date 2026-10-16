"""여행 장소 선택·음악 단계의 모델 호출을 조율하는 플래너."""
from __future__ import annotations

import json

import httpx
from pydantic import ValidationError

from ai_service.clients.openai import OpenAIClient
from ai_service.prompts.travel import build_system_prompt, ITINERARY_RETRY_PROMPT
from ai_service.prompts.music import MUSIC_SYSTEM_PROMPT, MUSIC_RETRY_PROMPT
from ai_service.core.config import Settings
from ai_service.clients.youtube import YouTubeMusic
from ai_service.core.errors import (
    GenerationFailed,
    InvalidModelOutput,
    MusicRecommendationFailed,
    ServiceUnavailable,
)
from ai_service.schemas.travel import (
    ItineraryRequest, ModelSelection
)
from ai_service.schemas.music import (
    MusicRecommendation, MusicSuggestion, MusicRequest
)


class OpenAIPlanner:
    """모델에 후보 장소 선택과 음악 추천을 요청하고 결과 형식을 검증한다."""
    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self.client = client
        self.settings = settings
        self.openai = OpenAIClient(client, settings)
        self.youtube_music = YouTubeMusic(client)

    async def generate(
        self, context: dict, feedback: str | None = None, *, places_only: bool = False
    ) -> ModelSelection:
        """후보 ID와 날짜로 JSON Schema를 좁혀 장소 선택을 요청한다."""
        if not self.settings.openai_api_key:
            raise ServiceUnavailable(reason="openai_api_key_missing")
        prompt = build_system_prompt(context, places_only=places_only)
        messages = [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(context, ensure_ascii=False),
            },
        ]
        if feedback:
            messages.append(
                {
                    "role": "user",
                    "content": ITINERARY_RETRY_PROMPT
                    + feedback,
                }
            )
        schema = ModelSelection.model_json_schema()
        dates = [day["date"] for day in context["day_windows"]]
        schema["properties"]["days"].update(minItems=len(dates), maxItems=len(dates))
        schema["$defs"]["SelectionDay"]["properties"]["date"]["enum"] = dates
        schema["$defs"]["SelectionItem"]["properties"]["provider_place_id"]["enum"] = [
            place["provider_place_id"] for place in context["candidates"]
        ]
        raw = await self._complete(messages, schema, "audigo_itinerary")
        try:
            return ModelSelection.model_validate_json(raw)
        except ValidationError as exc:
            raise InvalidModelOutput(
                "model output must match itinerary schema"
            ) from exc

    async def recommend_music(
        self, request: ItineraryRequest | MusicRequest
    ) -> MusicRecommendation:
        """추천 곡의 실제 YouTube 영상을 확인하고 실패 시 다시 추천받는다."""
        context = {
            "region": request.region.model_dump(),
            "duration": request.duration.model_dump(mode="json"),
            "preference": request.preference.model_dump(),
        }
        if isinstance(request, ItineraryRequest):
            context["companion_type"] = request.companion_type
        schema = MusicSuggestion.model_json_schema()
        messages = [
            {
                "role": "system",
                "content": MUSIC_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": json.dumps(
                    context,
                    ensure_ascii=False,
                ),
            },
        ]
        for _ in range(2):
            try:
                raw = await self._complete(messages, schema, "audigo_music")
                choice = MusicSuggestion.model_validate_json(raw)
                selected = await self.youtube_music.find_video(choice)
                if selected is not None:
                    return selected
                messages.append({"role": "assistant", "content": choice.model_dump_json()})
            except GenerationFailed as exc:
                raise MusicRecommendationFailed(reason="music_model_refused") from exc
            except (InvalidModelOutput, ValidationError):
                pass
            messages.append(
                {
                    "role": "user",
                    "content": MUSIC_RETRY_PROMPT,
                }
            )
        raise MusicRecommendationFailed(reason="music_video_not_verified")

    async def _complete(self, messages: list[dict], schema: dict, name: str) -> str:
        return await self.openai.complete(messages, schema, name)
