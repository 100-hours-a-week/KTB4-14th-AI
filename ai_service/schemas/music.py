"""음악 추천의 요청·응답·곡 정보 스키마."""
from __future__ import annotations
from typing import Literal
from pydantic import Field, HttpUrl
from ai_service.schemas.common import Duration, NonEmpty, Region, StrictModel


class MusicRecommendation(StrictModel):
    title: NonEmpty
    artist: NonEmpty
    youtube_url: HttpUrl = Field(description="YouTube 검색 결과와 공개 메타데이터에서 확인한 단일 영상의 watch URL")


class MusicPreference(StrictModel):
    themes: list[NonEmpty] = Field(min_length=1, max_length=10)


class MusicRequest(StrictModel):
    travel_plan_id: int = Field(gt=0)
    region: Region
    duration: Duration
    preference: MusicPreference


class SelectedMusic(MusicRecommendation):
    travel_plan_id: int


class MusicResponse(StrictModel):
    message: Literal["ai_music_recommended"] = "ai_music_recommended"
    data: SelectedMusic


class MusicSuggestion(StrictModel):
    """모델은 곡명·가수만 제안하고 영상 URL은 YouTube 조회로 결정한다."""
    title: NonEmpty
    artist: NonEmpty
