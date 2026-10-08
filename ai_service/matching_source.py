"""DB 연동 전 서버에서 제공하는 가상 요청자와 동행자 후보."""

import json
from pathlib import Path

from ai_service.matching import MatchingRecommendationRequest, MatchingRequestCreate


class MockMatchingCandidateSource:
    def __init__(self):
        path = Path(__file__).with_name("data") / "matching_request.mock.json"
        self.context = MatchingRecommendationRequest.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )

    async def recommendation_request(self, preferences: MatchingRequestCreate) -> MatchingRecommendationRequest:
        # 서비스 토큰을 실제 로그인 사용자 ID로 해석하지 않는다.
        # 현재 요청자도 목데이터이며, 백엔드 연동 시 이 공급자를 교체한다.
        return MatchingRecommendationRequest(
            **preferences.model_dump(),
            requester_id=self.context.requester_id,
            candidates=[candidate.model_copy(deep=True) for candidate in self.context.candidates],
            top_k=self.context.top_k,
        )
