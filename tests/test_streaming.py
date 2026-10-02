"""Test the SSE boundary without changing or re-running generation logic."""
import asyncio
import json
import unittest
from unittest.mock import Mock, patch
from datetime import timedelta

from ai_service.config import Settings
from ai_service.streaming import stream_generation
from test_day_transport import request


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_pipeline_starts_before_provider_finishes_and_cleans_up(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def collect(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        places = Mock(collect=collect)
        settings = Settings(stream_heartbeat_seconds=0.01)
        stream = stream_generation(request(None, "CAR"), places, Mock(), settings, "req_live")
        async with asyncio.timeout(2):
            first = await anext(stream)
            self.assertIn('"status": "STARTED"', first)
            self.assertFalse(entered.is_set())
            self.assertEqual(await anext(stream), ": keep-alive\n\n")
            self.assertTrue(entered.is_set())
            await stream.aclose()
        self.assertTrue(cancelled.is_set())

    async def test_timeout_emits_error_and_cancels_pending_generation(self):
        closed = asyncio.Event()

        async def pipeline(*args):
            try:
                yield "ROUTES", "STARTED", None
                await asyncio.Event().wait()
            finally:
                closed.set()

        settings = Settings(stream_timeout_seconds=0.03, stream_heartbeat_seconds=0.005)
        with patch("ai_service.streaming.generation_stages", side_effect=pipeline), \
                self.assertLogs("uvicorn.error.audigo", level="ERROR"):
            async with asyncio.timeout(2):
                chunks = [chunk async for chunk in stream_generation(request(), None, None, settings, "req_timeout")]
        self.assertIn(": keep-alive\n\n", chunks)
        self.assertIn("event: error", chunks[-1])
        self.assertIn('"message": "ai_service_unavailable"', chunks[-1])
        self.assertTrue(closed.is_set())

    async def test_disconnect_cancels_provider_without_emitting_error(self):
        entered, closed = asyncio.Event(), asyncio.Event()
        chunks = []

        async def pipeline(*args):
            try:
                yield "PLACES", "STARTED", None
                entered.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

        async def consume():
            async for chunk in stream_generation(request(), None, None, Settings(), "req_cancel"):
                chunks.append(chunk)

        with patch("ai_service.streaming.generation_stages", side_effect=pipeline):
            task = asyncio.create_task(consume())
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(closed.is_set())
        self.assertFalse(any("event: error" in chunk for chunk in chunks))

    async def test_impossible_short_trip_fails_before_any_provider_call(self):
        body = request(extra="")
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
