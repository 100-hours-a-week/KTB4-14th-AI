"""Test the SSE boundary without changing or re-running generation logic."""
import json
import unittest
from unittest.mock import Mock, patch
from datetime import timedelta

from ai_service.config import Settings
from ai_service.streaming import stream_generation
from ai_service.schemas import ItineraryStreamRequest
from test_day_transport import request


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_impossible_short_trip_fails_before_any_provider_call(self):
        body = ItineraryStreamRequest.model_validate(request(extra="").model_dump())
        body.duration.departure_datetime = body.duration.arrival_datetime + timedelta(minutes=10)
        places, planner = Mock(), Mock()
        with self.assertLogs("uvicorn.error.audigo", level="ERROR") as logs:
            chunks = [chunk async for chunk in stream_generation(body, places, planner, Settings(), "req_short")]
        payload = json.loads(chunks[-1].split("data: ", 1)[1])
        self.assertEqual(payload["status"], "FAILED")
        self.assertEqual(payload["message"], "ai_itinerary_generation_failed")
        self.assertIn("insufficient_trip_time", logs.output[0])
        places.collect.assert_not_called()
        planner.generate.assert_not_called()

    async def test_error_log_has_correlation_stage_and_stack_without_exception_secrets(self):
        async def pipeline(*args):
            yield "ROUTES", "STARTED", None
            raise RuntimeError("secret-token-in-provider-url")
        with patch("ai_service.streaming.generation_stages", side_effect=pipeline), self.assertLogs("uvicorn.error.audigo", level="ERROR") as logs:
            chunks = [chunk async for chunk in stream_generation(request(), None, None, Settings(), "req_safe")]
        log = "".join(logs.output)
        for expected in ('"request_id": "req_safe"', '"stage": "ROUTES"', '"frames":', '"error_type": "RuntimeError"'):
            self.assertIn(expected, log)
        self.assertNotIn("secret-token", log)
        self.assertNotIn("secret-token", "".join(chunks))
        self.assertIn('"message": "internal_server_error"', chunks[-1])

    async def test_only_final_result_is_serialized_once(self):
        intermediate = Mock()
        intermediate.model_dump.side_effect = AssertionError("internal result must not be serialized")
        final = Mock()
        final.model_dump.return_value = {"itinerary": {"marker": "complete itinerary"}, "music": {"marker": "complete song"}}

        async def pipeline(*args):
            for stage in ("PLACES", "ACCOMMODATIONS", "ROUTES", "MUSIC"):
                yield stage, "STARTED", None
                yield stage, "COMPLETED", intermediate
            yield "COMPLETE", "COMPLETED", final

        with patch("ai_service.streaming.generation_stages", side_effect=pipeline):
            chunks = [chunk async for chunk in stream_generation(request(), None, None, Settings(), "req_test")]
        self.assertEqual(len(chunks), 9)
        for index, chunk in enumerate(chunks, 1):
            self.assertTrue(chunk.startswith(f"id: {index}\n"))
        payloads = [json.loads(chunk.split("data: ", 1)[1]) for chunk in chunks]
        self.assertTrue(all(set(p) == {"stage", "status"} for p in payloads[:-1]))
        self.assertEqual(payloads[-1], {
            "generation_job_id": 1, "stage": "COMPLETE", "status": "COMPLETED",
            "data": final.model_dump.return_value,
        })
        intermediate.model_dump.assert_not_called()
        final.model_dump.assert_called_once_with(mode="json")


if __name__ == "__main__":
    unittest.main()
