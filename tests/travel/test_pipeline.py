"""일정 생성 파이프라인의 사전 검사·단계별 실패·오류 로그를 검증한다."""
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

from ai_service.core.errors import GenerationFailed
from ai_service.services.travel.pipeline import generate_plan
from tests.travel.test_day_transport import request


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_impossible_short_trip_fails_before_provider_call(self):
        body = request(extra="")
        body.duration.departure_datetime = body.duration.arrival_datetime + timedelta(minutes=10)
        places, planner = Mock(), Mock()
        with self.assertRaises(GenerationFailed), self.assertLogs("uvicorn.error.audigo", level="ERROR") as logs:
            await generate_plan(body, places, planner)
        self.assertIn("insufficient_trip_time", "\n".join(logs.output))
        places.collect.assert_not_called()
        planner.generate.assert_not_called()

    async def test_unexpected_error_logs_stage_without_secret(self):
        body = request(extra="")
        places, planner = Mock(), Mock()
        with patch("ai_service.services.travel.pipeline.validate_generation_window", return_value=[]), self.assertLogs("uvicorn.error.audigo", level="ERROR") as logs:
            places.collect.side_effect = RuntimeError("secret-token-in-provider-url")
            with self.assertRaises(RuntimeError):
                await generate_plan(body, places, planner)
        log = "\n".join(logs.output)
        self.assertIn('"stage": "PLACE_RECOMMEND"', log)
        self.assertIn('"error_type": "RuntimeError"', log)
        self.assertNotIn("secret-token", log)
